import assert from 'node:assert/strict'
import test from 'node:test'

import {
  authorizeUrl,
  ensureWorkflowCanBeWritten,
  functionsWorkflowYaml,
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
  })

  assert.equal(session.includes('github-secret-token'), false)
  assert.deepEqual(readSession(session, 'user-oid'), {
    token: 'github-secret-token',
    login: 'octocat',
    avatarUrl: 'https://avatars.example/octocat',
  })
  assert.equal(readSession(session, 'other-user'), null)
  assert.equal(readSession(`${session}x`, 'user-oid'), null)
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