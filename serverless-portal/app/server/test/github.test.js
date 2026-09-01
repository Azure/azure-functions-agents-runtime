import assert from 'node:assert/strict'
import test from 'node:test'

import {
  authorizeUrl,
  ensureUserSession,
  ensureWorkflowCanBeWritten,
  exchangeCode,
  functionsWorkflowYaml,
  inspectAppInstallation,
  inspectAuthorizedApplication,
  pushFiles,
  readSession,
  readState,
  sealSession,
  validateDeployableRepoFiles,
} from '../src/github.js'

test('binds a validated localhost callback into signed OAuth state', () => {
  const callback = 'http://127.0.0.1:5173/api/github/callback'
  const url = new URL(authorizeUrl('user-oid', callback))
  const state = readState(url.searchParams.get('state'))

  assert.equal(url.searchParams.get('redirect_uri'), callback)
  assert.equal(state?.oid, 'user-oid')
  assert.equal(state?.callback, callback)
})

test('rejects an unconfigured remote callback supplied by the browser', () => {
  const url = new URL(authorizeUrl('user-oid', 'https://attacker.example/api/github/callback'))
  const state = readState(url.searchParams.get('state'))

  assert.equal(url.searchParams.has('redirect_uri'), false)
  assert.equal(state?.callback, '')
})

test('seals a GitHub session and binds it to the signed-in portal user', () => {
  const session = sealSession('user-oid', {
    token: 'github-secret-token',
    login: 'octocat',
    avatarUrl: 'https://avatars.example/octocat',
    refreshToken: 'github-refresh-token',
    expiresAt: 1_800_000_000_000,
    refreshExpiresAt: 1_900_000_000_000,
    validatedAt: 1_700_000_000_000,
  })

  assert.equal(session.includes('github-secret-token'), false)
  assert.equal(session.includes('github-refresh-token'), false)
  assert.deepEqual(readSession(session, 'user-oid'), {
    token: 'github-secret-token',
    login: 'octocat',
    avatarUrl: 'https://avatars.example/octocat',
    refreshToken: 'github-refresh-token',
    expiresAt: 1_800_000_000_000,
    refreshExpiresAt: 1_900_000_000_000,
    validatedAt: 1_700_000_000_000,
  })
  assert.equal(readSession(session, 'other-user'), null)
  assert.equal(readSession(`${session}x`, 'user-oid'), null)
})

test('OAuth code exchange preserves GitHub App refresh credentials', async () => {
  const now = Date.parse('2026-09-01T12:00:00Z')
  const session = await exchangeCode('valid-code', 'https://portal.example/api/github/callback', {
    now,
    fetchImpl: async (_url, init) => {
      const body = JSON.parse(init.body)
      assert.equal(body.code, 'valid-code')
      assert.equal(body.redirect_uri, 'https://portal.example/api/github/callback')
      return new Response(JSON.stringify({
        access_token: 'ghu_initial',
        expires_in: 28_800,
        refresh_token: 'ghr_initial',
        refresh_token_expires_in: 15_897_600,
        token_type: 'bearer',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    },
  })

  assert.deepEqual(session, {
    token: 'ghu_initial',
    expiresAt: now + 28_800_000,
    refreshToken: 'ghr_initial',
    refreshExpiresAt: now + 15_897_600_000,
  })
})

test('expired GitHub App sessions refresh without another authorization flow', async () => {
  const now = Date.parse('2026-09-01T12:00:00Z')
  const requests = []
  const result = await ensureUserSession({
    token: 'ghu_expired',
    refreshToken: 'ghr_initial',
    expiresAt: now - 1,
    refreshExpiresAt: now + 60_000,
    login: 'octocat',
    avatarUrl: 'https://avatars.example/octocat',
  }, {
    now,
    fetchImpl: async (_url, init) => {
      const body = JSON.parse(init.body)
      requests.push(body)
      return new Response(JSON.stringify({
        access_token: 'ghu_rotated',
        expires_in: 28_800,
        refresh_token: 'ghr_rotated',
        refresh_token_expires_in: 15_897_600,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    },
  })

  assert.deepEqual(requests, [{
    client_id: '',
    client_secret: '',
    grant_type: 'refresh_token',
    refresh_token: 'ghr_initial',
  }])
  assert.equal(result.changed, true)
  assert.equal(result.session.token, 'ghu_rotated')
  assert.equal(result.session.refreshToken, 'ghr_rotated')
  assert.equal(result.session.login, 'octocat')
})

test('a transient GitHub refresh failure does not expire the user session', async () => {
  const now = Date.parse('2026-09-01T12:00:00Z')
  await assert.rejects(
    ensureUserSession({
      token: 'ghu_expired',
      login: 'octocat',
      refreshToken: 'ghr_current',
      expiresAt: now - 1,
      refreshExpiresAt: now + 86_400_000,
    }, {
      now,
      fetchImpl: async () => new Response(JSON.stringify({ error: 'server_error' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      }),
    }),
    (error) => error.status === 502 && error.portalCode === undefined,
  )
})

test('legacy GitHub sessions are validated and rejected when their token is revoked', async () => {
  await assert.rejects(
    ensureUserSession({ token: 'revoked', login: 'octocat', avatarUrl: '' }, {
      now: Date.parse('2026-09-01T12:00:00Z'),
      fetchImpl: async () => new Response(
        JSON.stringify({ message: 'Bad credentials' }),
        { status: 401, headers: { 'Content-Type': 'application/json' } },
      ),
    }),
    (error) => error.status === 401 && error.portalCode === 'github_session_expired',
  )
})

test('repository mutations force validation even for recently checked sessions', async () => {
  const now = Date.parse('2026-09-01T12:00:00Z')
  let requests = 0
  const result = await ensureUserSession({
    token: 'ghu_current',
    login: 'octocat',
    avatarUrl: '',
    validatedAt: now - 1_000,
  }, {
    now,
    forceValidate: true,
    fetchImpl: async () => {
      requests += 1
      return new Response(JSON.stringify({ login: 'octocat', avatar_url: '' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    },
  })

  assert.equal(requests, 1)
  assert.equal(result.changed, true)
  assert.equal(result.session.validatedAt, now)
})

test('inspects the permissions granted to the current GitHub App installation', async () => {
  const result = await inspectAppInstallation('ghu_user', {
    owner: 'swapnil-nagar',
    requiredPermissions: {
      administration: 'write',
      contents: 'write',
      pull_requests: 'write',
    },
    fetchImpl: async (url) => {
      assert.match(String(url), /\/user\/installations\?per_page=100$/)
      return new Response(JSON.stringify({
        installations: [{
          id: 42,
          account: { login: 'swapnil-nagar' },
          repository_selection: 'all',
          permissions: {
            administration: 'read',
            contents: 'write',
            pull_requests: 'write',
          },
          html_url: 'https://github.com/settings/installations/42',
        }],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    },
  })

  assert.deepEqual(result, {
    installationFound: true,
    repositorySelection: 'all',
    missingPermissions: ['Administration: write'],
    settingsUrl: 'https://github.com/settings/installations/42',
  })
})

test('resolves a safe install URL for the GitHub App that issued the user token', async () => {
  const result = await inspectAuthorizedApplication('ghu_user', {
    clientId: 'local-client-id',
    clientSecret: 'local-client-secret',
    fetchImpl: async (url, init) => {
      assert.match(String(url), /\/applications\/local-client-id\/token$/)
      assert.match(String(init.headers.Authorization), /^Basic /)
      assert.deepEqual(JSON.parse(init.body), { access_token: 'ghu_user' })
      return new Response(JSON.stringify({
        app: {
          client_id: 'local-client-id',
          name: 'AI Apps Portal',
          url: 'https://api.github.com/apps/ai-apps-portal',
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    },
  })

  assert.deepEqual(result, {
    appName: 'AI Apps Portal',
    installUrl: 'https://github.com/apps/ai-apps-portal/installations/new',
  })
})

test('pushes a complete file set directly to an existing default branch', async (t) => {
  const requests = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (url, init = {}) => {
    requests.push({ url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null })
    const apiPath = new URL(String(url)).pathname
    const response = apiPath.endsWith('/git/ref/heads/main')
      ? { object: { sha: 'parent-sha' } }
      : apiPath.endsWith('/git/commits/parent-sha')
        ? { tree: { sha: 'parent-tree' } }
        : apiPath.endsWith('/git/trees')
          ? { sha: 'new-tree' }
          : apiPath.endsWith('/git/commits')
            ? { sha: 'new-commit' }
            : apiPath.endsWith('/git/blobs')
              ? { sha: `blob-${requests.length}` }
              : {}
    return new Response(JSON.stringify(response), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  t.after(() => {
    globalThis.fetch = originalFetch
  })

  const result = await pushFiles('token', {
    owner: 'octocat',
    repo: 'deployable-agent',
    branch: 'main',
    files: [
      { name: 'azure.yaml', data: 'name: deployable-agent\n' },
      { name: 'src/function_app.py', data: 'app = None\n' },
    ],
    message: 'Publish deployable app',
  })

  assert.deepEqual(result, { commitSha: 'new-commit' })
  const refUpdate = requests.at(-1)
  assert.equal(refUpdate.method, 'PATCH')
  assert.match(refUpdate.url, /\/git\/refs\/heads\/main$/)
  assert.deepEqual(refUpdate.body, { sha: 'new-commit', force: false })
})

test('generates a Python 3.13 Azure Functions deployment workflow', () => {
  const workflow = functionsWorkflowYaml({ appName: 'agent-app', branch: 'main' })

  assert.match(workflow, /PYTHON_VERSION: "3\.13"/)
  assert.match(workflow, /sku: flexconsumption/)
})

test('requires a complete deployable repository export', () => {
  const complete = [
    'azure.yaml',
    'README.md',
    '.gitignore',
    'infra/main.bicep',
    'infra/main.parameters.json',
    'src/function_app.py',
    'src/host.json',
    'src/requirements.txt',
    'src/report.agent.md',
  ].map((name) => ({ name, data: '' }))

  assert.equal(validateDeployableRepoFiles(complete), complete)
  assert.throws(
    () => validateDeployableRepoFiles(complete.filter((file) => file.name !== 'src/host.json')),
    /missing src\/host\.json/,
  )
})

test('does not overwrite a different deployment workflow', () => {
  const workflow = functionsWorkflowYaml({ appName: 'agent-app', branch: 'main' })

  assert.equal(ensureWorkflowCanBeWritten(null, workflow), true)
  assert.equal(ensureWorkflowCanBeWritten(workflow.replace(/\n/g, '\r\n'), workflow), false)
  assert.throws(
    () => ensureWorkflowCanBeWritten('name: Customer workflow\n', workflow),
    /different \.github\/workflows\/deploy\.yml already exists/,
  )
})