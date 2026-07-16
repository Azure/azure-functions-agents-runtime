// Serverless Agent Portal — Node.js backend (Express).
//
// A thin read-only control plane over live Azure discovery: it authenticates
// with the signed-in identity (DefaultAzureCredential), lists the user's
// subscriptions, and scans a subscription for serverless agents. See
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
// Azure (live discovery)
// ---------------------------------------------------------------------------

// Signed-in identity + the default subscription to scan.
app.get(
  '/api/identity',
  wrap(async (_req, res) => {
    const [user, subscriptionName] = await Promise.all([
      azure.getSignedInIdentity(),
      azure.getSubscriptionName(azure.DEFAULT_SUBSCRIPTION_ID),
    ])
    res.json({
      user,
      subscription: { id: azure.DEFAULT_SUBSCRIPTION_ID, name: subscriptionName },
    })
  }),
)

// List subscriptions the signed-in identity can see (for the top-bar picker).
app.get(
  '/api/subscriptions',
  wrap(async (_req, res) => {
    res.json(await azure.listSubscriptions())
  }),
)

// Discover agent apps + their agents. Defaults to the hardcoded subscription;
// a `subscription` id/name override drives the top-bar picker.
app.get(
  '/api/live/agents',
  wrap(async (req, res) => {
    const ref = String(req.query.subscription ?? '').trim()
    let subscriptionId = azure.DEFAULT_SUBSCRIPTION_ID
    if (ref) {
      try {
        subscriptionId = await azure.resolveSubscriptionId(ref)
      } catch (err) {
        if (err instanceof azure.SubscriptionNotFoundError) {
          throw new HttpError(404, err.message)
        }
        throw err
      }
    }
    const result = await azure.discoverAgentApps(subscriptionId)
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
        defaultHostName: a.defaultHostName,
      })),
    )
    res.json({ subscriptionId, apps: result.apps, agents })
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
