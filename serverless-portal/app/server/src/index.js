// Serverless Agent Portal — Node.js backend (Express).
//
// A thin read-only control plane over live Azure discovery. Every Azure call
// runs as the signed-in user: the browser authenticates via MSAL (the same
// first-party app as Polaris), acquires an ARM access token, and forwards it as
// a Bearer token, which this backend uses for all ARM requests. See
// serverless-portal/app/README.md.

import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs'
import { randomUUID } from 'node:crypto'

import './env.js' // load .env into process.env before anything reads config

import express from 'express'
import cors from 'cors'

import * as azure from './azure.js'
import * as github from './github.js'
import * as provision from './provision.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const DIST_DIR = path.resolve(__dirname, '..', '..', 'frontend', 'dist')
const PORT = Number(process.env.PORT) || 8080

const app = express()
app.use(express.json({ limit: '2mb' }))
app.use(
  cors({
    origin: [
      'http://localhost:8080',
      'http://127.0.0.1:8080',
      'http://localhost:5173',
      'http://127.0.0.1:5173',
    ],
    methods: ['GET', 'PUT', 'POST', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Authorization', 'Content-Type'],
  }),
)

// Raised by handlers to return a specific HTTP status + message.
class HttpError extends Error {
  constructor(status, detail) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

// Wrap an async route handler so thrown errors reach the error middleware.
const wrap = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next)

// Pull the forwarded ARM bearer token off the request, or 401.
function requireToken(req) {
  const header = String(req.get('authorization') ?? '')
  const match = /^Bearer\s+(.+)$/i.exec(header)
  if (!match) throw new HttpError(401, 'Missing or malformed Authorization header.')
  return match[1].trim()
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

app.get(
  '/api/health',
  wrap(async (_req, res) => {
    res.json({ status: 'ok' })
  }),
)

// ---------------------------------------------------------------------------
// Auth config (public) — MSAL bootstrap values for the SPA.
// ---------------------------------------------------------------------------

// Local-dev default: Polaris's already-tenant-consented app (works without admin
// consent). Deploys set MSAL_CLIENT_ID to the owned "Serverless Portal" app.
const MSAL_CLIENT_ID = process.env.MSAL_CLIENT_ID || '409cf302-c83f-43c3-94eb-ca581ab18c6d'
const MSAL_AUTHORITY =
  process.env.MSAL_AUTHORITY || 'https://login.microsoftonline.com/organizations'

app.get('/api/auth/config', (_req, res) => {
  res.json({
    authenticationEnabled: true,
    msalClientId: MSAL_CLIENT_ID,
    msalAuthority: MSAL_AUTHORITY,
  })
})

// ---------------------------------------------------------------------------
// Azure (live discovery). Every route below requires a forwarded ARM token.
// ---------------------------------------------------------------------------

// Signed-in identity + the default subscription to scan.
app.get(
  '/api/identity',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const user = azure.getSignedInIdentity(token)
    const subscriptionName = await azure.getSubscriptionName(token, azure.DEFAULT_SUBSCRIPTION_ID)
    res.json({
      user,
      subscription: { id: azure.DEFAULT_SUBSCRIPTION_ID, name: subscriptionName },
    })
  }),
)

// List subscriptions the signed-in identity can see (for the top-bar picker).
app.get(
  '/api/subscriptions',
  wrap(async (req, res) => {
    const token = requireToken(req)
    res.json(await azure.listSubscriptions(token))
  }),
)

// Discover agent apps + their agents. Defaults to the hardcoded subscription;
// a `subscription` id/name override drives the top-bar picker.
app.get(
  '/api/live/agents',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const ref = String(req.query.subscription ?? '').trim()
    let subscriptionId = azure.DEFAULT_SUBSCRIPTION_ID
    if (ref) {
      try {
        subscriptionId = await azure.resolveSubscriptionId(token, ref)
      } catch (err) {
        if (err instanceof azure.SubscriptionNotFoundError) {
          throw new HttpError(404, err.message)
        }
        throw err
      }
    }
    const result = await azure.discoverAgentApps(token, subscriptionId)
    // Flatten to an agent list the UI can render directly, keeping app context.
    const agents = result.apps.flatMap((a) =>
      a.agents.map((ag) => ({
        name: ag.name,
        app: a.name,
        resourceGroup: a.resourceGroup,
        region: a.location,
        provider: a.provider,
        trigger: ag.trigger,
        builtinEndpoints: ag.builtinEndpoints,
        routes: ag.routes ?? [],
        supportingFunctions: ag.supportingFunctions ?? [],
        defaultHostName: a.defaultHostName,
      })),
    )
    res.json({ subscriptionId, apps: result.apps, agents })
  }),
)

// ---------------------------------------------------------------------------
// Agent definition — read the deployed `*.agent.md` (or the portal draft) and
// save edits to a portal-side working copy. Publishing a draft to the live app
// is a separate, not-yet-wired step.
// ---------------------------------------------------------------------------

const DRAFTS_DIR = path.join(__dirname, '..', '.data', 'agent-drafts')

// Keep path segments to safe characters so query params can't traverse the FS.
const safeSegment = (value) => String(value ?? '').replace(/[^a-zA-Z0-9._-]/g, '_')

function draftPath(subscription, appName, name) {
  return path.join(
    DRAFTS_DIR,
    safeSegment(subscription),
    safeSegment(appName),
    `${safeSegment(name)}.agent.md`,
  )
}

async function readDraft(subscription, appName, name) {
  try {
    return await fs.promises.readFile(draftPath(subscription, appName, name), 'utf-8')
  } catch {
    return null
  }
}

async function writeDraft(subscription, appName, name, content) {
  const filePath = draftPath(subscription, appName, name)
  await fs.promises.mkdir(path.dirname(filePath), { recursive: true })
  await fs.promises.writeFile(filePath, content, 'utf-8')
}

// Read the optional storage-scoped AAD token the browser forwards so the backend
// can open a Flex app's deployment package with the caller's identity when the
// storage account has shared-key access disabled.
function storageTokenFrom(req) {
  const h = req.get('X-Storage-Token')
  return typeof h === 'string' && h.trim() ? h.trim() : null
}

// Parse `owner/repo` out of a GitHub repo URL (https or ssh, with/without .git).
function parseGitHubOwnerRepo(repoUrl) {
  const m = /github\.com[/:]([^/]+)\/([^/]+?)(?:\.git)?\/?$/i.exec(String(repoUrl || ''))
  return m ? { owner: m[1], repo: m[2] } : null
}

// Fallback source read from the app's connected GitHub repo: resolves the app's
// repo link + the caller's stored GitHub token, then tries each candidate path
// until one resolves. Returns content or null (never throws). Independent of
// storage/Kudu, so it works even when the deployment package can't be read.
async function readRepoFileForApp(token, subscription, resourceGroup, appName, candidatePaths) {
  if (!resourceGroup) return null
  let oid = ''
  try {
    oid = azure.getSignedInIdentity(token).oid
  } catch {
    return null
  }
  const entry = oid ? github.tokenStore.get(oid) : null
  if (!entry?.token) return null
  let link = null
  try {
    link = await azure.getAppGithubLink(token, subscription, resourceGroup, appName)
  } catch {
    return null
  }
  const parsed = link?.repoUrl ? parseGitHubOwnerRepo(link.repoUrl) : null
  if (!parsed) return null
  const branch = link.branch || 'main'
  for (const filePath of candidatePaths) {
    const content = await github.readRepoFile(entry.token, parsed.owner, parsed.repo, filePath, branch)
    if (content != null) return content
  }
  return null
}

// Read an agent's definition: the portal draft if one exists, else the deployed
// `*.agent.md` source. Requires ?subscription, ?resourceGroup, ?app, ?name.
app.get(
  '/api/agents/definition',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.query.app ?? '').trim()
    const resourceGroup = String(req.query.resourceGroup ?? '').trim()
    const name = String(req.query.name ?? '').trim()
    if (!appName || !name) throw new HttpError(400, 'app and name query parameters are required.')

    const storageToken = storageTokenFrom(req)
    const draftContent = await readDraft(subscription, appName, name)
    let deployedContent = null
    if (resourceGroup) {
      const site = await azure.getSite(token, subscription, resourceGroup, appName)
      if (site) deployedContent = await azure.readAgentDefinition(token, subscription, site, name, storageToken)
    }
    // Fallback: read the source straight from the app's connected GitHub repo.
    if (deployedContent == null) {
      deployedContent = await readRepoFileForApp(token, subscription, resourceGroup, appName, [
        `${name}.agent.md`,
        `src/${name}.agent.md`,
        `agents/${name}.agent.md`,
        `src/agents/${name}.agent.md`,
      ])
    }
    res.json({
      name,
      app: appName,
      draftContent,
      deployedContent,
      content: draftContent ?? deployedContent ?? '',
      source: draftContent != null ? 'draft' : deployedContent != null ? 'deployed' : 'none',
    })
  }),
)

// Save an agent definition draft (portal-side working copy).
app.put(
  '/api/agents/definition',
  wrap(async (req, res) => {
    requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.query.app ?? '').trim()
    const name = String(req.query.name ?? '').trim()
    const content = req.body?.content
    if (!appName || !name) throw new HttpError(400, 'app and name query parameters are required.')
    if (typeof content !== 'string') throw new HttpError(400, 'Request body must be { content: string }.')
    await writeDraft(subscription, appName, name, content)
    res.json({ ok: true, source: 'draft' })
  }),
)

// ---------------------------------------------------------------------------
// Source files — read a deployed source file (e.g. function_app.py, where the
// supporting functions live) or the portal draft, and save edits to a working
// copy. Same draft model as agent definitions; publishing is a separate step.
// ---------------------------------------------------------------------------

const SOURCE_DRAFTS_DIR = path.join(__dirname, '..', '.data', 'source-drafts')

function sourceDraftPath(subscription, appName, relPath) {
  // Preserve nested structure (e.g. tools/x.py, skills/y/SKILL.md) so the draft
  // round-trips to the right path — safeSegment each segment, keep the slashes.
  const safeRel = String(relPath)
    .split('/')
    .map(safeSegment)
    .join(path.sep)
  return path.join(SOURCE_DRAFTS_DIR, safeSegment(subscription), safeSegment(appName), safeRel)
}

async function readSourceDraft(subscription, appName, relPath) {
  try {
    return await fs.promises.readFile(sourceDraftPath(subscription, appName, relPath), 'utf-8')
  } catch {
    return null
  }
}

async function writeSourceDraft(subscription, appName, relPath, content) {
  const filePath = sourceDraftPath(subscription, appName, relPath)
  await fs.promises.mkdir(path.dirname(filePath), { recursive: true })
  await fs.promises.writeFile(filePath, content, 'utf-8')
}

// Read a source file's content: the portal draft if one exists, else the
// deployed file. Requires ?subscription, ?resourceGroup, ?app, ?path.
app.get(
  '/api/source',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.query.app ?? '').trim()
    const resourceGroup = String(req.query.resourceGroup ?? '').trim()
    const relPath = String(req.query.path ?? '').trim()
    if (!appName || !relPath) throw new HttpError(400, 'app and path query parameters are required.')
    if (relPath.includes('..')) throw new HttpError(400, 'Invalid path.')

    const storageToken = storageTokenFrom(req)
    const draftContent = await readSourceDraft(subscription, appName, relPath)
    let deployedContent = null
    if (resourceGroup) {
      const site = await azure.getSite(token, subscription, resourceGroup, appName)
      if (site) deployedContent = await azure.readSourceFile(token, subscription, site, relPath, storageToken)
    }
    // Fallback: read the file straight from the app's connected GitHub repo.
    if (deployedContent == null) {
      deployedContent = await readRepoFileForApp(token, subscription, resourceGroup, appName, [
        relPath,
        `src/${relPath}`,
      ])
    }
    res.json({
      path: relPath,
      app: appName,
      draftContent,
      deployedContent,
      content: draftContent ?? deployedContent ?? '',
      source: draftContent != null ? 'draft' : deployedContent != null ? 'deployed' : 'none',
    })
  }),
)

// Save a source-file draft (portal-side working copy).
app.put(
  '/api/source',
  wrap(async (req, res) => {
    requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.query.app ?? '').trim()
    const relPath = String(req.query.path ?? '').trim()
    const content = req.body?.content
    if (!appName || !relPath) throw new HttpError(400, 'app and path query parameters are required.')
    if (relPath.includes('..')) throw new HttpError(400, 'Invalid path.')
    if (typeof content !== 'string') throw new HttpError(400, 'Request body must be { content: string }.')
    await writeSourceDraft(subscription, appName, relPath, content)
    res.json({ ok: true, source: 'draft' })
  }),
)

// Delete a source-file draft (portal-side working copy). Used by the file tree
// so the user can revert a draft they no longer want to publish. Does NOT touch
// the deployed source — that only changes on the next "Deploy edits".
app.delete(
  '/api/source',
  wrap(async (req, res) => {
    requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.query.app ?? '').trim()
    const relPath = String(req.query.path ?? '').trim()
    if (!appName || !relPath) throw new HttpError(400, 'app and path query parameters are required.')
    if (relPath.includes('..')) throw new HttpError(400, 'Invalid path.')
    const filePath = sourceDraftPath(subscription, appName, relPath)
    let removed = false
    try {
      await fs.promises.unlink(filePath)
      removed = true
    } catch {
      /* already gone or never existed */
    }
    res.json({ ok: true, removed })
  }),
)

// List every source file for this app so the portal can render a real file
// tree. Merges (a) the deployed package (Kudu VFS or the Flex zip's central
// directory) with (b) any local drafts, tagging each entry so the UI can show
// which files have unpublished edits. Requires ?subscription, ?resourceGroup,
// ?app.
app.get(
  '/api/source/list',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.query.app ?? '').trim()
    const resourceGroup = String(req.query.resourceGroup ?? '').trim()
    if (!appName) throw new HttpError(400, 'app query parameter is required.')

    const storageToken = storageTokenFrom(req)
    const byPath = new Map()
    const upsert = (p, patch) => {
      const cur = byPath.get(p) ?? { path: p, size: 0, deployed: false, draft: false }
      byPath.set(p, { ...cur, ...patch })
    }

    // Deployed files — best effort; empty when the caller can't read source.
    if (resourceGroup) {
      const site = await azure.getSite(token, subscription, resourceGroup, appName)
      if (site) {
        try {
          const deployed = await azure.listSourceFiles(token, subscription, site, storageToken)
          for (const f of deployed ?? []) upsert(f.path, { size: f.size ?? 0, deployed: true })
        } catch {
          /* leave the deployed set empty */
        }
      }
    }

    // Local drafts (portal-side working copies).
    const sourceDir = path.join(SOURCE_DRAFTS_DIR, safeSegment(subscription), safeSegment(appName))
    for (const f of await readDirRecursive(sourceDir)) {
      upsert(f.name, { size: f.data.length, draft: true })
    }
    // Also treat a *.agent.md draft under the agent-drafts directory as a
    // top-level draft (that's where the DraftEditor for `.agent.md` files
    // writes). Draft names carry the `.agent.md` suffix directly.
    const agentDir = path.join(DRAFTS_DIR, safeSegment(subscription), safeSegment(appName))
    for (const name of await listDirFiles(agentDir)) {
      if (!/\.agent\.md$/i.test(name)) continue
      upsert(name, { draft: true })
    }

    const files = [...byPath.values()]
      .map((f) => ({
        path: f.path,
        size: f.size,
        source: f.draft && f.deployed ? 'both' : f.draft ? 'draft' : 'deployed',
      }))
      .sort((a, b) => a.path.localeCompare(b.path))
    res.json({ app: appName, files })
  }),
)

// ---------------------------------------------------------------------------
// Session history — enumerate and read the blob-backed sessions the runtime
// persists to the app's `AzureWebJobsStorage` account (default container
// `azure-functions-agents`, prefix `agent-sessions/`). The runtime does not
// expose an HTTP list endpoint, so the portal reads storage directly using
// the caller's ARM/storage tokens.
// ---------------------------------------------------------------------------

app.get(
  '/api/sessions',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.query.app ?? '').trim()
    const resourceGroup = String(req.query.resourceGroup ?? '').trim()
    if (!appName || !resourceGroup) throw new HttpError(400, 'app and resourceGroup are required.')
    const site = await azure.getSite(token, subscription, resourceGroup, appName)
    if (!site) throw new HttpError(404, `Function App "${appName}" was not found.`)
    const storageToken = storageTokenFrom(req)
    const sessions = await azure.listSessions(token, subscription, site, storageToken)
    if (sessions == null) {
      res.json({ app: appName, sessions: [], readable: false })
      return
    }
    res.json({ app: appName, sessions, readable: true })
  }),
)

app.get(
  '/api/sessions/:sessionId',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.query.app ?? '').trim()
    const resourceGroup = String(req.query.resourceGroup ?? '').trim()
    const sessionId = String(req.params.sessionId ?? '').trim()
    if (!appName || !resourceGroup) throw new HttpError(400, 'app and resourceGroup are required.')
    if (!/^[A-Za-z0-9._-]{1,128}$/.test(sessionId)) throw new HttpError(400, 'Invalid session id.')
    const site = await azure.getSite(token, subscription, resourceGroup, appName)
    if (!site) throw new HttpError(404, `Function App "${appName}" was not found.`)
    const storageToken = storageTokenFrom(req)
    const messages = await azure.readSession(token, subscription, site, sessionId, storageToken)
    if (messages == null) throw new HttpError(404, 'Session not readable (missing, no perms, or storage unavailable).')
    res.json({ app: appName, sessionId, messages })
  }),
)

// ---------------------------------------------------------------------------
// App Insights KQL — the portal runs a small, curated set of queries against
// the app's linked App Insights component (invocations, p95 latency, error
// count, recent failures). The client picks the query by `preset` name; ad-hoc
// KQL is only accepted when the caller passes an explicit query string.
// ---------------------------------------------------------------------------

const INSIGHTS_PRESETS = {
  // High-level per-app metrics for the monitor tab.
  summary: (timeRange) => `
requests
| where timestamp >= ago(${timeRange})
| where cloud_RoleName == "@APP@"
| summarize invocations = count(),
    failures = countif(success == false),
    p95_ms = percentile(duration, 95),
    avg_ms = avg(duration)
`,
  // Bucketed invocation counts for a sparkline.
  timeline: (timeRange) => `
requests
| where timestamp >= ago(${timeRange})
| where cloud_RoleName == "@APP@"
| summarize count() by bin(timestamp, 15m), success
| order by timestamp asc
`,
  // Per-agent breakdown: operation_Name is the Azure Functions function name;
  // the runtime registers agents with predictable names so we group on those.
  agents: (timeRange) => `
requests
| where timestamp >= ago(${timeRange})
| where cloud_RoleName == "@APP@"
| summarize invocations = count(),
    failures = countif(success == false),
    p95_ms = percentile(duration, 95)
    by operation_Name
| order by invocations desc
`,
  // Recent failed invocations for the "failures → open trace" list.
  recentFailures: (timeRange) => `
requests
| where timestamp >= ago(${timeRange})
| where cloud_RoleName == "@APP@" and success == false
| project timestamp, name, operation_Id, resultCode, duration, session = tostring(customDimensions["af.agent.session_id"])
| order by timestamp desc
| take 25
`,
}

// Sanitise a KQL string identifier — used to inline the app name into a
// preset query. Rejects anything with a quote or backslash.
function safeKqlString(value) {
  const s = String(value ?? '')
  if (/["\\]/.test(s)) return ''
  return s
}

app.post(
  '/api/app-insights/query',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.body?.app ?? '').trim()
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const preset = String(req.body?.preset ?? '').trim()
    const timeRange = String(req.body?.timeRange ?? '24h').trim()
    if (!appName || !resourceGroup) throw new HttpError(400, 'app and resourceGroup are required.')
    if (!/^(?:\d+)(?:m|h|d)$/.test(timeRange)) throw new HttpError(400, 'timeRange must look like 15m/24h/7d.')

    let query = ''
    if (preset) {
      const builder = INSIGHTS_PRESETS[preset]
      if (!builder) throw new HttpError(400, `Unknown preset "${preset}".`)
      const safeApp = safeKqlString(appName)
      if (!safeApp) throw new HttpError(400, 'App name contains disallowed characters.')
      query = builder(timeRange).replace(/@APP@/g, safeApp)
    } else if (typeof req.body?.query === 'string' && req.body.query.trim()) {
      query = String(req.body.query)
    } else {
      throw new HttpError(400, 'Provide either { preset } or { query }.')
    }

    const timespan = `P${timeRange.replace(/(\d+)(m|h|d)/, (_, n, u) => u === 'd' ? `${n}D` : u === 'h' ? `T${n}H` : `T${n}M`)}`
    const result = await azure.queryAppInsights(token, subscription, resourceGroup, appName, query, timespan)
    if (result.error === 'no-component') {
      throw new HttpError(404, 'This app has no linked Application Insights component.')
    }
    if (result.error) throw new HttpError(502, `App Insights query failed: ${result.error}`)
    res.json(result)
  }),
)

// ---------------------------------------------------------------------------
// Create / deploy agent — refresh the target Function App's portal-managed
// source tree, then provision (for a new app) and push it to Azure with a
// remote build. Every Azure call runs as the signed-in user's forwarded token.
// ---------------------------------------------------------------------------

const APP_SOURCES_DIR = path.join(__dirname, '..', '.data', 'app-sources')
const SCAFFOLD_DIR = path.join(__dirname, '..', 'scaffold')

// Portal's own authoring skills (a SKILL.md per capability kind), injected into
// generation prompts so output follows the runtime conventions. Editable under
// app/server/skills/; read fresh per request (no restart needed).
const PORTAL_SKILLS_DIR = path.join(__dirname, '..', 'skills')
const KIND_TO_PORTAL_SKILL = {
  http_trigger: 'authoring-triggers',
  connector_trigger: 'authoring-connector-trigger',
  timer_trigger: 'authoring-timer-trigger',
  custom_tool: 'authoring-custom-tools',
  skill: 'authoring-skills',
}
async function readPortalSkill(kind) {
  const slug = KIND_TO_PORTAL_SKILL[kind]
  if (!slug) return ''
  try {
    return await fs.promises.readFile(path.join(PORTAL_SKILLS_DIR, slug, 'SKILL.md'), 'utf-8')
  } catch {
    return ''
  }
}

const SCAFFOLD = {
  'function_app.py': 'from azure_functions_agents import create_function_app\n\napp = create_function_app()\n',
  'host.json':
    JSON.stringify(
      {
        version: '2.0',
        extensions: { http: { routePrefix: '' } },
        logging: { logLevel: { default: 'Information' } },
        extensionBundle: { id: 'Microsoft.Azure.Functions.ExtensionBundle', version: '[4.*, 5.0.0)' },
      },
      null,
      2,
    ) + '\n',
  'requirements.txt': 'azurefunctions-agents-runtime[monitor]\n',
  'agents.config.yaml': '# Global defaults for all agents in this app.\n',
}

async function scaffoldIfMissing(filePath, content) {
  try {
    await fs.promises.access(filePath)
  } catch {
    await fs.promises.writeFile(filePath, content, 'utf-8')
  }
}

// In-memory deploy jobs (single-instance portal). Provisioning + remote build
// can take minutes, so the client starts a job and polls for its status rather
// than holding one long request. Jobs are ephemeral and pruned after a while.
const deployJobs = new Map()
const DEPLOY_JOB_TTL_MS = 30 * 60 * 1000

function setJob(id, patch) {
  deployJobs.set(id, { ...(deployJobs.get(id) ?? {}), ...patch, updatedAt: Date.now() })
}

function pruneJobs() {
  const cutoff = Date.now() - DEPLOY_JOB_TTL_MS
  for (const [id, job] of deployJobs) if ((job.updatedAt ?? 0) < cutoff) deployJobs.delete(id)
}

// Azure portal deep links, so the user can watch a deployment in the portal
// itself instead of waiting on the wizard. Tenant is optional but targets the
// right directory when present.
const portalRoot = (tenantId) => `https://portal.azure.com/#${tenantId ? `@${tenantId}` : ''}`
function portalDeploymentUrl(tenantId, subscription, resourceGroup, deploymentName) {
  return `${portalRoot(tenantId)}/resource/subscriptions/${subscription}/resourceGroups/${resourceGroup}/providers/Microsoft.Resources/deployments/${deploymentName}/overview`
}
function portalAppDeploymentCenterUrl(tenantId, subscription, resourceGroup, appName) {
  return `${portalRoot(tenantId)}/resource/subscriptions/${subscription}/resourceGroups/${resourceGroup}/providers/Microsoft.Web/sites/${appName}/vstscd`
}
function portalResourceUrl(tenantId, subscription, resourceGroup, provider, name) {
  return `${portalRoot(tenantId)}/resource/subscriptions/${subscription}/resourceGroups/${resourceGroup}/providers/${provider}/${name}/overview`
}

// Best-effort tenant id from the forwarded token, for portal links.
function tenantFromToken(token) {
  try {
    return azure.getSignedInIdentity(token).tenantId || ''
  } catch {
    return ''
  }
}

const pathExists = async (p) => {
  try {
    await fs.promises.access(p)
    return true
  } catch {
    return false
  }
}

const listDirFiles = async (dir) => {
  try {
    return await fs.promises.readdir(dir)
  } catch {
    return []
  }
}

// Read a directory of files into `[{ name, data }]`.
async function readDirFiles(dir) {
  const names = await listDirFiles(dir)
  return Promise.all(
    names.sort().map(async (name) => ({ name, data: await fs.promises.readFile(path.join(dir, name)) })),
  )
}

// Read a directory tree into `[{ name, data }]` with forward-slash relative paths.
async function readDirRecursive(dir, base = dir) {
  const out = []
  let entries
  try {
    entries = await fs.promises.readdir(dir, { withFileTypes: true })
  } catch {
    return out
  }
  for (const e of entries) {
    const full = path.join(dir, e.name)
    if (e.isDirectory()) out.push(...(await readDirRecursive(full, base)))
    else
      out.push({
        name: path.relative(base, full).split(path.sep).join('/'),
        data: await fs.promises.readFile(full),
      })
  }
  return out
}

const REPO_GITIGNORE = `.venv/
__pycache__/
*.pyc
.python_packages/
local.settings.json
.azure/
.DS_Store
`

// Assemble a complete, azd-deployable repo from the app's function files: the
// bundled scaffold (azure.yaml, infra/**, README, src templates) at the root,
// the function app under src/, and a .gitignore. azure.yaml's project name is
// set to the app so `azd up` works from a clone.
async function buildRepoFiles(appFiles, appName) {
  const files = new Map()
  for (const f of await readDirRecursive(SCAFFOLD_DIR)) {
    let data = f.data
    if (f.name === 'azure.yaml') {
      data = Buffer.from(f.data.toString('utf-8').replace(/^name:.*$/m, `name: ${appName}`), 'utf-8')
    }
    files.set(f.name, data)
  }
  for (const f of appFiles) files.set(`src/${f.name}`, f.data)
  files.set('.gitignore', Buffer.from(REPO_GITIGNORE, 'utf-8'))
  return [...files].map(([name, data]) => ({ name, data }))
}

// Overlay this app's saved portal drafts (edited `*.agent.md` and source files)
// onto its base source files, matching by name/basename so an edit replaces the
// deployed file in place rather than duplicating it.
async function overlayDrafts(subscription, appName, baseFiles) {
  const byName = new Map(baseFiles.map((f) => [f.name, f.data]))
  const basenameToName = new Map(baseFiles.map((f) => [f.name.split('/').pop(), f.name]))
  const apply = (fileName, content) => {
    const target = byName.has(fileName) ? fileName : (basenameToName.get(fileName) ?? fileName)
    byName.set(target, Buffer.from(content, 'utf-8'))
  }

  const agentDir = path.join(DRAFTS_DIR, safeSegment(subscription), safeSegment(appName))
  for (const file of await listDirFiles(agentDir)) {
    apply(file, await fs.promises.readFile(path.join(agentDir, file), 'utf-8'))
  }
  const sourceDir = path.join(SOURCE_DRAFTS_DIR, safeSegment(subscription), safeSegment(appName))
  for (const f of await readDirRecursive(sourceDir)) {
    apply(f.name, f.data.toString('utf-8'))
  }
  return [...byName].map(([name, data]) => ({ name, data }))
}

// Gather this app's SKILL.md files (portal source drafts + the working copy) as
// {name, content}, so generation can be grounded in the app's existing skills.
async function gatherAppSkills(subscription, appName) {
  const out = []
  const seen = new Set()
  const add = (name, buf) => {
    if (/(^|\/)skills\/[^/]+\/SKILL\.md$/i.test(name) && !seen.has(name)) {
      seen.add(name)
      out.push({ name, content: buf.toString('utf-8') })
    }
  }
  const sourceDir = path.join(SOURCE_DRAFTS_DIR, safeSegment(subscription), safeSegment(appName))
  for (const f of await readDirRecursive(sourceDir)) add(f.name, f.data)
  const srcDir = path.join(APP_SOURCES_DIR, safeSegment(subscription), safeSegment(appName))
  for (const f of await readDirRecursive(srcDir)) add(f.name, f.data)
  return out
}

// Zip the prepared files and push them to the app with a remote build.
async function pushFilesToSite(id, token, site, files) {
  setJob(id, { message: 'Deploying source with a remote build…' })
  const zip = provision.zipStore(files)
  await provision.deployZipToApp(token, azure.scmHostName(site), zip)
}

// Provision (for a new app) then push the source with a remote build, updating
// the job as each stage completes. Runs detached from the HTTP request.
async function runDeployJob(id, token, ctx) {
  const { subscription, resourceGroup, appName, target, dir, deploymentName, fileName, tenantId } = ctx
  try {
    if (target.kind === 'new') {
      setJob(id, { message: 'Provisioning Azure resources…' })
      const provisioned = await provision.provisionFlexApp(token, {
        subscriptionId: subscription,
        resourceGroup,
        appName,
        region: target.region,
        foundryEndpoint: target.foundryEndpoint,
        foundryModel: target.foundryModel,
        deploymentName,
      })
      if (provisioned?.appInsightsName) {
        setJob(id, {
          insightsUrl: portalResourceUrl(
            tenantId,
            subscription,
            resourceGroup,
            'Microsoft.Insights/components',
            provisioned.appInsightsName,
          ),
        })
      }
    }

    setJob(id, { message: 'Resolving Function App…' })
    const site = await azure.getSite(token, subscription, resourceGroup, appName)
    if (!site) throw new Error(`Function App "${appName}" was not found in "${resourceGroup}".`)
    const principalId = site.identity?.principalId || ''

    // Auto-grant the new app's identity access to the Foundry account, so a
    // portal-created agent can call its model without a manual RBAC step. Done
    // before the (slow) source build so role propagation overlaps with it.
    // Best-effort: if the caller lacks roleAssignments/write the client falls
    // back to the manual "Grant access" control.
    let grantOutcome
    const fa = target.kind === 'new' ? target.foundryAccount : null
    if (principalId && fa && fa.subscription && fa.resourceGroup && fa.account) {
      setJob(id, { message: 'Granting the app access to Foundry…' })
      try {
        const r = await azure.grantFoundryAccess(token, {
          subscriptionId: fa.subscription,
          resourceGroup: fa.resourceGroup,
          account: fa.account,
          principalId,
        })
        grantOutcome = r.granted?.length ? (r.failed?.length ? 'partial' : 'granted') : 'failed'
      } catch {
        grantOutcome = 'failed'
      }
      setJob(id, { grantOutcome })
    }

    // Overlay any capabilities the user added as drafts (mcp.json, tools/,
    // skills/, and edits to the agent's .agent.md) so a portal-created app ships
    // with them, not just the bare generated agent.
    const files = await overlayDrafts(subscription, appName, await readDirFiles(dir))
    setJob(id, { files: files.map((f) => f.name).sort() })
    await pushFilesToSite(id, token, site, files)

    setJob(id, {
      status: 'deployed',
      message: `Deployed "${fileName}" to ${appName}.`,
      url: `https://${site.defaultHostName}`,
      ...(target.kind === 'new' && principalId ? { principalId } : {}),
      ...(grantOutcome ? { grantOutcome } : {}),
    })
  } catch (err) {
    setJob(id, { status: 'error', message: String(err?.message ?? err) })
  }
}

// Redeploy an existing app from its own current source with the portal's saved
// drafts overlaid, so a multi-agent app isn't reduced to a scaffold.
async function runRedeployJob(id, token, ctx) {
  const { subscription, resourceGroup, appName } = ctx
  try {
    setJob(id, { message: 'Resolving Function App…' })
    const site = await azure.getSite(token, subscription, resourceGroup, appName)
    if (!site) throw new Error(`Function App "${appName}" was not found in "${resourceGroup}".`)

    setJob(id, { message: 'Reading current app source…' })
    const appSrcDir = path.join(APP_SOURCES_DIR, safeSegment(subscription), safeSegment(appName))
    let base = (await pathExists(appSrcDir)) ? await readDirFiles(appSrcDir) : null
    if (!base || base.length === 0) base = await azure.readPackageFiles(token, subscription, site)
    if (!base || base.length === 0) {
      throw new Error("Couldn't read the app's current source to redeploy (permission or plan).")
    }

    const files = await overlayDrafts(subscription, appName, base)
    setJob(id, { files: files.map((f) => f.name).sort() })
    await pushFilesToSite(id, token, site, files)

    setJob(id, {
      status: 'deployed',
      message: `Redeployed ${appName} with your saved edits.`,
      url: `https://${site.defaultHostName}`,
    })
  } catch (err) {
    setJob(id, { status: 'error', message: String(err?.message ?? err) })
  }
}

// Start a deploy: refresh the app's source tree, then run provisioning + deploy
// in the background. Returns a job id the client polls via GET /api/deploy/:id.
app.post(
  '/api/deploy',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const agent = req.body?.agent
    const target = req.body?.target
    if (!agent || typeof agent.fileName !== 'string' || typeof agent.content !== 'string') {
      throw new HttpError(400, 'Request body must include agent { fileName, content }.')
    }
    if (!target || typeof target.kind !== 'string') {
      throw new HttpError(400, 'Request body must include a target.')
    }
    const appName = target.kind === 'existing' ? target.app : target.appName
    if (!appName) throw new HttpError(400, 'A target Function App name is required.')
    const resourceGroup = target.resourceGroup
    if (!resourceGroup) throw new HttpError(400, 'A target resource group is required.')
    if (target.kind === 'new' && !target.region) {
      throw new HttpError(400, 'A region is required to create a new app.')
    }
    const fileName = safeSegment(agent.fileName)
    if (!/\.agent\.md$/i.test(fileName)) throw new HttpError(400, 'Agent file must end with .agent.md.')

    // Portal-managed source tree (kept so portal-created apps redeploy from source).
    const dir = path.join(APP_SOURCES_DIR, safeSegment(subscription), safeSegment(appName))
    await fs.promises.mkdir(dir, { recursive: true })
    for (const [name, content] of Object.entries(SCAFFOLD)) {
      await scaffoldIfMissing(path.join(dir, name), content)
    }
    await fs.promises.writeFile(path.join(dir, fileName), agent.content, 'utf-8')
    const files = (await fs.promises.readdir(dir)).sort()

    pruneJobs()
    const jobId = randomUUID()
    const tenantId = tenantFromToken(token)
    const deploymentName = `portal-deploy-${Date.now()}`.slice(0, 64)
    const portalUrl =
      target.kind === 'new'
        ? portalDeploymentUrl(tenantId, subscription, resourceGroup, deploymentName)
        : portalAppDeploymentCenterUrl(tenantId, subscription, resourceGroup, appName)
    setJob(jobId, { status: 'running', message: 'Starting…', files, url: null, portalUrl })
    runDeployJob(jobId, token, { subscription, resourceGroup, appName, target, dir, deploymentName, fileName, tenantId })

    res.status(202).json({ jobId, status: 'running', files, portalUrl })
  }),
)

// Poll a deploy job.
app.get(
  '/api/deploy/:jobId',
  wrap(async (req, res) => {
    requireToken(req)
    const job = deployJobs.get(req.params.jobId)
    if (!job) throw new HttpError(404, 'Unknown or expired deploy job.')
    res.json({
      status: job.status,
      message: job.message,
      files: job.files ?? [],
      ...(job.url ? { url: job.url } : {}),
      ...(job.portalUrl ? { portalUrl: job.portalUrl } : {}),
      ...(job.principalId ? { principalId: job.principalId } : {}),
      ...(job.insightsUrl ? { insightsUrl: job.insightsUrl } : {}),
      ...(job.grantOutcome ? { grantOutcome: job.grantOutcome } : {}),
    })
  }),
)

// Redeploy an existing app with the portal's saved edits overlaid onto its own
// current source. Safe for multi-agent apps; returns a job id to poll.
app.post(
  '/api/redeploy',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.body?.app ?? '').trim()
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    if (!appName || !resourceGroup) throw new HttpError(400, 'app and resourceGroup are required.')

    pruneJobs()
    const jobId = randomUUID()
    const portalUrl = portalAppDeploymentCenterUrl(tenantFromToken(token), subscription, resourceGroup, appName)
    setJob(jobId, { status: 'running', message: 'Starting…', files: [], url: null, portalUrl })
    runRedeployJob(jobId, token, { subscription, resourceGroup, appName })

    res.status(202).json({ jobId, status: 'running', files: [], portalUrl })
  }),
)

// ---------------------------------------------------------------------------
// Agent playground — chat with a deployed agent's built-in endpoint, proxied so
// the browser needs no function key and makes no cross-origin call.
// ---------------------------------------------------------------------------

app.post(
  '/api/agent/chat',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.body?.app ?? '').trim()
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const agentName = String(req.body?.agent ?? '').trim()
    const prompt = req.body?.prompt
    const sessionId = String(req.body?.sessionId ?? '').trim()
    if (!appName || !resourceGroup || !agentName) {
      throw new HttpError(400, 'app, resourceGroup, and agent are required.')
    }
    if (typeof prompt !== 'string' || !prompt.trim()) {
      throw new HttpError(400, 'A non-empty prompt is required.')
    }

    const site = await azure.getSite(token, subscription, resourceGroup, appName)
    if (!site) throw new HttpError(404, `Function App "${appName}" was not found.`)
    if (!site.defaultHostName) throw new HttpError(502, 'The app has no host name.')

    const key = await azure.functionHostKey(token, subscription, resourceGroup, appName)
    try {
      res.json(await azure.callAgentChat(site.defaultHostName, agentName, prompt.trim(), { key, sessionId }))
    } catch (err) {
      throw new HttpError(err.status ?? 502, String(err?.message ?? err))
    }
  }),
)

// Stream a chat with a deployed agent (SSE), proxied so the browser needs no
// key and makes no cross-origin call. Emits the runtime's own event vocabulary
// (session/delta/tool_start/tool_end/done/error) unchanged.
app.post(
  '/api/agent/chatstream',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.body?.app ?? '').trim()
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const agentName = String(req.body?.agent ?? '').trim()
    const prompt = req.body?.prompt
    const sessionId = String(req.body?.sessionId ?? '').trim()
    if (!appName || !resourceGroup || !agentName) {
      throw new HttpError(400, 'app, resourceGroup, and agent are required.')
    }
    if (typeof prompt !== 'string' || !prompt.trim()) {
      throw new HttpError(400, 'A non-empty prompt is required.')
    }

    const site = await azure.getSite(token, subscription, resourceGroup, appName)
    if (!site) throw new HttpError(404, `Function App "${appName}" was not found.`)
    if (!site.defaultHostName) throw new HttpError(502, 'The app has no host name.')
    const key = await azure.functionHostKey(token, subscription, resourceGroup, appName)

    const controller = new AbortController()
    req.on('close', () => controller.abort())

    // Opening the upstream may fail (bad route, upstream error) before any SSE
    // is sent — surface that as a normal JSON error the client checks first.
    let upstream
    try {
      upstream = await azure.openAgentChatStream(site.defaultHostName, agentName, prompt.trim(), {
        key,
        sessionId,
        signal: controller.signal,
      })
    } catch (err) {
      throw new HttpError(err.status ?? 502, String(err?.message ?? err))
    }

    res.setHeader('Content-Type', 'text/event-stream')
    res.setHeader('Cache-Control', 'no-cache, no-transform')
    res.setHeader('Connection', 'keep-alive')
    res.setHeader('X-Accel-Buffering', 'no')
    res.flushHeaders?.()

    const reader = upstream.body.getReader()
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        res.write(Buffer.from(value))
      }
    } catch {
      if (!controller.signal.aborted) {
        res.write(`data: ${JSON.stringify({ type: 'error', content: 'Stream interrupted.' })}\n\n`)
      }
    } finally {
      res.end()
    }
  }),
)

// ---------------------------------------------------------------------------
// Microsoft Foundry — discover models for the create flow, and generate an
// agent's instructions with the chosen model.
// ---------------------------------------------------------------------------

app.get(
  '/api/foundry',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const ref = String(req.query.subscription ?? '').trim()
    let subscriptionId = azure.DEFAULT_SUBSCRIPTION_ID
    if (ref) {
      try {
        subscriptionId = await azure.resolveSubscriptionId(token, ref)
      } catch (err) {
        if (err instanceof azure.SubscriptionNotFoundError) throw new HttpError(404, err.message)
        throw err
      }
    }
    res.json(await azure.discoverFoundry(token, subscriptionId))
  }),
)

app.post(
  '/api/generate-agent-md',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const foundry = req.body?.foundry ?? {}
    const name = String(req.body?.name ?? '').trim()
    const description = String(req.body?.description ?? '').trim()
    if (!name && !description) throw new HttpError(400, 'Provide a name or description to generate from.')
    try {
      res.json(
        await azure.generateAgentInstructions(token, subscription, {
          resourceGroup: String(foundry.resourceGroup ?? ''),
          account: String(foundry.account ?? ''),
          openaiEndpoint: String(foundry.openaiEndpoint ?? ''),
          model: String(foundry.model ?? ''),
          name,
          description,
        }),
      )
    } catch (err) {
      throw new HttpError(err.status ?? 502, String(err?.message ?? err))
    }
  }),
)

// Read a portal authoring skill by folder slug (for the capability planner).
async function readPortalSkillFile(slug) {
  try {
    return await fs.promises.readFile(path.join(PORTAL_SKILLS_DIR, slug, 'SKILL.md'), 'utf-8')
  } catch {
    return ''
  }
}

// Plan the capabilities an agent needs from its description (skill-grounded).
app.post(
  '/api/plan-capabilities',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const description = String(req.body?.description ?? '').trim()
    const foundry = req.body?.foundry ?? {}
    if (!description) throw new HttpError(400, 'A description is required.')
    const guidance = await readPortalSkillFile('authoring-capability-planner')
    res.json(
      await azure.planCapabilities(token, subscription, {
        resourceGroup: String(foundry.resourceGroup ?? ''),
        account: String(foundry.account ?? ''),
        openaiEndpoint: String(foundry.openaiEndpoint ?? ''),
        model: String(foundry.model ?? ''),
        description,
        guidance,
      }),
    )
  }),
)

// Generate the code/config for an agent capability (HTTP trigger .agent.md,
// connector trigger .agent.md, or a custom Python tool) with a Foundry model.
app.post(
  '/api/generate-capability',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const foundry = req.body?.foundry ?? {}
    const kind = String(req.body?.kind ?? '').trim()
    const name = String(req.body?.name ?? '').trim()
    const description = String(req.body?.description ?? '').trim()
    const appName = String(req.body?.app ?? '').trim()
    const triggerType = String(req.body?.triggerType ?? '').trim()
    if (!['http_trigger', 'connector_trigger', 'timer_trigger', 'custom_tool', 'skill'].includes(kind)) {
      throw new HttpError(400, 'kind must be http_trigger, connector_trigger, timer_trigger, custom_tool, or skill.')
    }
    if (!description) throw new HttpError(400, 'A description is required to generate.')

    // Optionally ground the generation in the app's existing skills.
    let skillsContext = ''
    if (req.body?.groundInSkills && appName) {
      try {
        const skills = await gatherAppSkills(subscription, appName)
        skillsContext = skills
          .map((s) => `### skill: ${s.name}\n${s.content.slice(0, 1600)}`)
          .join('\n\n')
          .slice(0, 8000)
      } catch {
        /* best-effort grounding */
      }
    }

    // Ground generation in the portal's authoritative authoring skill for this kind.
    const guidance = await readPortalSkill(kind)

    try {
      res.json(
        await azure.generateCapabilityCode(token, subscription, {
          resourceGroup: String(foundry.resourceGroup ?? ''),
          account: String(foundry.account ?? ''),
          openaiEndpoint: String(foundry.openaiEndpoint ?? ''),
          model: String(foundry.model ?? ''),
          kind,
          name,
          description,
          triggerType,
          skillsContext,
          guidance,
        }),
      )
    } catch (err) {
      throw new HttpError(err.status ?? 502, String(err?.message ?? err))
    }
  }),
)

// Resource groups in a subscription, for the create flow's RG picker.
app.get(
  '/api/resource-groups',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const ref = String(req.query.subscription ?? '').trim()
    let subscriptionId = azure.DEFAULT_SUBSCRIPTION_ID
    if (ref) {
      try {
        subscriptionId = await azure.resolveSubscriptionId(token, ref)
      } catch (err) {
        if (err instanceof azure.SubscriptionNotFoundError) throw new HttpError(404, err.message)
        throw err
      }
    }
    res.json(await azure.listResourceGroups(token, subscriptionId))
  }),
)

// Check if a Function App name is globally available (for the deploy flow).
app.get(
  '/api/check-name',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const ref = String(req.query.subscription ?? '').trim()
    const name = String(req.query.name ?? '').trim()
    if (!name) throw new HttpError(400, 'A name is required.')
    let subscriptionId = azure.DEFAULT_SUBSCRIPTION_ID
    if (ref) {
      try {
        subscriptionId = await azure.resolveSubscriptionId(token, ref)
      } catch (err) {
        if (err instanceof azure.SubscriptionNotFoundError) throw new HttpError(404, err.message)
        throw err
      }
    }
    res.json(await azure.checkFunctionAppNameAvailable(token, subscriptionId, name))
  }),
)

// Grant a deployed app's identity access to a Foundry account (works cross-sub).
app.post(
  '/api/foundry/grant-access',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim()
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const account = String(req.body?.account ?? '').trim()
    const principalId = String(req.body?.principalId ?? '').trim()
    if (!subscription || !resourceGroup || !account || !principalId) {
      throw new HttpError(400, 'subscription, resourceGroup, account, and principalId are required.')
    }
    try {
      res.json(
        await azure.grantFoundryAccess(token, {
          subscriptionId: subscription,
          resourceGroup,
          account,
          principalId,
        }),
      )
    } catch (err) {
      throw new HttpError(err.status ?? 502, String(err?.message ?? err))
    }
  }),
)

// ---------------------------------------------------------------------------
// GitHub connection (Phase 1) — OAuth App sign-in, then create/connect a repo,
// push the app's source, and record the repo link on the Function App. The
// OAuth token is kept server-side keyed by the user's oid; the browser never
// sees it. See serverless-portal/app/server/src/github.js.
// ---------------------------------------------------------------------------

// Whether OAuth is configured on the server, and whether THIS user is connected.
app.get(
  '/api/github/status',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const { oid } = azure.getSignedInIdentity(token)
    const entry = github.tokenStore.get(oid)
    res.json({
      configured: github.isConfigured(),
      connected: Boolean(entry),
      ...(entry ? { login: entry.login, avatarUrl: entry.avatarUrl } : {}),
    })
  }),
)

// Whether a specific app already has a GitHub repo recorded on it (from the app
// settings written at connect time). Used by the create + detail flows to show
// the connected state instead of the connect form.
app.get(
  '/api/github/app-connection',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.query.resourceGroup ?? '').trim()
    const appName = String(req.query.app ?? '').trim()
    if (!resourceGroup || !appName) throw new HttpError(400, 'resourceGroup and app are required.')
    res.json(await azure.getAppGithubLink(token, subscription, resourceGroup, appName))
  }),
)

// Mint the GitHub authorize URL bound to this user (opened by the SPA in a popup).
app.post(
  '/api/github/login-url',
  wrap(async (req, res) => {
    const token = requireToken(req)
    if (!github.isConfigured()) throw new HttpError(501, 'GitHub sign-in is not configured on the server.')
    const { oid } = azure.getSignedInIdentity(token)
    res.json({ authorizeUrl: github.authorizeUrl(oid) })
  }),
)

// OAuth callback — a top-level browser navigation, so it carries no ARM token;
// the signed `state` binds it to the user. Returns a tiny self-closing page.
// Registered at both paths so it works whether the app's registered Callback URL
// is /api/github/callback or /oauth/github/callback.
app.get(
  ['/api/github/callback', '/oauth/github/callback'],
  wrap(async (req, res) => {
    const code = String(req.query.code || '')
    const state = github.readState(req.query.state)
    if (!code || !state) {
      console.error('[github/callback] missing code or invalid/expired state', {
        hasCode: Boolean(code),
        validState: Boolean(state),
      })
      return res.status(400).send(github.closePage('GitHub sign-in failed (invalid or expired state).', false))
    }
    try {
      const accessToken = await github.exchangeCode(code)
      const user = await github.getUser(accessToken)
      github.tokenStore.set(state.oid, { token: accessToken, login: user.login, avatarUrl: user.avatarUrl })
      console.log('[github/callback] connected as', user.login, 'for oid', state.oid)
      res.send(github.closePage(`Connected as ${user.login}. You can close this window.`, true))
    } catch (err) {
      console.error('[github/callback] FAILED:', String(err?.message ?? err))
      res.status(502).send(github.closePage(`GitHub sign-in failed: ${String(err?.message ?? err).slice(0, 200)}`, false))
    }
  }),
)

// Disconnect this user's GitHub.
app.post(
  '/api/github/disconnect',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const { oid } = azure.getSignedInIdentity(token)
    github.tokenStore.clear(oid)
    res.json({ configured: github.isConfigured(), connected: false })
  }),
)

// Remove the portal-recorded GitHub repo link from a Function App so it can be
// connected to a different repository. Clears only the app-setting metadata the
// portal wrote; a Deployment Center (GitHub Actions) connection set up in the
// Azure portal is left untouched (flagged in the response so the UI can note it).
app.post(
  '/api/github/unlink',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const appName = String(req.body?.app ?? '').trim()
    // Opt-in: also disconnect the Function App's Deployment Center source control
    // (the GitHub Actions link set up in the Azure portal) so it stops pointing at
    // the old repo. Destructive — the UI gates it behind a confirmation.
    const disconnectDC = req.body?.deploymentCenter === true
    if (!resourceGroup || !appName) throw new HttpError(400, 'resourceGroup and app are required.')
    // Note whether the live link currently comes from the Deployment Center.
    let deploymentCenter = false
    try {
      const link = await azure.getAppGithubLink(token, subscription, resourceGroup, appName)
      deploymentCenter = link?.source === 'deploymentCenter'
    } catch {
      /* best-effort */
    }
    let cleared = false
    try {
      cleared = await azure.clearAppGithubLink(token, subscription, resourceGroup, appName)
    } catch (err) {
      throw new HttpError(err.status ?? 502, String(err?.message ?? err))
    }
    // Best-effort DELETE of the Deployment Center source when explicitly requested.
    let deploymentCenterCleared = false
    if (disconnectDC) {
      try {
        const r = await azure.deleteDeploymentSource(token, subscription, resourceGroup, appName)
        deploymentCenterCleared = r.ok
        if (!r.ok) console.error('[github/unlink] deployment center disconnect failed:', r.status)
      } catch (err) {
        console.error('[github/unlink] deployment center disconnect error:', String(err?.message ?? err))
      }
    }
    res.json({ ok: true, cleared, deploymentCenter, deploymentCenterCleared })
  }),
)

// List the connected user's repos (for the "existing repo" picker).
app.get(
  '/api/github/repos',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const { oid } = azure.getSignedInIdentity(token)
    const entry = github.tokenStore.get(oid)
    if (!entry) throw new HttpError(401, 'Not connected to GitHub.')
    try {
      res.json({ repos: await github.listRepos(entry.token) })
    } catch (err) {
      throw new HttpError(err.status ?? 502, String(err?.message ?? err))
    }
  }),
)

// Create (or connect) a repo, open a PR with the app's source on a customer
// branch, and record the repo link on the Function App.
app.post(
  '/api/github/connect',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const { oid } = azure.getSignedInIdentity(token)
    const entry = github.tokenStore.get(oid)
    if (!entry) throw new HttpError(401, 'Not connected to GitHub.')

    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const appName = String(req.body?.app ?? '').trim()
    const mode = String(req.body?.mode ?? 'new')
    if (!resourceGroup || !appName) throw new HttpError(400, 'resourceGroup and app are required.')

    // Assemble this app's source: the portal-managed working copy if present,
    // else the app's deployed package — then overlay any saved (unpublished)
    // drafts so the PR includes edits the user hasn't deployed yet.
    const dir = path.join(APP_SOURCES_DIR, safeSegment(subscription), safeSegment(appName))
    let baseFiles = (await pathExists(dir)) ? await readDirFiles(dir) : null
    if (!baseFiles || baseFiles.length === 0) {
      const site = await azure.getSite(token, subscription, resourceGroup, appName)
      if (site) baseFiles = await azure.readPackageFiles(token, subscription, site)
    }
    if (!baseFiles || baseFiles.length === 0) {
      throw new HttpError(404, 'No source found for this app to push. Deploy or save a draft first.')
    }
    const appFiles = await overlayDrafts(subscription, appName, baseFiles)
    if (!appFiles.length) throw new HttpError(404, 'No files to push for this app.')

    try {
      // Resolve the target repo (create new, or use an existing one).
      let repo
      if (mode === 'existing') {
        const fullName = String(req.body?.repo ?? '').trim()
        const [owner, name] = fullName.split('/')
        if (!owner || !name) throw new HttpError(400, 'An existing repo "owner/name" is required.')
        repo = {
          owner,
          name,
          defaultBranch: String(req.body?.branch ?? '').trim() || 'main',
          htmlUrl: `https://github.com/${owner}/${name}`,
        }
      } else {
        const name = safeSegment(String(req.body?.repoName ?? appName).trim() || appName)
        const priv = req.body?.private !== false
        const org = String(req.body?.org ?? '').trim()
        let created
        try {
          created = await github.createRepo(entry.token, { name, private: priv, org })
        } catch (e) {
          // Common on retries: the repo already exists. Reuse it instead of failing.
          if (e.status === 422 || e.status === 404) {
            const owner = org || entry.login
            created = await github.getRepo(entry.token, owner, name).catch(() => null)
            if (!created) throw e
          } else {
            throw e
          }
        }
        repo = {
          owner: created.owner,
          name: created.name,
          defaultBranch: created.defaultBranch,
          htmlUrl: created.htmlUrl,
        }
      }

      // Assemble the complete, azd-deployable repo (azure.yaml + infra/** +
      // README + the function app under src/) to push.
      const files = await buildRepoFiles(appFiles, appName)

      // Option B — one rolling PR per app: reuse the open PR's branch so edits
      // accumulate into a single PR; a fresh branch starts once it's merged.
      const base = repo.defaultBranch || 'main'
      const seg = (s) =>
        String(s)
          .replace(/[^A-Za-z0-9._-]/g, '-')
          .replace(/^-+|-+$/g, '') || 'x'
      const prefix = `agents/${seg(entry.login)}/${seg(appName)}`
      const head = await github.resolveRollingBranch(entry.token, repo.owner, repo.name, base, prefix)
      const pr = await github.openPullRequest(entry.token, {
        owner: repo.owner,
        repo: repo.name,
        base,
        head,
        files,
        message: `Update agent "${appName}" (via AI Apps)`,
        title: `Agent "${appName}" via AI Apps`,
        body: `Opened by AI Apps on behalf of @${entry.login}.\n\nAdds/updates the source for agent app \`${appName}\` on branch \`${head}\`. Edits roll into this PR until it's merged.`,
      })

      // Persist the connection as app-setting metadata (the reliable store that
      // later visits/edits read back). We intentionally do NOT write the Function
      // App's Deployment Center source control here: on Flex Consumption that's a
      // GitHub Actions integration (set up via the Azure portal), and a plain PUT
      // would be rejected or could overwrite an existing GitHub Actions
      // connection. getAppGithubLink still READS the Deployment Center and
      // prefers a repo connected there.
      const repoUrl = `https://github.com/${repo.owner}/${repo.name}`
      let stored = true
      try {
        await azure.setAppSettings(token, subscription, resourceGroup, appName, {
          GITHUB_REPO_URL: repoUrl,
          GITHUB_BRANCH: base,
          GITHUB_CONNECTED_BY: entry.login,
        })
      } catch {
        stored = false
      }

      // Best-effort: also reflect the repo in the Function App's Deployment Center
      // so it shows there. Never clobber an existing connection to a DIFFERENT repo
      // (which may be a live GitHub Actions pipeline) — to repoint that, disconnect
      // it first. If the DC already points at this repo, report it as recorded.
      let deploymentCenter = false
      try {
        const existing = await azure.readDeploymentSource(token, subscription, resourceGroup, appName)
        if (existing && existing.repoUrl === repoUrl) {
          deploymentCenter = true
        } else if (!existing) {
          const dc = await azure.setGithubActionSource(token, subscription, resourceGroup, appName, {
            repoUrl,
            branch: base,
            githubToken: entry.token,
          })
          deploymentCenter = dc.ok
        }
      } catch {
        /* non-fatal */
      }

      res.json({
        htmlUrl: repo.htmlUrl || repoUrl,
        repoUrl,
        owner: repo.owner,
        name: repo.name,
        base,
        branch: head,
        prUrl: pr.prUrl,
        prNumber: pr.prNumber,
        stored,
        deploymentCenter,
        pushed: files.map((f) => f.name).sort(),
      })
    } catch (err) {
      if (err instanceof HttpError) throw err
      console.error('[github/connect] FAILED:', err?.status ?? '', String(err?.message ?? err))
      throw new HttpError(err.status ?? 502, String(err?.message ?? err))
    }
  }),
)

// Provision passwordless GitHub Actions CI/CD (OIDC) from a connected repo to the
// Function App and re-point the Deployment Center to it: a user-assigned managed
// identity + federated credential + Contributor role assignment, a committed
// workflow, and repo variables. Infra-mutating — the UI gates it behind a confirm.
app.post(
  '/api/github/provision-deployment',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const { oid, tenantId } = azure.getSignedInIdentity(token)
    const entry = github.tokenStore.get(oid)
    if (!entry) throw new HttpError(401, 'Not connected to GitHub.')

    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const appName = String(req.body?.app ?? '').trim()
    const fullName = String(req.body?.repo ?? '').trim()
    const branch = String(req.body?.branch ?? '').trim() || 'main'
    const [owner, name] = fullName.split('/')
    if (!resourceGroup || !appName) throw new HttpError(400, 'resourceGroup and app are required.')
    if (!owner || !name) throw new HttpError(400, 'A repo "owner/name" is required.')
    if (!tenantId) throw new HttpError(400, 'Could not resolve the tenant from the access token.')

    const site = await azure.getSite(token, subscription, resourceGroup, appName)
    const location = site?.location
    if (!location) throw new HttpError(404, 'Could not resolve the Function App location.')

    const steps = {}
    try {
      // 1) Commit the workflow FIRST so we fail fast if the GitHub App lacks the
      //    dedicated "Workflows" write permission (required for .github/workflows).
      const workflow = github.functionsWorkflowYaml({ appName, branch, packagePath: 'src' })
      try {
        await github.putRepoContent(
          entry.token,
          owner,
          name,
          '.github/workflows/deploy.yml',
          Buffer.from(workflow, 'utf-8'),
          'Add Azure Functions deploy workflow (AI Apps)',
          branch,
        )
      } catch (e) {
        // GitHub returns 404 (or 403) when writing under .github/workflows without
        // the App's "Workflows" permission, even though Contents:write is present.
        if (e.status === 404 || e.status === 403) {
          throw new HttpError(
            403,
            'The GitHub App needs the "Workflows" (read & write) permission to commit ' +
              '.github/workflows/deploy.yml. Add it in the GitHub App settings, approve the updated ' +
              'permission on the installation, then retry.',
          )
        }
        throw e
      }
      steps.workflow = '.github/workflows/deploy.yml'

      // 2) User-assigned managed identity for GitHub to assume via OIDC.
      const idName = `id-${appName}-gha`.replace(/[^A-Za-z0-9_-]/g, '-').slice(0, 120)
      const identity = await azure.ensureUserAssignedIdentity(token, subscription, resourceGroup, idName, location)
      if (!identity.clientId || !identity.principalId)
        throw new HttpError(502, 'Managed identity created but its client/principal id did not populate.')
      steps.identity = idName

      // 3) Federated credential binding the repo+branch to that identity.
      const subject = `repo:${owner}/${name}:ref:refs/heads/${branch}`
      const ficName = `gh-${branch}`.replace(/[^A-Za-z0-9_-]/g, '-').slice(0, 120)
      await azure.ensureFederatedCredential(token, subscription, resourceGroup, idName, ficName, subject)
      steps.federatedCredential = subject

      // 4) Contributor on the resource group so the workflow can deploy.
      await azure.ensureRoleAssignment(token, subscription, resourceGroup, identity.principalId)
      steps.roleAssignment = `Contributor @ ${resourceGroup}`

      // 5) Repo variables the workflow's azure/login reads (not secrets).
      await github.setRepoVariable(entry.token, owner, name, 'AZURE_CLIENT_ID', identity.clientId)
      await github.setRepoVariable(entry.token, owner, name, 'AZURE_TENANT_ID', tenantId)
      await github.setRepoVariable(entry.token, owner, name, 'AZURE_SUBSCRIPTION_ID', subscription)
      steps.variables = ['AZURE_CLIENT_ID', 'AZURE_TENANT_ID', 'AZURE_SUBSCRIPTION_ID']

      // 6) Best-effort: light up the Deployment Center blade for the new repo.
      const dc = await azure.setGithubActionSource(token, subscription, resourceGroup, appName, {
        repoUrl: `https://github.com/${owner}/${name}`,
        branch,
        githubToken: entry.token,
      })
      steps.deploymentCenter = dc.ok

      // Record the repo link on the app settings too (UI source of truth).
      try {
        await azure.setAppSettings(token, subscription, resourceGroup, appName, {
          GITHUB_REPO_URL: `https://github.com/${owner}/${name}`,
          GITHUB_BRANCH: branch,
          GITHUB_CONNECTED_BY: entry.login,
        })
      } catch {
        /* non-fatal */
      }

      res.json({
        ok: true,
        steps,
        clientId: identity.clientId,
        workflowUrl: `https://github.com/${owner}/${name}/blob/${branch}/.github/workflows/deploy.yml`,
        runsUrl: `https://github.com/${owner}/${name}/actions`,
      })
    } catch (err) {
      if (err instanceof HttpError) throw err
      const at = Object.keys(steps).pop() || 'start'
      console.error('[github/provision] FAILED after', at, ':', err?.status ?? '', String(err?.message ?? err))
      throw new HttpError(err.status ?? 502, `Provisioning failed after ${at}: ${String(err?.message ?? err)}`)
    }
  }),
)

// Any unmatched /api/* path is a 404 JSON (never the SPA shell).
app.use('/api', (_req, res) => res.status(404).json({ detail: 'Not found' }))

// ---------------------------------------------------------------------------
// React SPA (built assets). Registered after /api so it never shadows the API.
// Run `npm run build` in frontend/ to produce dist/. In dev, use the Vite
// server on :5173 (it proxies /api here).
// ---------------------------------------------------------------------------

if (fs.existsSync(path.join(DIST_DIR, 'index.html'))) {
  app.use(express.static(DIST_DIR))
  // Client-side routing: serve the SPA shell for any other path.
  app.get('*', (_req, res) => res.sendFile(path.join(DIST_DIR, 'index.html')))
} else {
  app.get('/', (_req, res) =>
    res
      .status(200)
      .send(
        '<h3>Frontend not built</h3>' +
          '<p>Run <code>npm install &amp;&amp; npm run build</code> in ' +
          '<code>serverless-portal/app/frontend/</code>, then restart. ' +
          "For development, run <code>npm run dev</code> and use " +
          "<a href='http://localhost:5173/'>http://localhost:5173/</a>.</p>",
      ),
  )
}

// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------

// eslint-disable-next-line no-unused-vars
app.use((err, _req, res, _next) => {
  if (err instanceof HttpError) {
    return res.status(err.status).json({ detail: err.detail })
  }
  console.error(err)
  res.status(500).json({ detail: 'Internal server error' })
})

app.listen(PORT, () => {
  console.log(`Serverless Agent Portal backend listening on http://127.0.0.1:${PORT}`)
})
