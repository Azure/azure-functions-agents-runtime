// Serverless Agent Portal — GitHub connection (OAuth App + repo push).
//
// Phase 1: after an agent is created, the user connects a GitHub account via an
// OAuth App (browser sign-in). The portal then creates a new repo (or uses an
// existing one), pushes the app's source, and records the repo link on the
// Function App. Later phases open pull requests for edits.
//
// The raw OAuth token never reaches browser JavaScript. `authorizeUrl()` embeds
// a signed `state` bound to the portal user's `oid`; the callback encrypts the
// GitHub identity into an HttpOnly cookie that every API request re-binds to the
// caller's ARM identity. A shared state secret keeps this stateless across
// replicas and revisions.

import crypto from 'node:crypto'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const GH_API = 'https://api.github.com'
const GH_OAUTH = 'https://github.com/login/oauth'
const SCOPE = 'repo read:user'
const UA = 'serverless-agent-portal'
const execFileAsync = promisify(execFile)

// --- config ----------------------------------------------------------------

export function githubConfig() {
  return {
    clientId: process.env.GITHUB_OAUTH_CLIENT_ID || '',
    clientSecret: process.env.GITHUB_OAUTH_CLIENT_SECRET || '',
    // Empty by default: when unset, the portal omits redirect_uri so GitHub
    // uses the app's own registered Callback URL (simplest for GitHub Apps).
    callback: process.env.GITHUB_OAUTH_CALLBACK || '',
    stateSecret: process.env.GITHUB_OAUTH_STATE_SECRET || '',
  }
}

export function isConfigured() {
  const c = githubConfig()
  return Boolean(c.clientId && c.clientSecret && c.stateSecret)
}

// --- signed state (CSRF + user binding, stateless) -------------------------

// OAuth is reported as unconfigured unless this value is provided. The random
// fallback exists only so the pure helpers remain usable in isolated tests.
const STATE_SECRET = process.env.GITHUB_OAUTH_STATE_SECRET || crypto.randomBytes(32).toString('hex')
const STATE_TTL_MS = 10 * 60 * 1000
const SESSION_TTL_MS = 30 * 24 * 60 * 60 * 1000
const TOKEN_REFRESH_WINDOW_MS = 5 * 60 * 1000
const SESSION_VALIDATION_INTERVAL_MS = 5 * 60 * 1000
const SESSION_KEY = crypto.createHmac('sha256', STATE_SECRET).update('github-session-v1').digest()
export const GITHUB_SESSION_COOKIE = 'serverless-portal-github'
export const GITHUB_SESSION_MAX_AGE_MS = SESSION_TTL_MS

function normalizeLocalCallback(value) {
  if (!value) return ''
  try {
    const url = new URL(String(value))
    const localHost = url.hostname === 'localhost' || url.hostname === '127.0.0.1'
    if (!localHost || url.protocol !== 'http:' || url.pathname !== '/api/github/callback') return ''
    if (url.username || url.password || url.search || url.hash) return ''
    return `${url.origin}${url.pathname}`
  } catch {
    return ''
  }
}

function makeState(oid, callback = '') {
  const payload = Buffer.from(
    JSON.stringify({ oid, callback, n: crypto.randomBytes(8).toString('hex'), t: Date.now() }),
  ).toString('base64url')
  const sig = crypto.createHmac('sha256', STATE_SECRET).update(payload).digest('base64url')
  return `${payload}.${sig}`
}

export function readState(state) {
  const [payload, sig] = String(state || '').split('.')
  if (!payload || !sig) return null
  const expected = crypto.createHmac('sha256', STATE_SECRET).update(payload).digest('base64url')
  const a = Buffer.from(sig)
  const b = Buffer.from(expected)
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null
  let obj
  try {
    obj = JSON.parse(Buffer.from(payload, 'base64url').toString('utf-8'))
  } catch {
    return null
  }
  if (!obj?.oid || Date.now() - Number(obj.t || 0) > STATE_TTL_MS) return null
  return obj
}

export function sealSession(oid, {
  token,
  login,
  avatarUrl = '',
  refreshToken = '',
  expiresAt = 0,
  refreshExpiresAt = 0,
  validatedAt = 0,
}) {
  const iv = crypto.randomBytes(12)
  const cipher = crypto.createCipheriv('aes-256-gcm', SESSION_KEY, iv)
  const plaintext = Buffer.from(JSON.stringify({
    oid,
    token,
    login,
    avatarUrl,
    t: Date.now(),
    ...(refreshToken ? { refreshToken } : {}),
    ...(expiresAt ? { expiresAt } : {}),
    ...(refreshExpiresAt ? { refreshExpiresAt } : {}),
    ...(validatedAt ? { validatedAt } : {}),
  }), 'utf-8')
  const encrypted = Buffer.concat([cipher.update(plaintext), cipher.final()])
  return ['v1', iv.toString('base64url'), encrypted.toString('base64url'), cipher.getAuthTag().toString('base64url')].join('.')
}

export function readSession(value, expectedOid) {
  const [version, iv, encrypted, tag] = String(value || '').split('.')
  if (version !== 'v1' || !iv || !encrypted || !tag || !expectedOid) return null
  try {
    const decipher = crypto.createDecipheriv('aes-256-gcm', SESSION_KEY, Buffer.from(iv, 'base64url'))
    decipher.setAuthTag(Buffer.from(tag, 'base64url'))
    const plaintext = Buffer.concat([
      decipher.update(Buffer.from(encrypted, 'base64url')),
      decipher.final(),
    ]).toString('utf-8')
    const session = JSON.parse(plaintext)
    if (
      session?.oid !== expectedOid ||
      !session?.token ||
      !session?.login ||
      Date.now() - Number(session?.t || 0) > SESSION_TTL_MS
    ) {
      return null
    }
    return {
      token: session.token,
      login: session.login,
      avatarUrl: session.avatarUrl || '',
      ...(session.refreshToken ? { refreshToken: session.refreshToken } : {}),
      ...(Number(session.expiresAt) ? { expiresAt: Number(session.expiresAt) } : {}),
      ...(Number(session.refreshExpiresAt) ? { refreshExpiresAt: Number(session.refreshExpiresAt) } : {}),
      ...(Number(session.validatedAt) ? { validatedAt: Number(session.validatedAt) } : {}),
    }
  } catch {
    return null
  }
}

// --- OAuth flow ------------------------------------------------------------

export function authorizeUrl(oid, requestedCallback = '') {
  const c = githubConfig()
  const callback = c.callback || normalizeLocalCallback(requestedCallback)
  const params = new URLSearchParams({
    client_id: c.clientId,
    scope: SCOPE,
    state: makeState(oid, callback),
    allow_signup: 'false',
  })
  // Only pin redirect_uri when explicitly configured. Omitting it lets GitHub
  // fall back to the app's registered Callback URL, avoiding the strict
  // "redirect_uri is not associated with this application" match (GitHub Apps
  // require an EXACT match to a registered Callback URL).
  if (callback) params.set('redirect_uri', callback)
  return `${GH_OAUTH}/authorize?${params.toString()}`
}

function githubSessionError(message) {
  return Object.assign(new Error(message), { status: 401, portalCode: 'github_session_expired' })
}

function tokenSession(json, now) {
  if (!json?.access_token) {
    throw githubSessionError(json?.error_description || json?.error || 'GitHub token exchange failed.')
  }
  const expiresIn = Number(json.expires_in ?? 0)
  const refreshExpiresIn = Number(json.refresh_token_expires_in ?? 0)
  return {
    token: String(json.access_token),
    ...(expiresIn > 0 ? { expiresAt: now + expiresIn * 1000 } : {}),
    ...(json.refresh_token ? { refreshToken: String(json.refresh_token) } : {}),
    ...(refreshExpiresIn > 0 ? { refreshExpiresAt: now + refreshExpiresIn * 1000 } : {}),
  }
}

async function exchangeToken(payload, { fetchImpl = fetch, now = Date.now() } = {}) {
  const c = githubConfig()
  const res = await fetchImpl(`${GH_OAUTH}/access_token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'User-Agent': UA },
    body: JSON.stringify({
      client_id: c.clientId,
      client_secret: c.clientSecret,
      ...payload,
    }),
    signal: AbortSignal.timeout(15000),
  })
  const json = await res.json().catch(() => ({}))
  if (!res.ok) {
    const message = json.error_description || json.error || 'GitHub token exchange failed.'
    if (res.status === 400 || res.status === 401) throw githubSessionError(message)
    throw Object.assign(new Error(message), { status: res.status >= 500 ? 502 : res.status })
  }
  return tokenSession(json, now)
}

export async function exchangeCode(code, callback = '', options = {}) {
  return exchangeToken({ code, ...(callback ? { redirect_uri: callback } : {}) }, options)
}

async function refreshUserAccessToken(refreshToken, options = {}) {
  return exchangeToken({ grant_type: 'refresh_token', refresh_token: refreshToken }, options)
}

export async function ensureUserSession(
  session,
  { fetchImpl = fetch, now = Date.now(), forceValidate = false } = {},
) {
  if (!session?.token) throw githubSessionError('GitHub sign-in is required.')
  const expiresAt = Number(session.expiresAt ?? 0)
  if (expiresAt && expiresAt <= now + TOKEN_REFRESH_WINDOW_MS) {
    const refreshToken = String(session.refreshToken ?? '')
    const refreshExpiresAt = Number(session.refreshExpiresAt ?? 0)
    if (!refreshToken || (refreshExpiresAt && refreshExpiresAt <= now)) {
      throw githubSessionError('Your GitHub session expired. Connect GitHub again.')
    }
    const refreshed = await refreshUserAccessToken(refreshToken, { fetchImpl, now })
    return {
      changed: true,
      session: {
        ...session,
        ...refreshed,
        validatedAt: now,
      },
    }
  }

  const validatedAt = Number(session.validatedAt ?? 0)
  if (!forceValidate && validatedAt && now - validatedAt < SESSION_VALIDATION_INTERVAL_MS) {
    return { changed: false, session }
  }
  try {
    const user = await getUser(session.token, fetchImpl)
    return {
      changed: true,
      session: {
        ...session,
        login: user.login,
        avatarUrl: user.avatarUrl,
        validatedAt: now,
      },
    }
  } catch (error) {
    if (Number(error?.status) === 401) {
      throw githubSessionError('Your GitHub session is no longer valid. Connect GitHub again.')
    }
    throw error
  }
}

// Minimal HTML returned to the OAuth popup: nudge the opener to refresh, then
// close. No secret is transmitted; the opener re-checks status server-side.
export function closePage(message, ok) {
  const safe = String(message).replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' })[c])
  return `<!doctype html><meta charset="utf-8"><body style="font:14px system-ui;padding:24px;color:#111">
<p>${safe}</p>
<script>
  try { if (window.opener) window.opener.postMessage({ type: 'github-oauth', ok: ${ok ? 'true' : 'false'} }, '*') } catch (e) {}
  setTimeout(function () { window.close() }, ${ok ? 700 : 2500})
</script></body>`
}

// --- GitHub REST helpers ---------------------------------------------------

async function gh(token, apiPath, { method = 'GET', body, fetchImpl = fetch } = {}) {
  const res = await fetchImpl(`${GH_API}${apiPath}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': UA,
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(30000),
  })
  const text = await res.text()
  let json
  try {
    json = text ? JSON.parse(text) : {}
  } catch {
    json = { raw: text }
  }
  if (!res.ok) {
    // Surface GitHub's per-field validation detail (the errors[] array), which
    // carries the real reason behind generic messages like "Repository creation
    // failed." or "Validation Failed".
    const detail = Array.isArray(json?.errors)
      ? json.errors
          .map((e) => e?.message || [e?.resource, e?.field, e?.code].filter(Boolean).join(' '))
          .filter(Boolean)
          .join('; ')
      : ''
    const msg = [json?.message || `${res.status} ${res.statusText}`, detail].filter(Boolean).join(' — ')
    const err = new Error(String(msg).slice(0, 600))
    err.status = res.status
    throw err
  }
  return json
}

export async function getUser(token, fetchImpl = fetch) {
  const u = await gh(token, '/user', { fetchImpl })
  return { login: u.login, avatarUrl: u.avatar_url, name: u.name || u.login }
}

export async function getLocalCliSession() {
  try {
    const { stdout } = await execFileAsync('gh', ['auth', 'token', '--hostname', 'github.com'], {
      timeout: 10_000,
      windowsHide: true,
      maxBuffer: 16 * 1024,
    })
    const token = stdout.trim()
    if (!token) throw new Error('GitHub CLI returned an empty token.')
    const user = await getUser(token)
    return { token, login: user.login, avatarUrl: user.avatarUrl, validatedAt: Date.now() }
  } catch (cause) {
    const error = new Error('GitHub CLI is not authenticated. Run "gh auth login --hostname github.com", then try again.')
    error.status = 401
    error.cause = cause
    throw error
  }
}

export async function listRepos(token) {
  const repos = await gh(
    token,
    '/user/repos?per_page=100&sort=updated&affiliation=owner,organization_member',
  )
  return (Array.isArray(repos) ? repos : []).map((r) => ({
    fullName: r.full_name,
    name: r.name,
    owner: r.owner?.login || '',
    private: Boolean(r.private),
    defaultBranch: r.default_branch || 'main',
    htmlUrl: r.html_url,
  }))
}

const GITHUB_PERMISSION_LABELS = {
  administration: 'Administration',
  contents: 'Contents',
  pull_requests: 'Pull requests',
  workflows: 'Workflows',
}

function permissionIncludes(actual, required) {
  const levels = { none: 0, read: 1, write: 2, admin: 3 }
  return (levels[String(actual || 'none')] ?? 0) >= (levels[String(required || 'none')] ?? 0)
}

export async function inspectAppInstallation(
  token,
  { owner = '', requiredPermissions = {}, fetchImpl = fetch } = {},
) {
  const payload = await gh(token, '/user/installations?per_page=100', { fetchImpl })
  const installations = Array.isArray(payload?.installations) ? payload.installations : []
  const ownerKey = String(owner).toLowerCase()
  const installation = installations.find(
    (candidate) => String(candidate?.account?.login || '').toLowerCase() === ownerKey,
  )
  const missingPermissions = Object.entries(requiredPermissions)
    .filter(([permission, access]) => !permissionIncludes(installation?.permissions?.[permission], access))
    .map(([permission, access]) => `${GITHUB_PERMISSION_LABELS[permission] || permission}: ${access}`)

  return {
    installationFound: Boolean(installation),
    repositorySelection: installation?.repository_selection || '',
    missingPermissions,
    settingsUrl: installation?.html_url || 'https://github.com/settings/installations',
  }
}

export async function inspectAuthorizedApplication(
  token,
  {
    fetchImpl = fetch,
    clientId = githubConfig().clientId,
    clientSecret = githubConfig().clientSecret,
  } = {},
) {
  if (!token || !clientId || !clientSecret) return null
  const res = await fetchImpl(`${GH_API}/applications/${encodeURIComponent(clientId)}/token`, {
    method: 'POST',
    headers: {
      Authorization: `Basic ${Buffer.from(`${clientId}:${clientSecret}`, 'utf-8').toString('base64')}`,
      Accept: 'application/vnd.github+json',
      'Content-Type': 'application/json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': UA,
    },
    body: JSON.stringify({ access_token: token }),
    signal: AbortSignal.timeout(15000),
  })
  if (!res.ok) return null
  const payload = await res.json().catch(() => ({}))
  const appName = String(payload?.app?.name || '').trim()
  let installUrl = ''
  try {
    const apiUrl = new URL(String(payload?.app?.url || ''))
    const match = apiUrl.hostname === 'api.github.com' ? /^\/apps\/([A-Za-z0-9-]+)$/.exec(apiUrl.pathname) : null
    if (match) installUrl = `https://github.com/apps/${match[1]}/installations/new`
  } catch {
    /* leave the install URL empty */
  }
  return { appName, installUrl }
}

export async function createRepo(token, { name, private: priv = true, org = '' }) {
  const apiPath = org ? `/orgs/${encodeURIComponent(org)}/repos` : '/user/repos'
  const r = await gh(token, apiPath, {
    method: 'POST',
    body: {
      name,
      private: priv,
      // Seed the default branch (a README commit) so we can immediately open a
      // PR against it — the portal always contributes via a PR, never a direct
      // push to the default branch.
      auto_init: true,
      description: 'Serverless agent app — managed by AI Apps',
    },
  })
  return {
    fullName: r.full_name,
    owner: r.owner?.login || '',
    name: r.name,
    defaultBranch: r.default_branch || 'main',
    htmlUrl: r.html_url,
    private: Boolean(r.private),
  }
}

// Fetch a single repo (used to reuse an already-existing repo on retry).
export async function getRepo(token, owner, name) {
  const r = await gh(token, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`)
  return {
    fullName: r.full_name,
    owner: r.owner?.login || owner,
    name: r.name,
    defaultBranch: r.default_branch || 'main',
    htmlUrl: r.html_url,
    private: Boolean(r.private),
  }
}

// Read a file's decoded text content from a repo via the Contents API. Returns
// null when the file/repo/ref isn't found or can't be read (never throws).
export async function readRepoFile(token, owner, repo, filePath, ref) {
  const b = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`
  const p = String(filePath)
    .split('/')
    .map(encodeURIComponent)
    .join('/')
  const q = ref ? `?ref=${encodeURIComponent(ref)}` : ''
  try {
    const meta = await gh(token, `${b}/contents/${p}${q}`)
    if (meta && typeof meta.content === 'string' && (meta.encoding ?? 'base64') === 'base64') {
      return Buffer.from(meta.content, 'base64').toString('utf-8')
    }
    return null
  } catch {
    return null
  }
}

export function validateDeployableRepoFiles(files) {
  const names = new Set(files.map((file) => String(file.name).replace(/\\/g, '/')))
  const required = [
    'azure.yaml',
    'README.md',
    '.gitignore',
    'infra/main.bicep',
    'infra/main.parameters.json',
    'src/function_app.py',
    'src/host.json',
    'src/requirements.txt',
  ]
  const missing = required.filter((name) => !names.has(name))
  if (![...names].some((name) => /^src\/.+\.agent\.md$/i.test(name))) {
    missing.push('src/*.agent.md')
  }
  if (missing.length) {
    const error = new Error(`The repository export is incomplete: missing ${missing.join(', ')}.`)
    error.status = 422
    throw error
  }
  return files
}

export function ensureWorkflowCanBeWritten(existing, desired) {
  if (existing == null || existing.replace(/\r\n/g, '\n').trim() === desired.replace(/\r\n/g, '\n').trim()) {
    return existing == null
  }
  const error = new Error(
    'A different .github/workflows/deploy.yml already exists. Rename or remove it before setting up AI Apps deployment.',
  )
  error.status = 409
  throw error
}

// Rolling-branch resolution (one PR per app until merged): reuse the branch of an
// OPEN PR under `${prefix}/` so edits accumulate into a single PR; start a fresh
// timestamped branch when there's no open PR (first connect, or the last PR was
// merged/closed).
export async function resolveRollingBranch(token, owner, repo, base, prefix) {
  const b = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`
  try {
    const open = await gh(token, `${b}/pulls?state=open&base=${encodeURIComponent(base)}&per_page=100`)
    const match = (Array.isArray(open) ? open : []).find((p) => {
      const ref = String(p.head?.ref || '')
      // Reuse this app's rolling PR: a stamped/suffixed branch, or a legacy branch
      // named exactly for the prefix (created before the stamped scheme).
      return ref === prefix || ref.startsWith(`${prefix}/`) || ref.startsWith(`${prefix}-`)
    })
    if (match?.head?.ref) return match.head.ref
  } catch {
    /* fall through to a fresh branch */
  }
  // Separate the stamp with a hyphen (not a slash) so the branch never collides
  // with a branch named exactly `${prefix}`: Git forbids a ref being both a leaf
  // and a directory, which otherwise surfaces as 422 "Reference update failed".
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-T:]/g, '')
  return `${prefix}-${stamp}`
}

// Create or update a single file on a branch (upsert via the Contents API).
export async function putRepoContent(token, owner, repo, filePath, contentBuffer, message, branch) {
  const b = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`
  const p = String(filePath)
    .split('/')
    .map(encodeURIComponent)
    .join('/')
  let sha
  try {
    const existing = await gh(token, `${b}/contents/${p}?ref=${encodeURIComponent(branch)}`)
    sha = existing?.sha
  } catch (e) {
    if (e.status !== 404) throw e // 404 = file doesn't exist yet (create)
  }
  return gh(token, `${b}/contents/${p}`, {
    method: 'PUT',
    body: {
      message,
      content: Buffer.from(contentBuffer).toString('base64'),
      branch,
      ...(sha ? { sha } : {}),
    },
  })
}

// Upsert an Actions repository *variable* (plain, non-secret — no encryption).
export async function setRepoVariable(token, owner, repo, name, value) {
  const b = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/actions/variables`
  try {
    await gh(token, b, { method: 'POST', body: { name, value: String(value) } })
  } catch (e) {
    if (e.status !== 409) throw e // 409 = variable already exists (update it)
    await gh(token, `${b}/${encodeURIComponent(name)}`, { method: 'PATCH', body: { name, value: String(value) } })
  }
}

// Build a GitHub Actions workflow that deploys the function under `packagePath`
// to a Flex Consumption Function App using OIDC (azure/login + functions-action).
export function functionsWorkflowYaml({ appName, branch, packagePath = 'src', pythonVersion = '3.13' }) {
  return `name: Deploy to Azure Functions

on:
  push:
    branches: ["${branch}"]
  workflow_dispatch:

permissions:
  id-token: write
  contents: read

env:
  AZURE_FUNCTIONAPP_NAME: "${appName}"
  PACKAGE_PATH: "${packagePath}"
  PYTHON_VERSION: "${pythonVersion}"

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: \${{ env.PYTHON_VERSION }}

      - name: Azure login (OIDC)
        uses: azure/login@v2
        with:
          client-id: \${{ vars.AZURE_CLIENT_ID }}
          tenant-id: \${{ vars.AZURE_TENANT_ID }}
          subscription-id: \${{ vars.AZURE_SUBSCRIPTION_ID }}

      - name: Deploy to Azure Functions (Flex Consumption)
        uses: Azure/functions-action@v1
        with:
          app-name: \${{ env.AZURE_FUNCTIONAPP_NAME }}
          package: \${{ env.PACKAGE_PATH }}
          sku: flexconsumption
          remote-build: true
`
}

// Push a set of { name, data } text files as a single commit. Handles both an
// empty repo (first commit: no base tree / no parent, then create the ref) and
// an existing branch (add a commit on top, then fast-forward the ref).
export async function pushFiles(token, { owner, repo, branch, files, message }) {
  const base = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`

  const treeItems = []
  for (const f of files) {
    const blob = await gh(token, `${base}/git/blobs`, {
      method: 'POST',
      body: { content: Buffer.from(f.data, 'utf-8').toString('base64'), encoding: 'base64' },
    })
    treeItems.push({ path: f.name, mode: '100644', type: 'blob', sha: blob.sha })
  }

  let parentSha = null
  let baseTreeSha
  try {
    const ref = await gh(token, `${base}/git/ref/heads/${encodeURIComponent(branch)}`)
    parentSha = ref.object.sha
    const parentCommit = await gh(token, `${base}/git/commits/${parentSha}`)
    baseTreeSha = parentCommit.tree.sha
  } catch (e) {
    if (e.status !== 404 && e.status !== 409) throw e // 404/409 = empty repo / no branch yet
  }

  const tree = await gh(token, `${base}/git/trees`, {
    method: 'POST',
    body: { ...(baseTreeSha ? { base_tree: baseTreeSha } : {}), tree: treeItems },
  })
  const commit = await gh(token, `${base}/git/commits`, {
    method: 'POST',
    body: { message, tree: tree.sha, ...(parentSha ? { parents: [parentSha] } : {}) },
  })

  if (parentSha) {
    await gh(token, `${base}/git/refs/heads/${encodeURIComponent(branch)}`, {
      method: 'PATCH',
      body: { sha: commit.sha, force: false },
    })
  } else {
    await gh(token, `${base}/git/refs`, {
      method: 'POST',
      body: { ref: `refs/heads/${branch}`, sha: commit.sha },
    })
  }
  return { commitSha: commit.sha }
}

// Commit the given files onto a customer-named branch (created off the repo's
// base branch if it doesn't exist) and open a pull request against the base.
// If an open PR already exists for head->base, it's reused (the branch is
// fast-forwarded to the new commit). Commits are authored by the token's user
// (the customer), so the branch + commits + PR are all under their alias.
// Returns { prUrl, prNumber, branch, base }.
export async function openPullRequest(token, { owner, repo, base, head, files, message, title, body }) {
  const b = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`

  // Resolve the base branch head + tree. If the repo is empty (no commits yet —
  // e.g. a repo created without auto_init), seed the base branch with an initial
  // README commit so we have something to branch + PR against.
  let baseSha
  let baseTreeSha
  try {
    const baseRef = await gh(token, `${b}/git/ref/heads/${encodeURIComponent(base)}`)
    baseSha = baseRef.object.sha
    baseTreeSha = (await gh(token, `${b}/git/commits/${baseSha}`)).tree.sha
  } catch (e) {
    if (e.status !== 404 && e.status !== 409) throw e // 404/409 = empty repo / no base branch
    // Seed the first commit via the Contents API. The low-level Git Data API
    // (blobs/trees/commits) returns 409 "Git Repository is empty" on a repo with
    // no commits, whereas PUT /contents creates the first commit + base branch
    // in a single call and works on an empty repo.
    const seeded = await gh(token, `${b}/contents/README.md`, {
      method: 'PUT',
      body: {
        message: 'Initial commit',
        content: Buffer.from(`# ${repo}\n\nManaged by AI Apps.\n`, 'utf-8').toString(
          'base64',
        ),
        branch: base,
      },
    })
    baseSha = seeded.commit.sha
    baseTreeSha = (await gh(token, `${b}/git/commits/${baseSha}`)).tree.sha
  }

  // If the head branch already exists, commit onto its tip; else branch off base.
  let parentSha = baseSha
  let parentTreeSha = baseTreeSha
  let headExists = false
  try {
    const headRef = await gh(token, `${b}/git/ref/heads/${encodeURIComponent(head)}`)
    parentSha = headRef.object.sha
    const headCommit = await gh(token, `${b}/git/commits/${parentSha}`)
    parentTreeSha = headCommit.tree.sha
    headExists = true
  } catch (e) {
    if (e.status !== 404 && e.status !== 409) throw e
  }

  // Blobs -> tree (on top of the parent) -> commit.
  const treeItems = []
  for (const f of files) {
    const blob = await gh(token, `${b}/git/blobs`, {
      method: 'POST',
      body: { content: Buffer.from(f.data, 'utf-8').toString('base64'), encoding: 'base64' },
    })
    treeItems.push({ path: f.name, mode: '100644', type: 'blob', sha: blob.sha })
  }
  const tree = await gh(token, `${b}/git/trees`, {
    method: 'POST',
    body: { base_tree: parentTreeSha, tree: treeItems },
  })
  const commit = await gh(token, `${b}/git/commits`, {
    method: 'POST',
    body: { message, tree: tree.sha, parents: [parentSha] },
  })

  // Create or fast-forward the head branch.
  if (headExists) {
    await gh(token, `${b}/git/refs/heads/${encodeURIComponent(head)}`, {
      method: 'PATCH',
      body: { sha: commit.sha, force: true },
    })
  } else {
    await gh(token, `${b}/git/refs`, {
      method: 'POST',
      body: { ref: `refs/heads/${head}`, sha: commit.sha },
    })
  }

  // Open a PR, or reuse the existing open one for head->base.
  try {
    const pr = await gh(token, `${b}/pulls`, { method: 'POST', body: { title, head, base, body } })
    return { prUrl: pr.html_url, prNumber: pr.number, branch: head, base }
  } catch (e) {
    if (e.status === 422) {
      const existing = await gh(
        token,
        `${b}/pulls?head=${encodeURIComponent(`${owner}:${head}`)}&base=${encodeURIComponent(base)}&state=open`,
      )
      if (Array.isArray(existing) && existing[0]) {
        return { prUrl: existing[0].html_url, prNumber: existing[0].number, branch: head, base }
      }
    }
    throw e
  }
}
