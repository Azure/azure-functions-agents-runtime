targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment which is used to generate a short unique hash used in all resources.')
param environmentName string

@minLength(1)
@description('Primary location for all resources. Must support Azure Functions Flex Consumption, Microsoft.Web connectorGateways, ACA session pools, and the default Microsoft Foundry gpt-5.4 Global Standard deployment.')
@allowed([
  'centralus'
  'eastus'
  'eastus2'
  'northcentralus'
  'southcentralus'
  'westcentralus'
  'westus'
])
@metadata({
  azd: {
    type: 'location'
  }
})
param location string

@description('Microsoft Foundry model deployment name.')
param foundryModel string = 'gpt-5.4'

@description('Microsoft Foundry model name.')
param foundryModelName string = 'gpt-5.4'

@description('Microsoft Foundry model version.')
param foundryModelVersion string = '2026-03-05'

@description('Microsoft Foundry deployment capacity.')
param foundryDeploymentCapacity int = 50

@description('Connector Gateway location. Defaults to westcentralus because connector trigger configs are known to work there while preview rollout continues.')
@allowed([
  'centralus'
  'eastus'
  'eastus2'
  'northcentralus'
  'southcentralus'
  'westcentralus'
  'westus'
])
param connectorGatewayLocation string = 'westcentralus'

@description('Only emails sent to this mailbox address should result in a drafted reply.')
param watchedSenderEmail string

@description('Optional managed identity client ID to use when authenticating to the Office 365 Outlook MCP server. Leave empty to use the app-wide identity selection.')
param o365McpClientId string = ''

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }
var functionAppName = '${abbrs.webSitesFunctions}agent-func-${resourceToken}'
var foundryAccountName = 'cog-${resourceToken}'
var foundryProjectName = '${foundryAccountName}-proj'
var deploymentStorageContainerName = 'app-package-${take(functionAppName, 32)}-${take(toLower(uniqueString(functionAppName, resourceToken)), 7)}'
var deployerPrincipalId = deployer().objectId
var connectorGatewayName = 'cg-${resourceToken}'
var office365ConnectionName = 'office365-outlook'
var office365McpServerConfigName = 'Office-365-Outlook-draft-replies'
var sessionPoolName = 'sessionpool${resourceToken}'
var connectorTriggerFunctionName = 'OnNewEmail'

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

module apiUserAssignedIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.4.1' = {
  name: 'apiUserAssignedIdentity'
  scope: rg
  params: {
    location: location
    tags: tags
    name: '${abbrs.managedIdentityUserAssignedIdentities}agent-func-${resourceToken}'
  }
}

module foundry './app/foundry.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    accountName: foundryAccountName
    projectName: foundryProjectName
    location: location
    tags: tags
    modelDeploymentName: foundryModel
    modelName: foundryModelName
    modelVersion: foundryModelVersion
    deploymentCapacity: foundryDeploymentCapacity
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
  }
}

module office365Connector './app/connector-gateway.bicep' = {
  name: 'office365Connector'
  scope: rg
  params: {
    connectorGatewayName: connectorGatewayName
    connectionName: office365ConnectionName
    mcpServerConfigName: office365McpServerConfigName
    location: connectorGatewayLocation
    tags: tags
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
    deployerPrincipalId: deployerPrincipalId
    tenantId: tenant().tenantId
  }
}

module appServicePlan 'br/public:avm/res/web/serverfarm:0.1.1' = {
  name: 'appserviceplan'
  scope: rg
  params: {
    name: '${abbrs.webServerFarms}${resourceToken}'
    sku: {
      name: 'FC1'
      tier: 'FlexConsumption'
    }
    reserved: true
    location: location
    tags: tags
  }
}

module api './app/api.bicep' = {
  name: 'api'
  scope: rg
  params: {
    name: functionAppName
    location: location
    tags: tags
    applicationInsightsName: monitoring.outputs.name
    appServicePlanId: appServicePlan.outputs.resourceId
    runtimeName: 'python'
    runtimeVersion: '3.13'
    storageAccountName: storage.outputs.name
    deploymentStorageContainerName: deploymentStorageContainerName
    identityId: apiUserAssignedIdentity.outputs.resourceId
    identityClientId: apiUserAssignedIdentity.outputs.clientId
    appSettings: {
      AZURE_FUNCTIONS_AGENTS_PROVIDER: 'foundry'
      FOUNDRY_PROJECT_ENDPOINT: foundry.outputs.projectEndpoint
      FOUNDRY_MODEL: foundry.outputs.modelDeploymentName
      AZURE_CLIENT_ID: apiUserAssignedIdentity.outputs.clientId
      ACA_SESSION_POOL_ENDPOINT: sessionPool.outputs.poolManagementEndpoint
      WATCHED_SENDER_EMAIL: watchedSenderEmail
      O365_MCP_SERVER_URL: office365Connector.outputs.mcpEndpointUrl
      O365_MCP_CLIENT_ID: o365McpClientId
      ENABLE_MULTIPLATFORM_BUILD: 'true'
      PYTHON_ENABLE_INIT_INDEXING: '1'
    }
  }
}

module storage 'br/public:avm/res/storage/storage-account:0.8.3' = {
  name: 'storage'
  scope: rg
  params: {
    name: '${abbrs.storageStorageAccounts}${resourceToken}'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    dnsEndpointType: 'Standard'
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    blobServices: {
      containers: [{ name: deploymentStorageContainerName }]
    }
    minimumTlsVersion: 'TLS1_2'
    location: location
    tags: tags
  }
}

module rbac './app/rbac.bicep' = {
  name: 'rbacAssignments'
  scope: rg
  params: {
    storageAccountName: storage.outputs.name
    appInsightsName: monitoring.outputs.name
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
  }
}

module sessionPool './app/session-pool.bicep' = {
  name: 'sessionPool'
  scope: rg
  params: {
    sessionPoolName: sessionPoolName
    location: location
    tags: tags
  }
}

module sessionPoolRbac './app/session-pool-rbac.bicep' = {
  name: 'sessionPoolRbac'
  scope: rg
  dependsOn: [sessionPool]
  params: {
    sessionPoolName: sessionPoolName
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
    userPrincipalId: deployerPrincipalId
  }
}

module logAnalytics 'br/public:avm/res/operational-insights/workspace:0.7.0' = {
  name: '${uniqueString(deployment().name, location)}-loganalytics'
  scope: rg
  params: {
    name: '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
    location: location
    tags: tags
    dataRetention: 30
  }
}

module monitoring 'br/public:avm/res/insights/component:0.4.1' = {
  name: '${uniqueString(deployment().name, location)}-appinsights'
  scope: rg
  params: {
    name: '${abbrs.insightsComponents}${resourceToken}'
    location: location
    tags: tags
    workspaceResourceId: logAnalytics.outputs.resourceId
    disableLocalAuth: true
  }
}

output AZURE_LOCATION string = location
output AZURE_FUNCTION_NAME string = api.outputs.SERVICE_API_NAME
output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output FOUNDRY_MODEL string = foundry.outputs.modelDeploymentName
output O365_CONNECTOR_GATEWAY_NAME string = office365Connector.outputs.connectorGatewayName
output O365_CONNECTION_ID string = office365Connector.outputs.connectionId
output O365_MCP_SERVER_URL string = office365Connector.outputs.mcpEndpointUrl
output CONNECTOR_TRIGGER_FUNCTION_NAME string = connectorTriggerFunctionName
output CONNECTOR_TRIGGER_CONFIG_NAME string = 'Office-365-Outlook-on-new-email'
