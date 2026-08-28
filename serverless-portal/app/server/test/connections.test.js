import assert from 'node:assert/strict'
import test from 'node:test'

import {
  attachOutlookConnection,
  coordinateOutlookConnectionSetup,
  connectionSummary,
  connectorPortalUrl,
  coordinateOutlookConnectionRemoval,
  createOutlookConnection,
  deleteOutlookConnection,
  decodeConnectionId,
  encodeConnectionId,
  ensureOutlookMcpSource,
  functionAppResourceId,
  listOutlookConnectionCandidates,
  listOutlookConnections,
  normalizeConnectionStatus,
  outlookAppSettings,
  outlookMcpProperties,
  outlookMcpSourceDefinition,
  outlookResourceIds,
  outlookResourceNames,
  removeOutlookMcpSource,
  testOutlookConnection,
  validateConnectionId,
} from '../src/connections.js'

const appId = '/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-demo/providers/Microsoft.Web/sites/stock-report'

test('builds a validated Function App resource ID for pre-provision connection discovery', () => {
  assert.equal(
    functionAppResourceId('11111111-1111-1111-1111-111111111111', 'rg-demo', 'stock-report'),
    appId,
  )
  assert.throws(
    () => functionAppResourceId('not-a-subscription', 'rg-demo', 'stock-report'),
    (error) => error.status === 400 && error.portalCode === 'invalid_app_target',
  )
  assert.throws(
    () => functionAppResourceId('11111111-1111-1111-1111-111111111111', 'rg-demo?api-version=1', 'stock-report'),
    (error) => error.status === 400 && error.portalCode === 'invalid_app_target',
  )
})

test('derives stable app-scoped Outlook resource names', () => {
  const first = outlookResourceNames(appId)
  const second = outlookResourceNames(appId.toUpperCase())

  assert.deepEqual(first, second)
  assert.match(first.gateway, /^cg-o365-[a-f0-9]{12}$/)
  assert.equal(first.connection, 'office365-outlook')
  assert.equal(first.mcpConfig, 'Office-365-Outlook-send-email-only')
})

test('encodes and validates same-subscription connection IDs', () => {
  const expected = outlookResourceIds(appId).connection
  const encoded = encodeConnectionId(expected)

  assert.equal(decodeConnectionId(encoded), expected)
  assert.equal(validateConnectionId(encoded, appId), expected)
  const crossSubscription = expected.replace(
    '11111111-1111-1111-1111-111111111111',
    '22222222-2222-2222-2222-222222222222',
  )
  assert.throws(
    () => validateConnectionId(encodeConnectionId(crossSubscription), appId),
    (error) => error.status === 404 && error.portalCode === 'connection_not_found',
  )
  assert.throws(
    () => decodeConnectionId(encodeConnectionId(outlookResourceIds(appId).gateway)),
    (error) => error.status === 400 && error.portalCode === 'invalid_connection_id',
  )
})

test('normalizes provider status fail-closed', () => {
  assert.equal(normalizeConnectionStatus('Connected', true), 'Connected')
  assert.equal(normalizeConnectionStatus('connected', false), 'Action required')
  assert.equal(normalizeConnectionStatus('Expired', true), 'Expired')
  assert.equal(normalizeConnectionStatus('PendingAuth', true), 'Action required')
  assert.equal(normalizeConnectionStatus('', true), 'Action required')
})

test('MCP configuration exposes only SendEmailV2', () => {
  const properties = outlookMcpProperties()
  const operations = properties.connectors.flatMap((connector) => connector.operations)

  assert.equal(properties.state, 'Enabled')
  assert.deepEqual(operations.map((operation) => operation.name), ['SendEmailV2'])
  assert.equal(properties.settings.textOnlyContent, true)
})

test('removes only the Outlook server from mcp.json', () => {
  const source = JSON.stringify({
    version: 1,
    servers: {
      'office365-outlook': { url: '$O365_MCP_SERVER_URL', tools: ['office365_SendEmailV2'] },
      'microsoft-learn': { url: 'https://learn.microsoft.com/api/mcp' },
    },
    metadata: { owner: 'customer' },
  })

  const result = removeOutlookMcpSource(source)

  assert.equal(result.changed, true)
  assert.deepEqual(JSON.parse(result.content), {
    version: 1,
    servers: { 'microsoft-learn': { url: 'https://learn.microsoft.com/api/mcp' } },
    metadata: { owner: 'customer' },
  })
})

test('Outlook source removal is idempotent and rejects invalid JSON', () => {
  const source = '{"servers":{"microsoft-learn":{"url":"https://learn.microsoft.com/api/mcp"}}}'

  assert.deepEqual(removeOutlookMcpSource(source), { content: source, changed: false })
  assert.deepEqual(removeOutlookMcpSource(''), { content: '', changed: false })
  assert.throws(
    () => removeOutlookMcpSource('{not-json'),
    (error) => error.status === 409 && error.portalCode === 'invalid_mcp_source',
  )
})

test('plans Outlook MCP source for deployed, draft, and missing entries', () => {
  const outlook = {
    type: 'http',
    url: '$O365_MCP_SERVER_URL',
    tools: ['office365_SendEmailV2'],
    auth: {
      scope: 'https://apihub.azure.com/.default',
      client_id: '$O365_MCP_CLIENT_ID',
    },
  }
  const configured = JSON.stringify({ servers: { 'office365-outlook': outlook } })

  assert.deepEqual(ensureOutlookMcpSource(configured, 'deployed'), {
    content: configured,
    changed: false,
    deploymentRequired: false,
    state: 'deployed',
  })
  assert.deepEqual(ensureOutlookMcpSource(configured, 'draft'), {
    content: configured,
    changed: false,
    deploymentRequired: true,
    state: 'draft',
  })
  const withoutClientId = JSON.stringify({
    servers: {
      'office365-outlook': {
        type: 'http',
        url: '$O365_MCP_SERVER_URL',
        tools: ['office365_SendEmailV2'],
        auth: { scope: 'https://apihub.azure.com/.default' },
      },
    },
  })
  assert.equal(ensureOutlookMcpSource(withoutClientId, 'deployed').deploymentRequired, false)

  const missing = JSON.stringify({
    servers: { 'microsoft-learn': { type: 'http', url: 'https://learn.microsoft.com/api/mcp' } },
    metadata: { owner: 'customer' },
  })
  const result = ensureOutlookMcpSource(missing, 'deployed')
  assert.equal(result.changed, true)
  assert.equal(result.deploymentRequired, true)
  assert.equal(result.state, 'draft')
  assert.deepEqual(JSON.parse(result.content), {
    servers: {
      'microsoft-learn': { type: 'http', url: 'https://learn.microsoft.com/api/mcp' },
      'office365-outlook': outlook,
    },
    metadata: { owner: 'customer' },
  })
})

test('Outlook MCP source planning replaces an unsupported entry and rejects invalid JSON', () => {
  const outdated = JSON.stringify({
    servers: {
      'office365-outlook': { type: 'http', url: 'https://wrong.example.test', tools: ['*'] },
    },
  })
  const result = ensureOutlookMcpSource(outdated, 'deployed')

  assert.equal(result.changed, true)
  assert.deepEqual(JSON.parse(result.content).servers['office365-outlook'].tools, ['office365_SendEmailV2'])
  assert.throws(
    () => ensureOutlookMcpSource('{not-json', 'deployed'),
    (error) => error.status === 409 && error.portalCode === 'invalid_mcp_source',
  )
})

test('setup coordinator rolls back a newly staged draft when Azure setup fails', async () => {
  const calls = []
  const failure = new Error('Azure setup failed')

  await assert.rejects(
    () => coordinateOutlookConnectionSetup({
      sourceBefore: { content: '{"servers":{}}', source: 'deployed' },
      stageSource: async () => calls.push('stage-source'),
      rollbackSource: async () => calls.push('rollback-source'),
      configureAzure: async () => {
        calls.push('configure-azure')
        throw failure
      },
    }),
    (error) => error === failure && error.sourceCleanup === 'rolled_back',
  )
  assert.deepEqual(calls, ['stage-source', 'configure-azure', 'rollback-source'])
})

test('setup coordinator preserves a correct draft and reports deployment required', async () => {
  const configured = JSON.stringify({
    servers: { 'office365-outlook': outlookMcpSourceDefinition() },
  })
  let staged = false
  const result = await coordinateOutlookConnectionSetup({
    sourceBefore: { content: configured, source: 'draft' },
    stageSource: async () => { staged = true },
    rollbackSource: async () => undefined,
    configureAzure: async () => ({ id: 'connection' }),
  })

  assert.equal(staged, false)
  assert.deepEqual(result, {
    connection: { id: 'connection' },
    source: {
      path: 'mcp.json',
      changed: false,
      deploymentRequired: true,
      state: 'draft',
    },
  })
})

test('removal coordinator restores source and setting when Azure cleanup fails', async () => {
  const calls = []
  const failure = Object.assign(new Error('Azure delete failed'), { status: 502, portalCode: 'delete_failed' })

  await assert.rejects(
    () => coordinateOutlookConnectionRemoval({
      sourceBefore: {
        content: '{"servers":{"office365-outlook":{"url":"$O365_MCP_SERVER_URL"}}}',
        source: 'draft',
      },
      stageSource: async () => calls.push('stage-source'),
      rollbackSource: async () => calls.push('rollback-source'),
      removeSetting: async () => {
        calls.push('remove-setting')
        return { removed: true, key: 'O365_MCP_SERVER_URL', value: 'https://example.test/mcp' }
      },
      restoreSetting: async () => calls.push('restore-setting'),
      deleteAzure: async () => {
        calls.push('delete-azure')
        throw failure
      },
    }),
    (error) => error === failure &&
      error.cleanup.sourceDraft === 'rolled_back' &&
      error.cleanup.appSetting === 'restored' &&
      error.cleanup.azure === 'failed',
  )
  assert.deepEqual(calls, [
    'stage-source',
    'remove-setting',
    'delete-azure',
    'rollback-source',
    'restore-setting',
  ])
})

test('removal coordinator restores the known endpoint when setting removal errors', async () => {
  const calls = []
  const failure = new Error('Setting update response failed')

  await assert.rejects(
    () => coordinateOutlookConnectionRemoval({
      sourceBefore: {
        content: '{"servers":{"office365-outlook":{"url":"$O365_MCP_SERVER_URL"}}}',
        source: 'deployed',
      },
      settingBefore: {
        removed: true,
        key: 'O365_MCP_SERVER_URL',
        value: 'https://example.test/mcp',
      },
      stageSource: async () => calls.push('stage-source'),
      rollbackSource: async () => calls.push('rollback-source'),
      removeSetting: async () => {
        calls.push('remove-setting')
        throw failure
      },
      restoreSetting: async () => calls.push('restore-setting'),
      deleteAzure: async () => calls.push('delete-azure'),
    }),
    (error) => error === failure &&
      error.cleanup.sourceDraft === 'rolled_back' &&
      error.cleanup.appSetting === 'restored' &&
      error.cleanup.azure === 'failed',
  )
  assert.deepEqual(calls, ['stage-source', 'remove-setting', 'rollback-source', 'restore-setting'])
})

test('creates and verifies the complete send-only ARM resource set', async () => {
  const context = {
    appResourceId: appId,
    location: 'eastus2',
    runtimePrincipalId: 'runtime-principal',
    deployerPrincipalId: 'deployer-principal',
    tenantId: 'tenant-id',
    configuredMcpUrl: 'https://example.test/mcp',
  }
  const ids = outlookResourceIds(appId)
  const stored = new Map()
  const requests = []
  const mockFetch = async (url, options = {}) => {
    const resourceId = new URL(url).pathname
    const method = options.method ?? 'GET'
    requests.push({ resourceId, method, body: options.body ? JSON.parse(options.body) : null })
    if (method === 'PUT') {
      const input = JSON.parse(options.body)
      const body = {
        id: resourceId,
        name: resourceId.split('/').pop(),
        ...input,
        ...(resourceId === ids.connection
          ? {
              properties: {
                ...input.properties,
                overallStatus: 'Connected',
                provisioningState: 'Succeeded',
                authenticatedUser: { name: 'demo@example.com' },
                statuses: [{ status: 'Connected' }],
                connectionRuntimeUrl: 'https://example.test/runtime',
              },
            }
          : {}),
        ...(resourceId === ids.mcpConfigId
          ? { properties: { ...input.properties, mcpEndpointUrl: 'https://example.test/mcp' } }
          : {}),
      }
      stored.set(resourceId.toLowerCase(), body)
      return new Response(JSON.stringify(body), { status: 201, headers: { 'Content-Type': 'application/json' } })
    }
    const body = stored.get(resourceId.toLowerCase())
    return new Response(body ? JSON.stringify(body) : '', {
      status: body ? 200 : 404,
      headers: body ? { 'Content-Type': 'application/json' } : {},
    })
  }

  const connection = await createOutlookConnection('token', context, 'Outlook reports', mockFetch)
  const result = await testOutlookConnection('token', context, connection.id, mockFetch)

  assert.equal(connection.status, 'Connected')
  assert.equal(connection.displayName, 'Outlook reports')
  assert.equal(result.ok, true)
  assert.deepEqual(result.checks.map((check) => check.name), [
    'Provisioning completed',
    'Microsoft sign-in',
    'Provider connected',
    'Function App access',
    'Signed-in user access',
    'MCP server enabled',
    'Function App endpoint configured',
    'Runtime endpoints available',
    'Send email only',
  ])
  const puts = requests.filter((request) => request.method === 'PUT')
  assert.equal(puts.length, 5)
  assert.equal(puts[0].body.tags['azfunc-agents-app-id'], appId)
  assert.deepEqual(
    puts.at(-1).body.properties.connectors[0].operations.map((operation) => operation.name),
    ['SendEmailV2'],
  )
})

test('retries transient ARM responses during idempotent setup', async () => {
  const context = {
    appResourceId: appId,
    location: 'eastus2',
    runtimePrincipalId: 'runtime-principal',
    deployerPrincipalId: 'deployer-principal',
    tenantId: 'tenant-id',
  }
  let attempts = 0
  const mockFetch = async () => {
    attempts += 1
    if (attempts < 3) return new Response('', { status: 503 })
    return new Response('', { status: 404 })
  }

  await assert.rejects(
    () => createOutlookConnection('token', context, 'Outlook reports', mockFetch),
    (error) => error.portalCode === 'connector_arm_failed',
  )
  assert.equal(attempts, 5)
})

test('lists eligible existing Office 365 connections without exposing another app gateway', async () => {
  const context = {
    appResourceId: appId,
    subscriptionId: '11111111-1111-1111-1111-111111111111',
  }
  const allowedGateway = `${appId.split('/providers/')[0]}/providers/Microsoft.Web/connectorGateways/shared-gateway`
  const foreignGateway = `${appId.split('/providers/')[0]}/providers/Microsoft.Web/connectorGateways/foreign-gateway`
  const mockFetch = async (url) => {
    const resourceId = new URL(url).pathname
    if (resourceId.endsWith('/providers/Microsoft.Web/connectorGateways')) {
      return Response.json({ value: [
        { id: allowedGateway, name: 'shared-gateway', tags: {} },
        {
          id: foreignGateway,
          name: 'foreign-gateway',
          tags: {
            'azfunc-agents-portal': 'managed',
            'azfunc-agents-app-id': `${appId}-other`,
          },
        },
      ] })
    }
    if (resourceId === `${allowedGateway}/connections`) {
      return Response.json({ value: [
        {
          id: `${allowedGateway}/connections/outlook-existing`,
          name: 'outlook-existing',
          properties: {
            connectorName: 'office365',
            displayName: 'Finance mailbox',
            overallStatus: 'Connected',
            authenticatedUser: { name: 'finance@example.com' },
          },
        },
        {
          id: `${allowedGateway}/connections/teams-existing`,
          name: 'teams-existing',
          properties: { connectorName: 'teams', displayName: 'Teams' },
        },
      ] })
    }
    throw new Error(`Unexpected ARM request: ${resourceId}`)
  }

  const result = await listOutlookConnectionCandidates('token', context, mockFetch)

  assert.equal(result.partial, false)
  assert.deepEqual(result.connections.map((connection) => ({
    displayName: connection.displayName,
    gatewayName: connection.gatewayName,
    authenticatedUser: connection.authenticatedUser,
  })), [{
    displayName: 'Finance mailbox',
    gatewayName: 'shared-gateway',
    authenticatedUser: 'finance@example.com',
  }])
})

test('lists and validates candidates from an explicitly selected connector subscription', async () => {
  const connectorSubscriptionId = '22222222-2222-2222-2222-222222222222'
  const context = { appResourceId: appId, connectorSubscriptionId }
  const gatewayId = `/subscriptions/${connectorSubscriptionId}/resourceGroups/remote-rg/providers/Microsoft.Web/connectorGateways/remote-gateway`
  const connectionId = `${gatewayId}/connections/remote-outlook`
  const requests = []
  const mockFetch = async (url) => {
    const resourceId = new URL(url).pathname
    requests.push(resourceId)
    if (resourceId === `/subscriptions/${connectorSubscriptionId}/providers/Microsoft.Web/connectorGateways`) {
      return Response.json({ value: [{ id: gatewayId, name: 'remote-gateway', tags: {} }] })
    }
    if (resourceId === `${gatewayId}/connections`) {
      return Response.json({ value: [{
        id: connectionId,
        name: 'remote-outlook',
        properties: {
          connectorName: 'office365',
          displayName: 'Remote mailbox',
          overallStatus: 'Connected',
        },
      }] })
    }
    throw new Error(`Unexpected ARM request: ${resourceId}`)
  }

  const result = await listOutlookConnectionCandidates('token', context, mockFetch)

  assert.equal(result.connections[0].subscriptionId, connectorSubscriptionId)
  assert.equal(validateConnectionId(encodeConnectionId(connectionId), appId, connectorSubscriptionId), connectionId)
  assert.throws(
    () => validateConnectionId(
      encodeConnectionId(connectionId),
      appId,
      '33333333-3333-3333-3333-333333333333',
    ),
    (error) => error.status === 404 && error.portalCode === 'connection_not_found',
  )
  assert.equal(requests.some((resourceId) => resourceId.includes('11111111-1111-1111-1111-111111111111')), false)
})

test('recovers a remote attachment directly from the persisted connection ID', async () => {
  const connectorSubscriptionId = '22222222-2222-2222-2222-222222222222'
  const gatewayId = `/subscriptions/${connectorSubscriptionId}/resourceGroups/remote-rg/providers/Microsoft.Web/connectorGateways/remote-gateway`
  const connectionId = `${gatewayId}/connections/remote-outlook`
  const configId = `${gatewayId}/mcpserverconfigs/${outlookResourceNames(appId).attachedMcpConfig}`
  const context = {
    appResourceId: appId,
    runtimePrincipalId: 'runtime-principal',
    deployerPrincipalId: 'deployer-principal',
    configuredMcpUrl: 'https://example.test/remote-mcp',
    configuredConnectionId: connectionId,
  }
  const requests = []
  const mockFetch = async (url) => {
    const resourceId = new URL(url).pathname
    requests.push(resourceId)
    if (resourceId === gatewayId) return Response.json({ id: gatewayId, tags: {} })
    if (resourceId === connectionId) {
      return Response.json({
        id: connectionId,
        name: 'remote-outlook',
        properties: {
          connectorName: 'office365',
          displayName: 'Remote mailbox',
          overallStatus: 'Connected',
          provisioningState: 'Succeeded',
          authenticatedUser: { name: 'remote@example.com' },
          statuses: [{ status: 'Connected' }],
          connectionRuntimeUrl: 'https://example.test/runtime',
        },
      })
    }
    if (resourceId === configId) {
      return Response.json({
        id: configId,
        properties: {
          ...outlookMcpProperties('remote-outlook'),
          mcpEndpointUrl: 'https://example.test/remote-mcp',
        },
      })
    }
    if (resourceId.includes('/accessPolicies/')) {
      const principalId = resourceId.split('/').pop()
      return Response.json({ properties: { principal: { identity: { objectId: principalId } } } })
    }
    throw new Error(`Unexpected ARM request: ${resourceId}`)
  }

  const connections = await listOutlookConnections('token', context, mockFetch)

  assert.equal(connections[0].subscriptionId, connectorSubscriptionId)
  assert.equal(connections[0].displayName, 'Remote mailbox')
  assert.equal(connections[0].status, 'Connected')
  assert.equal(requests.some((resourceId) => resourceId.includes('/providers/Microsoft.Web/connectorGateways') && resourceId.includes('11111111-1111-1111-1111-111111111111')), false)
})

test('marks candidate discovery partial when an individual gateway is inaccessible', async () => {
  const context = { appResourceId: appId }
  const base = appId.split('/providers/')[0]
  const readableGateway = `${base}/providers/Microsoft.Web/connectorGateways/readable`
  const forbiddenGateway = `${base}/providers/Microsoft.Web/connectorGateways/forbidden`
  const mockFetch = async (url) => {
    const resourceId = new URL(url).pathname
    if (resourceId.endsWith('/providers/Microsoft.Web/connectorGateways')) {
      return Response.json({ value: [
        { id: readableGateway, name: 'readable', tags: {} },
        { id: forbiddenGateway, name: 'forbidden', tags: {} },
      ] })
    }
    if (resourceId === `${forbiddenGateway}/connections`) {
      return Response.json({ error: { message: 'Forbidden' } }, { status: 403 })
    }
    if (resourceId === `${readableGateway}/connections`) {
      return Response.json({ value: [{
        id: `${readableGateway}/connections/outlook`,
        name: 'outlook',
        properties: { connectorName: 'office365', displayName: 'Readable mailbox' },
      }] })
    }
    throw new Error(`Unexpected ARM request: ${resourceId}`)
  }

  const result = await listOutlookConnectionCandidates('token', context, mockFetch)

  assert.equal(result.partial, true)
  assert.deepEqual(result.connections.map((connection) => connection.displayName), ['Readable mailbox'])
})

test('attaches an existing connection without updating its gateway or connection', async () => {
  const connectorSubscriptionId = '22222222-2222-2222-2222-222222222222'
  const context = {
    appResourceId: appId,
    subscriptionId: '11111111-1111-1111-1111-111111111111',
    connectorSubscriptionId,
    runtimePrincipalId: 'runtime-principal',
    deployerPrincipalId: 'deployer-principal',
    tenantId: 'tenant-id',
    configuredMcpUrl: 'https://example.test/shared-mcp',
  }
  const gatewayId = `/subscriptions/${connectorSubscriptionId}/resourceGroups/shared-rg/providers/Microsoft.Web/connectorGateways/shared-gateway`
  const connectionId = `${gatewayId}/connections/existing-outlook`
  const stored = new Map()
  const requests = []
  const gateway = { id: gatewayId, name: 'shared-gateway', tags: {} }
  const existingConnection = {
    id: connectionId,
    name: 'existing-outlook',
    properties: {
      connectorName: 'office365',
      displayName: 'Existing mailbox',
      overallStatus: 'Connected',
      provisioningState: 'Succeeded',
      authenticatedUser: { name: 'existing@example.com' },
      statuses: [{ status: 'Connected' }],
      connectionRuntimeUrl: 'https://example.test/runtime',
    },
  }
  const mockFetch = async (url, options = {}) => {
    const resourceId = new URL(url).pathname
    const method = options.method ?? 'GET'
    requests.push({ resourceId, method, body: options.body ? JSON.parse(options.body) : null })
    if (method === 'PUT') {
      const input = JSON.parse(options.body)
      const body = resourceId.includes('/mcpserverconfigs/')
        ? { id: resourceId, ...input, properties: { ...input.properties, mcpEndpointUrl: 'https://example.test/shared-mcp' } }
        : { id: resourceId, ...input }
      stored.set(resourceId.toLowerCase(), body)
      return Response.json(body, { status: 201 })
    }
    if (resourceId === outlookResourceIds(appId).gateway) return new Response('', { status: 404 })
    if (resourceId === '/subscriptions/11111111-1111-1111-1111-111111111111/providers/Microsoft.Web/connectorGateways') {
      return Response.json({ value: [] })
    }
    if (resourceId === gatewayId) return Response.json(gateway)
    if (resourceId === connectionId) return Response.json(existingConnection)
    const body = stored.get(resourceId.toLowerCase())
    return new Response(body ? JSON.stringify(body) : '', {
      status: body ? 200 : 404,
      headers: body ? { 'Content-Type': 'application/json' } : {},
    })
  }

  const connection = await attachOutlookConnection(
    'token',
    context,
    encodeConnectionId(connectionId),
    mockFetch,
  )

  assert.equal(connection.source, 'Existing')
  assert.equal(connection.subscriptionId, connectorSubscriptionId)
  assert.equal(connection.status, 'Connected')
  const puts = requests.filter((request) => request.method === 'PUT')
  assert.equal(puts.length, 3)
  assert.equal(puts.some((request) => request.resourceId === gatewayId), false)
  assert.equal(puts.some((request) => request.resourceId === connectionId), false)
  assert.match(puts.at(-1).resourceId, /mcpserverconfigs\/Office-365-Outlook-send-email-only-[a-f0-9]{12}$/)
  assert.deepEqual(
    puts.at(-1).body.properties.connectors[0].operations.map((operation) => operation.name),
    ['SendEmailV2'],
  )
})

test('rejects attachment to a gateway managed by another app', async () => {
  const context = {
    appResourceId: appId,
    runtimePrincipalId: 'runtime-principal',
    deployerPrincipalId: 'deployer-principal',
    tenantId: 'tenant-id',
  }
  const gatewayId = '/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/shared-rg/providers/Microsoft.Web/connectorGateways/foreign-gateway'
  const connectionId = `${gatewayId}/connections/outlook`
  const requests = []
  const mockFetch = async (url, options = {}) => {
    const resourceId = new URL(url).pathname
    requests.push({ resourceId, method: options.method ?? 'GET' })
    if (resourceId === outlookResourceIds(appId).gateway) return new Response('', { status: 404 })
    if (resourceId.endsWith('/providers/Microsoft.Web/connectorGateways')) {
      return Response.json({ value: [] })
    }
    if (resourceId === gatewayId) {
      return Response.json({
        id: gatewayId,
        tags: {
          'azfunc-agents-portal': 'managed',
          'azfunc-agents-app-id': `${appId}-other`,
        },
      })
    }
    throw new Error(`Unexpected ARM request: ${resourceId}`)
  }

  await assert.rejects(
    () => attachOutlookConnection('token', context, encodeConnectionId(connectionId), mockFetch),
    (error) => error.status === 409 && error.portalCode === 'connection_conflict',
  )
  assert.equal(requests.some((request) => request.method === 'PUT'), false)
})

test('deletes only the verified app-owned gateway for a created connection', async () => {
  const context = {
    appResourceId: appId,
    runtimePrincipalId: 'runtime-principal',
    deployerPrincipalId: 'deployer-principal',
    configuredMcpUrl: 'https://example.test/mcp',
  }
  const ids = outlookResourceIds(appId)
  let gatewayDeleted = false
  const requests = []
  const mockFetch = async (url, options = {}) => {
    const resourceId = new URL(url).pathname
    const method = options.method ?? 'GET'
    requests.push({ resourceId, method })
    if (method === 'DELETE' && resourceId === ids.gateway) {
      gatewayDeleted = true
      return new Response('', { status: 202 })
    }
    if (resourceId === ids.gateway) {
      return gatewayDeleted
        ? new Response('', { status: 404 })
        : Response.json({
            id: ids.gateway,
            tags: { 'azfunc-agents-portal': 'managed', 'azfunc-agents-app-id': appId },
          })
    }
    if (resourceId === ids.connection) {
      return Response.json({
        id: ids.connection,
        properties: { connectorName: 'office365', displayName: 'Outlook reports' },
      })
    }
    if (resourceId === ids.mcpConfigId) {
      return Response.json({ id: ids.mcpConfigId, properties: outlookMcpProperties() })
    }
    if (resourceId.includes('/accessPolicies/')) {
      const principalId = resourceId.split('/').pop()
      return Response.json({ properties: { principal: { identity: { objectId: principalId } } } })
    }
    throw new Error(`Unexpected ARM request: ${method} ${resourceId}`)
  }

  const result = await deleteOutlookConnection(
    'token',
    context,
    encodeConnectionId(ids.connection),
    mockFetch,
  )

  assert.deepEqual(result, { source: 'Created', azure: 'deleted' })
  assert.deepEqual(
    requests.filter((request) => request.method === 'DELETE').map((request) => request.resourceId),
    [ids.gateway],
  )
})

test('rejects stale deletion IDs and reports accepted deletion still pending', async () => {
  const context = {
    appResourceId: appId,
    runtimePrincipalId: 'runtime-principal',
    deployerPrincipalId: 'deployer-principal',
    configuredMcpUrl: 'https://example.test/mcp',
  }
  const ids = outlookResourceIds(appId)
  const requests = []
  const mockFetch = async (url, options = {}) => {
    const resourceId = new URL(url).pathname
    const method = options.method ?? 'GET'
    requests.push({ resourceId, method })
    if (method === 'DELETE' && resourceId === ids.gateway) return new Response(null, { status: 202 })
    if (resourceId === ids.gateway) {
      return Response.json({
        id: ids.gateway,
        tags: { 'azfunc-agents-portal': 'managed', 'azfunc-agents-app-id': appId },
      })
    }
    if (resourceId === ids.connection) {
      return Response.json({ id: ids.connection, properties: { connectorName: 'office365' } })
    }
    if (resourceId === ids.mcpConfigId) {
      return Response.json({
        id: ids.mcpConfigId,
        properties: { ...outlookMcpProperties(), mcpEndpointUrl: 'https://example.test/mcp' },
      })
    }
    if (resourceId.includes('/accessPolicies/')) {
      const principalId = resourceId.split('/').pop()
      return Response.json({ properties: { principal: { identity: { objectId: principalId } } } })
    }
    throw new Error(`Unexpected ARM request: ${method} ${resourceId}`)
  }

  await assert.rejects(
    () => deleteOutlookConnection(
      'token',
      context,
      encodeConnectionId(`${ids.gateway}/connections/stale-outlook`),
      mockFetch,
    ),
    (error) => error.status === 409 && error.portalCode === 'connection_mismatch',
  )
  assert.equal(requests.some((request) => request.method === 'DELETE'), false)

  const result = await deleteOutlookConnection(
    'token',
    context,
    encodeConnectionId(ids.connection),
    mockFetch,
  )
  assert.deepEqual(result, { source: 'Created', azure: 'deletion_pending' })
})

test('detaches app resources without deleting a shared existing connection', async () => {
  const connectorSubscriptionId = '22222222-2222-2222-2222-222222222222'
  const gatewayId = `/subscriptions/${connectorSubscriptionId}/resourceGroups/shared-rg/providers/Microsoft.Web/connectorGateways/shared-gateway`
  const connectionId = `${gatewayId}/connections/existing-outlook`
  const context = {
    appResourceId: appId,
    runtimePrincipalId: 'runtime-principal',
    deployerPrincipalId: 'deployer-principal',
    configuredMcpUrl: 'https://example.test/mcp',
    configuredConnectionId: connectionId,
  }
  const configName = outlookResourceNames(appId).attachedMcpConfig
  const mcpConfigId = `${gatewayId}/mcpserverconfigs/${configName}`
  const runtimePolicyId = `${connectionId}/accessPolicies/runtime-principal`
  const deployerPolicyId = `${connectionId}/accessPolicies/deployer-principal`
  const removed = new Set()
  const requests = []
  const mockFetch = async (url, options = {}) => {
    const resourceId = new URL(url).pathname
    const method = options.method ?? 'GET'
    requests.push({ resourceId, method })
    if (method === 'DELETE') {
      removed.add(resourceId.toLowerCase())
      return new Response(null, { status: 204 })
    }
    if (removed.has(resourceId.toLowerCase())) return new Response('', { status: 404 })
    if (resourceId === gatewayId) return Response.json({ id: gatewayId, tags: {} })
    if (resourceId === connectionId) {
      return Response.json({ id: connectionId, properties: { connectorName: 'office365' } })
    }
    if (resourceId === mcpConfigId) {
      return Response.json({
        id: mcpConfigId,
        properties: { ...outlookMcpProperties('existing-outlook'), mcpEndpointUrl: 'https://example.test/mcp' },
      })
    }
    if (resourceId === runtimePolicyId) {
      return Response.json({ properties: { principal: { identity: { objectId: 'runtime-principal' } } } })
    }
    if (resourceId === deployerPolicyId) {
      return Response.json({ properties: { principal: { identity: { objectId: 'deployer-principal' } } } })
    }
    throw new Error(`Unexpected ARM request: ${method} ${resourceId}`)
  }

  const result = await deleteOutlookConnection(
    'token',
    context,
    encodeConnectionId(connectionId),
    mockFetch,
  )

  assert.deepEqual(result, { source: 'Existing', azure: 'detached' })
  assert.deepEqual(
    requests.filter((request) => request.method === 'DELETE').map((request) => request.resourceId),
    [runtimePolicyId, mcpConfigId],
  )
  assert.equal(requests.some((request) => request.method === 'DELETE' && request.resourceId === gatewayId), false)
  assert.equal(requests.some((request) => request.method === 'DELETE' && request.resourceId === connectionId), false)
  assert.equal(requests.some((request) => request.method === 'DELETE' && request.resourceId === deployerPolicyId), false)
})

test('maps an unauthenticated connection to the Connector Namespace authorization flow', () => {
  const context = {
    appResourceId: appId,
    runtimePrincipalId: 'runtime-principal',
    deployerPrincipalId: 'deployer-principal',
    configuredMcpUrl: 'https://example.test/mcp',
  }
  const ids = outlookResourceIds(appId)
  const summary = connectionSummary(context, {
    ids,
    source: 'Created',
    connection: {
      properties: {
        displayName: 'Outlook reports',
        overallStatus: 'Error',
        provisioningState: 'Succeeded',
        authenticatedUser: {},
        statuses: [{
          status: 'Error',
          target: 'token',
          error: { code: 'Unauthenticated', message: 'This connection is not authenticated.' },
        }],
        connectionRuntimeUrl: 'https://example.test/runtime',
      },
    },
    runtimePolicy: { properties: { principal: { identity: { objectId: 'runtime-principal' } } } },
    deployerPolicy: { properties: { principal: { identity: { objectId: 'deployer-principal' } } } },
    mcp: {
      properties: {
        ...outlookMcpProperties(),
        mcpEndpointUrl: 'https://example.test/mcp',
      },
    },
  })

  assert.equal(summary.status, 'Action required')
  assert.equal(summary.authorizationRequired, true)
  assert.equal(summary.providerErrorCode, 'Unauthenticated')
  assert.equal(summary.providerErrorMessage, 'This connection is not authenticated.')
  assert.equal(
    summary.portalUrl,
    `https://connectors.azure.com/11111111-1111-1111-1111-111111111111/rg-demo/${outlookResourceNames(appId).gateway}/overview`,
  )
  assert.equal(connectorPortalUrl(ids.connection), summary.portalUrl)
})

test('persists the MCP endpoint and selected remote connection ID together', () => {
  const remoteConnectionId = '/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/remote-rg/providers/Microsoft.Web/connectorGateways/remote-gateway/connections/remote-outlook'

  assert.deepEqual(outlookAppSettings({
    id: encodeConnectionId(remoteConnectionId),
    mcpEndpointUrl: 'https://example.test/mcp',
  }), {
    O365_MCP_SERVER_URL: 'https://example.test/mcp',
    AZURE_FUNCTIONS_AGENTS_OUTLOOK_CONNECTION_ID: remoteConnectionId,
  })
  assert.throws(
    () => outlookAppSettings({ id: encodeConnectionId(remoteConnectionId), mcpEndpointUrl: 'http://example.test/mcp' }),
    (error) => error.status === 502 && error.portalCode === 'invalid_mcp_endpoint',
  )
})