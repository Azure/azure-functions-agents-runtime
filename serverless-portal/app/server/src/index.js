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
import * as YAML from 'js-yaml'

import * as azure from './azure.js'
import { validateAgentFiles, validateAgentMarkdown } from './agent-validation.js'
import { purgePortalAppData, validateAppLifecycleRequest } from './app-lifecycle.js'
import {
  azureRoleScope,
  customToolPath,
  mergeRequirements,
  renderAzureRestTool,
  validateAzureRestSource,
} from './custom-tools.js'
import {
  attachOutlookConnection,
  coordinateOutlookConnectionSetup,
  coordinateOutlookConnectionRemoval,
  createOutlookConnection,
  deleteOutlookConnection,
  functionAppResourceId,
  getOutlookConnection,
  listOutlookConnectionCandidates,
  listOutlookConnections,
  OUTLOOK_CONNECTION_ID_SETTING,
  outlookAppSettings,
  removeOutlookMcpSource,
  testOutlookConnection,
} from './connections.js'
import * as github from './github.js'
import * as provision from './provision.js'
import { recoverSourceDrafts, writeSourceDrafts } from './source-draft-store.js'

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
  constructor(status, detail, metadata = {}) {
    super(detail)
    this.status = status
    this.detail = detail
    this.metadata = metadata
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

function requestCookie(req, name) {
  for (const part of String(req.get('cookie') ?? '').split(';')) {
    const separator = part.indexOf('=')
    if (separator < 0 || part.slice(0, separator).trim() !== name) continue
    try {
      return decodeURIComponent(part.slice(separator + 1).trim())
    } catch {
      return ''
    }
  }
  return ''
}

function githubEntry(req, oid) {
  const sealed = requestCookie(req, github.GITHUB_SESSION_COOKIE)
  return github.readSession(sealed, oid)
}

function githubCookieOptions(req) {
  const forwardedProtocol = String(req.get('x-forwarded-proto') ?? '').split(',')[0].trim()
  const configuredCallbackIsSecure = github.githubConfig().callback.startsWith('https://')
  const localHost = req.hostname === 'localhost' || req.hostname === '127.0.0.1'
  return {
    httpOnly: true,
    sameSite: 'lax',
    secure: configuredCallbackIsSecure || !localHost || req.secure || forwardedProtocol === 'https',
    path: '/',
  }
}

function setGithubSessionCookie(req, res, oid, session) {
  res.cookie(
    github.GITHUB_SESSION_COOKIE,
    github.sealSession(oid, session),
    { ...githubCookieOptions(req), maxAge: github.GITHUB_SESSION_MAX_AGE_MS },
  )
}

async function activeGithubEntry(req, res, oid, { forceValidate = false } = {}) {
  const stored = githubEntry(req, oid)
  if (!stored) return null
  try {
    const resolved = await github.ensureUserSession(stored, { forceValidate })
    if (resolved.changed) setGithubSessionCookie(req, res, oid, resolved.session)
    return resolved.session
  } catch (error) {
    if (error?.status === 401) {
      res.clearCookie(github.GITHUB_SESSION_COOKIE, githubCookieOptions(req))
      return null
    }
    throw new HttpError(error?.status ?? 502, String(error?.message ?? error))
  }
}

function expiredGithubSession(req, res) {
  res.clearCookie(github.GITHUB_SESSION_COOKIE, githubCookieOptions(req))
  return new HttpError(401, 'Your GitHub session expired. Connect GitHub again.', {
    error: 'github_session_expired',
  })
}

function githubOperationHttpError(req, res, error) {
  if (Number(error?.status ?? error?.statusCode) === 401) {
    return expiredGithubSession(req, res)
  }
  return new HttpError(error?.status ?? 502, String(error?.message ?? error))
}

function isLocalDevelopmentRequest(req) {
  return process.env.NODE_ENV !== 'production' && (req.hostname === 'localhost' || req.hostname === '127.0.0.1')
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

async function connectionContext(token, subscription, resourceGroup, appName) {
  if (!subscription || !resourceGroup || !appName) {
    throw new HttpError(400, 'subscription, resourceGroup, and app are required.')
  }
  const [site, runtimeIdentity, appSettings] = await Promise.all([
    azure.getSite(token, subscription, resourceGroup, appName),
    azure.resolveRuntimeIdentity(token, subscription, resourceGroup, appName),
    azure.readAppSettingsStrict(token, subscription, resourceGroup, appName),
  ])
  if (!site?.id) throw new HttpError(404, `Function App "${appName}" was not found.`)
  const deployer = azure.getSignedInIdentity(token)
  if (!deployer.oid || !deployer.tenantId) {
    throw new HttpError(403, 'The signed-in identity is missing required Entra ID claims.')
  }
  return {
    appResourceId: site.id,
    location: site.location || 'eastus2',
    runtimePrincipalId: runtimeIdentity.principalId,
    deployerPrincipalId: deployer.oid,
    tenantId: deployer.tenantId,
    subscriptionId: subscription,
    resourceGroup,
    appName,
    configuredMcpUrl: String(appSettings.O365_MCP_SERVER_URL ?? ''),
    configuredConnectionId: String(appSettings[OUTLOOK_CONNECTION_ID_SETTING] ?? ''),
  }
}

function connectionHttpError(error) {
  return new HttpError(error?.status ?? 502, String(error?.message ?? error), {
    ...(error?.portalCode ? { error: error.portalCode } : {}),
    ...(error?.sourceCleanup ? { sourceCleanup: error.sourceCleanup } : {}),
  })
}

async function configureOutlookWithSource(req, route, configureAzure) {
  const sourceBefore = await readCurrentSource(
    req,
    route.subscription,
    route.resourceGroup,
    route.appName,
    'mcp.json',
  )
  return coordinateOutlookConnectionSetup({
    sourceBefore,
    stageSource: (content) => writeSourceDraft(route.subscription, route.appName, 'mcp.json', content),
    rollbackSource: async (previous) => {
      if (previous.source === 'draft') {
        await writeSourceDraft(route.subscription, route.appName, 'mcp.json', previous.content)
      } else {
        await removeSourceDraft(route.subscription, route.appName, 'mcp.json')
      }
    },
    configureAzure,
  })
}

async function wireOutlookEndpoint(token, context, connection) {
  let settings
  try {
    settings = outlookAppSettings(connection)
  } catch {
    throw new HttpError(502, 'Azure did not return a valid Outlook MCP endpoint.')
  }
  const connectionResourceId = settings[OUTLOOK_CONNECTION_ID_SETTING]
  try {
    await azure.setAppSettings(token, context.subscriptionId, context.resourceGroup, context.appName, settings)
  } catch (error) {
    const forbidden = error?.status === 403 || error?.statusCode === 403
    throw new HttpError(
      forbidden ? 403 : 502,
      forbidden
        ? 'Outlook resources were configured, but the portal cannot update this Function App setting. Grant application-settings write access and retry setup.'
        : 'Outlook resources were configured, but the Function App endpoint setting could not be updated. Retry setup.',
      { error: forbidden ? 'app_settings_forbidden' : 'app_settings_update_failed' },
    )
  }
  const updatedSettings = await azure.readAppSettingsStrict(
    token,
    context.subscriptionId,
    context.resourceGroup,
    context.appName,
  )
  const persistedEndpoint = String(updatedSettings.O365_MCP_SERVER_URL ?? '').trim().replace(/\/$/, '')
  const expectedEndpoint = settings.O365_MCP_SERVER_URL.trim().replace(/\/$/, '')
  const persistedConnectionId = String(updatedSettings[OUTLOOK_CONNECTION_ID_SETTING] ?? '').trim()
  if (persistedEndpoint !== expectedEndpoint || persistedConnectionId.toLowerCase() !== connectionResourceId.toLowerCase()) {
    throw new HttpError(
      502,
      'Outlook resources were configured, but the Function App connection settings were not persisted. Retry setup.',
      { error: 'app_settings_not_persisted' },
    )
  }
  return getOutlookConnection(
    token,
    {
      ...context,
      configuredMcpUrl: String(updatedSettings.O365_MCP_SERVER_URL),
      configuredConnectionId: persistedConnectionId,
    },
    connection.id,
  )
}

app.get(
  '/api/connections',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.query.resourceGroup ?? '').trim()
    const appName = String(req.query.app ?? '').trim()
    try {
      const context = await connectionContext(token, subscription, resourceGroup, appName)
      res.json({ connections: await listOutlookConnections(token, context) })
    } catch (error) {
      throw connectionHttpError(error)
    }
  }),
)

app.post(
  '/api/connections',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const route = connectionMutationContext(req)
    const displayName = String(req.body?.displayName ?? '').trim()
    try {
      const context = await connectionContext(token, route.subscription, route.resourceGroup, route.appName)
      const result = await configureOutlookWithSource(req, route, async () => {
        const created = await createOutlookConnection(token, context, displayName)
        return wireOutlookEndpoint(token, context, created)
      })
      res.status(201).json(result)
    } catch (error) {
      throw connectionHttpError(error)
    }
  }),
)

function connectionQueryContext(req) {
  return {
    subscription: String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID,
    resourceGroup: String(req.query.resourceGroup ?? '').trim(),
    appName: String(req.query.app ?? '').trim(),
  }
}

function connectionMutationContext(req) {
  return {
    subscription: String(req.query.subscription ?? req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID,
    resourceGroup: String(req.query.resourceGroup ?? req.body?.resourceGroup ?? '').trim(),
    appName: String(req.query.app ?? req.body?.app ?? '').trim(),
  }
}

function connectorSubscriptionId(value, fallback) {
  const subscriptionId = String(value || fallback).trim()
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(subscriptionId)) {
    throw new HttpError(400, 'connectorSubscription must be a valid Azure subscription ID.')
  }
  return subscriptionId
}

app.get(
  '/api/connections/candidates',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const route = connectionQueryContext(req)
    const selectedConnectorSubscription = connectorSubscriptionId(
      req.query.connectorSubscription,
      route.subscription,
    )
    try {
      const context = req.query.planned === 'true'
        ? { appResourceId: functionAppResourceId(route.subscription, route.resourceGroup, route.appName) }
        : await connectionContext(token, route.subscription, route.resourceGroup, route.appName)
      res.json(await listOutlookConnectionCandidates(token, {
        ...context,
        connectorSubscriptionId: selectedConnectorSubscription,
      }))
    } catch (error) {
      throw connectionHttpError(error)
    }
  }),
)

app.post(
  '/api/connections/attach',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const route = connectionMutationContext(req)
    const connectionId = String(req.body?.connectionId ?? '').trim()
    const selectedConnectorSubscription = connectorSubscriptionId(
      req.body?.connectorSubscription,
      route.subscription,
    )
    try {
      const context = await connectionContext(token, route.subscription, route.resourceGroup, route.appName)
      const result = await configureOutlookWithSource(req, route, async () => {
        const attached = await attachOutlookConnection(
          token,
          { ...context, connectorSubscriptionId: selectedConnectorSubscription },
          connectionId,
        )
        return wireOutlookEndpoint(token, context, attached)
      })
      res.json(result)
    } catch (error) {
      throw connectionHttpError(error)
    }
  }),
)

app.get(
  '/api/connections/:connectionId/status',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const route = connectionQueryContext(req)
    try {
      const context = await connectionContext(token, route.subscription, route.resourceGroup, route.appName)
      res.json({ connection: await getOutlookConnection(token, context, req.params.connectionId) })
    } catch (error) {
      throw connectionHttpError(error)
    }
  }),
)

app.get(
  '/api/connections/:connectionId/auth-link',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const route = connectionQueryContext(req)
    try {
      const context = await connectionContext(token, route.subscription, route.resourceGroup, route.appName)
      const connection = await getOutlookConnection(token, context, req.params.connectionId)
      res.json({ url: connection.portalUrl })
    } catch (error) {
      throw connectionHttpError(error)
    }
  }),
)

app.post(
  '/api/connections/:connectionId/test',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const route = connectionMutationContext(req)
    try {
      const context = await connectionContext(token, route.subscription, route.resourceGroup, route.appName)
      res.json(await testOutlookConnection(token, context, req.params.connectionId))
    } catch (error) {
      throw connectionHttpError(error)
    }
  }),
)

app.delete(
  '/api/connections/:connectionId',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const route = connectionQueryContext(req)
    let context
    try {
      context = await connectionContext(token, route.subscription, route.resourceGroup, route.appName)
      await getOutlookConnection(token, context, req.params.connectionId)

      const sourceBefore = await readCurrentSource(req, route.subscription, route.resourceGroup, route.appName, 'mcp.json')
      const result = await coordinateOutlookConnectionRemoval({
        sourceBefore,
        settingBefore: context.configuredMcpUrl || context.configuredConnectionId
          ? {
              removed: true,
              values: {
                O365_MCP_SERVER_URL: context.configuredMcpUrl,
                ...(context.configuredConnectionId
                  ? { [OUTLOOK_CONNECTION_ID_SETTING]: context.configuredConnectionId }
                  : {}),
              },
            }
          : null,
        stageSource: (content) => writeSourceDraft(route.subscription, route.appName, 'mcp.json', content),
        rollbackSource: async (previous) => {
          if (previous.source === 'draft') {
            await writeSourceDraft(route.subscription, route.appName, 'mcp.json', previous.content)
          } else {
            await removeSourceDraft(route.subscription, route.appName, 'mcp.json')
          }
        },
        removeSetting: async () => {
          const settingResult = await azure.removeAppSettings(
            token,
            route.subscription,
            route.resourceGroup,
            route.appName,
            ['O365_MCP_SERVER_URL', OUTLOOK_CONNECTION_ID_SETTING],
          )
          return Object.keys(settingResult.removedValues).length
            ? { removed: true, values: settingResult.removedValues }
            : { removed: false }
        },
        restoreSetting: (setting) => azure.setAppSettings(
          token,
          route.subscription,
          route.resourceGroup,
          route.appName,
          setting.values,
        ),
        deleteAzure: () => deleteOutlookConnection(token, context, req.params.connectionId),
      })
      res.json({ removed: true, ...result })
    } catch (error) {
      throw new HttpError(error?.status ?? 502, String(error?.message ?? error), {
        ...(error?.portalCode ? { error: error.portalCode } : {}),
        ...(error?.cleanup ? { cleanup: error.cleanup } : {}),
      })
    }
  }),
)

// ---------------------------------------------------------------------------
// Agent definition — read the deployed `*.agent.md` (or the portal draft) and
// save edits to a portal-side working copy. Publishing a draft to the live app
// happens through deploy/redeploy, which clears the published working copy.
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

function agentSlug(value) {
  const slug = String(value ?? '')
    .replace(/\.agent\.md$/i, '')
    .replace(/[^a-zA-Z0-9_]/g, '_')
    .replace(/^_+|_+$/g, '')
  if (!slug) return 'agent_function'
  return /^[0-9]/.test(slug) ? `fn_${slug}` : slug
}

async function readPublishedAgentDefinition(subscription, appName, name) {
  const sourceDir = path.join(APP_SOURCES_DIR, safeSegment(subscription), safeSegment(appName))
  const target = agentSlug(name)
  const files = await readDirRecursive(sourceDir)
  const match = files.find((file) => {
    const base = file.name.split('/').pop() ?? ''
    return /\.agent\.md$/i.test(base) && agentSlug(base) === target
  })
  return match?.data.toString('utf-8') ?? null
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
async function readRepoFileForApp(req, token, subscription, resourceGroup, appName, candidatePaths) {
  if (!resourceGroup) return null
  let oid = ''
  try {
    oid = azure.getSignedInIdentity(token).oid
  } catch {
    return null
  }
  const entry = oid ? githubEntry(req, oid) : null
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
    if (deployedContent == null) {
      deployedContent = await readPublishedAgentDefinition(subscription, appName, name)
    }
    // Fallback: read the source straight from the app's connected GitHub repo.
    if (deployedContent == null) {
      deployedContent = await readRepoFileForApp(req, token, subscription, resourceGroup, appName, [
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

function sourceDraftAppDir(subscription, appName) {
  return path.join(SOURCE_DRAFTS_DIR, safeSegment(subscription), safeSegment(appName))
}

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
  await recoverSourceDrafts(sourceDraftAppDir(subscription, appName))
  try {
    return await fs.promises.readFile(sourceDraftPath(subscription, appName, relPath), 'utf-8')
  } catch {
    return null
  }
}

async function writeSourceDraft(subscription, appName, relPath, content) {
  await writeSourceDrafts(sourceDraftAppDir(subscription, appName), [{ path: relPath, content }])
}

async function removeSourceDraft(subscription, appName, relPath) {
  await recoverSourceDrafts(sourceDraftAppDir(subscription, appName))
  try {
    await fs.promises.unlink(sourceDraftPath(subscription, appName, relPath))
    return true
  } catch (error) {
    if (error?.code === 'ENOENT') return false
    throw error
  }
}

async function readCurrentSource(req, subscription, resourceGroup, appName, relPath) {
  const draft = await readSourceDraft(subscription, appName, relPath)
  if (draft != null) return { content: draft, source: 'draft' }
  const token = requireToken(req)
  const storageToken = storageTokenFrom(req)
  let deployed = null
  if (resourceGroup) {
    const site = await azure.getSite(token, subscription, resourceGroup, appName)
    if (site) deployed = await azure.readSourceFile(token, subscription, site, relPath, storageToken)
  }
  if (deployed == null) {
    deployed = await readRepoFileForApp(req, token, subscription, resourceGroup, appName, [relPath, `src/${relPath}`])
  }
  return { content: deployed ?? '', source: deployed == null ? 'none' : 'deployed' }
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
      deployedContent = await readRepoFileForApp(req, token, subscription, resourceGroup, appName, [
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

app.post(
  '/api/custom-tools/azure-rest/preview',
  wrap(async (req, res) => {
    requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const appName = String(req.body?.app ?? '').trim()
    const toolName = String(req.body?.toolName ?? 'azure_rest').trim()
    if (!appName || !resourceGroup) throw new HttpError(400, 'app and resourceGroup are required.')
    let toolPath
    try {
      toolPath = customToolPath(toolName)
    } catch (error) {
      throw new HttpError(400, error.message)
    }
    const [tool, requirements] = await Promise.all([
      readCurrentSource(req, subscription, resourceGroup, appName, toolPath),
      readCurrentSource(req, subscription, resourceGroup, appName, 'requirements.txt'),
    ])
    const merged = mergeRequirements(requirements.content)
    res.json({
      toolPath,
      python: renderAzureRestTool(toolName),
      requirements: merged.content,
      addedDependencies: merged.added,
      existingToolSource: tool.source,
      requiresOverwrite: tool.source !== 'none',
    })
  }),
)

app.post(
  '/api/custom-tools/azure-rest/save',
  wrap(async (req, res) => {
    requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const appName = String(req.body?.app ?? '').trim()
    const toolName = String(req.body?.toolName ?? 'azure_rest').trim()
    const overwrite = req.body?.overwrite === true
    if (!appName || !resourceGroup) throw new HttpError(400, 'app and resourceGroup are required.')
    let toolPath
    let python
    try {
      toolPath = customToolPath(toolName)
      python = validateAzureRestSource(req.body?.python, toolName)
    } catch (error) {
      throw new HttpError(400, error.message)
    }
    const [tool, requirements] = await Promise.all([
      readCurrentSource(req, subscription, resourceGroup, appName, toolPath),
      readCurrentSource(req, subscription, resourceGroup, appName, 'requirements.txt'),
    ])
    if (tool.source !== 'none' && !overwrite) {
      throw new HttpError(409, `${toolPath} already exists. Confirm overwrite to replace it.`, {
        error: 'tool_exists',
      })
    }
    const merged = mergeRequirements(requirements.content)
    await writeSourceDrafts(sourceDraftAppDir(subscription, appName), [
      { path: toolPath, content: python },
      { path: 'requirements.txt', content: merged.content },
    ])
    res.json({
      ok: true,
      source: 'draft',
      toolPath,
      requirementsPath: 'requirements.txt',
      addedDependencies: merged.added,
    })
  }),
)

function customToolHttpError(error) {
  return new HttpError(error?.status ?? 502, String(error?.message ?? error), {
    ...(error?.portalCode ? { error: error.portalCode } : {}),
    ...(error?.candidates ? { candidates: error.candidates } : {}),
  })
}

app.get(
  '/api/custom-tools/roles',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    let scope
    try {
      scope = azureRoleScope(subscription, String(req.query.scopeType ?? ''), String(req.query.resourceGroup ?? ''))
      res.json({ scope, roles: await azure.listAssignableRoles(token, subscription, scope) })
    } catch (error) {
      throw error?.status ? customToolHttpError(error) : new HttpError(400, error.message)
    }
  }),
)

app.get(
  '/api/custom-tools/identity',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.query.resourceGroup ?? '').trim()
    const appName = String(req.query.app ?? '').trim()
    if (!resourceGroup || !appName) throw new HttpError(400, 'resourceGroup and app are required.')
    try {
      res.json({ identity: await azure.resolveRuntimeIdentity(token, subscription, resourceGroup, appName) })
    } catch (error) {
      throw customToolHttpError(error)
    }
  }),
)

app.get(
  '/api/custom-tools/configured-model',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.query.resourceGroup ?? '').trim()
    const appName = String(req.query.app ?? '').trim()
    if (!resourceGroup || !appName) throw new HttpError(400, 'resourceGroup and app are required.')
    try {
      const configured = await azure.resolveConfiguredModel(token, subscription, resourceGroup, appName)
      res.json({ provider: configured.provider, model: configured.model, available: true })
    } catch (error) {
      throw customToolHttpError(error)
    }
  }),
)

app.post(
  '/api/custom-tools/access',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const appName = String(req.body?.app ?? '').trim()
    if (!resourceGroup || !appName) throw new HttpError(400, 'resourceGroup and app are required.')
    try {
      const scope = azureRoleScope(subscription, String(req.body?.scopeType ?? ''), String(req.body?.scopeResourceGroup ?? ''))
      const identity = await azure.resolveRuntimeIdentity(
        token,
        subscription,
        resourceGroup,
        appName,
        String(req.body?.identityClientId ?? ''),
      )
      const assignment = await azure.assignRole(
        token,
        subscription,
        scope,
        identity.principalId,
        String(req.body?.roleDefinitionId ?? ''),
      )
      res.json({ identity, ...assignment })
    } catch (error) {
      throw error?.status ? customToolHttpError(error) : new HttpError(400, error.message)
    }
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
    const removed = await removeSourceDraft(subscription, appName, relPath)
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
    const sourceDir = sourceDraftAppDir(subscription, appName)
    await recoverSourceDrafts(sourceDir)
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
  // One row per runtime invocation, projected onto OpenTelemetry span fields.
    // Prefer runtime dependency spans because they carry af.* and gen_ai.*
    // attributes. Fall back to Azure Functions request spans so timer and HTTP
    // runs remain visible when runtime OTLP export is not enabled.
  invocations: (timeRange) => `
  let runtime_spans = dependencies
    | where @TIME_FILTER@
    | where name startswith "agent.run "
    | extend agent_name = tostring(customDimensions["af.agent.name"])
    | where agent_name == "@AGENT@"
    | project start_time = timestamp,
      trace_id = operation_Id,
      span_id = id,
      parent_span_id = operation_ParentId,
      span_name = name,
      span_kind = "SPAN_KIND_INTERNAL",
      duration_ms = toreal(duration),
      status_code = iff(success == false, "STATUS_CODE_ERROR", "STATUS_CODE_OK"),
      attributes = customDimensions,
      source_priority = 0;
  let runtime_span_count = toscalar(runtime_spans | count);
  let host_invocations = requests
    | where @TIME_FILTER@
    | where cloud_RoleName == "@APP@"
    | where operation_Name == "@AGENT@" or operation_Name startswith "agent_@AGENT@_builtin_"
    | where runtime_span_count == 0
    | extend attributes = bag_merge(customDimensions, bag_pack(
      "faas.invoked_name", operation_Name,
      "telemetry.source", "azure.functions.requests"))
    | project start_time = timestamp,
      trace_id = operation_Id,
      span_id = id,
      parent_span_id = operation_ParentId,
      span_name = strcat("function.invoke ", operation_Name),
      span_kind = "SPAN_KIND_SERVER",
      duration_ms = toreal(duration),
      status_code = iff(success == false, "STATUS_CODE_ERROR", "STATUS_CODE_OK"),
      attributes,
      source_priority = 1;
  let all_invocations =
    union runtime_spans, host_invocations
    | summarize arg_min(source_priority, *) by trace_id
    | project start_time, trace_id, span_id, parent_span_id, span_name, span_kind,
      duration_ms, status_code, attributes;
  let page_rows = materialize(
    all_invocations
    | order by start_time desc
    | serialize row_number = row_number()
    | where row_number > @OFFSET@
    | take @PAGE_SIZE@ + 1);
  let has_more = toscalar(page_rows | count) > @PAGE_SIZE@;
  page_rows
  | where row_number <= @OFFSET@ + @PAGE_SIZE@
  | extend has_more
  | project start_time, trace_id, span_id, parent_span_id, span_name, span_kind,
    duration_ms, status_code, attributes, has_more
`,
  trace: (timeRange) => `
union
  (requests
    | where @TIME_FILTER@
    | where operation_Id == "@TRACE@"
    | project start_time = timestamp, item_type = "request", span_name = name,
        span_id = id, parent_span_id = operation_ParentId,
        duration_ms = toreal(duration),
        status_code = iff(success == false, "STATUS_CODE_ERROR", "STATUS_CODE_OK"),
        attributes = customDimensions),
  (dependencies
    | where @TIME_FILTER@
    | where operation_Id == "@TRACE@"
    | project start_time = timestamp, item_type = "dependency", span_name = name,
        span_id = id, parent_span_id = operation_ParentId,
        duration_ms = toreal(duration),
        status_code = iff(success == false, "STATUS_CODE_ERROR", "STATUS_CODE_OK"),
        attributes = customDimensions)
| order by start_time asc
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
    const agentName = String(req.body?.agent ?? '').trim()
    const traceId = String(req.body?.traceId ?? '').trim()
    const timeRange = String(req.body?.timeRange ?? '24h').trim()
    const startTime = String(req.body?.startTime ?? '').trim()
    const endTime = String(req.body?.endTime ?? '').trim()
    const page = Number(req.body?.page ?? 1)
    const pageSize = Number(req.body?.pageSize ?? 25)
    if (!appName || !resourceGroup) throw new HttpError(400, 'app and resourceGroup are required.')
    if (!/^(?:\d+)(?:m|h|d)$/.test(timeRange)) throw new HttpError(400, 'timeRange must look like 15m/24h/7d.')
    if ((startTime && !endTime) || (!startTime && endTime)) throw new HttpError(400, 'startTime and endTime must be provided together.')
    const startMs = startTime ? Date.parse(startTime) : NaN
    const endMs = endTime ? Date.parse(endTime) : NaN
    if (startTime && (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs >= endMs)) {
      throw new HttpError(400, 'startTime and endTime must be valid timestamps with startTime before endTime.')
    }
    if (!Number.isSafeInteger(page) || page < 1) throw new HttpError(400, 'page must be a positive integer.')
    if (!Number.isSafeInteger(pageSize) || pageSize < 1 || pageSize > 100) throw new HttpError(400, 'pageSize must be an integer from 1 to 100.')

    let query = ''
    if (preset) {
      const builder = INSIGHTS_PRESETS[preset]
      if (!builder) throw new HttpError(400, `Unknown preset "${preset}".`)
      const safeApp = safeKqlString(appName)
      if (!safeApp) throw new HttpError(400, 'App name contains disallowed characters.')
      const safeAgent = safeKqlString(agentName)
      if (preset === 'invocations' && !safeAgent) throw new HttpError(400, 'agent is required for the invocations preset.')
      if (preset === 'trace' && !/^[a-f\d]{32}$/i.test(traceId)) throw new HttpError(400, 'traceId must be a 32-character hexadecimal trace ID.')
      query = builder(timeRange)
        .replace(/@APP@/g, safeApp)
        .replace(/@AGENT@/g, safeAgent)
        .replace(/@TRACE@/g, traceId)
        .replace(/@TIME_FILTER@/g, startTime
          ? `timestamp between (datetime(${new Date(startMs).toISOString()}) .. datetime(${new Date(endMs).toISOString()}))`
          : `timestamp >= ago(${timeRange})`)
        .replace(/@OFFSET@/g, String((page - 1) * pageSize))
        .replace(/@PAGE_SIZE@/g, String(pageSize))
    } else if (typeof req.body?.query === 'string' && req.body.query.trim()) {
      query = String(req.body.query)
    } else {
      throw new HttpError(400, 'Provide either { preset } or { query }.')
    }

    const timespan = startTime
      ? `${new Date(startMs).toISOString()}/${new Date(endMs).toISOString()}`
      : `P${timeRange.replace(/(\d+)(m|h|d)/, (_, n, u) => u === 'd' ? `${n}D` : u === 'h' ? `T${n}H` : `T${n}M`)}`
    const result = await azure.queryAppInsights(token, subscription, resourceGroup, appName, query, timespan)
    if (result.error === 'no-component') {
      throw new HttpError(404, 'This app has no linked Application Insights component.')
    }
    if (result.error) throw new HttpError(502, `App Insights query failed: ${result.error}`)
    res.json(result)
  }),
)

// ---------------------------------------------------------------------------
// Samples — enumerate the runtime's bundled sample apps so the landing page
// and Create wizard can offer a "Start from a sample" gallery. Read-only from
// disk; every sample lives under `samples/<slug>/src/`.
// ---------------------------------------------------------------------------

// Runtime root two levels above serverless-portal/, then into samples/.
// In dev: samples live at the runtime root; in the container image they're
// baked in under /app/samples via the Dockerfile.
const SAMPLES_DIR = (() => {
  const candidates = [
    path.resolve(__dirname, '..', '..', '..', '..', 'samples'),
    path.resolve(__dirname, '..', '..', 'samples'),
  ]
  for (const dir of candidates) {
    try {
      if (fs.statSync(dir).isDirectory()) return dir
    } catch { /* not this one */ }
  }
  return candidates[0]
})()

// Skip these when listing agents inside a sample — they exist for infra/dev
// purposes, not agent authoring.
const SAMPLE_IGNORE_DIRS = new Set(['__pycache__', '.venv', '.python_packages', '.azure'])
const SAMPLE_IGNORE_FILES = new Set(['local.settings.json', 'local.settings.template.json'])

// Split a Markdown file into YAML frontmatter + body. Returns `{ front, body }`
// or `{ front: null, body: raw }` when no frontmatter block is present.
function splitFrontmatter(raw) {
  const text = String(raw ?? '')
  const match = /^---\s*\r?\n([\s\S]*?)\r?\n---\s*\r?\n?([\s\S]*)$/.exec(text)
  if (!match) return { front: null, body: text }
  try {
    const parsed = YAML.load(match[1])
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return { front: parsed, body: match[2] ?? '' }
    }
  } catch {
    /* fall through */
  }
  return { front: null, body: text }
}

// Read a sample's primary README title + first paragraph for the tile blurb.
async function readSampleReadme(sampleDir) {
  try {
    const md = await fs.promises.readFile(path.join(sampleDir, 'README.md'), 'utf-8')
    const titleMatch = /^#\s+(.+?)\s*$/m.exec(md)
    const paraMatch = /^\s*\n([^\n#][^\n]*(?:\n[^\n#][^\n]*)*)/m.exec(md.replace(/^#[^\n]*\n?/, ''))
    return {
      title: titleMatch ? titleMatch[1].trim() : '',
      blurb: paraMatch ? paraMatch[1].replace(/\s+/g, ' ').trim().slice(0, 220) : '',
    }
  } catch {
    return { title: '', blurb: '' }
  }
}

// Enumerate the *.agent.md files inside a sample's src/ and agents/ folders,
// returning summary metadata (name, trigger, builtin_endpoints) parsed from
// their frontmatter.
async function collectSampleAgents(srcDir) {
  const results = []
  const paths = []
  try {
    for (const name of await fs.promises.readdir(srcDir)) {
      if (name.toLowerCase().endsWith('.agent.md')) paths.push(name)
    }
  } catch {
    return results
  }
  const agentsDir = path.join(srcDir, 'agents')
  if (await pathExists(agentsDir)) {
    try {
      for (const name of await fs.promises.readdir(agentsDir)) {
        if (name.toLowerCase().endsWith('.agent.md')) paths.push(path.join('agents', name))
      }
    } catch {
      /* ignore */
    }
  }
  for (const rel of paths.sort()) {
    try {
      const raw = await fs.promises.readFile(path.join(srcDir, rel), 'utf-8')
      const { front } = splitFrontmatter(raw)
      const trigger = front && typeof front === 'object' && front.trigger
      results.push({
        file: rel.split(path.sep).join('/'),
        name: (front && typeof front === 'object' && String(front.name || '')) || rel,
        description: (front && typeof front === 'object' && String(front.description || '')) || '',
        triggerType:
          trigger && typeof trigger === 'object' ? String(trigger.type ?? '') : '',
        builtinEndpoints:
          front && typeof front === 'object' && front.builtin_endpoints ? true : false,
      })
    } catch {
      /* skip an unreadable agent file */
    }
  }
  return results
}

// List every source file inside a sample's src/ tree (excluding build output
// and local dev settings) as `[{ path, content }]`. Used by the CreateAgent
// wizard when the user picks a sample to pre-fill.
async function collectSampleFiles(srcDir) {
  const files = []
  async function walk(dir, rel) {
    let entries
    try {
      entries = await fs.promises.readdir(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const entry of entries) {
      if (SAMPLE_IGNORE_DIRS.has(entry.name)) continue
      if (!entry.isDirectory() && SAMPLE_IGNORE_FILES.has(entry.name)) continue
      const abs = path.join(dir, entry.name)
      const childRel = rel ? `${rel}/${entry.name}` : entry.name
      if (entry.isDirectory()) {
        await walk(abs, childRel)
      } else {
        try {
          const content = await fs.promises.readFile(abs, 'utf-8')
          files.push({ path: childRel, content })
        } catch {
          /* skip binary or unreadable file */
        }
      }
    }
  }
  await walk(srcDir, '')
  return files
}

// List every sample with summary metadata. `includeFiles=1` returns full file
// contents (used by the wizard when a user clicks a sample tile).
app.get(
  '/api/samples',
  wrap(async (req, res) => {
    requireToken(req)
    const includeFiles = String(req.query.includeFiles ?? '') === '1'
    let entries
    try {
      entries = await fs.promises.readdir(SAMPLES_DIR, { withFileTypes: true })
    } catch {
      res.json({ samples: [] })
      return
    }
    const samples = []
    for (const entry of entries) {
      if (!entry.isDirectory()) continue
      const dir = path.join(SAMPLES_DIR, entry.name)
      const srcDir = path.join(dir, 'src')
      if (!(await pathExists(srcDir))) continue
      const readme = await readSampleReadme(dir)
      const agents = await collectSampleAgents(srcDir)
      samples.push({
        slug: entry.name,
        title: readme.title || entry.name,
        blurb: readme.blurb,
        agents,
        triggerTypes: [...new Set(agents.map((a) => a.triggerType).filter(Boolean))],
        hasMcp: await pathExists(path.join(srcDir, 'mcp.json')),
        hasSkills: await pathExists(path.join(srcDir, 'skills')),
        hasWorkflow: agents.some((a) => /workflows:/.test(a.description || '')),
        ...(includeFiles ? { files: await collectSampleFiles(srcDir) } : {}),
      })
    }
    samples.sort((a, b) => a.slug.localeCompare(b.slug))
    res.json({ samples })
  }),
)

// ---------------------------------------------------------------------------
// `.agent.md` validation — parse the frontmatter and check the fields against
// the runtime's schema so the editor can surface errors *before* deploy.
// Mirrors `azure_functions_agents/config/validation.py`'s allow/deny rules.
// ---------------------------------------------------------------------------

app.post(
  '/api/validate/agent-md',
  wrap(async (req, res) => {
    requireToken(req)
    const content = req.body?.content
    if (typeof content !== 'string') throw new HttpError(400, 'Request body must be { content: string }.')
    res.json(validateAgentMarkdown(content))
  }),
)

// ---------------------------------------------------------------------------
// Deployment history — every completed deploy job is snapshotted to disk so
// the app-detail page can show past runs, not just the latest one. Snapshots
// are keyed by (subscription, app) and stored under `.data/deploy-history/`.
// ---------------------------------------------------------------------------

const DEPLOY_HISTORY_DIR = path.join(__dirname, '..', '.data', 'deploy-history')

async function recordDeployHistory(subscription, appName, entry) {
  if (!subscription || !appName) return
  try {
    const dir = path.join(DEPLOY_HISTORY_DIR, safeSegment(subscription), safeSegment(appName))
    await fs.promises.mkdir(dir, { recursive: true })
    const stamp = new Date().toISOString().replace(/[:.]/g, '-')
    const file = path.join(dir, `${stamp}-${safeSegment(String(entry.jobId ?? 'unknown'))}.json`)
    await fs.promises.writeFile(file, JSON.stringify(entry, null, 2), 'utf-8')
  } catch {
    /* history is best-effort — never block a deploy */
  }
}

app.get(
  '/api/deploy-history',
  wrap(async (req, res) => {
    requireToken(req)
    const subscription = String(req.query.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const appName = String(req.query.app ?? '').trim()
    if (!appName) throw new HttpError(400, 'app query parameter is required.')
    const dir = path.join(DEPLOY_HISTORY_DIR, safeSegment(subscription), safeSegment(appName))
    const items = []
    let files = []
    try {
      files = (await fs.promises.readdir(dir)).sort().reverse()
    } catch {
      res.json({ app: appName, deploys: [] })
      return
    }
    for (const name of files.slice(0, 40)) {
      try {
        const raw = await fs.promises.readFile(path.join(dir, name), 'utf-8')
        items.push(JSON.parse(raw))
      } catch {
        /* skip a corrupt entry */
      }
    }
    res.json({ app: appName, deploys: items })
  }),
)

// ---------------------------------------------------------------------------
// Create / deploy agent — refresh the target Function App's portal-managed
// source tree, then provision (for a new app) and push it to Azure with a
// remote build. Every Azure call runs as the signed-in user's forwarded token.
// ---------------------------------------------------------------------------

const APP_SOURCES_DIR = path.join(__dirname, '..', '.data', 'app-sources')
const SCAFFOLD_DIR = path.join(__dirname, '..', 'scaffold')

const PORTAL_APP_DATA_ROOTS = {
  agentDrafts: DRAFTS_DIR,
  sourceDrafts: SOURCE_DRAFTS_DIR,
  appSources: APP_SOURCES_DIR,
  deployHistory: DEPLOY_HISTORY_DIR,
}

function appLifecycleHttpError(error) {
  return new HttpError(error?.status ?? 502, String(error?.message ?? error), {
    ...(error?.portalCode ? { error: error.portalCode } : {}),
  })
}

app.post(
  '/api/apps/stop',
  wrap(async (req, res) => {
    const token = requireToken(req)
    try {
      const target = validateAppLifecycleRequest(req.body)
      const result = await azure.stopFunctionApp(token, target)
      res.status(result.pending ? 202 : 200).json(result)
    } catch (error) {
      throw appLifecycleHttpError(error)
    }
  }),
)

app.delete(
  '/api/apps',
  wrap(async (req, res) => {
    const token = requireToken(req)
    try {
      const target = validateAppLifecycleRequest(req.body)
      const result = await azure.deleteFunctionApp(token, target)
      if (result.pending) {
        res.status(202).json(result)
        return
      }
      const cleanupResult = await purgePortalAppData({
        roots: PORTAL_APP_DATA_ROOTS,
        subscription: target.subscription,
        app: target.app,
      })
      if (cleanupResult.failures.length) {
        console.error('[app/delete] portal cleanup incomplete', {
          subscription: target.subscription,
          app: target.app,
          failures: cleanupResult.failures,
        })
      }
      res.status(200).json({ ...result, cleanup: cleanupResult.cleanup })
    } catch (error) {
      throw appLifecycleHttpError(error)
    }
  }),
)

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
    if (e.name === '.transactions' || /\.draft$/.test(e.name)) continue
    const full = path.join(dir, e.name)
    if (e.isDirectory()) out.push(...(await readDirRecursive(full, base)))
    else {
      try {
        out.push({
          name: path.relative(base, full).split(path.sep).join('/'),
          data: await fs.promises.readFile(full),
        })
      } catch (err) {
        if (err?.code !== 'ENOENT') throw err
      }
    }
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
  return github.validateDeployableRepoFiles([...files].map(([name, data]) => ({ name, data })))
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
  await recoverSourceDrafts(sourceDir)
  for (const f of await readDirRecursive(sourceDir)) {
    apply(f.name, f.data.toString('utf-8'))
  }
  return [...byName].map(([name, data]) => ({ name, data }))
}

async function cachePublishedSource(subscription, appName, files) {
  const sourceDir = path.join(APP_SOURCES_DIR, safeSegment(subscription), safeSegment(appName))
  let complete = true
  const expected = new Set()
  for (const file of files) {
    const segments = String(file.name).replace(/\\/g, '/').split('/').filter(Boolean)
    if (!segments.length || segments.includes('..')) continue
    expected.add(segments.join('/'))
    const filePath = path.join(sourceDir, ...segments)
    try {
      await fs.promises.mkdir(path.dirname(filePath), { recursive: true })
      await fs.promises.writeFile(filePath, file.data)
    } catch {
      complete = false
    }
  }
  for (const cached of await readDirRecursive(sourceDir)) {
    if (expected.has(cached.name)) continue
    try {
      await fs.promises.unlink(path.join(sourceDir, ...cached.name.split('/')))
    } catch (err) {
      if (err?.code !== 'ENOENT') complete = false
    }
  }
  return complete
}

// Remove drafts whose exact content was included in a successful deployment.
// A draft changed while deployment was running is intentionally retained.
async function clearPublishedDrafts(subscription, appName, deployedFiles) {
  const byName = new Map(deployedFiles.map((f) => [f.name, f.data]))
  const basenameToName = new Map(deployedFiles.map((f) => [f.name.split('/').pop(), f.name]))
  const failures = []
  const clearIfPublished = async (root, fileName, data) => {
    const target = byName.has(fileName) ? fileName : (basenameToName.get(fileName) ?? fileName)
    const deployed = byName.get(target)
    if (deployed && Buffer.compare(deployed, data) === 0) {
      try {
        await fs.promises.unlink(path.join(root, ...fileName.split('/')))
      } catch (err) {
        if (err?.code !== 'ENOENT') failures.push(fileName)
      }
    }
  }

  const agentDir = path.join(DRAFTS_DIR, safeSegment(subscription), safeSegment(appName))
  for (const file of await listDirFiles(agentDir)) {
    try {
      await clearIfPublished(agentDir, file, await fs.promises.readFile(path.join(agentDir, file)))
    } catch (err) {
      if (err?.code !== 'ENOENT') failures.push(file)
    }
  }
  const sourceDir = path.join(SOURCE_DRAFTS_DIR, safeSegment(subscription), safeSegment(appName))
  for (const file of await readDirRecursive(sourceDir)) {
    await clearIfPublished(sourceDir, file.name, file.data)
  }
  return failures
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
  const validation = validateAgentFiles(files)
  if (!validation.ok) {
    const failure = validation.failures[0]
    throw new Error(`Cannot deploy ${failure.file}: ${failure.errors[0].message}`)
  }
  setJob(id, { message: 'Deploying source with a remote build…' })
  const zip = provision.zipStore(files)
  await provision.deployZipToApp(token, azure.scmHostName(site), zip)
}

async function verifyPreparedApp(token, subscription, resourceGroup, appName, preparationId) {
  const site = await azure.getSite(token, subscription, resourceGroup, appName)
  if (!site?.id) return null
  const settings = await azure.readAppSettingsStrict(token, subscription, resourceGroup, appName)
  const managed = String(settings.AZURE_FUNCTIONS_AGENTS_PORTAL_MANAGED ?? '').toLowerCase() === 'true'
  const matches = String(settings.AZURE_FUNCTIONS_AGENTS_PREPARATION_ID ?? '') === preparationId
  if (!managed || !matches) {
    throw new HttpError(409, `Function App "${appName}" already exists and was not prepared by this New Skill session.`)
  }
  return site
}

async function runPrepareAppJob(id, token, { subscription, target, deploymentName }) {
  const { appName, resourceGroup } = target
  try {
    const existing = await verifyPreparedApp(token, subscription, resourceGroup, appName, target.preparationId)
    if (!existing) {
      const availability = await azure.checkFunctionAppNameAvailable(token, subscription, appName)
      if (!availability.available) {
        throw new Error(availability.message || `Function App name "${appName}" is not available.`)
      }
    }

    setJob(id, { message: 'Preparing the Function App and managed identity…' })
    const provisioned = await provision.provisionFlexApp(token, {
      subscriptionId: subscription,
      resourceGroup,
      appName,
      region: target.region,
      foundryEndpoint: target.foundryEndpoint,
      foundryModel: target.foundryModel,
      preparationId: target.preparationId,
      deploymentName,
    })
    const site = await azure.getSite(token, subscription, resourceGroup, appName)
    if (!site?.id) throw new Error(`Function App "${appName}" was not found after preparation.`)
    const principalId = site.identity?.principalId || provisioned?.principalId || ''

    let grantOutcome
    const foundry = target.foundryAccount
    if (principalId && foundry?.subscription && foundry.resourceGroup && foundry.account) {
      setJob(id, { message: 'Granting the app access to Foundry…' })
      try {
        const result = await azure.grantFoundryAccess(token, {
          subscriptionId: foundry.subscription,
          resourceGroup: foundry.resourceGroup,
          account: foundry.account,
          principalId,
        })
        grantOutcome = result.granted?.length ? (result.failed?.length ? 'partial' : 'granted') : 'failed'
      } catch {
        grantOutcome = 'failed'
      }
    }

    setJob(id, {
      status: 'prepared',
      message: `Function App "${appName}" is ready for tools and connections.`,
      url: `https://${site.defaultHostName}`,
      principalId,
      ...(grantOutcome ? { grantOutcome } : {}),
    })
  } catch (error) {
    setJob(id, { status: 'error', message: String(error?.message ?? error) })
  }
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
    } else if (target.kind === 'prepared') {
      setJob(id, { message: 'Verifying the prepared Function App…' })
      await verifyPreparedApp(token, subscription, resourceGroup, appName, target.preparationId)
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
    if (target.kind !== 'existing') {
      setJob(id, { message: 'Activating the Function App…' })
      await azure.setAppSettings(token, subscription, resourceGroup, appName, {
        AZURE_FUNCTIONS_AGENTS_PORTAL_DEPLOYED: 'true',
      })
    }
    const sourceCached = await cachePublishedSource(subscription, appName, files)
    const unclearedDrafts = sourceCached ? await clearPublishedDrafts(subscription, appName, files) : []

    setJob(id, {
      status: 'deployed',
      message: !sourceCached
        ? `Deployed "${fileName}" to ${appName}, but the local published-source cache could not be updated.`
        : unclearedDrafts.length
        ? `Deployed "${fileName}" to ${appName}, but ${unclearedDrafts.length} local draft(s) could not be cleared.`
        : `Deployed "${fileName}" to ${appName}.`,
      url: `https://${site.defaultHostName}`,
      ...(target.kind !== 'existing' && principalId ? { principalId } : {}),
      ...(grantOutcome ? { grantOutcome } : {}),
    })
    await recordDeployHistory(subscription, appName, {
      jobId: id,
      kind: target.kind === 'existing' ? 'deploy' : 'create',
      status: 'deployed',
      finishedAt: new Date().toISOString(),
      files: files.map((f) => f.name).sort(),
      resourceGroup,
      url: `https://${site.defaultHostName}`,
      fileName,
      ...(grantOutcome ? { grantOutcome } : {}),
    })
  } catch (err) {
    setJob(id, { status: 'error', message: String(err?.message ?? err) })
    await recordDeployHistory(subscription, appName, {
      jobId: id,
      kind: target.kind === 'existing' ? 'deploy' : 'create',
      status: 'error',
      finishedAt: new Date().toISOString(),
      message: String(err?.message ?? err),
      resourceGroup,
      fileName,
    })
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
    const sourceCached = await cachePublishedSource(subscription, appName, files)
    const unclearedDrafts = sourceCached ? await clearPublishedDrafts(subscription, appName, files) : []

    setJob(id, {
      status: 'deployed',
      message: !sourceCached
        ? `Redeployed ${appName}, but the local published-source cache could not be updated.`
        : unclearedDrafts.length
        ? `Redeployed ${appName}, but ${unclearedDrafts.length} local draft(s) could not be cleared.`
        : `Redeployed ${appName} with your saved edits.`,
      url: `https://${site.defaultHostName}`,
    })
    await recordDeployHistory(subscription, appName, {
      jobId: id,
      kind: 'redeploy',
      status: 'deployed',
      finishedAt: new Date().toISOString(),
      files: files.map((f) => f.name).sort(),
      resourceGroup,
      url: `https://${site.defaultHostName}`,
    })
  } catch (err) {
    setJob(id, { status: 'error', message: String(err?.message ?? err) })
    await recordDeployHistory(subscription, appName, {
      jobId: id,
      kind: 'redeploy',
      status: 'error',
      finishedAt: new Date().toISOString(),
      message: String(err?.message ?? err),
      resourceGroup,
    })
  }
}

// Prepare a new Function App and managed identity without deploying source, so
// the New Skill wizard can configure live tools and connections before review.
app.post(
  '/api/prepare-app',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const target = req.body?.target
    if (!target || target.kind !== 'new') throw new HttpError(400, 'A new app target is required.')
    const appName = String(target.appName ?? '').trim()
    const resourceGroup = String(target.resourceGroup ?? '').trim()
    const preparationId = String(target.preparationId ?? '').trim()
    if (!appName || !resourceGroup || !target.region) {
      throw new HttpError(400, 'App name, resource group, and region are required.')
    }
    if (!/^[A-Za-z0-9-]{16,80}$/.test(preparationId)) {
      throw new HttpError(400, 'A valid preparation ID is required.')
    }

    pruneJobs()
    const jobId = randomUUID()
    const deploymentName = `portal-prepare-${Date.now()}`.slice(0, 64)
    const portalUrl = portalDeploymentUrl(tenantFromToken(token), subscription, resourceGroup, deploymentName)
    setJob(jobId, { kind: 'prepare', status: 'running', message: 'Starting app preparation…', files: [], portalUrl })
    runPrepareAppJob(jobId, token, {
      subscription,
      target: { ...target, appName, resourceGroup, preparationId },
      deploymentName,
    })
    res.status(202).json({ jobId, status: 'running', files: [], portalUrl })
  }),
)

app.get(
  '/api/prepare-app/:jobId',
  wrap(async (req, res) => {
    requireToken(req)
    const job = deployJobs.get(req.params.jobId)
    if (!job || job.kind !== 'prepare') throw new HttpError(404, 'Unknown or expired preparation job.')
    res.json({
      status: job.status,
      message: job.message,
      ...(job.url ? { url: job.url } : {}),
      ...(job.portalUrl ? { portalUrl: job.portalUrl } : {}),
      ...(job.principalId ? { principalId: job.principalId } : {}),
      ...(job.grantOutcome ? { grantOutcome: job.grantOutcome } : {}),
    })
  }),
)

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
    const validation = validateAgentMarkdown(agent.content)
    if (!validation.ok) {
      throw new HttpError(400, `Agent source is invalid: ${validation.errors[0].message}`, {
        errors: validation.errors,
      })
    }
    if (!target || typeof target.kind !== 'string') {
      throw new HttpError(400, 'Request body must include a target.')
    }
    if (!['existing', 'new', 'prepared'].includes(target.kind)) {
      throw new HttpError(400, 'Target kind must be existing, new, or prepared.')
    }
    const appName = target.kind === 'existing' ? target.app : target.appName
    if (!appName) throw new HttpError(400, 'A target Function App name is required.')
    const resourceGroup = target.resourceGroup
    if (!resourceGroup) throw new HttpError(400, 'A target resource group is required.')
    if (target.kind === 'new' && !target.region) {
      throw new HttpError(400, 'A region is required to create a new app.')
    }
    if (target.kind === 'prepared' && !/^[A-Za-z0-9-]{16,80}$/.test(String(target.preparationId ?? ''))) {
      throw new HttpError(400, 'A valid preparation ID is required for a prepared app.')
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
    try {
      res.json(await azure.discoverFoundry(token, subscriptionId))
    } catch (error) {
      throw new HttpError(error?.status ?? 502, String(error?.message ?? error), {
        ...(error?.portalCode ? { error: error.portalCode } : {}),
      })
    }
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

// Generate the code/config for an agent capability. Custom tools use the
// selected Function App's model configuration rather than a client target.
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
      if (kind === 'custom_tool') {
        const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
        if (!appName || !resourceGroup) {
          throw new HttpError(400, 'app and resourceGroup are required for custom-tool generation.')
        }
        return res.json(
          await azure.generateCapabilityCodeForApp(token, subscription, {
            resourceGroup,
            appName,
            kind,
            name,
            description,
            triggerType,
            skillsContext,
            guidance,
          }),
        )
      }
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
      if (err instanceof HttpError) throw err
      throw customToolHttpError(err)
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

// One-click "Grant access" for a running app — resolves the app's principalId
// and Foundry account from its app settings and grants the two roles it needs
// to call the model. Powers the Playground's self-heal button.
app.post(
  '/api/foundry/heal-access',
  wrap(async (req, res) => {
    const token = requireToken(req)
    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const appName = String(req.body?.app ?? '').trim()
    if (!resourceGroup || !appName) throw new HttpError(400, 'app and resourceGroup are required.')
    try {
      res.json(await azure.healFoundryAccess(token, subscription, resourceGroup, appName))
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
    const entry = await activeGithubEntry(req, res, oid)
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
    const callbackUrl = String(req.body?.callbackUrl ?? '').trim()
    res.json({ authorizeUrl: github.authorizeUrl(oid, callbackUrl) })
  }),
)

// Local development can reuse an authenticated GitHub CLI session instead of
// requiring a localhost callback on the GitHub OAuth application. The token is
// read only by the backend and immediately sealed into the same HttpOnly,
// ARM-user-bound cookie as the production OAuth flow.
app.post(
  '/api/github/local-session',
  wrap(async (req, res) => {
    if (!isLocalDevelopmentRequest(req)) throw new HttpError(404, 'Not found.')
    const token = requireToken(req)
    const { oid } = azure.getSignedInIdentity(token)
    if (!github.githubConfig().stateSecret) {
      throw new HttpError(501, 'GITHUB_OAUTH_STATE_SECRET is required for a local GitHub session.')
    }
    try {
      const session = await github.getLocalCliSession()
      setGithubSessionCookie(req, res, oid, session)
      res.json({ configured: true, connected: true, login: session.login, avatarUrl: session.avatarUrl })
    } catch (error) {
      throw new HttpError(error.status ?? 502, String(error?.message ?? error))
    }
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
      const credentials = await github.exchangeCode(code, state.callback)
      const user = await github.getUser(credentials.token)
      const session = {
        ...credentials,
        login: user.login,
        avatarUrl: user.avatarUrl,
        validatedAt: Date.now(),
      }
      setGithubSessionCookie(req, res, state.oid, session)
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
    azure.getSignedInIdentity(token)
    res.clearCookie(github.GITHUB_SESSION_COOKIE, githubCookieOptions(req))
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
    const entry = await activeGithubEntry(req, res, oid, { forceValidate: true })
    if (!entry) throw expiredGithubSession(req, res)
    try {
      res.json({ repos: await github.listRepos(entry.token) })
    } catch (err) {
      throw githubOperationHttpError(req, res, err)
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
    const entry = await activeGithubEntry(req, res, oid, { forceValidate: true })
    if (!entry) throw expiredGithubSession(req, res)

    const subscription = String(req.body?.subscription ?? '').trim() || azure.DEFAULT_SUBSCRIPTION_ID
    const resourceGroup = String(req.body?.resourceGroup ?? '').trim()
    const appName = String(req.body?.app ?? '').trim()
    const mode = String(req.body?.mode ?? 'new')
    const publishMode = String(req.body?.publishMode ?? 'pr')
    if (!resourceGroup || !appName) throw new HttpError(400, 'resourceGroup and app are required.')
    if (!['pr', 'direct'].includes(publishMode)) {
      throw new HttpError(400, 'publishMode must be "pr" or "direct".')
    }
    const requiredPermissions = {
      contents: 'write',
      ...(publishMode === 'pr' ? { pull_requests: 'write' } : {}),
      ...(mode === 'new' ? { administration: 'write' } : {}),
    }
    let githubOwner = entry.login
    let githubStep = 'access repository'

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
        githubOwner = owner
        githubStep = 'access existing repository'
        repo = await github.getRepo(entry.token, owner, name)
        const requestedBranch = String(req.body?.branch ?? '').trim()
        if (requestedBranch) repo.defaultBranch = requestedBranch
      } else {
        const name = safeSegment(String(req.body?.repoName ?? appName).trim() || appName)
        const priv = req.body?.private !== false
        const org = String(req.body?.org ?? '').trim()
        githubOwner = org || entry.login
        githubStep = 'create repository'
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

      const base = repo.defaultBranch || 'main'
      let branch = base
      let pr = null
      let commitSha
      if (publishMode === 'direct') {
        githubStep = 'push repository contents'
        const pushed = await github.pushFiles(entry.token, {
          owner: repo.owner,
          repo: repo.name,
          branch: base,
          files,
          message: `Update agent "${appName}" (via AI Apps)`,
        })
        commitSha = pushed.commitSha
      } else {
        githubStep = 'write a branch and open a pull request'
        // Reuse one rolling PR per app; start a fresh branch after it is merged.
        const seg = (s) =>
          String(s)
            .replace(/[^A-Za-z0-9._-]/g, '-')
            .replace(/^-+|-+$/g, '') || 'x'
        const prefix = `agents/${seg(entry.login)}/${seg(appName)}`
        branch = await github.resolveRollingBranch(entry.token, repo.owner, repo.name, base, prefix)
        pr = await github.openPullRequest(entry.token, {
          owner: repo.owner,
          repo: repo.name,
          base,
          head: branch,
          files,
          message: `Update agent "${appName}" (via AI Apps)`,
          title: `Agent "${appName}" via AI Apps`,
          body: `Opened by AI Apps on behalf of @${entry.login}.\n\nAdds/updates the source for agent app \`${appName}\` on branch \`${branch}\`. Edits roll into this PR until it's merged.`,
        })
      }

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
        publishMode,
        base,
        branch,
        ...(commitSha ? { commitSha } : {}),
        ...(pr ? { prUrl: pr.prUrl, prNumber: pr.prNumber } : {}),
        stored,
        deploymentCenter,
        pushed: files.map((f) => f.name).sort(),
      })
    } catch (err) {
      if (err instanceof HttpError) throw err
      console.error('[github/connect] FAILED:', err?.status ?? '', String(err?.message ?? err))
      if (Number(err?.status ?? err?.statusCode) === 403) {
        let diagnostic = null
        let application = null
        try {
          diagnostic = await github.inspectAppInstallation(entry.token, {
            owner: githubOwner,
            requiredPermissions,
          })
          if (!diagnostic.installationFound) {
            application = await github.inspectAuthorizedApplication(entry.token)
          }
        } catch (diagnosticError) {
          console.error('[github/connect] installation inspection failed:', String(diagnosticError?.message ?? diagnosticError))
        }

        let detail = `GitHub denied permission to ${githubStep}.`
        if (diagnostic && !diagnostic.installationFound) {
          detail += ` Install ${application?.appName || 'the GitHub App'} for ${githubOwner}, then reconnect GitHub.`
        } else if (diagnostic?.missingPermissions?.length) {
          detail += ` Approve ${diagnostic.missingPermissions.join(', ')} for the GitHub App installation, then reconnect GitHub.`
        } else if (mode === 'new' && diagnostic?.repositorySelection === 'selected') {
          detail += ' The GitHub App is limited to selected repositories. Choose All repositories, or create and select the repository in GitHub first.'
        } else {
          detail += ' The installation reports the required permissions; check whether an account or organization policy blocks this operation.'
        }
        const metadata = {
          error: 'github_permission_denied',
          githubStep,
          githubOwner,
          ...(diagnostic
            ? {
                installationFound: diagnostic.installationFound,
                repositorySelection: diagnostic.repositorySelection,
                missingPermissions: diagnostic.missingPermissions,
                settingsUrl: application?.installUrl || diagnostic.settingsUrl,
                ...(application?.appName ? { appName: application.appName } : {}),
              }
            : {}),
        }
        console.error('[github/connect] access diagnostic:', JSON.stringify(metadata))
        throw new HttpError(403, detail, metadata)
      }
      throw githubOperationHttpError(req, res, err)
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
    const entry = await activeGithubEntry(req, res, oid, { forceValidate: true })
    if (!entry) throw expiredGithubSession(req, res)

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
        const existingWorkflow = await github.readRepoFile(
          entry.token,
          owner,
          name,
          '.github/workflows/deploy.yml',
          branch,
        )
        const needsWorkflow = github.ensureWorkflowCanBeWritten(existingWorkflow, workflow)
        if (needsWorkflow) {
          await github.putRepoContent(
            entry.token,
            owner,
            name,
            '.github/workflows/deploy.yml',
            Buffer.from(workflow, 'utf-8'),
            'Add Azure Functions deploy workflow (AI Apps)',
            branch,
          )
        }
        steps.workflow = needsWorkflow ? 'created' : 'already configured'
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
      if (Number(err?.status ?? err?.statusCode) === 401) throw expiredGithubSession(req, res)
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
    return res.status(err.status).json({ detail: err.detail, ...err.metadata })
  }
  console.error(err)
  res.status(500).json({ detail: 'Internal server error' })
})

app.listen(PORT, () => {
  console.log(`Serverless Agent Portal backend listening on http://127.0.0.1:${PORT}`)
})
