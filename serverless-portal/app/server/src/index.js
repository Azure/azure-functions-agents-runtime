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

import express from 'express'
import cors from 'cors'

import * as azure from './azure.js'
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
    methods: ['GET', 'PUT', 'POST', 'OPTIONS'],
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

    const draftContent = await readDraft(subscription, appName, name)
    let deployedContent = null
    if (resourceGroup) {
      const site = await azure.getSite(token, subscription, resourceGroup, appName)
      if (site) deployedContent = await azure.readAgentDefinition(token, subscription, site, name)
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
  return path.join(
    SOURCE_DRAFTS_DIR,
    safeSegment(subscription),
    safeSegment(appName),
    safeSegment(relPath),
  )
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

    const draftContent = await readSourceDraft(subscription, appName, relPath)
    let deployedContent = null
    if (resourceGroup) {
      const site = await azure.getSite(token, subscription, resourceGroup, appName)
      if (site) deployedContent = await azure.readSourceFile(token, subscription, site, relPath)
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

// ---------------------------------------------------------------------------
// Create / deploy agent — refresh the target Function App's portal-managed
// source tree, then provision (for a new app) and push it to Azure with a
// remote build. Every Azure call runs as the signed-in user's forwarded token.
// ---------------------------------------------------------------------------

const APP_SOURCES_DIR = path.join(__dirname, '..', '.data', 'app-sources')

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
  for (const file of await listDirFiles(sourceDir)) {
    apply(file, await fs.promises.readFile(path.join(sourceDir, file), 'utf-8'))
  }
  return [...byName].map(([name, data]) => ({ name, data }))
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
  const { subscription, resourceGroup, appName, target, dir, deploymentName, fileName } = ctx
  try {
    if (target.kind === 'new') {
      setJob(id, { message: 'Provisioning Azure resources…' })
      await provision.provisionFlexApp(token, {
        subscriptionId: subscription,
        resourceGroup,
        appName,
        region: target.region,
        foundryEndpoint: target.foundryEndpoint,
        foundryModel: target.foundryModel,
        deploymentName,
      })
    }

    setJob(id, { message: 'Resolving Function App…' })
    const site = await azure.getSite(token, subscription, resourceGroup, appName)
    if (!site) throw new Error(`Function App "${appName}" was not found in "${resourceGroup}".`)
    const principalId = site.identity?.principalId || ''

    await pushFilesToSite(id, token, site, await readDirFiles(dir))

    const foundryNote =
      target.kind === 'new' && principalId
        ? ` Grant the app identity (principalId ${principalId}) access to your Foundry project for live model calls.`
        : ''
    setJob(id, {
      status: 'deployed',
      message: `Deployed "${fileName}" to ${appName}.${foundryNote}`,
      url: `https://${site.defaultHostName}`,
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
    runDeployJob(jobId, token, { subscription, resourceGroup, appName, target, dir, deploymentName, fileName })

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
