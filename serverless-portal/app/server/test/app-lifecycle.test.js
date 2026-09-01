import assert from 'node:assert/strict'
import path from 'node:path'
import test from 'node:test'

import {
  deleteAgentFunctionApp,
  normalizeFunctionAppState,
  purgePortalAppData,
  stopAgentFunctionApp,
  validateAppLifecycleRequest,
} from '../src/app-lifecycle.js'

const target = {
  subscription: '11111111-1111-4111-8111-111111111111',
  resourceGroup: 'rg-agents',
  app: 'func-agents-demo',
  confirmation: 'func-agents-demo',
}

const site = (state = 'Running') => ({
  id: `/subscriptions/${target.subscription}/resourceGroups/${target.resourceGroup}/providers/Microsoft.Web/sites/${target.app}`,
  name: target.app,
  kind: 'functionapp,linux',
  state,
})

const settings = { properties: { AZURE_FUNCTIONS_AGENTS_PROVIDER: 'foundry' } }
const noWait = { pollAttempts: 2, pollIntervalMs: 0, sleepImpl: async () => undefined }

test('validates lifecycle confirmation before Azure lookup', async () => {
  assert.throws(
    () => validateAppLifecycleRequest({ ...target, confirmation: 'wrong-app' }),
    (error) => error.status === 400 && error.portalCode === 'app_confirmation_mismatch',
  )

  let reads = 0
  const webApps = {
    get: async () => {
      reads += 1
      return site()
    },
  }
  await assert.rejects(
    stopAgentFunctionApp(webApps, { ...target, confirmation: 'wrong-app' }, noWait),
    (error) => error.status === 400 && error.portalCode === 'app_confirmation_mismatch',
  )
  assert.equal(reads, 0)
})

test('rejects malformed lifecycle targets', () => {
  for (const invalid of [
    { ...target, subscription: 'not-a-subscription' },
    { ...target, resourceGroup: 'rg/demo' },
    { ...target, app: '-invalid-app' },
    { ...target, confirmation: target.app.toUpperCase() },
  ]) {
    assert.throws(
      () => validateAppLifecycleRequest(invalid),
      (error) => error.status === 400,
    )
  }
})

test('normalizes supported Function App states and fails unknown values closed', () => {
  assert.equal(normalizeFunctionAppState('running'), 'Running')
  assert.equal(normalizeFunctionAppState('STOPPED'), 'Stopped')
  assert.equal(normalizeFunctionAppState('Stopping'), 'Unknown')
  assert.equal(normalizeFunctionAppState(''), 'Unknown')
})

test('stop validates the target and waits for Stopped', async () => {
  const states = [site('Running'), site('Running'), site('Stopped')]
  let stops = 0
  const webApps = {
    get: async () => states.shift() ?? site('Stopped'),
    listApplicationSettings: async () => settings,
    stop: async () => {
      stops += 1
    },
  }

  const result = await stopAgentFunctionApp(webApps, target, noWait)

  assert.deepEqual(result, { app: target.app, state: 'Stopped', pending: false })
  assert.equal(stops, 1)
})

test('stop is idempotent when the app is already stopped', async () => {
  let stops = 0
  const webApps = {
    get: async () => site('Stopped'),
    listApplicationSettings: async () => settings,
    stop: async () => {
      stops += 1
    },
  }

  const result = await stopAgentFunctionApp(webApps, target, noWait)

  assert.deepEqual(result, { app: target.app, state: 'Stopped', pending: false })
  assert.equal(stops, 0)
})

test('stop reports pending when Azure has not converged', async () => {
  const webApps = {
    get: async () => site('Running'),
    listApplicationSettings: async () => settings,
    stop: async () => undefined,
  }

  const result = await stopAgentFunctionApp(webApps, target, {
    pollAttempts: 1,
    pollIntervalMs: 0,
    sleepImpl: async () => undefined,
  })

  assert.deepEqual(result, { app: target.app, state: 'Stopping', pending: true })
})

test('lifecycle mutations reject non-agent Function Apps before mutation', async () => {
  let stops = 0
  const webApps = {
    get: async () => site(),
    listApplicationSettings: async () => ({ properties: {} }),
    stop: async () => {
      stops += 1
    },
  }

  await assert.rejects(
    stopAgentFunctionApp(webApps, target, noWait),
    (error) => error.status === 409 && error.portalCode === 'not_agent_function_app',
  )
  assert.equal(stops, 0)
})

test('lifecycle mutations reject non-Function-App sites before reading settings', async () => {
  let settingsReads = 0
  const webApps = {
    get: async () => ({ ...site(), kind: 'app,linux' }),
    listApplicationSettings: async () => {
      settingsReads += 1
      return settings
    },
    stop: async () => undefined,
  }

  await assert.rejects(
    stopAgentFunctionApp(webApps, target, noWait),
    (error) => error.status === 409 && error.portalCode === 'not_agent_function_app',
  )
  assert.equal(settingsReads, 0)
})

test('delete targets only the Function App and confirms absence', async () => {
  let reads = 0
  const deletes = []
  const webApps = {
    get: async () => {
      reads += 1
      if (reads > 1) throw Object.assign(new Error('Not found'), { statusCode: 404 })
      return site()
    },
    listApplicationSettings: async () => settings,
    delete: async (...args) => {
      deletes.push(args)
    },
  }

  const result = await deleteAgentFunctionApp(webApps, target, noWait)

  assert.deepEqual(result, { app: target.app, deleted: true, pending: false })
  assert.deepEqual(deletes, [[target.resourceGroup, target.app]])
})

test('delete reports pending while the exact site remains visible', async () => {
  let deletes = 0
  const webApps = {
    get: async () => site(),
    listApplicationSettings: async () => settings,
    delete: async () => {
      deletes += 1
    },
  }

  const result = await deleteAgentFunctionApp(webApps, target, {
    pollAttempts: 1,
    pollIntervalMs: 0,
    sleepImpl: async () => undefined,
  })

  assert.deepEqual(result, { app: target.app, deleted: false, pending: true })
  assert.equal(deletes, 1)
})

test('delete reports a stale missing target as 404 before mutation', async () => {
  let deletes = 0
  const webApps = {
    get: async () => {
      throw Object.assign(new Error('Not found'), { statusCode: 404 })
    },
    delete: async () => {
      deletes += 1
    },
  }

  await assert.rejects(
    deleteAgentFunctionApp(webApps, target, noWait),
    (error) => error.status === 404 && error.portalCode === 'app_not_found',
  )
  assert.equal(deletes, 0)
})

test('Azure authorization errors retain an actionable 403', async () => {
  const webApps = {
    get: async () => site(),
    listApplicationSettings: async () => settings,
    stop: async () => {
      throw Object.assign(new Error('Forbidden'), { statusCode: 403 })
    },
  }

  await assert.rejects(
    stopAgentFunctionApp(webApps, target, noWait),
    (error) => error.status === 403 && /Microsoft\.Web\/sites\/write/.test(error.message),
  )
})

test('Azure conflict and unexpected failures retain action-specific status', async () => {
  for (const expected of [
    { sourceStatus: 409, status: 409, portalCode: 'app_stop_conflict' },
    { sourceStatus: 500, status: 502, portalCode: 'app_stop_failed' },
  ]) {
    const webApps = {
      get: async () => site(),
      listApplicationSettings: async () => settings,
      stop: async () => {
        throw Object.assign(new Error('Provider failure'), { statusCode: expected.sourceStatus })
      },
    }
    await assert.rejects(
      stopAgentFunctionApp(webApps, target, noWait),
      (error) => error.status === expected.status && error.portalCode === expected.portalCode,
    )
  }
})

test('portal cleanup targets only the exact app path in all four stores', async () => {
  const removed = []
  const roots = {
    agentDrafts: 'data/agent-drafts',
    sourceDrafts: 'data/source-drafts',
    appSources: 'data/app-sources',
    deployHistory: 'data/deploy-history',
  }

  const result = await purgePortalAppData({
    roots,
    subscription: target.subscription,
    app: target.app,
    rmImpl: async (value, options) => {
      removed.push({ value, options })
      if (value.includes('source-drafts')) throw new Error('locked')
    },
  })

  assert.deepEqual(
    removed.map((entry) => entry.value),
    Object.values(roots).map((root) => path.join(root, target.subscription, target.app)),
  )
  assert.ok(removed.every((entry) => entry.options.recursive && entry.options.force))
  assert.deepEqual(result.cleanup, {
    agentDrafts: 'cleared',
    sourceDrafts: 'failed',
    appSources: 'cleared',
    deployHistory: 'cleared',
  })
  assert.deepEqual(result.failures, ['sourceDrafts: locked'])
})
