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
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const GH_API = 'https://api.github.com'
const GH_OAUTH = 'https://github.com/login/oauth'
const SCOPE = 'repo read:user'
const UA = 'serverless-agent-portal'

// --- config ----------------------------------------------------------------

export function githubConfig() {
  return {
    clientId: process.env.GITHUB_OAUTH_CLIENT_ID || '',
    clientSecret: process.env.GITHUB_OAUTH_CLIENT_SECRET || '',
    // Empty by default: when unset, the portal omits redirect_uri so GitHub
    // uses the app's own registered Callback URL (simplest for GitHub Apps).
    callback: process.env.GITHUB_OAUTH_CALLBACK || '',
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

// Dev-only persistence: keep the token map in a gitignored file so backend
// reloads (node --watch) don't drop the connection. Prod should use Key Vault.
const TOKENS_FILE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '.data',
  'github-tokens.json',
)
function loadTokens() {
  try {
    return new Map(Object.entries(JSON.parse(fs.readFileSync(TOKENS_FILE, 'utf-8'))))
  } catch {
    return new Map()
  }
}
function saveTokens(map) {
  try {
    fs.mkdirSync(path.dirname(TOKENS_FILE), { recursive: true })
    fs.writeFileSync(TOKENS_FILE, JSON.stringify(Object.fromEntries(map)), 'utf-8')
  } catch {
    /* best-effort */
  }
}
const tokens = loadTokens() // oid -> { token, login, avatarUrl, connectedAt }
export const tokenStore = {
  get: (oid) => tokens.get(oid) || null,
  set: (oid, data) => {
    tokens.set(oid, { ...data, connectedAt: Date.now() })
    saveTokens(tokens)
  },
  clear: (oid) => {
    tokens.delete(oid)
    saveTokens(tokens)
  },
}

// --- OAuth flow ------------------------------------------------------------

export function authorizeUrl(oid) {
  const c = githubConfig()
  const params = new URLSearchParams({
    client_id: c.clientId,
    scope: SCOPE,
    state: makeState(oid),
    allow_signup: 'false',
  })
  // Only pin redirect_uri when explicitly configured. Omitting it lets GitHub
  // fall back to the app's registered Callback URL, avoiding the strict
  // "redirect_uri is not associated with this application" match (GitHub Apps
  // require an EXACT match to a registered Callback URL).
  if (c.callback) params.set('redirect_uri', c.callback)
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
      ...(c.callback ? { redirect_uri: c.callback } : {}),
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
      // Seed the default branch (a README commit) so we can immediately open a
      // PR against it — the portal always contributes via a PR, never a direct
      // push to the default branch.
      auto_init: true,
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

// Rolling-branch resolution (one PR per app until merged): reuse the branch of an
// OPEN PR under `${prefix}/` so edits accumulate into a single PR; start a fresh
// timestamped branch when there's no open PR (first connect, or the last PR was
// merged/closed).
export async function resolveRollingBranch(token, owner, repo, base, prefix) {
  const b = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`
  try {
    const open = await gh(token, `${b}/pulls?state=open&base=${encodeURIComponent(base)}&per_page=100`)
    const match = (Array.isArray(open) ? open : []).find((p) =>
      String(p.head?.ref || '').startsWith(`${prefix}/`),
    )
    if (match?.head?.ref) return match.head.ref
  } catch {
    /* fall through to a fresh branch */
  }
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-T:]/g, '')
  return `${prefix}/${stamp}`
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
        content: Buffer.from(`# ${repo}\n\nManaged by the Serverless Agent Portal.\n`, 'utf-8').toString(
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
