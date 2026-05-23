targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment which is used to generate a short unique hash used in all resources.')
param environmentName string

@minLength(1)
@description('Primary location for all resources')
@metadata({
  azd: {
    type: 'location'
  }
})
param location string

@description('Azure AI Foundry project endpoint (e.g. https://<name>.services.ai.azure.com/api/projects/<project>).')
@minLength(1)
param foundryProjectEndpoint string

@description('Azure AI Foundry model name or deployment name.')
param foundryModel string = 'gpt-5.4'

@description('Reasoning effort for supported Foundry reasoning models.')
@allowed([
  'none'
  'low'
  'medium'
  'high'
  'xhigh'
])
param reasoningEffort string = 'high'

@description('Reasoning summary mode for supported Foundry reasoning models.')
@allowed([
  'auto'
  'concise'
  'detailed'
])
param reasoningSummary string = 'concise'

@description('Email address to send the daily Azure report to.')
param toEmail string

@description('Office 365 Outlook MCP server URL for sending email.')
@minLength(1)
param o365McpServerUrl string

@description('Optional managed identity client ID to use when authenticating to the Office 365 Outlook MCP server. Leave empty to use the app-wide identity selection.')
param o365McpClientId string = ''

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }
var functionAppName = '${abbrs.webSitesFunctions}agent-func-${resourceToken}'
var deploymentStorageContainerName = 'app-package-${take(functionAppName, 32)}-${take(toLower(uniqueString(functionAppName, resourceToken)), 7)}'

// Reader role definition ID
var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

// Resource Group
resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

// User Assigned Managed Identity
module apiUserAssignedIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.4.1' = {
  name: 'apiUserAssignedIdentity'
  scope: rg
  params: {
    location: location
    tags: tags
    name: '${abbrs.managedIdentityUserAssignedIdentities}agent-func-${resourceToken}'
  }
}

// App Service Plan (Flex Consumption)
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

// Function App
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
    runtimeVersion: '3.12'
    storageAccountName: storage.outputs.name
    deploymentStorageContainerName: deploymentStorageContainerName
    identityId: apiUserAssignedIdentity.outputs.resourceId
    identityClientId: apiUserAssignedIdentity.outputs.clientId
    appSettings: {
      MAF_PROVIDER: 'foundry'
      FOUNDRY_PROJECT_ENDPOINT: foundryProjectEndpoint
      FOUNDRY_MODEL: foundryModel
      MAF_REASONING_EFFORT: reasoningEffort
      MAF_REASONING_SUMMARY: reasoningSummary
      AZURE_CLIENT_ID: apiUserAssignedIdentity.outputs.clientId
      TO_EMAIL: toEmail
      SUBSCRIPTION_ID: subscription().subscriptionId
      O365_MCP_SERVER_URL: o365McpServerUrl
      O365_MCP_CLIENT_ID: o365McpClientId
      ENABLE_MULTIPLATFORM_BUILD: 'true'
      PYTHON_ENABLE_INIT_INDEXING: '1'
    }
  }
}

// Storage Account
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

// RBAC — storage, app insights
module rbac './app/rbac.bicep' = {
  name: 'rbacAssignments'
  scope: rg
  params: {
    storageAccountName: storage.outputs.name
    appInsightsName: monitoring.outputs.name
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
  }
}

// RBAC — Reader on subscription for azure_rest custom tool
resource subscriptionReaderRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, functionAppName, readerRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
    principalId: apiUserAssignedIdentity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// Log Analytics
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

// Application Insights
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

// Outputs
output AZURE_LOCATION string = location
output AZURE_FUNCTION_NAME string = api.outputs.SERVICE_API_NAME
