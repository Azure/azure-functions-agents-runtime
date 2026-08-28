import { createHash } from 'node:crypto'

const API_VERSION = '2026-05-01-preview'
const ARM_ORIGIN = 'https://management.azure.com'
const OWNER_TAG = 'azfunc-agents-portal'
const APP_TAG = 'azfunc-agents-app-id'
const TRANSIENT_ARM_STATUSES = new Set([408, 429, 500, 502, 503, 504])
export const OUTLOOK_CONNECTION_ID_SETTING = 'AZURE_FUNCTIONS_AGENTS_OUTLOOK_CONNECTION_ID'

function isHttpsUrl(value) {
  try {
    return new URL(value).protocol === 'https:'
  } catch {
    return false
  }
}

function portalError(status, message, portalCode) {
  return Object.assign(new Error(message), { status, portalCode })
}

function canonical(value) {
  return String(value ?? '').trim().replace(/\/$/, '').toLowerCase()
}

function appResourceParts(appResourceId) {
  const match = /^\/subscriptions\/([^/]+)\/resourceGroups\/([^/]+)\/providers\/Microsoft\.Web\/sites\/([^/]+)$/i.exec(
    String(appResourceId ?? '').trim().replace(/\/$/, ''),
  )
  if (!match) throw portalError(400, 'Invalid Function App resource ID.', 'invalid_app_resource_id')
  return { subscriptionId: match[1], resourceGroup: match[2], appName: match[3] }
}

function gatewayResourceParts(gatewayResourceId) {
  const match = /^\/subscriptions\/([^/]+)\/resourceGroups\/([^/]+)\/providers\/Microsoft\.Web\/connectorGateways\/([^/]+)$/i.exec(
    String(gatewayResourceId ?? '').trim().replace(/\/$/, ''),
  )
  if (!match) return null
  return { subscriptionId: match[1], resourceGroup: match[2], gatewayName: match[3] }
}

function connectionResourceParts(connectionResourceId) {
  const match = /^(\/subscriptions\/([^/]+)\/resourceGroups\/([^/]+)\/providers\/Microsoft\.Web\/connectorGateways\/([^/]+))\/connections\/([^/]+)$/i.exec(
    String(connectionResourceId ?? '').trim().replace(/\/$/, ''),
  )
  if (!match) return null
  return {
    gatewayId: match[1],
    subscriptionId: match[2],
    resourceGroup: match[3],
    gatewayName: match[4],
    connectionName: match[5],
  }
}

function appIdentityHash(appResourceId) {
  return createHash('sha256').update(canonical(appResourceId)).digest('hex').slice(0, 12)
}

export function outlookResourceNames(appResourceId) {
  const hash = appIdentityHash(appResourceId)
  return {
    gateway: `cg-o365-${hash}`,
    connection: 'office365-outlook',
    mcpConfig: 'Office-365-Outlook-send-email-only',
    attachedMcpConfig: `Office-365-Outlook-send-email-only-${hash}`,
  }
}

export function outlookResourceIds(appResourceId) {
  const { subscriptionId: subscription, resourceGroup } = appResourceParts(appResourceId)
  const names = outlookResourceNames(appResourceId)
  const gateway = `/subscriptions/${subscription}/resourceGroups/${resourceGroup}/providers/Microsoft.Web/connectorGateways/${names.gateway}`
  const connection = `${gateway}/connections/${names.connection}`
  return {
    ...names,
    gateway,
    connection,
    mcpConfigId: `${gateway}/mcpserverconfigs/${names.mcpConfig}`,
  }
}

export function encodeConnectionId(resourceId) {
  return Buffer.from(String(resourceId), 'utf8').toString('base64url')
}

export function decodeConnectionId(encoded) {
  try {
    const decoded = Buffer.from(String(encoded), 'base64url').toString('utf8')
    if (!connectionResourceParts(decoded)) throw new Error('Invalid connection resource ID')
    return decoded
  } catch {
    throw portalError(400, 'Invalid connection ID.', 'invalid_connection_id')
  }
}

export function validateConnectionId(encoded, appResourceId, expectedSubscriptionId = '') {
  const decoded = decodeConnectionId(encoded)
  const connection = connectionResourceParts(decoded)
  const app = appResourceParts(appResourceId)
  const allowedSubscriptionId = String(expectedSubscriptionId || app.subscriptionId).trim()
  if (!connection || canonical(connection.subscriptionId) !== canonical(allowedSubscriptionId)) {
    throw portalError(404, 'Connection not found.', 'connection_not_found')
  }
  return decoded
}

export function connectorPortalUrl(connectionResourceId) {
  const connection = connectionResourceParts(connectionResourceId)
  if (!connection) throw portalError(400, 'Invalid connection resource ID.', 'invalid_connection_id')
  return [
    'https://connectors.azure.com',
    encodeURIComponent(connection.subscriptionId),
    encodeURIComponent(connection.resourceGroup),
    encodeURIComponent(connection.gatewayName),
    'overview',
  ].join('/')
}

export function outlookAppSettings(connection) {
  const endpoint = new URL(connection.mcpEndpointUrl)
  if (endpoint.protocol !== 'https:') {
    throw portalError(502, 'Azure did not return a secure Outlook MCP endpoint.', 'invalid_mcp_endpoint')
  }
  return {
    O365_MCP_SERVER_URL: endpoint.toString(),
    [OUTLOOK_CONNECTION_ID_SETTING]: decodeConnectionId(connection.id),
  }
}

export function normalizeConnectionStatus(providerStatus, configured = true) {
  const value = String(providerStatus ?? '').trim()
  if (configured && value.toLowerCase() === 'connected') return 'Connected'
  if (value.toLowerCase() === 'expired') return 'Expired'
  return 'Action required'
}

export function outlookMcpProperties(connectionName = 'office365-outlook') {
  return {
    state: 'Enabled',
    description: 'Send email through Office 365 Outlook.',
    connectors: [
      {
        name: 'office365',
        connectionName,
        displayName: 'Office 365 Outlook',
        description: '',
        operations: [
          {
            name: 'SendEmailV2',
            displayName: 'Send an email',
            description: 'This operation sends an email message.',
            userParameters: [],
            agentParameters: [
              {
                name: 'emailMessage',
                schema: {
                  type: 'object',
                  properties: {
                    To: {
                      type: 'string',
                      format: 'email',
                      description: 'Specify email addresses separated by semicolons.',
                      required: true,
                    },
                    Subject: { type: 'string', description: 'Specify the subject of the mail.', required: true },
                    Body: { type: 'string', format: 'html', description: 'Specify the body of the mail.', required: true },
                  },
                },
              },
            ],
          },
        ],
      },
    ],
    policies: [],
    settings: { textOnlyContent: true },
  }
}

export function outlookMcpSourceDefinition() {
  return {
    type: 'http',
    url: '$O365_MCP_SERVER_URL',
    tools: ['office365_SendEmailV2'],
    auth: {
      scope: 'https://apihub.azure.com/.default',
      client_id: '$O365_MCP_CLIENT_ID',
    },
  }
}

function isSupportedOutlookMcpSource(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const type = String(value.type ?? '').toLowerCase()
  const tools = Array.isArray(value.tools) ? value.tools.map(String) : []
  const auth = value.auth
  const clientId = String(auth?.client_id ?? '')
  return (!type || ['http', 'streamable-http'].includes(type)) &&
    value.url === '$O365_MCP_SERVER_URL' &&
    tools.length === 1 &&
    tools[0] === 'office365_SendEmailV2' &&
    auth && typeof auth === 'object' &&
    auth.scope === 'https://apihub.azure.com/.default' &&
    (!clientId || clientId === '$O365_MCP_CLIENT_ID')
}

function parseMcpSource(content) {
  const source = String(content ?? '')
  if (!source.trim()) return { source, document: {} }
  let document
  try {
    document = JSON.parse(source)
  } catch {
    throw portalError(409, 'mcp.json is not valid JSON. Repair it before configuring Outlook.', 'invalid_mcp_source')
  }
  if (!document || typeof document !== 'object' || Array.isArray(document)) {
    throw portalError(409, 'mcp.json must contain a JSON object.', 'invalid_mcp_source')
  }
  if (document.servers !== undefined && (
    !document.servers || typeof document.servers !== 'object' || Array.isArray(document.servers)
  )) {
    throw portalError(409, 'mcp.json servers must contain a JSON object.', 'invalid_mcp_source')
  }
  return { source, document }
}

export function ensureOutlookMcpSource(content, sourceState) {
  const { source, document } = parseMcpSource(content)
  const expected = outlookMcpSourceDefinition()
  const existing = document.servers?.['office365-outlook']
  if (isSupportedOutlookMcpSource(existing)) {
    return {
      content: source,
      changed: false,
      deploymentRequired: sourceState === 'draft',
      state: sourceState === 'draft' ? 'draft' : 'deployed',
    }
  }
  document.servers = { ...(document.servers ?? {}), 'office365-outlook': expected }
  return {
    content: `${JSON.stringify(document, null, 2)}\n`,
    changed: true,
    deploymentRequired: true,
    state: 'draft',
  }
}

export async function coordinateOutlookConnectionSetup({
  sourceBefore,
  stageSource,
  rollbackSource,
  configureAzure,
}) {
  const source = ensureOutlookMcpSource(sourceBefore.content, sourceBefore.source)
  try {
    if (source.changed) await stageSource(source.content)
    const connection = await configureAzure()
    return {
      connection,
      source: {
        path: 'mcp.json',
        changed: source.changed,
        deploymentRequired: source.deploymentRequired,
        state: source.state,
      },
    }
  } catch (error) {
    if (source.changed) {
      try {
        await rollbackSource(sourceBefore)
        error.sourceCleanup = 'rolled_back'
      } catch {
        error.sourceCleanup = 'rollback_failed'
      }
    }
    throw error
  }
}

export function removeOutlookMcpSource(content) {
  const source = String(content ?? '')
  if (!source.trim()) return { content: source, changed: false }

  let document
  try {
    document = JSON.parse(source)
  } catch {
    throw portalError(
      409,
      'mcp.json is not valid JSON. Repair it before removing the Outlook connection.',
      'invalid_mcp_source',
    )
  }
  if (!document || typeof document !== 'object' || Array.isArray(document)) {
    throw portalError(409, 'mcp.json must contain a JSON object.', 'invalid_mcp_source')
  }
  if (document.servers === undefined) return { content: source, changed: false }
  if (!document.servers || typeof document.servers !== 'object' || Array.isArray(document.servers)) {
    throw portalError(409, 'mcp.json servers must contain a JSON object.', 'invalid_mcp_source')
  }
  if (!Object.prototype.hasOwnProperty.call(document.servers, 'office365-outlook')) {
    return { content: source, changed: false }
  }
  delete document.servers['office365-outlook']
  return { content: `${JSON.stringify(document, null, 2)}\n`, changed: true }
}

export async function coordinateOutlookConnectionRemoval({
  sourceBefore,
  settingBefore = null,
  stageSource,
  rollbackSource,
  removeSetting,
  restoreSetting,
  deleteAzure,
}) {
  const sourceAfter = removeOutlookMcpSource(sourceBefore.content)
  const cleanup = { sourceDraft: 'unchanged', appSetting: 'absent', azure: 'not_started' }
  let removedSetting = settingBefore
  try {
    if (sourceAfter.changed) {
      await stageSource(sourceAfter.content)
      cleanup.sourceDraft = 'updated'
    }
    removedSetting = await removeSetting() ?? removedSetting
    if (removedSetting?.removed) cleanup.appSetting = 'removed'
    const result = await deleteAzure()
    cleanup.azure = result.azure
    return { ...result, sourceDraftChanged: sourceAfter.changed, cleanup }
  } catch (error) {
    if (sourceAfter.changed) {
      try {
        await rollbackSource(sourceBefore)
        cleanup.sourceDraft = 'rolled_back'
      } catch {
        cleanup.sourceDraft = 'rollback_failed'
      }
    }
    if (removedSetting?.removed) {
      try {
        await restoreSetting(removedSetting)
        cleanup.appSetting = 'restored'
      } catch {
        cleanup.appSetting = 'restore_failed'
      }
    }
    cleanup.azure = cleanup.azure === 'not_started' ? 'failed' : cleanup.azure
    error.cleanup = cleanup
    throw error
  }
}

async function armJson(accessToken, resourceId, options = {}, fetchImpl = fetch) {
  const target = String(resourceId).startsWith('https://') ? new URL(resourceId) : new URL(resourceId, ARM_ORIGIN)
  if (target.origin !== ARM_ORIGIN) throw portalError(502, 'ARM returned an invalid continuation URL.', 'connector_arm_failed')
  if (!target.searchParams.has('api-version')) target.searchParams.set('api-version', API_VERSION)
  const attempts = options.retry === false ? 1 : 3
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const response = await fetchImpl(target.toString(), {
      method: options.method ?? 'GET',
      headers: {
        Authorization: `Bearer ${accessToken}`,
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: AbortSignal.timeout(20_000),
    })
    let body = null
    try {
      body = await response.json()
    } catch {
      body = null
    }
    const method = options.method ?? 'GET'
    const retryable = TRANSIENT_ARM_STATUSES.has(response.status) ||
      (response.status === 409 && ['PUT', 'DELETE'].includes(method))
    if (response.ok || !retryable || attempt === attempts) {
      return { ok: response.ok, status: response.status, body }
    }
    const retryAfter = response.headers.get('Retry-After')
    const retryAfterSeconds = retryAfter === null ? Number.NaN : Number(retryAfter)
    const delayMs = Number.isFinite(retryAfterSeconds)
      ? Math.min(retryAfterSeconds * 1_000, 5_000)
      : 250 * 2 ** (attempt - 1)
    await new Promise((resolve) => setTimeout(resolve, delayMs))
  }
  throw new Error('Unreachable ARM retry state.')
}

async function armList(accessToken, resourceId, fetchImpl) {
  const items = []
  let nextLink = resourceId
  for (let page = 0; nextLink && page < 20; page += 1) {
    const result = await armJson(accessToken, nextLink, {}, fetchImpl)
    if (!result.ok) return { ok: false, status: result.status, body: result.body, items }
    items.push(...(Array.isArray(result.body?.value) ? result.body.value : []))
    nextLink = String(result.body?.nextLink ?? '')
  }
  if (nextLink) throw portalError(502, 'Connector Gateway listing exceeded the pagination limit.', 'connector_arm_failed')
  return { ok: true, status: 200, body: null, items }
}

async function deleteArmResource(accessToken, resourceId, fetchImpl) {
  const result = await armJson(accessToken, resourceId, { method: 'DELETE' }, fetchImpl)
  if (result.status === 404) return 'already_absent'
  if (!result.ok) throw armFailure(result, `Could not delete ${resourceId.split('/').pop()}`)

  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const read = await armJson(accessToken, resourceId, {}, fetchImpl)
    if (read.status === 404) return 'deleted'
    if (!read.ok) return 'deletion_pending'
    if (attempt < 3) await new Promise((resolve) => setTimeout(resolve, 250 * 2 ** (attempt - 1)))
  }
  return 'deletion_pending'
}

function armFailure(result, action) {
  const detail = result.body?.error?.message || `HTTP ${result.status}`
  return portalError(result.status === 403 ? 403 : 502, `${action}: ${detail}`, 'connector_arm_failed')
}

function accessPolicyId(connectionId, principalId) {
  return `${connectionId}/accessPolicies/${principalId}`
}

function requiredOperations(mcpResource) {
  const connectors = mcpResource?.properties?.connectors ?? []
  return connectors.flatMap((connector) => connector?.operations ?? []).map((operation) => String(operation?.name ?? ''))
}

function isOwnedGateway(gateway, appResourceId) {
  const tags = gateway?.tags ?? {}
  return tags[OWNER_TAG] === 'managed' && canonical(tags[APP_TAG]) === canonical(appResourceId)
}

function isGatewayManagedByOtherApp(gateway, appResourceId) {
  const tags = gateway?.tags ?? {}
  return tags[OWNER_TAG] === 'managed' && canonical(tags[APP_TAG]) !== canonical(appResourceId)
}

export async function listOutlookConnectionCandidates(accessToken, context, fetchImpl = fetch) {
  const app = appResourceParts(context.appResourceId)
  const subscriptionId = String(context.connectorSubscriptionId || app.subscriptionId).trim()
  const gatewayList = await armList(
    accessToken,
    `/subscriptions/${subscriptionId}/providers/Microsoft.Web/connectorGateways`,
    fetchImpl,
  )
  if (!gatewayList.ok) throw armFailure(gatewayList, 'Could not list Connector Gateways')

  const eligibleGateways = gatewayList.items.filter(
    (gateway) => gatewayResourceParts(gateway?.id) && !isGatewayManagedByOtherApp(gateway, context.appResourceId),
  )
  const connections = []
  let partial = false
  for (let offset = 0; offset < eligibleGateways.length; offset += 5) {
    const batch = eligibleGateways.slice(offset, offset + 5)
    const results = await Promise.all(batch.map(async (gateway) => ({
      gateway,
      result: await armList(accessToken, `${gateway.id}/connections`, fetchImpl),
    })))
    for (const { gateway, result } of results) {
      if (!result.ok && [403, 404].includes(result.status)) {
        partial = true
        continue
      }
      if (!result.ok) throw armFailure(result, `Could not list connections for ${gateway.name}`)
      const gatewayParts = gatewayResourceParts(gateway.id)
      for (const connection of result.items) {
        const connectionParts = connectionResourceParts(connection?.id)
        if (!connectionParts || canonical(connectionParts.gatewayId) !== canonical(gateway.id)) continue
        if (String(connection?.properties?.connectorName ?? '').toLowerCase() !== 'office365') continue
        const providerStatus = String(connection?.properties?.overallStatus ?? '')
        connections.push({
          id: encodeConnectionId(connection.id),
          subscriptionId,
          displayName: String(connection?.properties?.displayName ?? connection.name ?? 'Office 365 Outlook'),
          connectionName: String(connection.name ?? ''),
          gatewayName: gatewayParts.gatewayName,
          resourceGroup: gatewayParts.resourceGroup,
          status: normalizeConnectionStatus(providerStatus),
          providerStatus,
          authenticatedUser: String(connection?.properties?.authenticatedUser?.name ?? ''),
        })
      }
    }
  }
  connections.sort((left, right) => left.displayName.localeCompare(right.displayName))
  return { connections, partial }
}

export function connectionSummary(context, resources) {
  const ids = resources.ids
  const providerStatus = String(resources.connection?.properties?.overallStatus ?? '')
  const provisioningState = String(resources.connection?.properties?.provisioningState ?? '')
  const authenticatedUser = String(resources.connection?.properties?.authenticatedUser?.name ?? '')
  const connectionRuntimeUrl = String(resources.connection?.properties?.connectionRuntimeUrl ?? '')
  const mcpEndpointUrl = String(resources.mcp?.properties?.mcpEndpointUrl ?? '')
  const providerStatuses = resources.connection?.properties?.statuses ?? []
  const statusValues = providerStatuses
    .map((entry) => String(entry?.status ?? '').toLowerCase())
  const providerError = providerStatuses.find((entry) => entry?.error)?.error
  const providerErrorCode = String(providerError?.code ?? '')
  const providerErrorMessage = String(providerError?.message ?? '')
  const operations = requiredOperations(resources.mcp)
  const infrastructureReady =
    resources.runtimePolicy?.properties?.principal?.identity?.objectId === context.runtimePrincipalId &&
    resources.deployerPolicy?.properties?.principal?.identity?.objectId === context.deployerPrincipalId &&
    String(resources.mcp?.properties?.state ?? '').toLowerCase() === 'enabled' &&
    isHttpsUrl(connectionRuntimeUrl) &&
    isHttpsUrl(mcpEndpointUrl) &&
    canonical(context.configuredMcpUrl) === canonical(mcpEndpointUrl) &&
    operations.length === 1 &&
    operations[0] === 'SendEmailV2'
  const authenticationReady =
    providerStatus.toLowerCase() === 'connected' &&
    provisioningState.toLowerCase() === 'succeeded' &&
    !!authenticatedUser &&
    statusValues.includes('connected')
  const authorizationRequired = infrastructureReady && (
    !authenticatedUser ||
    providerStatus.toLowerCase() === 'expired' ||
    providerErrorCode.toLowerCase() === 'unauthenticated'
  )
  const configured = infrastructureReady && authenticationReady
  const status = normalizeConnectionStatus(providerStatus, configured)
  if (
    providerStatus &&
    !['connected', 'expired'].includes(providerStatus.toLowerCase()) &&
    providerErrorCode.toLowerCase() !== 'unauthenticated'
  ) {
    console.warn('Unknown Connector Gateway status', {
      appResourceId: context.appResourceId,
      connectionResourceId: ids.connection,
      providerStatus,
    })
  }
  return {
    id: encodeConnectionId(ids.connection),
    displayName: String(resources.connection?.properties?.displayName ?? 'Office 365 Outlook'),
    service: 'Office 365 Outlook',
    allowedOperations: ['SendEmailV2'],
    status,
    providerStatus,
    provisioningState,
    authenticatedUser,
    authorizationRequired,
    providerErrorCode,
    providerErrorMessage,
    source: resources.source,
    subscriptionId: connectionResourceParts(ids.connection)?.subscriptionId ?? '',
    gatewayName: connectionResourceParts(ids.connection)?.gatewayName ?? '',
    resourceGroup: connectionResourceParts(ids.connection)?.resourceGroup ?? '',
    infrastructureReady,
    authenticationReady,
    detail: configured
      ? ''
      : !infrastructureReady
        ? canonical(context.configuredMcpUrl) !== canonical(mcpEndpointUrl)
          ? 'Function App endpoint configuration is incomplete. Retry setup to repair it.'
          : 'Connection setup is incomplete. Retry setup to repair Azure resources.'
        : authorizationRequired
          ? providerErrorMessage || 'Authorize this connection before it can be used.'
          : providerErrorMessage || 'Azure reports that this connection needs attention.',
    mcpEndpointUrl,
    portalUrl: connectorPortalUrl(ids.connection),
  }
}

function resourceSetIds(connectionId, mcpConfigName) {
  const connection = connectionResourceParts(connectionId)
  if (!connection) throw portalError(404, 'Connection not found.', 'connection_not_found')
  return {
    gateway: connection.gatewayId,
    connection: connectionId,
    mcpConfigId: `${connection.gatewayId}/mcpserverconfigs/${mcpConfigName}`,
  }
}

function optionalResource(result, action) {
  if (result.ok) return result.body
  if (result.status === 404) return null
  throw armFailure(result, action)
}

async function readResourceSet(accessToken, context, ids, source, requireOwnedGateway, fetchImpl) {
  const gatewayResult = await armJson(accessToken, ids.gateway, {}, fetchImpl)
  if (gatewayResult.status === 404) return null
  if (!gatewayResult.ok) throw armFailure(gatewayResult, 'Could not read Connector Gateway')
  if (requireOwnedGateway && !isOwnedGateway(gatewayResult.body, context.appResourceId)) {
    throw portalError(409, 'The deterministic Connector Gateway name is already owned by another app.', 'connection_conflict')
  }
  const [connectionResult, runtimePolicyResult, deployerPolicyResult, mcpResult] = await Promise.all([
    armJson(accessToken, ids.connection, {}, fetchImpl),
    armJson(accessToken, accessPolicyId(ids.connection, context.runtimePrincipalId), {}, fetchImpl),
    armJson(accessToken, accessPolicyId(ids.connection, context.deployerPrincipalId), {}, fetchImpl),
    armJson(accessToken, ids.mcpConfigId, {}, fetchImpl),
  ])
  return {
    ids,
    source,
    gateway: gatewayResult.body,
    connection: optionalResource(connectionResult, 'Could not read Outlook connection'),
    runtimePolicy: optionalResource(runtimePolicyResult, 'Could not read Function App access policy'),
    deployerPolicy: optionalResource(deployerPolicyResult, 'Could not read signed-in user access policy'),
    mcp: optionalResource(mcpResult, 'Could not read Outlook MCP configuration'),
  }
}

async function readManagedResources(accessToken, context, fetchImpl) {
  return readResourceSet(
    accessToken,
    context,
    outlookResourceIds(context.appResourceId),
    'Created',
    true,
    fetchImpl,
  )
}

async function readPersistedConnectionResources(accessToken, context, fetchImpl) {
  const connectionId = String(context.configuredConnectionId ?? '').trim()
  const connection = connectionResourceParts(connectionId)
  if (!connection) return null

  const managedIds = outlookResourceIds(context.appResourceId)
  const managed = canonical(connectionId) === canonical(managedIds.connection)
  const mcpConfigName = managed
    ? outlookResourceNames(context.appResourceId).mcpConfig
    : outlookResourceNames(context.appResourceId).attachedMcpConfig
  const resources = await readResourceSet(
    accessToken,
    context,
    resourceSetIds(connectionId, mcpConfigName),
    managed ? 'Created' : 'Existing',
    managed,
    fetchImpl,
  )
  if (!resources) return null
  if (!managed && isGatewayManagedByOtherApp(resources.gateway, context.appResourceId)) {
    throw portalError(409, 'The configured Connector Gateway is managed by another app.', 'connection_conflict')
  }
  if (
    resources.connection &&
    String(resources.connection?.properties?.connectorName ?? '').toLowerCase() !== 'office365'
  ) {
    throw portalError(409, 'The configured connection is not Office 365 Outlook.', 'connection_conflict')
  }
  if (resources.mcp && !mcpConfigTargetsConnection(resources.mcp, connection.connectionName)) {
    throw portalError(409, 'The configured Outlook MCP resource is invalid.', 'connection_conflict')
  }
  return resources
}

function mcpConfigTargetsConnection(mcp, connectionName) {
  const connectors = Array.isArray(mcp?.properties?.connectors) ? mcp.properties.connectors : []
  return String(mcp?.properties?.state ?? '').toLowerCase() === 'enabled' &&
    connectors.length === 1 &&
    canonical(connectors[0]?.connectionName) === canonical(connectionName) &&
    requiredOperations(mcp).length === 1 &&
    requiredOperations(mcp)[0] === 'SendEmailV2'
}

async function findAttachedResources(accessToken, context, fetchImpl) {
  const { subscriptionId } = appResourceParts(context.appResourceId)
  const gatewayList = await armList(
    accessToken,
    `/subscriptions/${subscriptionId}/providers/Microsoft.Web/connectorGateways`,
    fetchImpl,
  )
  if (!gatewayList.ok && gatewayList.status === 404) return null
  if (!gatewayList.ok) throw armFailure(gatewayList, 'Could not recover existing Outlook attachment')

  const configName = outlookResourceNames(context.appResourceId).attachedMcpConfig
  const matches = []
  const gateways = gatewayList.items.filter(
    (gateway) => gatewayResourceParts(gateway?.id) && !isGatewayManagedByOtherApp(gateway, context.appResourceId),
  )
  for (let offset = 0; offset < gateways.length; offset += 5) {
    const batch = gateways.slice(offset, offset + 5)
    const results = await Promise.all(batch.map(async (gateway) => ({
      gateway,
      result: await armJson(accessToken, `${gateway.id}/mcpserverconfigs/${configName}`, {}, fetchImpl),
    })))
    for (const { gateway, result } of results) {
      if ([403, 404].includes(result.status)) continue
      if (!result.ok) throw armFailure(result, `Could not read Outlook attachment on ${gateway.name}`)
      const connector = result.body?.properties?.connectors?.[0]
      const connectionName = String(connector?.connectionName ?? '')
      if (!connectionName || connectionName.includes('/') || !mcpConfigTargetsConnection(result.body, connectionName)) {
        throw portalError(409, 'The existing Outlook attachment configuration is invalid.', 'connection_conflict')
      }
      matches.push(resourceSetIds(`${gateway.id}/connections/${connectionName}`, configName))
    }
  }
  if (matches.length > 1) {
    throw portalError(409, 'Multiple existing Outlook attachments were found for this app.', 'connection_ambiguous')
  }
  if (!matches[0]) return null
  return readResourceSet(accessToken, context, matches[0], 'Existing', false, fetchImpl)
}

async function configuredResources(accessToken, context, fetchImpl) {
  const persisted = await readPersistedConnectionResources(accessToken, context, fetchImpl)
  if (persisted) return persisted
  const managed = await readManagedResources(accessToken, context, fetchImpl)
  if (managed) return managed
  return findAttachedResources(accessToken, context, fetchImpl)
}

function resourcesConverged(resources) {
  if (!resources?.connection || !resources.runtimePolicy || !resources.deployerPolicy || !resources.mcp) return false
  const provisioningState = String(resources.connection.properties?.provisioningState ?? '').toLowerCase()
  return !['creating', 'updating', 'accepted'].includes(provisioningState) &&
    isHttpsUrl(resources.mcp.properties?.mcpEndpointUrl)
}

async function readConvergedResources(accessToken, context, ids, source, requireOwnedGateway, fetchImpl) {
  let resources = null
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    resources = await readResourceSet(accessToken, context, ids, source, requireOwnedGateway, fetchImpl)
    if (resourcesConverged(resources) || attempt === 3) return resources
    await new Promise((resolve) => setTimeout(resolve, 250 * 2 ** (attempt - 1)))
  }
  return resources
}

export async function listOutlookConnections(accessToken, context, fetchImpl = fetch) {
  const resources = await configuredResources(accessToken, context, fetchImpl)
  return resources ? [connectionSummary(context, resources)] : []
}

export async function getOutlookConnection(accessToken, context, encodedId, fetchImpl = fetch) {
  const resourceId = decodeConnectionId(encodedId)
  const resources = await configuredResources(accessToken, context, fetchImpl)
  if (!resources || canonical(resources.ids.connection) !== canonical(resourceId)) {
    throw portalError(404, 'Connection not found.', 'connection_not_found')
  }
  return connectionSummary(context, resources)
}

export async function createOutlookConnection(accessToken, context, displayName, fetchImpl = fetch) {
  const name = String(displayName ?? '').trim()
  if (!name || name.length > 80) throw portalError(400, 'Connection name must be between 1 and 80 characters.', 'invalid_connection_name')
  const ids = outlookResourceIds(context.appResourceId)
  const managed = await readManagedResources(accessToken, context, fetchImpl)
  if (!managed) {
    const attached = await findAttachedResources(accessToken, context, fetchImpl)
    if (attached) throw portalError(409, 'This app already uses a different Outlook connection.', 'connection_conflict')
  }

  const resources = [
    [ids.gateway, {
      location: context.location,
      tags: { [OWNER_TAG]: 'managed', [APP_TAG]: context.appResourceId },
      identity: { type: 'SystemAssigned' },
      properties: {},
    }],
    [ids.connection, { properties: { connectorName: 'office365', displayName: name } }],
    [accessPolicyId(ids.connection, context.runtimePrincipalId), {
      properties: { principal: { type: 'ActiveDirectory', identity: { objectId: context.runtimePrincipalId, tenantId: context.tenantId } } },
    }],
    [accessPolicyId(ids.connection, context.deployerPrincipalId), {
      properties: { principal: { type: 'ActiveDirectory', identity: { objectId: context.deployerPrincipalId, tenantId: context.tenantId } } },
    }],
    [ids.mcpConfigId, { properties: outlookMcpProperties(ids.connection.split('/').pop()) }],
  ]
  for (const [resourceId, body] of resources) {
    const result = await armJson(accessToken, resourceId, { method: 'PUT', body }, fetchImpl)
    if (!result.ok) throw armFailure(result, `Could not create ${resourceId.split('/').pop()}`)
  }
  const created = await readConvergedResources(accessToken, context, ids, 'Created', true, fetchImpl)
  if (!created) throw portalError(502, 'Connector Gateway was not available after creation.', 'connector_not_ready')
  return connectionSummary(context, created)
}

export async function attachOutlookConnection(accessToken, context, encodedId, fetchImpl = fetch) {
  const connectionId = validateConnectionId(
    encodedId,
    context.appResourceId,
    context.connectorSubscriptionId,
  )
  const selected = connectionResourceParts(connectionId)
  const current = await configuredResources(accessToken, context, fetchImpl)
  if (current && canonical(current.ids.connection) !== canonical(connectionId)) {
    throw portalError(409, 'This app already uses a different Outlook connection.', 'connection_conflict')
  }
  if (current?.source === 'Created') {
    return createOutlookConnection(
      accessToken,
      context,
      String(current.connection?.properties?.displayName ?? 'Office 365 Outlook'),
      fetchImpl,
    )
  }

  const gatewayResult = await armJson(accessToken, selected.gatewayId, {}, fetchImpl)
  if (!gatewayResult.ok) {
    if (gatewayResult.status === 404) throw portalError(404, 'Connection not found.', 'connection_not_found')
    throw armFailure(gatewayResult, 'Could not read selected Connector Gateway')
  }
  if (isGatewayManagedByOtherApp(gatewayResult.body, context.appResourceId)) {
    throw portalError(409, 'The selected Connector Gateway is managed by another app.', 'connection_conflict')
  }
  const connectionResult = await armJson(accessToken, connectionId, {}, fetchImpl)
  if (!connectionResult.ok) {
    if (connectionResult.status === 404) throw portalError(404, 'Connection not found.', 'connection_not_found')
    throw armFailure(connectionResult, 'Could not read selected Outlook connection')
  }
  if (String(connectionResult.body?.properties?.connectorName ?? '').toLowerCase() !== 'office365') {
    throw portalError(409, 'The selected connection is not Office 365 Outlook.', 'connection_conflict')
  }

  const configName = outlookResourceNames(context.appResourceId).attachedMcpConfig
  const ids = resourceSetIds(connectionId, configName)
  const existingConfig = await armJson(accessToken, ids.mcpConfigId, {}, fetchImpl)
  if (existingConfig.ok && !mcpConfigTargetsConnection(existingConfig.body, selected.connectionName)) {
    throw portalError(409, 'The app-specific MCP configuration already targets another connection.', 'connection_conflict')
  }
  if (!existingConfig.ok && existingConfig.status !== 404) {
    throw armFailure(existingConfig, 'Could not inspect app-specific MCP configuration')
  }

  const resources = [
    [accessPolicyId(connectionId, context.runtimePrincipalId), {
      properties: { principal: { type: 'ActiveDirectory', identity: { objectId: context.runtimePrincipalId, tenantId: context.tenantId } } },
    }],
    [accessPolicyId(connectionId, context.deployerPrincipalId), {
      properties: { principal: { type: 'ActiveDirectory', identity: { objectId: context.deployerPrincipalId, tenantId: context.tenantId } } },
    }],
    [ids.mcpConfigId, { properties: outlookMcpProperties(selected.connectionName) }],
  ]
  for (const [resourceId, body] of resources) {
    const result = await armJson(accessToken, resourceId, { method: 'PUT', body }, fetchImpl)
    if (!result.ok) throw armFailure(result, `Could not configure ${resourceId.split('/').pop()}`)
  }
  const attached = await readConvergedResources(accessToken, context, ids, 'Existing', false, fetchImpl)
  if (!attached) throw portalError(502, 'The selected Outlook connection was not available after configuration.', 'connector_not_ready')
  return connectionSummary(context, attached)
}

export async function deleteOutlookConnection(accessToken, context, encodedId, fetchImpl = fetch) {
  const resourceId = decodeConnectionId(encodedId)
  const current = await configuredResources(accessToken, context, fetchImpl)
  if (!current || canonical(current.ids.connection) !== canonical(resourceId)) {
    throw portalError(
      409,
      'The connection no longer matches the connection configured for this app. Refresh before removing it.',
      'connection_mismatch',
    )
  }

  if (current.source === 'Created') {
    const expectedGateway = outlookResourceIds(context.appResourceId).gateway
    if (canonical(current.ids.gateway) !== canonical(expectedGateway) || !isOwnedGateway(current.gateway, context.appResourceId)) {
      throw portalError(409, 'The Connector Gateway is not owned by this app.', 'connection_conflict')
    }
    const outcome = await deleteArmResource(accessToken, current.ids.gateway, fetchImpl)
    return {
      source: 'Created',
      azure: outcome === 'deletion_pending'
        ? 'deletion_pending'
        : outcome === 'already_absent' ? 'already_absent' : 'deleted',
    }
  }

  const runtimePolicyId = accessPolicyId(current.ids.connection, context.runtimePrincipalId)
  const policyOutcome = await deleteArmResource(accessToken, runtimePolicyId, fetchImpl)
  const configOutcome = await deleteArmResource(accessToken, current.ids.mcpConfigId, fetchImpl)
  return {
    source: 'Existing',
    azure: [policyOutcome, configOutcome].includes('deletion_pending') ? 'deletion_pending' : 'detached',
  }
}

export async function testOutlookConnection(accessToken, context, encodedId, fetchImpl = fetch) {
  const resourceId = decodeConnectionId(encodedId)
  const resources = await configuredResources(accessToken, context, fetchImpl)
  if (!resources || canonical(resources.ids.connection) !== canonical(resourceId)) {
    throw portalError(404, 'Connection not found.', 'connection_not_found')
  }
  const operations = requiredOperations(resources.mcp)
  const providerStatuses = (resources.connection?.properties?.statuses ?? [])
    .map((entry) => String(entry?.status ?? '').toLowerCase())
  const providerError = (resources.connection?.properties?.statuses ?? [])
    .find((entry) => entry?.error)?.error
  const checks = [
    {
      name: 'Provisioning completed',
      ok: String(resources.connection?.properties?.provisioningState ?? '').toLowerCase() === 'succeeded',
      detail: String(resources.connection?.properties?.provisioningState ?? 'Not reported'),
    },
    {
      name: 'Microsoft sign-in',
      ok: !!resources.connection?.properties?.authenticatedUser?.name,
      detail: String(resources.connection?.properties?.authenticatedUser?.name ?? providerError?.message ?? 'Not authorized'),
    },
    {
      name: 'Provider connected',
      ok: String(resources.connection?.properties?.overallStatus ?? '').toLowerCase() === 'connected' && providerStatuses.includes('connected'),
      detail: String(providerError?.message ?? resources.connection?.properties?.overallStatus ?? 'Not reported'),
    },
    { name: 'Function App access', ok: resources.runtimePolicy?.properties?.principal?.identity?.objectId === context.runtimePrincipalId },
    { name: 'Signed-in user access', ok: resources.deployerPolicy?.properties?.principal?.identity?.objectId === context.deployerPrincipalId },
    { name: 'MCP server enabled', ok: String(resources.mcp?.properties?.state ?? '').toLowerCase() === 'enabled' },
    { name: 'Function App endpoint configured', ok: canonical(context.configuredMcpUrl) === canonical(resources.mcp?.properties?.mcpEndpointUrl) },
    {
      name: 'Runtime endpoints available',
      ok: isHttpsUrl(resources.connection?.properties?.connectionRuntimeUrl) &&
        isHttpsUrl(resources.mcp?.properties?.mcpEndpointUrl),
    },
    { name: 'Send email only', ok: operations.length === 1 && operations[0] === 'SendEmailV2' },
  ]
  return { ok: checks.every((check) => check.ok), checks, connection: connectionSummary(context, resources) }
}
