import fs from 'node:fs/promises'
import path from 'node:path'

const PROVIDER_SETTING = 'AZURE_FUNCTIONS_AGENTS_PROVIDER'

function portalError(status, message, portalCode) {
  return Object.assign(new Error(message), { status, portalCode })
}

function statusOf(error) {
  return Number(error?.status ?? error?.statusCode ?? 0)
}

function lifecycleFailure(action, app, error) {
  if (error?.portalCode && error?.status) return error
  const status = statusOf(error)
  if (status === 403) {
    const permission = action === 'stop' ? 'Microsoft.Web/sites/write' : 'Microsoft.Web/sites/delete'
    return portalError(
      403,
      `You do not have permission to ${action} Function App "${app}". Ask an Azure administrator for ${permission}.`,
      `app_${action}_forbidden`,
    )
  }
  if (status === 404) {
    return portalError(404, `Function App "${app}" was not found. Refresh Hosted Skills.`, 'app_not_found')
  }
  if (status === 409) {
    return portalError(
      409,
      `Azure could not ${action} Function App "${app}" because it is in a conflicting state. Refresh and retry.`,
      `app_${action}_conflict`,
    )
  }
  return portalError(
    502,
    `Azure could not ${action} Function App "${app}": ${String(error?.message ?? error)}`,
    `app_${action}_failed`,
  )
}

export function validateAppLifecycleRequest(input) {
  const subscription = String(input?.subscription ?? '').trim()
  const resourceGroup = String(input?.resourceGroup ?? '').trim()
  const app = String(input?.app ?? '').trim()
  const confirmation = String(input?.confirmation ?? '').trim()
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(subscription)) {
    throw portalError(400, 'subscription must be a valid Azure subscription ID.', 'invalid_app_target')
  }
  if (!resourceGroup || resourceGroup.length > 90 || !/^[a-z0-9._()-]+$/i.test(resourceGroup) || resourceGroup.endsWith('.')) {
    throw portalError(400, 'resourceGroup must be a valid Azure resource group name.', 'invalid_app_target')
  }
  if (!/^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$/i.test(app)) {
    throw portalError(400, 'app must be a valid Function App name.', 'invalid_app_target')
  }
  if (confirmation !== app) {
    throw portalError(400, 'Type the exact Function App name to confirm this action.', 'app_confirmation_mismatch')
  }
  return { subscription, resourceGroup, app, confirmation }
}

export function normalizeFunctionAppState(value) {
  const state = String(value ?? '').trim().toLowerCase()
  if (state === 'running') return 'Running'
  if (state === 'stopped') return 'Stopped'
  return 'Unknown'
}

async function resolveAgentFunctionApp(webApps, target, action) {
  let site
  try {
    site = await webApps.get(target.resourceGroup, target.app)
  } catch (error) {
    throw lifecycleFailure(action, target.app, error)
  }
  const kinds = String(site?.kind ?? '').toLowerCase().split(',').map((kind) => kind.trim())
  if (!kinds.includes('functionapp')) {
    throw portalError(409, `Azure resource "${target.app}" is not a Function App.`, 'not_agent_function_app')
  }
  let settings
  try {
    settings = await webApps.listApplicationSettings(target.resourceGroup, target.app)
  } catch (error) {
    throw lifecycleFailure(action, target.app, error)
  }
  if (!String(settings?.properties?.[PROVIDER_SETTING] ?? '').trim()) {
    throw portalError(
      409,
      `Function App "${target.app}" is not managed as a Hosted Skills app.`,
      'not_agent_function_app',
    )
  }
  return site
}

function pollingOptions(options) {
  return {
    pollAttempts: Number.isInteger(options?.pollAttempts) ? Math.max(0, options.pollAttempts) : 15,
    pollIntervalMs: Number.isFinite(options?.pollIntervalMs) ? Math.max(0, options.pollIntervalMs) : 2000,
    sleepImpl: options?.sleepImpl ?? ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))),
  }
}

export async function stopAgentFunctionApp(webApps, input, options = {}) {
  const target = validateAppLifecycleRequest(input)
  const current = await resolveAgentFunctionApp(webApps, target, 'stop')
  if (normalizeFunctionAppState(current.state) === 'Stopped') {
    return { app: target.app, state: 'Stopped', pending: false }
  }
  try {
    await webApps.stop(target.resourceGroup, target.app)
  } catch (error) {
    throw lifecycleFailure('stop', target.app, error)
  }

  const { pollAttempts, pollIntervalMs, sleepImpl } = pollingOptions(options)
  for (let attempt = 0; attempt < pollAttempts; attempt += 1) {
    await sleepImpl(pollIntervalMs)
    let site
    try {
      site = await webApps.get(target.resourceGroup, target.app)
    } catch (error) {
      throw lifecycleFailure('stop', target.app, error)
    }
    if (normalizeFunctionAppState(site?.state) === 'Stopped') {
      return { app: target.app, state: 'Stopped', pending: false }
    }
  }
  return { app: target.app, state: 'Stopping', pending: true }
}

export async function deleteAgentFunctionApp(webApps, input, options = {}) {
  const target = validateAppLifecycleRequest(input)
  await resolveAgentFunctionApp(webApps, target, 'delete')
  try {
    await webApps.delete(target.resourceGroup, target.app)
  } catch (error) {
    throw lifecycleFailure('delete', target.app, error)
  }

  const { pollAttempts, pollIntervalMs, sleepImpl } = pollingOptions(options)
  for (let attempt = 0; attempt < pollAttempts; attempt += 1) {
    await sleepImpl(pollIntervalMs)
    try {
      await webApps.get(target.resourceGroup, target.app)
    } catch (error) {
      if (statusOf(error) === 404) {
        return { app: target.app, deleted: true, pending: false }
      }
      throw lifecycleFailure('delete', target.app, error)
    }
  }
  return { app: target.app, deleted: false, pending: true }
}

function safeSegment(value) {
  return String(value ?? '').replace(/[^a-zA-Z0-9._-]/g, '_')
}

export async function purgePortalAppData({ roots, subscription, app, rmImpl = fs.rm }) {
  const cleanup = {}
  const failures = []
  for (const [name, root] of Object.entries(roots)) {
    const target = path.join(root, safeSegment(subscription), safeSegment(app))
    try {
      await rmImpl(target, { recursive: true, force: true })
      cleanup[name] = 'cleared'
    } catch (error) {
      cleanup[name] = 'failed'
      failures.push(`${name}: ${String(error?.message ?? error)}`)
    }
  }
  return { cleanup, failures }
}
