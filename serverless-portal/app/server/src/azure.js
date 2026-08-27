// Live Azure discovery for the Serverless Agent Portal.
//
// Scans a subscription for Function Apps that host agents built on
// `azurefunctions-agents-runtime`, and enumerates the agents inside each one —
// without invoking the running apps.
//
// How agents are identified (see requirements.md §5.2 and verified against the
// deployed `func-agent-func-*` apps):
//
//   1. DEFINITION — a Function App IS a serverless agent app if — and only if —
//      it carries the app-setting marker `AZURE_FUNCTIONS_AGENTS_PROVIDER` (its
//      value is the model provider, e.g. `foundry`). This is the sole, reliable
//      "is this an agent app?" signal available from ARM.
//   2. The `*.agent.md` source files are the source of truth for the agent set
//      (a developer can add their own plain Functions to `function_app.py`, and
//      those are indistinguishable from agent triggers in ARM metadata). We read
//      that file list — via the Kudu VFS API, or, on Flex Consumption (no Kudu),
//      by listing the deployment package zip's central directory. Given the
//      list: a registered function whose name matches an `.agent.md` slug (or an
//      `agent_<slug>_builtin_*` endpoint) is an agent; every other function is a
//      supporting function; and `.agent.md` files with no registered function
//      (no trigger, no built-in endpoints) are surfaced as trigger-less agents.
//      When the file list can't be read, we fall back to treating every
//      non-built-in function as its own agent.
//
// Function enumeration prefers the ARM control plane (`listFunctions`). That
// endpoint returns nothing on Linux Consumption / Flex Consumption plans, so we
// fall back to the app's read-only admin metadata API (`/admin/functions`,
// authorised with the master host key fetched via the caller's ARM token). Only
// function *definitions* are read there — the agent code is never invoked.
//
// Auth uses the caller's ARM access token, acquired in the browser via MSAL
// (the same first-party app as Polaris) and forwarded as a Bearer token. Every
// ARM call below runs as the signed-in user — no `az login` required.

import { PassThrough } from 'node:stream'

import { SubscriptionClient } from '@azure/arm-resources-subscriptions'
import { WebSiteManagementClient } from '@azure/arm-appservice'
import { ContainerClient, StorageSharedKeyCredential } from '@azure/storage-blob'
import yauzl from 'yauzl'
import { randomUUID } from 'node:crypto'

const AGENT_PROVIDER_SETTING = 'AZURE_FUNCTIONS_AGENTS_PROVIDER'

// v1 scope: a single default subscription. Override with PORTAL_SUBSCRIPTION_ID.
// The signed-in identity (the forwarded ARM token) authorises every call.
export const DEFAULT_SUBSCRIPTION_ID =
  process.env.PORTAL_SUBSCRIPTION_ID || '1a839f1f-10b2-4613-95ad-0800a22abbf2'

// Built-in endpoint function suffixes we recognise, longest first so the agent
// name is stripped correctly (e.g. `_builtin_chatstream` before `_builtin_chat`).
const BUILTIN_SUFFIXES = [
  '_builtin_workflow_status',
  '_builtin_workflows',
  '_builtin_chatstream',
  '_builtin_chat_page',
  '_builtin_chat',
  '_builtin_mcp',
]

// App-level functions the runtime registers to power Durable workflows. They
// belong to no single agent, so they are surfaced as supporting functions —
// never as agents.
const SYSTEM_FUNCTION_NAMES = new Set(['agents_workflow_orchestrator', 'agents_workflow_run_tool'])

/**
 * Wrap a raw ARM access token (forwarded from the browser) as a `TokenCredential`
 * the Azure SDK clients can consume. The SDK ignores the requested scope and
 * simply attaches this bearer token; ARM validates its audience.
 * @param {string} accessToken
 */
function credentialFromToken(accessToken) {
  if (!accessToken) throw new Error('An ARM access token is required.')
  return {
    // The SDK only reads `.token`; expiry is advisory. The browser refreshes
    // and re-sends a fresh token on every request, so a short window is safe.
    getToken: async () => ({
      token: accessToken,
      expiresOnTimestamp: Date.now() + 5 * 60 * 1000,
    }),
  }
}

function webClient(accessToken, subscriptionId) {
  return new WebSiteManagementClient(credentialFromToken(accessToken), subscriptionId)
}

function subscriptionClient(accessToken) {
  return new SubscriptionClient(credentialFromToken(accessToken))
}

/** Raised when a subscription name/id cannot be resolved for the caller. */
export class SubscriptionNotFoundError extends Error {}

/**
 * Read the signed-in principal from the forwarded ARM access token claims.
 * @param {string} accessToken
 * @returns {{name: string, username: string, oid: string, tenantId: string}}
 */
export function getSignedInIdentity(accessToken) {
  if (!accessToken) throw new Error('An ARM access token is required.')
  const [, payload] = accessToken.split('.')
  const claims = JSON.parse(Buffer.from(payload, 'base64').toString('utf-8'))
  return {
    name: claims.name ?? '',
    username: claims.upn ?? claims.unique_name ?? claims.preferred_username ?? '',
    oid: claims.oid ?? '',
    tenantId: claims.tid ?? '',
  }
}

/**
 * Look up a subscription's display name by id. Falls back to the id if the
 * signed-in identity cannot enumerate subscriptions.
 * @param {string} accessToken
 * @param {string} subscriptionId
 */
export async function getSubscriptionName(accessToken, subscriptionId) {
  try {
    const sub = await subscriptionClient(accessToken).subscriptions.get(subscriptionId)
    return sub.displayName ?? subscriptionId
  } catch {
    return subscriptionId
  }
}

/**
 * List subscriptions the signed-in identity can see.
 * @param {string} accessToken
 * @returns {Promise<Array<{id: string, name: string, state: string}>>}
 */
export async function listSubscriptions(accessToken) {
  const client = subscriptionClient(accessToken)
  const out = []
  for await (const sub of client.subscriptions.list()) {
    if (!sub.subscriptionId) continue
    out.push({
      id: sub.subscriptionId,
      name: sub.displayName ?? sub.subscriptionId,
      state: sub.state ?? 'Unknown',
    })
  }
  out.sort((a, b) => a.name.localeCompare(b.name))
  return out
}

/**
 * Resolve a subscription reference (id or display name) to its id.
 * @param {string} accessToken
 * @param {string} ref subscription id or display name
 */
export async function resolveSubscriptionId(accessToken, ref) {
  const value = String(ref ?? '').trim()
  if (!value) throw new SubscriptionNotFoundError('No subscription specified.')
  const subs = await listSubscriptions(accessToken)
  const byId = subs.find((s) => s.id.toLowerCase() === value.toLowerCase())
  if (byId) return byId.id
  const byName = subs.find((s) => s.name.toLowerCase() === value.toLowerCase())
  if (byName) return byName.id
  throw new SubscriptionNotFoundError(`Subscription '${ref}' not found or not accessible.`)
}

// Extract `{ subscriptionId, resourceGroup }` from an ARM resource id.
function parseResourceGroup(resourceId) {
  const match = /\/resourceGroups\/([^/]+)/i.exec(String(resourceId ?? ''))
  return match ? match[1] : ''
}

// Turn a settings array/object into a plain lookup map.
function settingsToMap(properties) {
  const map = {}
  if (!properties) return map
  for (const [key, value] of Object.entries(properties)) {
    map[key] = value
  }
  return map
}

// Recover the built-in agent slug from a function that belongs to the built-in
// endpoint set — either from its `agents/<slug>/…` route or the
// `agent_<slug>_builtin_*` function name. Returns null for non-built-in
// functions (i.e. an agent's own custom trigger).
function builtinSlugFromFunction(shortName, route) {
  const routeMatch = /^agents\/([^/]+)\//.exec(route)
  if (routeMatch) return routeMatch[1]
  if (shortName.startsWith('agent_') && shortName.includes('_builtin_')) {
    let base = shortName.slice('agent_'.length)
    for (const suffix of BUILTIN_SUFFIXES) {
      if (base.endsWith(suffix)) {
        base = base.slice(0, -suffix.length)
        break
      }
    }
    if (base) return base
  }
  return null
}

// Map a raw Functions trigger binding type to a short, display-friendly label.
function normalizeTrigger(type) {
  const t = String(type ?? '')
  const known = {
    httpTrigger: 'http',
    timerTrigger: 'timer',
    queueTrigger: 'queue',
    blobTrigger: 'blob',
    serviceBusTrigger: 'servicebus',
    eventHubTrigger: 'eventhub',
    eventGridTrigger: 'eventgrid',
    cosmosDBTrigger: 'cosmos',
    connectorTrigger: 'connector',
    orchestrationTrigger: 'orchestration',
    activityTrigger: 'activity',
  }
  if (known[t]) return known[t]
  return t.toLowerCase().endsWith('trigger') ? t.slice(0, -'trigger'.length).toLowerCase() : t.toLowerCase()
}

// Fold a collection of function definitions (from ARM or the admin API) into the
// agents they represent plus the supporting functions that back them.
//
// `agentSlugs` is the authoritative set of agent slugs from the deployed
// `*.agent.md` files; `authoritative` is true when that list is known to be
// complete. With it, a plain trigger function is an agent only when its name
// matches an `.agent.md` slug — otherwise it is a developer-defined supporting
// function. Without it (files unreadable) we fall back to treating every
// non-built-in function as its own agent.
//
// Returns the agent list (each with the built-in endpoint functions that back
// it) and the app-level supporting functions (developer functions + Durable
// workflow plumbing) that belong to no single agent.
function parseAgentsFromFunctions(functions, agentSlugs = new Set(), authoritative = false) {
  // slug → { name, triggers:Set, builtinEndpoints, routes:Set, supporting:Set }
  const agents = new Map()
  const appSupporting = new Map() // function name → trigger label
  const getEntry = (name) => {
    let entry = agents.get(name)
    if (!entry) {
      entry = { name, triggers: new Set(), builtinEndpoints: false, routes: new Set(), supporting: new Set() }
      agents.set(name, entry)
    }
    return entry
  }

  for (const fn of functions) {
    // Function names arrive as `<app>/<function>`; keep the last segment.
    const shortName = String(fn?.name ?? '').split('/').pop() ?? ''
    if (!shortName) continue
    const bindings = fn?.config?.bindings ?? []
    const triggerBinding = bindings.find(
      (b) => typeof b?.type === 'string' && b.type.toLowerCase().endsWith('trigger'),
    )
    const routeBinding = bindings.find((b) => typeof b?.route === 'string' && b.route)
    const route = routeBinding?.route ?? ''
    const triggerLabel = triggerBinding?.type ? normalizeTrigger(triggerBinding.type) : 'http'

    // App-level runtime plumbing (Durable workflow engine) — never an agent.
    if (SYSTEM_FUNCTION_NAMES.has(shortName)) {
      appSupporting.set(shortName, triggerLabel)
      continue
    }

    // Built-in endpoint / workflow function → a supporting function of the agent
    // identified by its `agents/<slug>/…` route or `agent_<slug>_builtin_*` name.
    const builtinSlug = builtinSlugFromFunction(shortName, route)
    if (builtinSlug) {
      const entry = getEntry(builtinSlug)
      entry.builtinEndpoints = true
      entry.supporting.add(shortName)
      continue
    }

    // Plain trigger function. It's an agent when its name matches an `.agent.md`
    // slug (or when we have no file list to check against); otherwise it's a
    // developer-defined supporting function.
    if (!authoritative || agentSlugs.has(shortName)) {
      const entry = getEntry(shortName)
      if (triggerBinding?.type) entry.triggers.add(String(triggerBinding.type))
      if (route) entry.routes.add(route)
    } else {
      appSupporting.set(shortName, triggerLabel)
    }
  }

  // `.agent.md` files that register no function at all (no trigger, no endpoint).
  if (authoritative) {
    for (const slug of agentSlugs) {
      if (!agents.has(slug)) getEntry(slug)
    }
  }

  const agentList = [...agents.values()]
    .map((a) => ({
      name: a.name,
      trigger: a.triggers.has('httpTrigger')
        ? 'http'
        : a.triggers.size > 0
          ? normalizeTrigger([...a.triggers][0])
          : a.builtinEndpoints
            ? 'http'
            : 'none',
      builtinEndpoints: a.builtinEndpoints,
      routes: [...a.routes],
      supportingFunctions: [...a.supporting].sort(),
    }))
    .sort((x, y) => x.name.localeCompare(y.name))

  const appSupportingFunctions = [...appSupporting.entries()]
    .map(([name, trigger]) => ({ name, trigger }))
    .sort((x, y) => x.name.localeCompare(y.name))

  return { agents: agentList, appSupportingFunctions }
}

// Enumerate an app's functions via the ARM control plane. Reliable on Windows /
// Elastic Premium / dedicated plans, but returns an empty list on Linux
// Consumption and (often) Flex Consumption plans — see `functionsFromAdminApi`.
async function functionsFromArm(client, resourceGroup, appName) {
  const out = []
  try {
    for await (const fn of client.webApps.listFunctions(resourceGroup, appName)) {
      out.push(fn)
    }
  } catch {
    /* control-plane listing unavailable — caller falls back to the admin API */
  }
  return out
}

// Fallback enumeration for plans where ARM `listFunctions` returns nothing
// (Linux Consumption / Flex Consumption). Reads the app's own read-only admin
// metadata endpoint (`/admin/functions`) — function definitions only, the agent
// code is never invoked — authorised with the master host key, which we fetch
// using the caller's forwarded ARM token (no key handling by the browser).
async function functionsFromAdminApi(client, resourceGroup, appName, defaultHostName) {
  if (!defaultHostName) return []
  let masterKey
  try {
    const keys = await client.webApps.listHostKeys(resourceGroup, appName)
    masterKey = keys?.masterKey
  } catch {
    return [] // caller lacks listHostKeys permission — keep the app-level fallback
  }
  if (!masterKey) return []
  try {
    const res = await fetch(`https://${defaultHostName}/admin/functions`, {
      headers: { 'x-functions-key': masterKey },
    })
    if (!res.ok) return []
    const data = await res.json()
    return Array.isArray(data) ? data : []
  } catch {
    return []
  }
}

// Derive the Kudu/SCM host for a site (e.g. `app.scm.azurewebsites.net`), used
// to read deployed source files. Prefers an advertised `*.scm.*` host name and
// otherwise inserts `.scm.` after the app segment of the default host name.
export function scmHostName(site) {
  const enabled = Array.isArray(site?.enabledHostNames) ? site.enabledHostNames : []
  const advertised = enabled.find((h) => typeof h === 'string' && /\.scm\./i.test(h))
  if (advertised) return advertised
  const def = String(site?.defaultHostName ?? '')
  return def ? def.replace(/^([^.]+)\./, '$1.scm.') : ''
}

// Replicate the runtime's function-name sanitisation so a `<name>.agent.md`
// filename maps to the same slug the runtime registers functions under.
function safeFunctionName(rawName) {
  const name = String(rawName ?? '')
    .replace(/[^a-zA-Z0-9_]/g, '_')
    .replace(/^_+|_+$/g, '')
  if (!name) return 'agent_function'
  if (/^[0-9]/.test(name)) return `fn_${name}`
  return name
}

// Map an `<name>.agent.md` filename to its agent slug.
function agentSlugFromFileName(fileName) {
  const base = String(fileName ?? '').replace(/\.agent\.md$/i, '')
  return safeFunctionName(base)
}

// Enumerate the agent source files (`*.agent.md`) deployed in an app via the
// read-only Kudu VFS API, authorised with the caller's forwarded ARM token. The
// agent code is never invoked. Returns `{ files, ok }` where `ok` is true only
// when the app root listing succeeded, so callers know whether the list is
// complete. Fails (`ok: false`) when Kudu/VFS is unavailable (e.g. Flex
// Consumption) or the caller lacks permission. The runtime discovers *.agent.md
// at the app root and directly under agents/ (no deeper nesting).
async function agentSourceFiles(accessToken, site) {
  const scm = scmHostName(site)
  if (!scm) return { files: [], ok: false }
  try {
    const rootRes = await fetch(`https://${scm}/api/vfs/site/wwwroot/`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      signal: AbortSignal.timeout(2500),
    })
    if (!rootRes.ok) return { files: [], ok: false }
    const rootEntries = await rootRes.json()
    if (!Array.isArray(rootEntries)) return { files: [], ok: false }

    const files = rootEntries
      .map((entry) => String(entry?.name ?? ''))
      .filter((name) => name.toLowerCase().endsWith('.agent.md'))
    const agentsDirectory = rootEntries.find(
      (entry) => String(entry?.name ?? '').toLowerCase() === 'agents' && String(entry?.mime ?? '') === 'inode/directory',
    )
    if (!agentsDirectory?.name) return { files, ok: true }

    const agentsRes = await fetch(`https://${scm}/api/vfs/site/wwwroot/${encodeURIComponent(String(agentsDirectory.name))}/`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      signal: AbortSignal.timeout(2500),
    })
    if (!agentsRes.ok) return { files, ok: true }
    const agentEntries = await agentsRes.json()
    if (Array.isArray(agentEntries)) {
      files.push(
        ...agentEntries
          .map((entry) => String(entry?.name ?? ''))
          .filter((name) => name.toLowerCase().endsWith('.agent.md')),
      )
    }
    return { files, ok: true }
  } catch {
    return { files: [], ok: false }
  }
}

// ---------------------------------------------------------------------------
// Flex Consumption source-file reading.
//
// Flex Consumption has no Kudu VFS, so `*.agent.md` files can't be read from the
// live file system. Instead we read the deployment package: Flex stores the
// app's zip in a blob container (functionAppConfig.deployment.storage), and we
// list just the zip's central directory (a few KB, via ranged reads) to
// enumerate its `*.agent.md` files. Storage is accessed with an account key
// fetched via the caller's ARM token (listKeys). Best-effort throughout.
// ---------------------------------------------------------------------------

// Resolve the Flex deployment package's blob container URL for a site.
async function flexDeploymentContainerUrl(accessToken, site) {
  const inline = site?.functionAppConfig?.deployment?.storage?.value
  if (inline) return String(inline)
  if (!site?.id) return ''
  try {
    const res = await fetch(`https://management.azure.com${site.id}?api-version=2023-12-01`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      signal: AbortSignal.timeout(10000),
    })
    if (!res.ok) return ''
    const body = await res.json()
    return String(body?.properties?.functionAppConfig?.deployment?.storage?.value ?? '')
  } catch {
    return ''
  }
}

// Split a blob container URL into its account + container names.
function parseBlobContainerUrl(url) {
  try {
    const parsed = new URL(url)
    const account = parsed.hostname.split('.')[0]
    const container = parsed.pathname.replace(/^\/+/, '').split('/')[0]
    return { account, container }
  } catch {
    return { account: '', container: '' }
  }
}

// Fetch a storage account access key via ARM listKeys. Tries the app's resource
// group first (Flex co-locates them), then locates the account by name.
async function storageAccountKey(accessToken, subscriptionId, appResourceGroup, account) {
  const listKeys = async (rg) => {
    if (!rg) return ''
    try {
      const url = `https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${rg}/providers/Microsoft.Storage/storageAccounts/${account}/listKeys?api-version=2023-01-01`
      const res = await fetch(url, {
        method: 'POST',
        headers: { Authorization: `Bearer ${accessToken}` },
        signal: AbortSignal.timeout(10000),
      })
      if (!res.ok) return ''
      const body = await res.json()
      return String(body?.keys?.[0]?.value ?? '')
    } catch {
      return ''
    }
  }
  const primary = await listKeys(appResourceGroup)
  if (primary) return primary
  try {
    const accounts = await armList(
      accessToken,
      `/subscriptions/${subscriptionId}/providers/Microsoft.Storage/storageAccounts?api-version=2023-01-01`,
      { timeoutMs: 10000 },
    )
    const match = accounts.find((s) => s?.name === account)
    const rg = match ? parseResourceGroup(match.id) : ''
    if (rg && rg !== appResourceGroup) return await listKeys(rg)
  } catch {
    /* fall through to empty */
  }
  return ''
}

// A yauzl random-access reader backed by ranged Azure Blob downloads, so only
// the zip's central directory is fetched — never the whole package.
class BlobRandomAccessReader extends yauzl.RandomAccessReader {
  constructor(blob) {
    super()
    this._blob = blob
  }
  _readStreamForRange(start, end) {
    const pass = new PassThrough()
    this._blob
      .download(start, end - start)
      .then((resp) => {
        const body = resp.readableStreamBody
        if (!body) {
          pass.end()
          return
        }
        body.on('error', (err) => pass.destroy(err))
        body.pipe(pass)
      })
      .catch((err) => pass.destroy(err))
    return pass
  }
}

// List `*.agent.md` entries in a zip blob by reading only its central directory.
function agentFilesFromZip(blob, size) {
  return new Promise((resolve) => {
    const files = []
    let settled = false
    const done = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(files)
    }
    // Safety net so a stuck reader can't hang the subscription scan.
    const timer = setTimeout(done, 15000)
    try {
      const reader = new BlobRandomAccessReader(blob)
      yauzl.fromRandomAccessReader(reader, size, { lazyEntries: true, autoClose: false }, (err, zip) => {
        if (err || !zip) return done()
        zip.on('entry', (entry) => {
          const name = String(entry?.fileName ?? '')
          if (/\.agent\.md$/i.test(name)) files.push(name.split('/').pop())
          zip.readEntry()
        })
        zip.on('end', () => {
          try {
            zip.close()
          } catch {
            /* ignore */
          }
          done()
        })
        zip.on('error', done)
        zip.readEntry()
      })
    } catch {
      done()
    }
  })
}

// A minimal TokenCredential that returns a pre-acquired storage-scoped AAD token
// (forwarded from the browser). Lets us read the deployment package with the
// caller's identity when the storage account has shared-key access disabled.
function staticTokenCredential(token) {
  return {
    getToken: async () => ({ token, expiresOnTimestamp: Date.now() + 5 * 60 * 1000 }),
  }
}

// Open the Flex Consumption deployment package blob for ranged reads. Returns
// `{ blob, size }` or null when unavailable (not Flex, unreachable, or no perms).
// Tries the storage account key (ARM listKeys) first, then the caller's AAD
// identity (a forwarded storage-scoped token) — the latter succeeds when
// shared-key access is disabled but the user has "Storage Blob Data Reader".
async function openFlexPackageBlob(accessToken, subscriptionId, site, storageToken) {
  const containerUrl = await flexDeploymentContainerUrl(accessToken, site)
  if (!containerUrl) return null
  const { account, container } = parseBlobContainerUrl(containerUrl)
  if (!account || !container) return null

  const credentials = []
  const key = await storageAccountKey(accessToken, subscriptionId, parseResourceGroup(site?.id), account)
  if (key) credentials.push(new StorageSharedKeyCredential(account, key))
  if (storageToken) credentials.push(staticTokenCredential(storageToken))

  for (const credential of credentials) {
    try {
      const containerClient = new ContainerClient(containerUrl, credential)
      let blobName = ''
      for await (const item of containerClient.listBlobsFlat()) {
        const name = String(item?.name ?? '')
        if (name.toLowerCase() === 'released-package.zip') {
          blobName = name
          break
        }
        if (!blobName && name.toLowerCase().endsWith('.zip')) blobName = name
      }
      if (!blobName) continue
      const blob = containerClient.getBlockBlobClient(blobName)
      const props = await blob.getProperties()
      const size = Number(props.contentLength ?? 0)
      if (!size) continue
      return { blob, size }
    } catch {
      /* try the next credential */
    }
  }
  return null
}

// Read the deployed `*.agent.md` file names from a Flex Consumption app's
// deployment package. Best-effort: returns `{ files: [], ok: false }` when the
// app isn't Flex, the package can't be reached, or the caller lacks permission.
async function flexPackageAgentFiles(accessToken, subscriptionId, site) {
  try {
    const pkg = await openFlexPackageBlob(accessToken, subscriptionId, site)
    if (!pkg) return { files: [], ok: false }
    return { files: await agentFilesFromZip(pkg.blob, pkg.size), ok: true }
  } catch {
    return { files: [], ok: false }
  }
}

// Download a Flex Consumption app's deployment package and return its editable
// SOURCE files (excludes build output such as `.python_packages/`), so an
// existing app can be redeployed from its own current source with edits
// overlaid. Returns `[{ name, data }]` or null when the package can't be read.
export async function readPackageFiles(accessToken, subscriptionId, site) {
  const pkg = await openFlexPackageBlob(accessToken, subscriptionId, site)
  if (!pkg) return null
  let buffer
  try {
    buffer = await pkg.blob.downloadToBuffer()
  } catch {
    return null
  }
  return extractSourceEntries(buffer)
}

// True for zip entries that are build output / caches rather than source.
function isBuildArtifact(name) {
  return (
    name.startsWith('.python_packages/') ||
    name.startsWith('.venv/') ||
    name.startsWith('__pycache__/') ||
    name.includes('/__pycache__/') ||
    name.endsWith('.pyc')
  )
}

// Extract every source file from a zip buffer as `[{ name, data }]`.
function extractSourceEntries(buffer) {
  return new Promise((resolve) => {
    const files = []
    let settled = false
    const finish = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(files)
    }
    const timer = setTimeout(finish, 20000)
    yauzl.fromBuffer(buffer, { lazyEntries: true }, (err, zip) => {
      if (err || !zip) return finish()
      zip.on('entry', (entry) => {
        const name = String(entry?.fileName ?? '')
        if (name.endsWith('/') || isBuildArtifact(name)) {
          zip.readEntry()
          return
        }
        zip.openReadStream(entry, (streamErr, stream) => {
          if (streamErr || !stream) {
            zip.readEntry()
            return
          }
          const chunks = []
          stream.on('data', (chunk) => chunks.push(chunk))
          stream.on('end', () => {
            files.push({ name, data: Buffer.concat(chunks) })
            zip.readEntry()
          })
          stream.on('error', () => zip.readEntry())
        })
      })
      zip.on('end', finish)
      zip.on('error', finish)
      zip.readEntry()
    })
  })
}

// Read the text of the first zip entry whose full name satisfies `matchFn`,
// using ranged reads (never the whole package). Returns null when not found.
function readZipEntryContent(blob, size, matchFn) {
  return new Promise((resolve) => {
    let settled = false
    const done = (val) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(val)
    }
    const timer = setTimeout(() => done(null), 20000)
    try {
      const reader = new BlobRandomAccessReader(blob)
      yauzl.fromRandomAccessReader(reader, size, { lazyEntries: true, autoClose: false }, (err, zip) => {
        if (err || !zip) return done(null)
        zip.on('entry', (entry) => {
          if (matchFn(String(entry?.fileName ?? ''))) {
            zip.openReadStream(entry, (streamErr, stream) => {
              if (streamErr || !stream) return done(null)
              const chunks = []
              stream.on('data', (chunk) => chunks.push(chunk))
              stream.on('end', () => {
                try {
                  zip.close()
                } catch {
                  /* ignore */
                }
                done(Buffer.concat(chunks).toString('utf-8'))
              })
              stream.on('error', () => done(null))
            })
            return
          }
          zip.readEntry()
        })
        zip.on('end', () => done(null))
        zip.on('error', () => done(null))
        zip.readEntry()
      })
    } catch {
      done(null)
    }
  })
}

// Read the `*.agent.md` entry whose slug matches `agentName` from a package zip.
function readAgentFileFromZip(blob, size, agentName) {
  const targetSlug = safeFunctionName(agentName)
  return readZipEntryContent(blob, size, (fullName) => {
    const base = fullName.split('/').pop() ?? ''
    return /\.agent\.md$/i.test(base) && agentSlugFromFileName(base) === targetSlug
  })
}

// Read an agent's `*.agent.md` content from a Flex Consumption deployment
// package. Returns null when unavailable.
async function flexPackageAgentDefinition(accessToken, subscriptionId, site, agentName, storageToken) {
  try {
    const pkg = await openFlexPackageBlob(accessToken, subscriptionId, site, storageToken)
    if (!pkg) return null
    return await readAgentFileFromZip(pkg.blob, pkg.size, agentName)
  } catch {
    return null
  }
}

// Read an agent's `*.agent.md` content via the Kudu VFS API (dedicated / some
// Consumption plans). Returns null when unavailable.
async function kuduAgentDefinition(accessToken, site, agentName) {
  const scm = scmHostName(site)
  if (!scm) return null
  const targetSlug = safeFunctionName(agentName)
  let agentsDirectory = 'agents'
  try {
    const rootRes = await fetch(`https://${scm}/api/vfs/site/wwwroot/`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      signal: AbortSignal.timeout(8000),
    })
    if (rootRes.ok) {
      const rootEntries = await rootRes.json()
      if (Array.isArray(rootEntries)) {
        const directory = rootEntries.find(
          (entry) => String(entry?.name ?? '').toLowerCase() === 'agents' && String(entry?.mime ?? '') === 'inode/directory',
        )
        if (directory?.name) agentsDirectory = String(directory.name)
      }
    }
  } catch {
    /* probe the conventional directory names below */
  }
  for (const dir of ['site/wwwroot', `site/wwwroot/${agentsDirectory}`]) {
    try {
      const listRes = await fetch(`https://${scm}/api/vfs/${dir}/`, {
        headers: { Authorization: `Bearer ${accessToken}` },
        signal: AbortSignal.timeout(8000),
      })
      if (!listRes.ok) continue
      const entries = await listRes.json()
      if (!Array.isArray(entries)) continue
      const match = entries
        .map((e) => String(e?.name ?? ''))
        .find((n) => n.toLowerCase().endsWith('.agent.md') && agentSlugFromFileName(n) === targetSlug)
      if (!match) continue
      const fileRes = await fetch(`https://${scm}/api/vfs/${dir}/${encodeURIComponent(match)}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
        signal: AbortSignal.timeout(8000),
      })
      if (!fileRes.ok) continue
      return await fileRes.text()
    } catch {
      /* try next dir */
    }
  }
  return null
}

// Read the deployed `<agent>.agent.md` source content for a single agent.
// Prefers Kudu VFS; falls back to the Flex deployment package. Returns null when
// the source can't be read (e.g. caller lacks permission).
export async function readAgentDefinition(accessToken, subscriptionId, site, agentName, storageToken) {
  const kudu = await kuduAgentDefinition(accessToken, site, agentName)
  if (kudu != null) return kudu
  return flexPackageAgentDefinition(accessToken, subscriptionId, site, agentName, storageToken)
}

// Read the text content of a deployed source file at a wwwroot-relative path
// (e.g. `function_app.py`). Prefers Kudu VFS; falls back to the Flex deployment
// package. Returns null when the file can't be read.
export async function readSourceFile(accessToken, subscriptionId, site, relPath, storageToken) {
  const clean = String(relPath ?? '').replace(/^\.?\/+/, '')
  if (!clean || clean.includes('..')) return null
  const scm = scmHostName(site)
  if (scm) {
    try {
      const encoded = clean.split('/').map(encodeURIComponent).join('/')
      const res = await fetch(`https://${scm}/api/vfs/site/wwwroot/${encoded}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
        signal: AbortSignal.timeout(8000),
      })
      if (res.ok) return await res.text()
    } catch {
      /* fall through to the Flex package */
    }
  }
  try {
    const pkg = await openFlexPackageBlob(accessToken, subscriptionId, site, storageToken)
    if (pkg) return await readZipEntryContent(pkg.blob, pkg.size, (fullName) => fullName === clean)
  } catch {
    /* unavailable */
  }
  return null
}

// List every source file entry in a Flex Consumption deployment package by
// reading only the zip's central directory. Build artifacts are filtered out.
// Returns [{ path, size }] or null when the package can't be reached.
function listSourceEntriesFromZip(blob, size) {
  return new Promise((resolve) => {
    const files = []
    let settled = false
    const done = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(files)
    }
    const timer = setTimeout(done, 15000)
    try {
      const reader = new BlobRandomAccessReader(blob)
      yauzl.fromRandomAccessReader(reader, size, { lazyEntries: true, autoClose: false }, (err, zip) => {
        if (err || !zip) return done()
        zip.on('entry', (entry) => {
          const name = String(entry?.fileName ?? '')
          if (!name.endsWith('/') && !isBuildArtifact(name)) {
            files.push({ path: name, size: Number(entry?.uncompressedSize ?? 0) })
          }
          zip.readEntry()
        })
        zip.on('end', () => {
          try { zip.close() } catch { /* ignore */ }
          done()
        })
        zip.on('error', done)
        zip.readEntry()
      })
    } catch {
      done()
    }
  })
}

// Recursively walk Kudu VFS starting at site/wwwroot. Skips node_modules and
// python virtualenv caches. Returns [{ path, size }] or null on failure.
async function listKuduSourceFiles(accessToken, site) {
  const scm = scmHostName(site)
  if (!scm) return null
  const files = []
  const skip = new Set(['node_modules', '.venv', '__pycache__', '.python_packages', '.git'])
  async function walk(rel) {
    const url = `https://${scm}/api/vfs/${rel ? `site/wwwroot/${rel}/` : 'site/wwwroot/'}`
    let entries
    try {
      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${accessToken}` },
        signal: AbortSignal.timeout(6000),
      })
      if (!res.ok) return
      entries = await res.json()
    } catch {
      return
    }
    if (!Array.isArray(entries)) return
    for (const entry of entries) {
      const name = String(entry?.name ?? '')
      if (!name || skip.has(name)) continue
      const childRel = rel ? `${rel}/${name}` : name
      if (String(entry?.mime ?? '') === 'inode/directory') {
        await walk(childRel)
      } else {
        files.push({ path: childRel, size: Number(entry?.size ?? 0) })
      }
    }
  }
  try {
    await walk('')
  } catch {
    return null
  }
  return files
}

// List every source file in a deployed app (wwwroot-relative paths). Prefers
// Kudu VFS; falls back to the Flex Consumption deployment package. Returns
// [{ path, size }] or null when neither source is reachable.
export async function listSourceFiles(accessToken, subscriptionId, site, storageToken) {
  const kudu = await listKuduSourceFiles(accessToken, site)
  if (kudu && kudu.length) return kudu
  try {
    const pkg = await openFlexPackageBlob(accessToken, subscriptionId, site, storageToken)
    if (pkg) return await listSourceEntriesFromZip(pkg.blob, pkg.size)
  } catch {
    /* unavailable */
  }
  return kudu ?? null
}

// ---------------------------------------------------------------------------
// Blob-backed session history — the runtime writes each conversation to
// `<container>/agent-sessions/<session_id>.jsonl` on the app's own storage
// account (`AzureWebJobsStorage`). The portal enumerates and reads those blobs
// directly (no per-app HTTP endpoint required) so users can browse prior
// conversations from the Playground.
// ---------------------------------------------------------------------------

const SESSION_CONTAINER_ENV = 'AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER'
const DEFAULT_SESSION_CONTAINER = 'azure-functions-agents'
const DEFAULT_SESSION_PREFIX = 'agent-sessions/'

// Read a Function App's application settings (best effort — returns `{}` on
// failure). Callers use it to resolve the session container name and to find
// the App Insights connection string.
async function readAppSettings(accessToken, subscriptionId, resourceGroup, appName) {
  try {
    const client = webClient(accessToken, subscriptionId)
    const current = await client.webApps.listApplicationSettings(resourceGroup, appName)
    return { ...(current?.properties || {}) }
  } catch {
    return {}
  }
}

// Parse `AccountName=` out of an Azure Storage connection string.
function accountFromConnString(connString) {
  const match = /AccountName=([^;]+)/i.exec(String(connString ?? ''))
  return match ? match[1] : ''
}

// Resolve the Function App's own storage account (the one holding
// `AzureWebJobsStorage`, where the runtime writes session history blobs).
// Returns { account, blobEndpoint } or null when the app-setting is missing.
async function resolveAppStorageAccount(accessToken, subscriptionId, resourceGroup, appName, settings) {
  const props = settings ?? (await readAppSettings(accessToken, subscriptionId, resourceGroup, appName))
  const conn = props.AzureWebJobsStorage
  if (conn && conn.includes('AccountName=')) {
    const account = accountFromConnString(conn)
    if (account) return { account, blobEndpoint: `https://${account}.blob.core.windows.net` }
  }
  const uri = props.AzureWebJobsStorage__blobServiceUri
  if (uri) {
    try {
      const parsed = new URL(uri)
      const account = parsed.hostname.split('.')[0]
      if (account) return { account, blobEndpoint: uri.replace(/\/+$/, '') }
    } catch {
      /* fall through */
    }
  }
  return null
}

// Open the app's session container with whichever credential is available
// (shared-key first, then the forwarded storage-scoped AAD token). Returns
// { containerClient, container } or null.
async function openSessionContainer(accessToken, subscriptionId, site, storageToken) {
  const resourceGroup = parseResourceGroup(site?.id)
  const appName = site?.name || ''
  if (!appName || !resourceGroup) return null
  const settings = await readAppSettings(accessToken, subscriptionId, resourceGroup, appName)
  const container = String(settings[SESSION_CONTAINER_ENV] || DEFAULT_SESSION_CONTAINER)
  const resolved = await resolveAppStorageAccount(accessToken, subscriptionId, resourceGroup, appName, settings)
  if (!resolved) return null
  const containerUrl = `${resolved.blobEndpoint}/${container}`

  const credentials = []
  const key = await storageAccountKey(accessToken, subscriptionId, resourceGroup, resolved.account)
  if (key) credentials.push(new StorageSharedKeyCredential(resolved.account, key))
  if (storageToken) credentials.push(staticTokenCredential(storageToken))
  for (const credential of credentials) {
    try {
      const client = new ContainerClient(containerUrl, credential)
      // Touch the container to fail fast on 403/404 before returning.
      await client.getProperties()
      return { containerClient: client, container }
    } catch {
      /* try the next credential */
    }
  }
  return null
}

// Enumerate the app's persisted sessions. Returns [{ sessionId, size,
// lastModified }] sorted newest-first, or null when the storage container
// isn't reachable. The `agent` parameter is currently informational — the
// runtime's blob layout is not agent-scoped; callers may filter later once
// the runtime writes per-agent prefixes.
export async function listSessions(accessToken, subscriptionId, site, storageToken) {
  const opened = await openSessionContainer(accessToken, subscriptionId, site, storageToken)
  if (!opened) return null
  const sessions = []
  try {
    for await (const item of opened.containerClient.listBlobsFlat({ prefix: DEFAULT_SESSION_PREFIX })) {
      const name = String(item?.name ?? '')
      if (!name.endsWith('.jsonl')) continue
      const sessionId = name.slice(DEFAULT_SESSION_PREFIX.length, -'.jsonl'.length)
      if (!sessionId) continue
      sessions.push({
        sessionId,
        size: Number(item?.properties?.contentLength ?? 0),
        lastModified: item?.properties?.lastModified?.toISOString?.() ?? null,
      })
    }
  } catch {
    return null
  }
  sessions.sort((a, b) => (b.lastModified ?? '').localeCompare(a.lastModified ?? ''))
  return sessions
}

// Fetch a single session's JSONL blob and return an array of parsed message
// dicts. Returns null when the blob can't be read; returns `[]` when the blob
// exists but is empty.
export async function readSession(accessToken, subscriptionId, site, sessionId, storageToken) {
  if (!/^[A-Za-z0-9._-]{1,128}$/.test(String(sessionId ?? ''))) return null
  const opened = await openSessionContainer(accessToken, subscriptionId, site, storageToken)
  if (!opened) return null
  const blobName = `${DEFAULT_SESSION_PREFIX}${sessionId}.jsonl`
  try {
    const blob = opened.containerClient.getAppendBlobClient(blobName)
    const buf = await blob.downloadToBuffer()
    const text = buf.toString('utf-8')
    const messages = []
    for (const line of text.split(/\r?\n/)) {
      const clean = line.trim()
      if (!clean) continue
      try {
        messages.push(JSON.parse(clean))
      } catch {
        /* skip a partially-written line */
      }
    }
    return messages
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// Application Insights (KQL via ARM). Every deployed agent app has an AI
// component (linked via `APPLICATIONINSIGHTS_CONNECTION_STRING`); we resolve
// the component by iKey / name so the portal can render invocation counts,
// p95 latency, error rate, and recent failures without asking the caller to
// pick a workspace.
// ---------------------------------------------------------------------------

// Parse `InstrumentationKey=` out of an App Insights connection string.
function iKeyFromConnString(connString) {
  const match = /InstrumentationKey=([^;]+)/i.exec(String(connString ?? ''))
  return match ? match[1] : ''
}

// Resolve the App Insights component id for a Function App. Tries same-name in
// same RG first (matches the portal's provision.js template), then falls back
// to iKey lookup across every AI component in the subscription. Returns the
// component id or ''.
async function resolveAppInsightsComponentId(accessToken, subscriptionId, resourceGroup, appName) {
  const settings = await readAppSettings(accessToken, subscriptionId, resourceGroup, appName)
  const conn = settings.APPLICATIONINSIGHTS_CONNECTION_STRING || ''
  const iKey = iKeyFromConnString(conn)

  // Same-name / same-RG probe (portal-created apps always match this).
  const sameName = `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}/providers/Microsoft.Insights/components/${appName}`
  try {
    const probe = await fetch(
      `https://management.azure.com${sameName}?api-version=2020-02-02`,
      { headers: { Authorization: `Bearer ${accessToken}` }, signal: AbortSignal.timeout(6000) },
    )
    if (probe.ok) return sameName
  } catch {
    /* fall through */
  }

  if (!iKey) return ''

  // Fallback: enumerate every AI component in the subscription and match iKey.
  try {
    const components = await armList(
      accessToken,
      `/subscriptions/${subscriptionId}/providers/Microsoft.Insights/components?api-version=2020-02-02`,
    )
    for (const component of components) {
      if (String(component?.properties?.InstrumentationKey ?? '').toLowerCase() === iKey.toLowerCase()) {
        return String(component?.id ?? '')
      }
    }
  } catch {
    /* unavailable */
  }
  return ''
}

// Execute a KQL query against an App Insights component via ARM's `/query`
// endpoint (uses the caller's ARM token — no separate AI scope). Returns the
// component id and the raw ARM `tables` payload, or an error object.
export async function queryAppInsights(accessToken, subscriptionId, resourceGroup, appName, query, timespan) {
  const componentId = await resolveAppInsightsComponentId(accessToken, subscriptionId, resourceGroup, appName)
  if (!componentId) return { componentId: '', error: 'no-component' }
  const url = `https://management.azure.com${componentId}/query?api-version=2018-04-20`
  const body = { query, ...(timespan ? { timespan } : {}) }
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${accessToken}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(20000),
    })
    if (!res.ok) {
      const detail = await res.text().catch(() => '')
      return { componentId, error: `HTTP ${res.status}${detail ? `: ${detail.slice(0, 240)}` : ''}` }
    }
    return { componentId, ...(await res.json()) }
  } catch (err) {
    return { componentId, error: String(err?.message ?? err) }
  }
}

// Fetch a Function App's site object (used when reading one agent).
export async function getSite(accessToken, subscriptionId, resourceGroup, appName) {
  try {
    const client = webClient(accessToken, subscriptionId)
    return await client.webApps.get(resourceGroup, appName)
  } catch {
    return null
  }
}

// Merge a patch into a site's application settings (read-modify-write, since the
// ARM update replaces the whole dictionary). Returns the merged properties.
export async function setAppSettings(accessToken, subscriptionId, resourceGroup, appName, patch) {
  const client = webClient(accessToken, subscriptionId)
  const current = await client.webApps.listApplicationSettings(resourceGroup, appName)
  const properties = { ...(current?.properties || {}), ...patch }
  await client.webApps.updateApplicationSettings(resourceGroup, appName, { properties })
  return properties
}

// Remove the portal-recorded GitHub repo link from a Function App's application
// settings (read-modify-write; the ARM update replaces the whole dictionary, so
// we delete the keys and write the rest back). Returns true when a link was
// present and cleared. Intentionally does NOT touch the Deployment Center source
// control — that may be a live GitHub Actions CI/CD set up in the Azure portal.
export async function clearAppGithubLink(accessToken, subscriptionId, resourceGroup, appName) {
  const client = webClient(accessToken, subscriptionId)
  const current = await client.webApps.listApplicationSettings(resourceGroup, appName)
  const properties = { ...(current?.properties || {}) }
  let removed = false
  for (const key of ['GITHUB_REPO_URL', 'GITHUB_BRANCH', 'GITHUB_CONNECTED_BY']) {
    if (key in properties) {
      delete properties[key]
      removed = true
    }
  }
  if (removed) await client.webApps.updateApplicationSettings(resourceGroup, appName, { properties })
  return removed
}

// Read the GitHub repo connected via the Function App's Deployment Center
// (Microsoft.Web/sites/sourcecontrols/web). Returns {repoUrl, branch} or null.
export async function readDeploymentSource(accessToken, subscriptionId, resourceGroup, appName) {
  const url = `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}/providers/Microsoft.Web/sites/${appName}/sourcecontrols/web?api-version=2023-12-01`
  const res = await armJson(accessToken, url, { timeoutMs: 15000 })
  const repoUrl = res.json?.properties?.repoUrl || ''
  if (!res.ok || !repoUrl) return null
  return { repoUrl, branch: res.json.properties.branch || 'main' }
}

// Record a GitHub repo on the Function App's Deployment Center as a manual
// integration (no CI/CD webhook — the portal keeps deploying directly). This is
// what makes the connection visible in the portal's Deployment Center blade and
// readable back. Best-effort: returns true when Azure accepted it.
// NOTE: not wired into the connect flow — on Flex Consumption the Deployment
// Center uses GitHub Actions, so a manual-integration PUT is rejected (400) or
// would overwrite an existing GitHub Actions connection.
async function setDeploymentSource(accessToken, subscriptionId, resourceGroup, appName, { repoUrl, branch }) {
  const url = `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}/providers/Microsoft.Web/sites/${appName}/sourcecontrols/web?api-version=2023-12-01`
  const body = {
    properties: {
      repoUrl,
      branch: branch || 'main',
      isManualIntegration: true,
      isGitHubAction: false,
      deploymentRollbackEnabled: false,
    },
  }
  const res = await armJson(accessToken, url, { method: 'PUT', body, timeoutMs: 60000 })
  return res.ok
}

// Disconnect the Function App's Deployment Center source control (removes the
// Azure-side GitHub link — the ARM DELETE the portal's "Disconnect" button
// performs). Does NOT delete the workflow file in the repo or any federated
// credentials. Returns { ok, status }. Best-effort.
export async function deleteDeploymentSource(accessToken, subscriptionId, resourceGroup, appName) {
  const url = `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}/providers/Microsoft.Web/sites/${appName}/sourcecontrols/web?api-version=2023-12-01`
  const res = await armJson(accessToken, url, { method: 'DELETE', timeoutMs: 60000 })
  return { ok: res.ok, status: res.status }
}

// --- GitHub Actions (OIDC) provisioning ------------------------------------
// Set up passwordless CI/CD from a GitHub repo to a Function App using a
// user-assigned managed identity + a federated identity credential. This is
// all-ARM (Microsoft.ManagedIdentity + a role assignment) — no Microsoft Graph
// or app registration is required.
const MSI_API = '2023-01-31'
const ROLE_CONTRIBUTOR = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// Create (or update) a user-assigned managed identity; return its ids. Polls
// briefly for clientId/principalId, which can lag right after creation.
export async function ensureUserAssignedIdentity(accessToken, subscriptionId, resourceGroup, name, location) {
  const base = `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/${name}`
  const put = await armJson(accessToken, `${base}?api-version=${MSI_API}`, {
    method: 'PUT',
    body: { location },
    timeoutMs: 60000,
  })
  if (!put.ok) {
    const msg = put.json?.error?.message || `HTTP ${put.status}`
    throw Object.assign(new Error(`managed identity: ${msg}`), { status: put.status })
  }
  let clientId = put.json?.properties?.clientId || ''
  let principalId = put.json?.properties?.principalId || ''
  for (let i = 0; i < 6 && (!clientId || !principalId); i++) {
    await sleep(2000)
    const g = await armJson(accessToken, `${base}?api-version=${MSI_API}`)
    clientId = g.json?.properties?.clientId || clientId
    principalId = g.json?.properties?.principalId || principalId
  }
  return { id: put.json?.id || base, name, clientId, principalId }
}

// Create/replace a federated identity credential on a user-assigned identity so
// GitHub Actions (OIDC) for a specific repo+branch can obtain Azure tokens.
export async function ensureFederatedCredential(
  accessToken,
  subscriptionId,
  resourceGroup,
  identityName,
  ficName,
  subject,
) {
  const url = `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/${identityName}/federatedIdentityCredentials/${ficName}?api-version=${MSI_API}`
  const res = await armJson(accessToken, url, {
    method: 'PUT',
    body: {
      properties: {
        issuer: 'https://token.actions.githubusercontent.com',
        subject,
        audiences: ['api://AzureADTokenExchange'],
      },
    },
    timeoutMs: 60000,
  })
  if (!res.ok) {
    const msg = res.json?.error?.message || `HTTP ${res.status}`
    throw Object.assign(new Error(`federated credential: ${msg}`), { status: res.status })
  }
  return { name: ficName, subject }
}

// Assign a built-in role to a principal at resource-group scope. Idempotent (an
// existing assignment counts as success), with retries while a freshly created
// identity's service principal propagates through AAD.
export async function ensureRoleAssignment(
  accessToken,
  subscriptionId,
  resourceGroup,
  principalId,
  roleDefinitionId = ROLE_CONTRIBUTOR,
) {
  const scope = `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}`
  const body = {
    properties: {
      roleDefinitionId: `/subscriptions/${subscriptionId}/providers/Microsoft.Authorization/roleDefinitions/${roleDefinitionId}`,
      principalId,
      principalType: 'ServicePrincipal',
    },
  }
  let last
  for (let i = 0; i < 6; i++) {
    const url = `${scope}/providers/Microsoft.Authorization/roleAssignments/${randomUUID()}?api-version=2022-04-01`
    const res = await armJson(accessToken, url, { method: 'PUT', body, timeoutMs: 30000 })
    const code = String(res.json?.error?.code ?? '')
    if (res.ok || res.status === 409 || /RoleAssignmentExists/i.test(code)) {
      return { scope, roleDefinitionId, existed: !res.ok }
    }
    last = res
    if (!/PrincipalNotFound/i.test(code)) break
    await sleep(5000)
  }
  const msg = last?.json?.error?.message || `HTTP ${last?.status}`
  throw Object.assign(new Error(`role assignment: ${msg}`), { status: last?.status })
}

// Best-effort: record the GitHub repo on the Function App's Deployment Center as a
// GitHub Actions source so the portal blade shows it. We do NOT ask Azure to
// generate a workflow (generateWorkflowFile:false) — we commit our own. Registers
// the user's GitHub token at the tenant source-control first. Never throws.
export async function setGithubActionSource(
  accessToken,
  subscriptionId,
  resourceGroup,
  appName,
  { repoUrl, branch, githubToken },
) {
  try {
    if (githubToken) {
      await armJson(accessToken, `/providers/Microsoft.Web/sourcecontrols/GitHub?api-version=2023-12-01`, {
        method: 'PUT',
        body: { properties: { token: githubToken } },
        timeoutMs: 30000,
      })
    }
    const url = `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}/providers/Microsoft.Web/sites/${appName}/sourcecontrols/web?api-version=2023-12-01`
    const res = await armJson(accessToken, url, {
      method: 'PUT',
      body: {
        properties: {
          repoUrl,
          branch: branch || 'main',
          isManualIntegration: false,
          isGitHubAction: true,
          gitHubActionConfiguration: { generateWorkflowFile: false, isLinux: true },
        },
      },
      timeoutMs: 60000,
    })
    return { ok: res.ok, status: res.status }
  } catch {
    return { ok: false }
  }
}

// Read the GitHub connection recorded on a Function App. Prefers the Deployment
// Center source control (the source of truth — also set when connected outside
// this portal), then falls back to the portal's app-setting metadata.
export async function getAppGithubLink(accessToken, subscriptionId, resourceGroup, appName) {
  try {
    const dc = await readDeploymentSource(accessToken, subscriptionId, resourceGroup, appName)
    if (dc?.repoUrl) {
      return { connected: true, repoUrl: dc.repoUrl, branch: dc.branch, source: 'deploymentCenter' }
    }
  } catch {
    /* fall back to app settings */
  }
  try {
    const client = webClient(accessToken, subscriptionId)
    const current = await client.webApps.listApplicationSettings(resourceGroup, appName)
    const p = current?.properties || {}
    const repoUrl = p.GITHUB_REPO_URL || ''
    if (repoUrl) {
      return {
        connected: true,
        repoUrl,
        branch: p.GITHUB_BRANCH || 'main',
        connectedBy: p.GITHUB_CONNECTED_BY || '',
        source: 'appSettings',
      }
    }
  } catch {
    /* ignore */
  }
  return { connected: false }
}

// Fetch a host-level function key for invoking an app's functions (the built-in
// chat endpoint defaults to FUNCTION auth). Best-effort: '' when unavailable.
export async function functionHostKey(accessToken, subscriptionId, resourceGroup, appName) {
  try {
    const client = webClient(accessToken, subscriptionId)
    const keys = await client.webApps.listHostKeys(resourceGroup, appName)
    return keys?.functionKeys?.default || keys?.masterKey || ''
  } catch {
    return ''
  }
}

// Call a deployed agent's built-in chat endpoint (`POST agents/<slug>/chat`).
// Tries the default route prefix and the `api` prefix. Returns the normalised
// chat result `{ sessionId, response, toolCalls }`.
export async function callAgentChat(host, agentSlug, prompt, { key = '', sessionId = '' } = {}) {
  const slug = encodeURIComponent(agentSlug)
  const paths = [`agents/${slug}/chat`, `api/agents/${slug}/chat`]
  const headers = { 'Content-Type': 'application/json' }
  if (key) headers['x-functions-key'] = key
  if (sessionId) headers['x-ms-session-id'] = sessionId

  let lastErr = 'no route matched'
  for (const p of paths) {
    let res
    try {
      res = await fetch(`https://${host}/${p}`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ prompt }),
        signal: AbortSignal.timeout(120000),
      })
    } catch (e) {
      lastErr = String(e?.message ?? e)
      continue
    }
    if (res.status === 404) {
      lastErr = `404 at /${p}`
      continue // wrong route prefix — try the next
    }
    const text = await res.text()
    let body
    try {
      body = text ? JSON.parse(text) : {}
    } catch {
      body = { response: text }
    }
    if (!res.ok) {
      const detail = body?.error || text || `${res.status} ${res.statusText}`
      const err = new Error(String(detail).slice(0, 600))
      err.status = res.status
      throw err
    }
    return {
      sessionId: body.session_id ?? res.headers.get('x-ms-session-id') ?? sessionId ?? '',
      response: body.response ?? '',
      toolCalls: Array.isArray(body.tool_calls) ? body.tool_calls : [],
    }
  }
  const err = new Error(`Agent chat endpoint not reachable (${lastErr}).`)
  err.status = 502
  throw err
}

// Open a deployed agent's streaming chat endpoint (`POST agents/<slug>/chatstream`,
// SSE). Tries the default route prefix then `api`. Returns the raw upstream
// Response so the caller can pipe `response.body` straight through. `signal`
// lets the caller abort when the browser disconnects.
export async function openAgentChatStream(host, agentSlug, prompt, { key = '', sessionId = '', signal } = {}) {
  const slug = encodeURIComponent(agentSlug)
  const paths = [`agents/${slug}/chatstream`, `api/agents/${slug}/chatstream`]
  const headers = { 'Content-Type': 'application/json', Accept: 'text/event-stream' }
  if (key) headers['x-functions-key'] = key
  if (sessionId) headers['x-ms-session-id'] = sessionId

  let lastErr = 'no route matched'
  for (const p of paths) {
    let res
    try {
      res = await fetch(`https://${host}/${p}`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ prompt }),
        signal,
      })
    } catch (e) {
      lastErr = String(e?.message ?? e)
      continue
    }
    if (res.status === 404) {
      lastErr = `404 at /${p}`
      continue
    }
    if (!res.ok || !res.body) {
      const text = await res.text().catch(() => '')
      const err = new Error(text?.slice(0, 500) || `${res.status} ${res.statusText}`)
      err.status = res.status
      throw err
    }
    return res
  }
  const err = new Error(`Agent chatstream endpoint not reachable (${lastErr}).`)
  err.status = 502
  throw err
}

// ---------------------------------------------------------------------------
// Microsoft Foundry (Azure AI Services) — discovery for the create flow, and
// model-powered generation of an agent's instructions. All via ARM (the
// caller's token); the model call uses the account key fetched with listKeys,
// so no extra token scope is needed.
// ---------------------------------------------------------------------------

const CS_ACCOUNTS_API = '2023-05-01'
const CS_DEPLOYMENTS_API = '2024-10-01'
const CS_PROJECTS_API = '2025-10-01-preview'
const OPENAI_CHAT_API = '2024-10-21'

async function armJson(accessToken, url, { method = 'GET', body, timeoutMs = 15000 } = {}) {
  try {
    const full = url.startsWith('http') ? url : `https://management.azure.com${url}`
    const res = await fetch(full, {
      method,
      headers: {
        Authorization: `Bearer ${accessToken}`,
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeoutMs),
    })
    return { ok: res.ok, status: res.status, json: await res.json().catch(() => null) }
  } catch {
    return { ok: false, status: 0, json: null }
  }
}

async function armList(accessToken, initialUrl, { timeoutMs = 15000, maxPages = 50 } = {}) {
  const values = []
  let url = initialUrl
  for (let page = 0; page < maxPages && url; page++) {
    const res = await armJson(accessToken, url, { timeoutMs })
    if (!res.ok) {
      const detail = res.json?.error?.message || res.json?.message || `HTTP ${res.status}`
      throw Object.assign(new Error(`ARM list request failed: ${detail}`), {
        status: res.status || 502,
      })
    }
    values.push(...(res.json?.value ?? []))
    url = res.json?.nextLink ?? ''
  }
  if (url) {
    throw Object.assign(new Error(`ARM list exceeded ${maxPages} pages.`), { status: 502 })
  }
  return values
}

/**
 * Discover Microsoft Foundry (Azure AI Services / OpenAI) accounts in a
 * subscription, with their chat model deployments and projects, so the create
 * flow can offer a picker.
 */
export async function discoverFoundry(accessToken, subscriptionId) {
  const listed = await armList(
    accessToken,
    `/subscriptions/${subscriptionId}/providers/Microsoft.CognitiveServices/accounts?api-version=${CS_ACCOUNTS_API}`,
    { timeoutMs: 20000 },
  )
  const raw = listed.filter((a) => {
    const kind = String(a?.kind ?? '')
    return kind === 'AIServices' || kind === 'OpenAI'
  })

  const accounts = await mapLimit(raw, 8, async (acc) => {
    const name = acc.name
    const resourceGroup = parseResourceGroup(acc.id)
    const endpoints = acc.properties?.endpoints ?? {}
    const foundryEndpoint = endpoints['AI Foundry API'] || ''
    const openaiEndpoint =
      endpoints['OpenAI Language Model Instance API'] || acc.properties?.endpoint || ''

    const [deps, projs] = await Promise.all([
      armList(
        accessToken,
        `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}/providers/Microsoft.CognitiveServices/accounts/${name}/deployments?api-version=${CS_DEPLOYMENTS_API}`,
      ),
      foundryEndpoint
        ? armList(
            accessToken,
            `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}/providers/Microsoft.CognitiveServices/accounts/${name}/projects?api-version=${CS_PROJECTS_API}`,
          )
        : Promise.resolve([]),
    ])

    const models = deps
      .map((d) => ({ deployment: d.name, model: d.properties?.model?.name ?? d.name }))
      .filter((m) => !/embedding|whisper|dall-?e|tts|sora|moderation|transcribe/i.test(m.model))
      .sort((a, b) => a.deployment.localeCompare(b.deployment))

    const projects = projs
      .map((p) => {
        const short = String(p.name ?? '').split('/').pop() ?? p.name
        return { name: short, endpoint: `${foundryEndpoint}api/projects/${short}` }
      })
      .sort((a, b) => a.name.localeCompare(b.name))

    return { name, resourceGroup, location: acc.location ?? '', kind: acc.kind ?? '', foundryEndpoint, openaiEndpoint, projects, models }
  })

  accounts.sort((a, b) => a.name.localeCompare(b.name))
  return { subscriptionId, accounts }
}

// Fetch a Cognitive Services account data-plane key via ARM listKeys.
async function foundryAccountKey(accessToken, subscriptionId, resourceGroup, account) {
  const res = await armJson(
    accessToken,
    `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}/providers/Microsoft.CognitiveServices/accounts/${account}/listKeys?api-version=${CS_ACCOUNTS_API}`,
    { method: 'POST' },
  )
  return res.json?.key1 || res.json?.key2 || ''
}

/**
 * Generate an agent's instructions (the `.agent.md` body) by calling the chosen
 * Foundry chat model. Key-auth via the account key (no extra token scope).
 */
export async function generateAgentInstructions(accessToken, subscriptionId, opts) {
  const { resourceGroup, account, openaiEndpoint, model, name, description } = opts
  const system =
    'You are an expert at writing system-prompt instructions for AI agents. Given an agent name and a short ' +
    "description of what it should do, write clear, effective instructions used verbatim as the agent's system " +
    'prompt. Describe the role, expected behavior, inputs and outputs, tone, and any constraints. Output ONLY ' +
    'the instructions as plain Markdown prose — no YAML front matter, no code fences, no "Instructions" heading.'
  const user = `Agent name: ${name || '(unnamed)'}\nWhat it should do: ${description || '(no description provided)'}`
  return foundryChat(accessToken, subscriptionId, { resourceGroup, account, openaiEndpoint, model, system, user })
}

// Call a Foundry chat model with a system + user prompt; returns { content }
// stripped of any wrapping code fence. Key-auth via the account key.
async function foundryChat(accessToken, subscriptionId, opts) {
  const { resourceGroup, account, openaiEndpoint, model, system, user, timeoutMs = 60000 } = opts
  if (!openaiEndpoint || !model || !account || !resourceGroup) {
    throw Object.assign(new Error('A Foundry account, model, and endpoint are required.'), { status: 400 })
  }
  const key = await foundryAccountKey(accessToken, subscriptionId, resourceGroup, account)
  if (!key) throw Object.assign(new Error('Could not read the Foundry account key (permission?).'), { status: 502 })

  const base = openaiEndpoint.endsWith('/') ? openaiEndpoint : `${openaiEndpoint}/`
  const url = `${base}openai/deployments/${encodeURIComponent(model)}/chat/completions?api-version=${OPENAI_CHAT_API}`
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'api-key': key, 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages: [{ role: 'system', content: system }, { role: 'user', content: user }] }),
    signal: AbortSignal.timeout(timeoutMs),
  })
  const text = await res.text()
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      detail = JSON.parse(text)?.error?.message || detail
    } catch {
      /* raw */
    }
    throw Object.assign(new Error(`Generation failed: ${String(detail).slice(0, 300)}`), { status: res.status })
  }
  let content = ''
  try {
    content = JSON.parse(text)?.choices?.[0]?.message?.content ?? ''
  } catch {
    /* leave empty */
  }
  content = content.replace(/^\s*```[a-z]*\n?/i, '').replace(/\n?```\s*$/i, '').trim()
  return { content }
}

// Capability code generators, grounded in the azure-functions-agents-runtime
// `.agent.md` front matter and `tools/` conventions.
const CAPABILITY_PROMPTS = {
  http_trigger: {
    system:
      'You are an expert author of AI agents for the azure-functions-agents-runtime (Azure Functions, Python). ' +
      'Generate a COMPLETE `.agent.md` file for a TRIGGERED agent. Triggers are DECLARATIVE front matter — NEVER write Python or a function_app.py. Output exactly:\n' +
      '1) YAML front matter between --- fences with keys:\n' +
      '   name: <concise name>\n   description: <one line>\n' +
      '   trigger:\n     type: <the requested trigger type>\n     args:\n       <exactly the args that trigger type requires>\n' +
      '   Use the correct trigger.type and args for the REQUESTED trigger type, following the authoritative guidance. ' +
      'For timer_trigger use args.schedule as an NCRONTAB string (6 fields); for http_trigger use route + methods + auth_level; for a connector use type: generic_trigger with args.type: connectorTrigger.\n' +
      '   Optionally add input_schema and response_schema (JSON Schema objects) when the task has structured input/output.\n' +
      '2) After the closing ---, the instructions as Markdown prose: the agent role and exactly what to do when the trigger fires (for a scheduled agent, the work to perform on each run).\n' +
      'Output ONLY the .agent.md content. No code fences, no commentary.',
    user: ({ name, description, triggerType }) =>
      `Agent name: ${name || '(unnamed)'}\nTarget trigger type: ${triggerType || 'http_trigger'}\nWhat the agent should do when it is triggered: ${description || '(describe the task)'}`,
  },
  connector_trigger: {
    system:
      'You are an expert author of AI agents for the azure-functions-agents-runtime. Generate a COMPLETE `.agent.md` ' +
      'for a CONNECTOR-triggered agent that runs when an Azure Connector event fires (e.g. a new Office 365 Outlook email). Output exactly:\n' +
      '1) YAML front matter between --- fences with keys:\n' +
      '   name: <concise name>\n   description: <one line>\n' +
      '   trigger:\n     type: generic_trigger\n     args:\n       type: connectorTrigger\n' +
      '2) After the closing ---, instructions: describe the connector event, how to read EVERY item in the trigger payload, the task to perform, and which tools/MCP servers to use (e.g. the Office 365 Outlook MCP to draft/send replies).\n' +
      'Output ONLY the .agent.md content. No code fences, no commentary.',
    user: ({ name, description }) =>
      `Agent name: ${name || '(unnamed)'}\nWhat it should do on the connector event: ${description || '(describe the event and task)'}`,
  },
  custom_tool: {
    system:
      'You are an expert Python engineer writing a custom tool for the azure-functions-agents-runtime `tools/` directory. Requirements:\n' +
      '- `from azure_functions_agents import tool` and `from pydantic import BaseModel, Field`.\n' +
      '- Define a Pydantic input model (BaseModel) with Field(description=...) on each field.\n' +
      '- Define ONE function decorated with `@tool` taking a single argument typed as that model and returning a `str` (JSON) or dict. It MAY be `async`.\n' +
      '- Give the function a clear docstring (used as the tool description).\n' +
      '- For Azure calls use `from azure.identity.aio import DefaultAzureCredential` (the app managed identity). Prefer aiohttp or httpx. Keep it a single self-contained file.\n' +
      'Output ONLY valid Python for one file. No markdown, no code fences, no commentary.',
    user: ({ name, description }) =>
      `Tool name: ${name || '(unnamed)'}\nWhat the tool should do: ${description || '(describe the tool)'}`,
  },
  skill: {
    system:
      'You are an expert at authoring reusable SKILL.md knowledge files for the azure-functions-agents-runtime `skills/` directory. Generate a COMPLETE SKILL.md. Output exactly:\n' +
      '1) YAML front matter between --- fences with:\n' +
      '   name: <kebab-case-name>\n   description: <one sentence: the knowledge/capability it provides and when to use it>\n' +
      '2) After the closing ---, Markdown giving the agent durable, reusable guidance: the domain knowledge, step-by-step how-to, key references/URLs/commands, best practices, and pitfalls. Keep it focused and actionable.\n' +
      'Output ONLY the SKILL.md content. No code fences, no commentary.',
    user: ({ name, description }) =>
      `Skill name: ${name || '(unnamed)'}\nWhat the skill should cover: ${description || '(describe the knowledge/capability)'}`,
  },
}

// Generate the code/config for an agent capability (HTTP trigger .agent.md,
// connector trigger .agent.md, or a custom Python tool) with a Foundry model.
export async function generateCapabilityCode(accessToken, subscriptionId, opts) {
  const { resourceGroup, account, openaiEndpoint, model, kind, name, description, triggerType, skillsContext, guidance } = opts
  const p = CAPABILITY_PROMPTS[kind]
  if (!p) throw Object.assign(new Error(`Unknown capability kind: ${kind}`), { status: 400 })
  let user = p.user({ name, description, triggerType })
  if (skillsContext) {
    user +=
      '\n\nUse these existing project skills as authoritative reference where relevant ' +
      '(match their conventions, tools, endpoints, and terminology):\n\n' +
      skillsContext
  }
  // Prepend the portal's authoritative authoring skill for this capability kind.
  const system = guidance
    ? `${p.system}\n\n--- Authoritative runtime guidance (follow it exactly) ---\n\n${guidance}`
    : p.system
  const { content } = await foundryChat(accessToken, subscriptionId, {
    resourceGroup,
    account,
    openaiEndpoint,
    model,
    system,
    user,
    timeoutMs: 90000,
  })
  return { content, kind }
}

// Fallback planner guidance if the portal skill file can't be read.
const PLANNER_SYSTEM =
  'You are a capability planner for the azure-functions-agents-runtime. Given an agent description, ' +
  'infer which capabilities it needs and output ONLY a JSON object: ' +
  '{"capabilities":[{"kind":"...","name":"short-name","description":"one line"}]}. ' +
  'kind is one of http_trigger, timer_trigger, connector_trigger, custom_tool, mcp, skill. ' +
  'Only include capabilities clearly implied by the description; an empty list is fine. Max 6. ' +
  'Output ONLY the JSON, no prose, no code fences.'

// Infer the capabilities an agent needs from its description (skill-grounded).
export async function planCapabilities(accessToken, subscriptionId, opts) {
  const { resourceGroup, account, openaiEndpoint, model, description, guidance } = opts
  const system = guidance || PLANNER_SYSTEM
  const user = `Agent description:\n${description || '(none)'}\n\nReturn the JSON plan now.`
  const { content } = await foundryChat(accessToken, subscriptionId, {
    resourceGroup,
    account,
    openaiEndpoint,
    model,
    system,
    user,
    timeoutMs: 60000,
  })
  let plan = null
  try {
    const start = content.indexOf('{')
    const end = content.lastIndexOf('}')
    if (start >= 0 && end > start) plan = JSON.parse(content.slice(start, end + 1))
  } catch {
    plan = null
  }
  const caps = Array.isArray(plan?.capabilities) ? plan.capabilities : []
  const allowed = new Set(['http_trigger', 'timer_trigger', 'connector_trigger', 'custom_tool', 'mcp', 'skill'])
  return {
    capabilities: caps
      .filter((c) => c && allowed.has(c.kind) && c.name)
      .slice(0, 6)
      .map((c) => ({
        kind: String(c.kind),
        name: String(c.name).slice(0, 60),
        description: String(c.description ?? '').slice(0, 300),
      })),
  }
}

// List the resource groups in a subscription (name + location), for the create
// flow's "existing resource group" picker.
export async function listResourceGroups(accessToken, subscriptionId) {
  const groups = await armList(
    accessToken,
    `/subscriptions/${subscriptionId}/resourcegroups?api-version=2021-04-01`,
  )
  const resourceGroups = groups.map((g) => ({ name: g.name, location: g.location ?? '' }))
  resourceGroups.sort((a, b) => a.name.localeCompare(b.name))
  return { subscriptionId, resourceGroups }
}

// Check whether a Function App name is globally available (Microsoft.Web/sites).
// Function App names share the *.azurewebsites.net namespace, so they must be
// globally unique; this lets the deploy flow validate before provisioning.
export async function checkFunctionAppNameAvailable(accessToken, subscriptionId, name) {
  const res = await armJson(
    accessToken,
    `/subscriptions/${subscriptionId}/providers/Microsoft.Web/checkNameAvailability?api-version=2023-12-01`,
    { method: 'POST', body: { name, type: 'Microsoft.Web/sites' }, timeoutMs: 15000 },
  )
  if (!res.ok || !res.json) {
    return { available: false, reason: '', message: 'Could not check name availability.' }
  }
  return {
    available: !!res.json.nameAvailable,
    reason: String(res.json.reason ?? ''),
    message: String(res.json.message ?? ''),
  }
}

// Roles a Foundry account's callers need (matches the reference infra).
const COGNITIVE_SERVICES_USER_ROLE = 'a97b65f3-24c7-4388-baec-2e87135dc908'
const COGNITIVE_SERVICES_OPENAI_USER_ROLE = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

// Grant a principal (a deployed app's managed identity) the roles needed to call
// a Foundry account's models. Works cross-subscription. Idempotent: an existing
// assignment counts as granted.
export async function grantFoundryAccess(accessToken, { subscriptionId, resourceGroup, account, principalId }) {
  if (!subscriptionId || !resourceGroup || !account || !principalId) {
    throw Object.assign(new Error('subscription, resourceGroup, account, and principalId are required.'), {
      status: 400,
    })
  }
  const scope = `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}/providers/Microsoft.CognitiveServices/accounts/${account}`
  const roles = [
    { name: 'Cognitive Services User', id: COGNITIVE_SERVICES_USER_ROLE },
    { name: 'Cognitive Services OpenAI User', id: COGNITIVE_SERVICES_OPENAI_USER_ROLE },
  ]
  const granted = []
  const failed = []
  for (const role of roles) {
    const url = `${scope}/providers/Microsoft.Authorization/roleAssignments/${randomUUID()}?api-version=2022-04-01`
    const body = {
      properties: {
        roleDefinitionId: `/subscriptions/${subscriptionId}/providers/Microsoft.Authorization/roleDefinitions/${role.id}`,
        principalId,
        principalType: 'ServicePrincipal',
      },
    }
    const res = await armJson(accessToken, url, { method: 'PUT', body, timeoutMs: 20000 })
    const code = String(res.json?.error?.code ?? '')
    if (res.ok || res.status === 409 || /RoleAssignmentExists/i.test(code)) {
      granted.push(role.name)
    } else {
      failed.push({ role: role.name, error: res.json?.error?.message || `status ${res.status}` })
    }
  }
  return { granted, failed, scope }
}

// End-to-end "Grant access" for a running app: resolve its managed identity
// principalId + the Foundry account it's configured to call, then grant
// Cognitive Services User + OpenAI User to it. Returns
// `{ granted, failed, scope, principalId, account }`. Called by the Playground's
// self-heal button when a chat returns 403 / permission-denied.
export async function healFoundryAccess(accessToken, subscriptionId, resourceGroup, appName) {
  const site = await getSite(accessToken, subscriptionId, resourceGroup, appName)
  if (!site) throw Object.assign(new Error(`Function App "${appName}" was not found.`), { status: 404 })
  const principalId = site?.identity?.principalId || ''
  if (!principalId) {
    throw Object.assign(
      new Error("This app has no system-assigned managed identity. Enable one on the Function App first."),
      { status: 409 },
    )
  }
  const settings = await readAppSettings(accessToken, subscriptionId, resourceGroup, appName)
  const endpoint = String(settings.FOUNDRY_PROJECT_ENDPOINT || settings.AZURE_OPENAI_ENDPOINT || '').trim()
  if (!endpoint) {
    throw Object.assign(
      new Error('No FOUNDRY_PROJECT_ENDPOINT app setting on this app — nothing to grant.'),
      { status: 404 },
    )
  }
  let host
  try {
    host = new URL(endpoint).hostname
  } catch {
    throw Object.assign(new Error(`Invalid Foundry endpoint: ${endpoint}`), { status: 400 })
  }
  const account = host.split('.')[0]
  if (!account) throw Object.assign(new Error(`Couldn't extract account name from ${host}.`), { status: 400 })

  // Locate the AI Services account across the subscription so we know its RG.
  let target = null
  try {
    const accounts = await armList(
      accessToken,
      `/subscriptions/${subscriptionId}/providers/Microsoft.CognitiveServices/accounts?api-version=2023-05-01`,
    )
    target = accounts.find((a) => String(a?.name ?? '') === account)
  } catch {
    /* unavailable — fall through */
  }
  if (!target) {
    throw Object.assign(
      new Error(
        `Couldn't find AI Services account "${account}" in this subscription. If it lives in a different subscription, grant access from the Foundry account's IAM blade in the Azure portal instead.`,
      ),
      { status: 404 },
    )
  }
  const accountResourceGroup = parseResourceGroup(target.id)
  const result = await grantFoundryAccess(accessToken, {
    subscriptionId,
    resourceGroup: accountResourceGroup,
    account,
    principalId,
  })
  return { ...result, principalId, account, accountResourceGroup }
}

// Resolve the authoritative set of agent slugs from the deployed `*.agent.md`
// files. Prefers Kudu VFS; on Flex Consumption reads the deployment package.
// `ok` is true only when a source returned the complete file list, so callers
// know whether to trust it over the function-name heuristic.
//
// Flex Consumption apps have no Kudu VFS — the probe always 404s after a wait —
// so skip it for them (they carry `functionAppConfig`) and read the deployment
// package directly.
function isFlexConsumption(site) {
  return Boolean(site?.functionAppConfig)
}

async function readAgentSlugs(accessToken, subscriptionId, site) {
  if (!isFlexConsumption(site)) {
    const kudu = await agentSourceFiles(accessToken, site)
    if (kudu.ok) {
      return { slugs: new Set(kudu.files.map(agentSlugFromFileName)), ok: true }
    }
  }
  const flex = await flexPackageAgentFiles(accessToken, subscriptionId, site)
  if (flex.ok) {
    return { slugs: new Set(flex.files.map(agentSlugFromFileName)), ok: true }
  }
  return { slugs: new Set(), ok: false }
}

// Enumerate a single Function App's functions. Prefers the ARM control plane; on
// plans where that yields nothing (Linux/Flex Consumption) it falls back to the
// app's read-only admin metadata API.
async function functionsInApp(client, resourceGroup, appName, defaultHostName) {
  const fromArm = await functionsFromArm(client, resourceGroup, appName)
  if (fromArm.length > 0) return fromArm
  return functionsFromAdminApi(client, resourceGroup, appName, defaultHostName)
}

// Bounded-concurrency map: apply `fn` across `items`, at most `limit` in flight.
// Keeps a whole-subscription refresh fast without tripping ARM 429 throttling.
async function mapLimit(items, limit, fn) {
  const results = new Array(items.length)
  let next = 0
  const worker = async () => {
    while (next < items.length) {
      const index = next++
      results[index] = await fn(items[index], index)
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker))
  return results
}

// Fan-out limits: the app-setting gate is cheap (higher), enrichment (functions
// + source reads) is heavier (lower).
const GATE_CONCURRENCY = 12
const ENRICH_CONCURRENCY = 8

/**
 * Discover every agent app + its agents in a subscription.
 *
 * @param {string} accessToken forwarded ARM access token
 * @param {string} subscriptionId resolved subscription id
 * @returns {Promise<{
 *   subscriptionId: string,
 *   apps: Array<{
 *     name: string,
 *     resourceGroup: string,
 *     location: string,
 *     provider: string,
 *     defaultHostName: string,
 *     agents: Array<{name: string, trigger: string, builtinEndpoints: boolean, routes: string[], supportingFunctions: string[]}>,
 *     supportingFunctions: Array<{name: string, trigger: string}>,
 *   }>,
 * }>}
 */
export async function discoverAgentApps(accessToken, subscriptionId) {
  const client = webClient(accessToken, subscriptionId)

  // Collect every Function App site up front so the per-app work can fan out
  // instead of running one app at a time.
  const sites = []
  for await (const site of client.webApps.list()) {
    if (!String(site.kind ?? '').includes('functionapp')) continue
    const resourceGroup = parseResourceGroup(site.id)
    const appName = site.name ?? ''
    if (appName && resourceGroup) sites.push({ site, resourceGroup, appName })
  }

  // Gate (parallel, cheap): an app IS a serverless agent app if — and only if —
  // it carries the AZURE_FUNCTIONS_AGENTS_PROVIDER app setting. Check them all
  // concurrently and drop the rest before doing any expensive work.
  const gated = (
    await mapLimit(sites, GATE_CONCURRENCY, async (entry) => {
      try {
        const settings = await client.webApps.listApplicationSettings(entry.resourceGroup, entry.appName)
        const settingsMap = settingsToMap(settings.properties)
        if (!(AGENT_PROVIDER_SETTING in settingsMap)) return null
        return { ...entry, provider: settingsMap[AGENT_PROVIDER_SETTING] ?? '' }
      } catch {
        return null
      }
    })
  ).filter(Boolean)

  // Enrich (parallel, heavier): list functions and read the authoritative
  // `*.agent.md` slugs for each agent app, then classify agents vs supporting
  // functions. The two reads per app run together.
  const apps = await mapLimit(gated, ENRICH_CONCURRENCY, async ({ site, resourceGroup, appName, provider }) => {
    const [functions, slugInfo] = await Promise.all([
      functionsInApp(client, resourceGroup, appName, site.defaultHostName),
      readAgentSlugs(accessToken, subscriptionId, site),
    ])
    const { agents, appSupportingFunctions } = parseAgentsFromFunctions(functions, slugInfo.slugs, slugInfo.ok)

    // Fall back to the app itself as a single agent when nothing was found.
    if (agents.length === 0) {
      agents.push({
        name: appName,
        trigger: 'http',
        builtinEndpoints: false,
        routes: [],
        supportingFunctions: [],
      })
    }
    return {
      name: appName,
      resourceGroup,
      location: site.location ?? '',
      provider,
      defaultHostName: site.defaultHostName ?? '',
      agents,
      supportingFunctions: appSupportingFunctions,
    }
  })

  apps.sort((a, b) => a.name.localeCompare(b.name))
  return { subscriptionId, apps }
}
