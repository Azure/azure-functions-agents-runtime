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
//   2. Agents inside a qualifying app are enumerated from the runtime naming
//      convention: every registered function is prefixed `agent_`, built-in
//      endpoints register at routes `agents/<name>/…` (chat, chatstream, page)
//      and an MCP tool trigger `agent_<name>_builtin_mcp`. If no agents can be
//      parsed, the app itself is surfaced as a single agent.
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

import { SubscriptionClient } from '@azure/arm-resources-subscriptions'
import { WebSiteManagementClient } from '@azure/arm-appservice'

const AGENT_PROVIDER_SETTING = 'AZURE_FUNCTIONS_AGENTS_PROVIDER'

// v1 scope: a single default subscription. Override with PORTAL_SUBSCRIPTION_ID.
// The signed-in identity (the forwarded ARM token) authorises every call.
export const DEFAULT_SUBSCRIPTION_ID =
  process.env.PORTAL_SUBSCRIPTION_ID || '1a839f1f-10b2-4613-95ad-0800a22abbf2'

// Built-in endpoint function suffixes we recognise, longest first so the agent
// name is stripped correctly (e.g. `_builtin_chatstream` before `_builtin_chat`).
const BUILTIN_SUFFIXES = [
  '_builtin_chatstream',
  '_builtin_chat_page',
  '_builtin_chat',
  '_builtin_mcp',
]

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
// distinct agents they represent. Shared so both discovery sources parse
// identically.
//
// Two agent shapes are recognised:
//   • Built-in-endpoint agents — one or more `agent_<slug>_builtin_*` functions
//     (chat UI/API/SSE/MCP) sharing a slug, grouped into a single agent.
//   • Custom-trigger agents — every other registered function is its own agent
//     (the runtime names the trigger function after the `<agent>.agent.md`
//     source file). Its trigger type and any HTTP route are captured so the UI
//     can surface the real invocation endpoint.
function parseAgentsFromFunctions(functions) {
  const agents = new Map() // name → { name, triggers:Set, builtinEndpoints, routes:Set }
  const getEntry = (name) => {
    let entry = agents.get(name)
    if (!entry) {
      entry = { name, triggers: new Set(), builtinEndpoints: false, routes: new Set() }
      agents.set(name, entry)
    }
    return entry
  }

  for (const fn of functions) {
    // Function names arrive as `<app>/<function>`; keep the last segment.
    const shortName = String(fn?.name ?? '').split('/').pop() ?? ''
    const bindings = fn?.config?.bindings ?? []
    const triggerBinding = bindings.find(
      (b) => typeof b?.type === 'string' && b.type.toLowerCase().endsWith('trigger'),
    )
    const routeBinding = bindings.find((b) => typeof b?.route === 'string' && b.route)
    const route = routeBinding?.route ?? ''

    const builtinSlug = builtinSlugFromFunction(shortName, route)
    if (builtinSlug) {
      const entry = getEntry(builtinSlug)
      entry.builtinEndpoints = true
      entry.triggers.add('httpTrigger')
      continue
    }

    if (!shortName) continue
    const entry = getEntry(shortName)
    if (triggerBinding?.type) entry.triggers.add(String(triggerBinding.type))
    if (route) entry.routes.add(route)
  }

  return [...agents.values()].map((a) => ({
    name: a.name,
    trigger: a.triggers.has('httpTrigger') ? 'http' : normalizeTrigger([...a.triggers][0] ?? 'httpTrigger'),
    builtinEndpoints: a.builtinEndpoints,
    routes: [...a.routes],
  }))
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

// List the agents hosted in a single Function App. Prefers the ARM control
// plane; on plans where that yields nothing (Linux/Flex Consumption) it falls
// back to the app's admin metadata API so multi-agent apps and their built-in
// endpoints are still discovered.
async function agentsInApp(client, resourceGroup, appName, defaultHostName) {
  let agents = parseAgentsFromFunctions(
    await functionsFromArm(client, resourceGroup, appName),
  )
  if (agents.length === 0) {
    agents = parseAgentsFromFunctions(
      await functionsFromAdminApi(client, resourceGroup, appName, defaultHostName),
    )
  }
  return agents
}

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
 *     agents: Array<{name: string, trigger: string, builtinEndpoints: boolean, routes: string[]}>,
 *   }>,
 * }>}
 */
export async function discoverAgentApps(accessToken, subscriptionId) {
  const client = webClient(accessToken, subscriptionId)
  const apps = []
  for await (const site of client.webApps.list()) {
    const kind = String(site.kind ?? '')
    if (!kind.includes('functionapp')) continue

    const resourceGroup = parseResourceGroup(site.id)
    const appName = site.name ?? ''
    if (!appName || !resourceGroup) continue

    // Identification rule: a Function App IS a serverless agent app if — and
    // only if — it carries the AZURE_FUNCTIONS_AGENTS_PROVIDER app setting.
    let settingsMap
    try {
      const settings = await client.webApps.listApplicationSettings(resourceGroup, appName)
      settingsMap = settingsToMap(settings.properties)
    } catch {
      continue
    }
    if (!(AGENT_PROVIDER_SETTING in settingsMap)) continue

    // The app qualifies. Enumerate individual agents from the runtime's function
    // naming convention; if none can be parsed (e.g. trigger-only agents), fall
    // back to representing the app itself as a single agent so it still appears.
    let agents = await agentsInApp(client, resourceGroup, appName, site.defaultHostName)
    if (agents.length === 0) {
      agents = [{ name: appName, trigger: 'http', builtinEndpoints: false, routes: [] }]
    }
    apps.push({
      name: appName,
      resourceGroup,
      location: site.location ?? '',
      provider: settingsMap[AGENT_PROVIDER_SETTING] ?? '',
      defaultHostName: site.defaultHostName ?? '',
      agents,
    })
  }
  apps.sort((a, b) => a.name.localeCompare(b.name))
  return { subscriptionId, apps }
}
