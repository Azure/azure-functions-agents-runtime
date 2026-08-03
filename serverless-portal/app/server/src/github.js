// Serverless Agent Portal — GitHub connection (OAuth App + repo push).
//
// Phase 1: after an agent is created, the user connects a GitHub account via an
// OAuth App (browser sign-in). The portal then creates a new repo (or uses an
// existing one), pushes the app's source, and records the repo link on the
// Function App. Later phases open pull requests for edits.
//
// The OAuth token never reaches the browser. `authorizeUrl()` embeds a signed
// `state` that binds the flow to the portal user's `oid`; the callback verifies
// the signature and stores the token server-side keyed by that oid. Every
// GitHub API route requires the caller's ARM token (so a user can only use
// their own connection).

import crypto from 'node:crypto'

const GH_API = 'https://api.github.com'
const GH_OAUTH = 'https://github.com/login/oauth'
const SCOPE = 'repo read:user'
const UA = 'serverless-agent-portal'

// --- config ----------------------------------------------------------------

export function githubConfig() {
  return {
    clientId: process.env.GITHUB_OAUTH_CLIENT_ID || '',
    clientSecret: process.env.GITHUB_OAUTH_CLIENT_SECRET || '',
    callback: process.env.GITHUB_OAUTH_CALLBACK || 'http://localhost:8080/api/github/callback',
  }
}

export function isConfigured() {
  const c = githubConfig()
  return Boolean(c.clientId && c.clientSecret)
}

// --- signed state (CSRF + user binding, stateless) -------------------------

// A per-process secret is fine: state is short-lived. Set the env var to keep
// states valid across restarts / multiple instances.
const STATE_SECRET = process.env.GITHUB_OAUTH_STATE_SECRET || crypto.randomBytes(32).toString('hex')
const STATE_TTL_MS = 10 * 60 * 1000

function makeState(oid) {
  const payload = Buffer.from(
    JSON.stringify({ oid, n: crypto.randomBytes(8).toString('hex'), t: Date.now() }),
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

// --- token store (in-memory; swap for Key Vault in prod) -------------------

const tokens = new Map() // oid -> { token, login, avatarUrl, connectedAt }
export const tokenStore = {
  get: (oid) => tokens.get(oid) || null,
  set: (oid, data) => tokens.set(oid, { ...data, connectedAt: Date.now() }),
  clear: (oid) => tokens.delete(oid),
}

// --- OAuth flow ------------------------------------------------------------

export function authorizeUrl(oid) {
  const c = githubConfig()
  const params = new URLSearchParams({
    client_id: c.clientId,
    redirect_uri: c.callback,
    scope: SCOPE,
    state: makeState(oid),
    allow_signup: 'false',
  })
  return `${GH_OAUTH}/authorize?${params.toString()}`
}

export async function exchangeCode(code) {
  const c = githubConfig()
  const res = await fetch(`${GH_OAUTH}/access_token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'User-Agent': UA },
    body: JSON.stringify({
      client_id: c.clientId,
      client_secret: c.clientSecret,
      code,
      redirect_uri: c.callback,
    }),
    signal: AbortSignal.timeout(15000),
  })
  const json = await res.json().catch(() => ({}))
  if (!res.ok || !json.access_token) {
    throw new Error(json.error_description || json.error || 'GitHub token exchange failed.')
  }
  return json.access_token
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

async function gh(token, apiPath, { method = 'GET', body } = {}) {
  const res = await fetch(`${GH_API}${apiPath}`, {
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
    const msg = json?.message || `${res.status} ${res.statusText}`
    const err = new Error(String(msg).slice(0, 500))
    err.status = res.status
    throw err
  }
  return json
}

export async function getUser(token) {
  const u = await gh(token, '/user')
  return { login: u.login, avatarUrl: u.avatar_url, name: u.name || u.login }
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

export async function createRepo(token, { name, private: priv = true, org = '' }) {
  const apiPath = org ? `/orgs/${encodeURIComponent(org)}/repos` : '/user/repos'
  const r = await gh(token, apiPath, {
    method: 'POST',
    body: {
      name,
      private: priv,
      auto_init: false,
      description: 'Serverless agent app — managed by the Serverless Agent Portal',
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
