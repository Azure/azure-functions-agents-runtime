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

import express from 'express'
import cors from 'cors'

import * as azure from './azure.js'

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
    methods: ['GET'],
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
