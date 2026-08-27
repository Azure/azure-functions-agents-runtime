// Serverless Agent Portal — provisioning + deployment.
//
// Turns a portal-managed source tree into a running Azure Functions agent app.
// Two paths, both authorised by the caller's forwarded ARM token:
//
//   • New app  — submit a lean Flex Consumption ARM deployment (storage + Flex
//     plan + function app, reusing the caller's existing Microsoft Foundry via
//     app settings; connection-string storage, so no RBAC is required to
//     deploy), then push the source.
//   • Both     — zip the source tree and push it to the app's SCM `publish`
//     endpoint with a remote build (Oryx installs requirements.txt), then poll
//     the deployment to completion.
//
// The ZIP is written by hand (store method) to avoid a build dependency; the
// payload is a handful of small text files.

const ARM = 'https://management.azure.com'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// --- ARM helpers -----------------------------------------------------------

// Issue an ARM request with the forwarded bearer token; parse JSON, surface the
// ARM error message on failure.
async function arm(token, url, { method = 'GET', body, timeoutMs = 30000 } = {}) {
  const res = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(timeoutMs),
  })
  const text = await res.text()
  let json
  try {
    json = text ? JSON.parse(text) : {}
  } catch {
    json = { raw: text }
  }
  if (!res.ok) {
    const detail =
      json?.error?.message || json?.Message || json?.message || text || `${res.status} ${res.statusText}`
    const err = new Error(String(detail).slice(0, 800))
    err.status = res.status
    throw err
  }
  return json
}

async function armList(token, initialUrl, { timeoutMs = 30000, maxPages = 50 } = {}) {
  const values = []
  let url = initialUrl
  for (let page = 0; page < maxPages && url; page++) {
    const result = await arm(token, url, { timeoutMs })
    values.push(...(result.value ?? []))
    url = result.nextLink ?? ''
  }
  if (url) throw new Error(`ARM list exceeded ${maxPages} pages.`)
  return values
}

// Create or update the target resource group (idempotent).
export async function ensureResourceGroup(token, subscriptionId, resourceGroup, location) {
  await arm(
    token,
    `${ARM}/subscriptions/${subscriptionId}/resourcegroups/${encodeURIComponent(resourceGroup)}?api-version=2021-04-01`,
    { method: 'PUT', body: { location } },
  )
}

// Derive a globally-unique storage account name (3–24 lowercase alphanumerics).
function storageAccountName(appName) {
  const clean = String(appName).toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 11) || 'agents'
  const rand = Math.random().toString(16).slice(2, 8)
  return `st${clean}${rand}`.slice(0, 24)
}

// Build the lean Flex Consumption ARM template. Storage auth uses a connection
// string (no managed identity / RBAC needed to deploy); the app carries a
// system-assigned identity so the caller can later grant it access to Foundry.
function flexTemplate({ appName, storageName, planName, containerName, workspaceName, insightsName, region, pythonVersion, foundryEndpoint, foundryModel }) {
  // Inner (unbracketed) resourceId expressions for nesting inside other ARM
  // expressions; wrap in `[ ]` only where an expression stands on its own.
  const storageIdE = `resourceId('Microsoft.Storage/storageAccounts', '${storageName}')`
  const containerIdE = `resourceId('Microsoft.Storage/storageAccounts/blobServices/containers', '${storageName}', 'default', '${containerName}')`
  const planIdE = `resourceId('Microsoft.Web/serverfarms', '${planName}')`
  const siteIdE = `resourceId('Microsoft.Web/sites', '${appName}')`
  const workspaceIdE = `resourceId('Microsoft.OperationalInsights/workspaces', '${workspaceName}')`
  const insightsIdE = `resourceId('Microsoft.Insights/components', '${insightsName}')`
  const connStr = `[format('DefaultEndpointsProtocol=https;AccountName={0};AccountKey={1};EndpointSuffix=core.windows.net', '${storageName}', listKeys(${storageIdE}, '2023-01-01').keys[0].value)]`
  const blobBase = `reference(${storageIdE}, '2023-01-01').primaryEndpoints.blob`

  const appSettings = [
    { name: 'AzureWebJobsStorage', value: connStr },
    { name: 'DEPLOYMENT_STORAGE_CONNECTION_STRING', value: connStr },
    { name: 'AZURE_FUNCTIONS_AGENTS_PROVIDER', value: 'foundry' },
    {
      name: 'APPLICATIONINSIGHTS_CONNECTION_STRING',
      value: `[reference(${insightsIdE}, '2020-02-02').ConnectionString]`,
    },
  ]
  if (foundryEndpoint) appSettings.push({ name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryEndpoint })
  if (foundryModel) appSettings.push({ name: 'FOUNDRY_MODEL', value: foundryModel })

  return {
    $schema: 'https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#',
    contentVersion: '1.0.0.0',
    resources: [
      {
        type: 'Microsoft.Storage/storageAccounts',
        apiVersion: '2023-01-01',
        name: storageName,
        location: region,
        sku: { name: 'Standard_LRS' },
        kind: 'StorageV2',
        properties: {
          minimumTlsVersion: 'TLS1_2',
          allowBlobPublicAccess: false,
          allowSharedKeyAccess: true,
        },
      },
      {
        type: 'Microsoft.Storage/storageAccounts/blobServices/containers',
        apiVersion: '2023-01-01',
        name: `${storageName}/default/${containerName}`,
        dependsOn: [`[${storageIdE}]`],
        properties: { publicAccess: 'None' },
      },
      {
        type: 'Microsoft.OperationalInsights/workspaces',
        apiVersion: '2023-09-01',
        name: workspaceName,
        location: region,
        properties: {
          sku: { name: 'PerGB2018' },
          retentionInDays: 30,
        },
      },
      {
        type: 'Microsoft.Insights/components',
        apiVersion: '2020-02-02',
        name: insightsName,
        location: region,
        kind: 'web',
        dependsOn: [`[${workspaceIdE}]`],
        properties: {
          Application_Type: 'web',
          WorkspaceResourceId: `[${workspaceIdE}]`,
        },
      },
      {
        type: 'Microsoft.Web/serverfarms',
        apiVersion: '2023-12-01',
        name: planName,
        location: region,
        kind: 'functionapp',
        sku: { name: 'FC1', tier: 'FlexConsumption' },
        properties: { reserved: true },
      },
      {
        type: 'Microsoft.Web/sites',
        apiVersion: '2023-12-01',
        name: appName,
        location: region,
        kind: 'functionapp,linux',
        identity: { type: 'SystemAssigned' },
        dependsOn: [`[${planIdE}]`, `[${storageIdE}]`, `[${containerIdE}]`, `[${insightsIdE}]`],
        properties: {
          serverFarmId: `[${planIdE}]`,
          httpsOnly: true,
          functionAppConfig: {
            deployment: {
              storage: {
                type: 'blobContainer',
                value: `[format('{0}${containerName}', ${blobBase})]`,
                authentication: {
                  type: 'StorageAccountConnectionString',
                  storageAccountConnectionStringName: 'DEPLOYMENT_STORAGE_CONNECTION_STRING',
                },
              },
            },
            runtime: { name: 'python', version: pythonVersion },
            scaleAndConcurrency: { instanceMemoryMB: 2048, maximumInstanceCount: 100 },
          },
          siteConfig: { appSettings },
        },
      },
    ],
    outputs: {
      defaultHostName: {
        type: 'string',
        value: `[reference(${siteIdE}, '2023-12-01').defaultHostName]`,
      },
      principalId: {
        type: 'string',
        value: `[reference(${siteIdE}, '2023-12-01', 'full').identity.principalId]`,
      },
    },
  }
}

// Best-effort extraction of a failed deployment's error messages.
async function deploymentError(token, subscriptionId, resourceGroup, deploymentName) {
  try {
    const operations = await armList(
      token,
      `${ARM}/subscriptions/${subscriptionId}/resourcegroups/${encodeURIComponent(resourceGroup)}/providers/Microsoft.Resources/deployments/${deploymentName}/operations?api-version=2021-04-01`,
    )
    const messages = operations
      .map((op) => {
        const sm = op?.properties?.statusMessage
        return sm?.error?.message || sm?.Message || (typeof sm === 'string' ? sm : '')
      })
      .filter(Boolean)
    return messages.join('; ') || 'see the deployment logs in the Azure portal'
  } catch {
    return 'see the deployment logs in the Azure portal'
  }
}

/**
 * Provision a new Flex Consumption agent app and wait for it to be ready.
 * @returns {Promise<{defaultHostName: string, principalId: string, storageName: string, appInsightsName: string}>}
 */
export async function provisionFlexApp(token, opts) {
  const {
    subscriptionId,
    resourceGroup,
    appName,
    region,
    foundryEndpoint = '',
    foundryModel = '',
    pythonVersion = '3.13',
    deploymentName = `portal-deploy-${Date.now()}`,
  } = opts

  await ensureResourceGroup(token, subscriptionId, resourceGroup, region)

  // Companion resource names come from a sanitized base so an app name with
  // characters that are invalid for a given resource type (e.g. underscores are
  // rejected by Log Analytics workspaces) can't fail the whole deployment.
  const nameBase =
    String(appName)
      .toLowerCase()
      .replace(/[^a-z0-9-]/g, '-')
      .replace(/-+/g, '-')
      .replace(/^-+|-+$/g, '') || 'agents'
  const trimTrailingHyphen = (s) => s.replace(/-+$/g, '')
  const storageName = storageAccountName(appName)
  const planName = trimTrailingHyphen(`${nameBase}-plan`.slice(0, 40))
  const containerName = 'app-package'
  const insightsName = trimTrailingHyphen(nameBase.slice(0, 60))
  const workspaceName = trimTrailingHyphen(`${nameBase}-logs`.slice(0, 63))
  const template = flexTemplate({
    appName,
    storageName,
    planName,
    containerName,
    workspaceName,
    insightsName,
    region,
    pythonVersion,
    foundryEndpoint,
    foundryModel,
  })

  const url = `${ARM}/subscriptions/${subscriptionId}/resourcegroups/${encodeURIComponent(resourceGroup)}/providers/Microsoft.Resources/deployments/${deploymentName}?api-version=2021-04-01`

  await arm(token, url, { method: 'PUT', body: { properties: { mode: 'Incremental', template } } })

  const deadline = Date.now() + 8 * 60 * 1000
  while (Date.now() < deadline) {
    await sleep(6000)
    const status = await arm(token, url).catch(() => null)
    const state = status?.properties?.provisioningState
    if (state === 'Succeeded') {
      const outputs = status.properties.outputs || {}
      return {
        defaultHostName: outputs.defaultHostName?.value || `${appName}.azurewebsites.net`,
        principalId: outputs.principalId?.value || '',
        storageName,
        appInsightsName: insightsName,
      }
    }
    if (state === 'Failed' || state === 'Canceled') {
      const detail = await deploymentError(token, subscriptionId, resourceGroup, deploymentName)
      throw new Error(`Provisioning ${state.toLowerCase()}: ${detail}`)
    }
  }
  throw new Error('Provisioning timed out after 8 minutes.')
}

// --- Deployment (SCM remote build) -----------------------------------------

// CRC-32 (IEEE) for ZIP entries.
const CRC_TABLE = (() => {
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c >>> 0
  }
  return table
})()

function crc32(buf) {
  let c = 0xffffffff
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

/**
 * Build a ZIP archive (store method — no compression) from in-memory files.
 * @param {Array<{name: string, data: Buffer}>} files
 * @returns {Buffer}
 */
export function zipStore(files) {
  const local = []
  const central = []
  let offset = 0
  for (const file of files) {
    const nameBuf = Buffer.from(file.name, 'utf8')
    const data = file.data
    const crc = crc32(data)

    const header = Buffer.alloc(30)
    header.writeUInt32LE(0x04034b50, 0) // local file header signature
    header.writeUInt16LE(20, 4) // version needed
    header.writeUInt16LE(0, 6) // flags
    header.writeUInt16LE(0, 8) // method: store
    header.writeUInt16LE(0, 10) // mod time
    header.writeUInt16LE(0x21, 12) // mod date (1980-01-01)
    header.writeUInt32LE(crc, 14)
    header.writeUInt32LE(data.length, 18) // compressed size
    header.writeUInt32LE(data.length, 22) // uncompressed size
    header.writeUInt16LE(nameBuf.length, 26)
    header.writeUInt16LE(0, 28) // extra length
    local.push(header, nameBuf, data)

    const record = Buffer.alloc(46)
    record.writeUInt32LE(0x02014b50, 0) // central directory signature
    record.writeUInt16LE(20, 4) // version made by
    record.writeUInt16LE(20, 6) // version needed
    record.writeUInt16LE(0, 8) // flags
    record.writeUInt16LE(0, 10) // method
    record.writeUInt16LE(0, 12) // mod time
    record.writeUInt16LE(0x21, 14) // mod date
    record.writeUInt32LE(crc, 16)
    record.writeUInt32LE(data.length, 20)
    record.writeUInt32LE(data.length, 24)
    record.writeUInt16LE(nameBuf.length, 28)
    record.writeUInt16LE(0, 30) // extra
    record.writeUInt16LE(0, 32) // comment
    record.writeUInt16LE(0, 34) // disk number
    record.writeUInt16LE(0, 36) // internal attrs
    record.writeUInt32LE(0, 38) // external attrs
    record.writeUInt32LE(offset, 42) // local header offset
    central.push(record, nameBuf)

    offset += header.length + nameBuf.length + data.length
  }

  const centralBuf = Buffer.concat(central)
  const eocd = Buffer.alloc(22)
  eocd.writeUInt32LE(0x06054b50, 0) // end of central directory signature
  eocd.writeUInt16LE(0, 4) // disk number
  eocd.writeUInt16LE(0, 6) // disk with central dir
  eocd.writeUInt16LE(files.length, 8) // entries on this disk
  eocd.writeUInt16LE(files.length, 10) // total entries
  eocd.writeUInt32LE(centralBuf.length, 12) // central dir size
  eocd.writeUInt32LE(offset, 16) // central dir offset
  eocd.writeUInt16LE(0, 20) // comment length

  return Buffer.concat([...local, centralBuf, eocd])
}

// Poll a Kudu deployment record until it completes. `status === 4` is success.
async function pollDeployment(token, scmHost, locationUrl) {
  const pollUrl = locationUrl || `https://${scmHost}/api/deployments/latest`
  const deadline = Date.now() + 10 * 60 * 1000
  let last = null
  while (Date.now() < deadline) {
    await sleep(6000)
    let res
    try {
      res = await fetch(pollUrl, {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(30000),
      })
    } catch {
      continue
    }
    if (!res.ok) continue
    last = await res.json().catch(() => null)
    if (last && last.complete === true) {
      if (Number(last.status) === 4) return { log: last.status_text || last.message || '' }
      throw new Error(
        `Remote build failed: ${last.status_text || last.progress || last.message || `status ${last.status}`}`,
      )
    }
  }
  throw new Error('Deployment timed out after 10 minutes waiting for the remote build.')
}

/**
 * Push a source ZIP to a Flex Consumption app via SCM `publish` with a remote
 * build, then wait for the deployment to finish.
 * @param {string} token forwarded ARM bearer token (accepted by SCM)
 * @param {string} scmHost e.g. `app.scm.azurewebsites.net`
 * @param {Buffer} zipBuffer source archive (requirements.txt at the root)
 */
export async function deployZipToApp(token, scmHost, zipBuffer) {
  if (!scmHost) throw new Error('Could not resolve the app SCM host for deployment.')
  const url = `https://${scmHost}/api/publish?RemoteBuild=true&Deployer=serverless-portal`
  const res = await fetch(url, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/zip' },
    body: zipBuffer,
    signal: AbortSignal.timeout(10 * 60 * 1000),
  })

  if (res.status !== 202 && !res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`Deploy request failed (${res.status}): ${text.slice(0, 500)}`)
  }

  // Some responses complete synchronously; otherwise poll (Location or latest).
  const body = await res.json().catch(() => null)
  if (body && body.complete === true && Number(body.status) === 4) {
    return { log: body.status_text || '' }
  }
  return pollDeployment(token, scmHost, res.headers.get('location'))
}
