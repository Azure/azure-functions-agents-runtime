targetScope = 'subscription'

@description('Authorized workload location. ACA Sandbox Groups for this spike are East US 2 only.')
@allowed([
  'eastus2'
])
param location string = 'eastus2'

@description('Exact dedicated resource group authorized for the private spike.')
param resourceGroupName string = 'larohra-durable-agent-loop'

param foundryAccountName string = 'aidurableloop0904e2'
param foundryProjectName string = 'durable-agent-loop'
param foundryModelDeploymentName string = 'gpt-6-astra'
param foundryModelName string = 'gpt-6-astra'
param foundryModelVersion string = '2026-09-03'
param foundryModelCapacity int = 200

param functionIdentityName string = 'id-durable-loop-func-0904'
param sandboxIdentityName string = 'id-durable-loop-sandbox-0904'
param storageAccountName string = 'stdurableloop0904e2'
param deploymentStorageContainerName string = 'app-package-func-durable-loop-0904'
param durableContentContainerName string = 'durable-loop-content'
param logAnalyticsName string = 'log-durable-loop-0904'
param applicationInsightsName string = 'appi-durable-loop-0904'
param sandboxGroupName string = 'sbg-durable-loop-0904'
param durableTaskSchedulerName string = 'dts-durable-loop-0904'
param durableTaskHubName string = 'durable-loop-demo'
param functionPlanName string = 'ASP-larohradurableagentloop-7c8a'
param functionAppName string = 'func-durable-loop-0904'

param sharedApimResourceGroupName string = 'larohra-operations-agent-3p-rg'
param sharedApimServiceName string = 'larohra-ai-gateway'
param modelApiName string = 'durable-agent-loop-model'
param modelControlApiName string = 'durable-agent-loop-model-control'
param mcpApiName string = 'durable-agent-loop-mcp'
param apimProductName string = 'durable-agent-loop-spike'
param apimSubscriptionName string = 'durable-agent-loop-spike'
param apimLoggerName string = 'durable-agent-loop-ai'
param apimInstrumentationKeyNamedValueName string = 'durable-agent-loop-appinsights-key'
param apimTokensPerMinute int = 250000

@description('Private runtime gate. Infrastructure is provisioned with the gate disabled by default.')
param featureGateEnabled bool = false

@description('Sensitive prompt, answer, argument, result, and response telemetry remains disabled.')
param enableSensitiveData bool = false

@description('App-setting name reserved for the final stacked application layer. Override if that layer finalizes a different private gate name.')
param featureGateSettingName string = 'AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_ENABLED'

@description('Principal used for local qualification data-plane access.')
param deployerPrincipalId string = deployer().objectId

@allowed([
  'User'
  'ServicePrincipal'
])
param deployerPrincipalType string = 'User'

@description('Exact live assignment names adopt the manually proven environment idempotently. Override these together with principals for another environment.')
param roleAssignmentNames object = {
  functionStorageBlobOwner: '8223abcb-b902-4f97-b5c3-ae1b99369c17'
  functionStorageQueueContributor: 'e1ec67c4-e2ee-4e9f-9f20-a56974d37124'
  functionStorageTableContributor: '3f12e745-744c-4f08-bda0-4d0b6280124a'
  functionMonitoringMetricsPublisher: '06be6dc2-a0e6-4784-8619-34025b495a3e'
  functionCognitiveServicesUser: 'f5bd3b1d-7436-4db2-bc17-7958f3353fdc'
  functionCognitiveServicesOpenAiUser: '36994145-3665-4faf-b0e0-439a66d2d86d'
  functionSandboxGroupDataOwner: '4e301a1d-0499-46df-ae5b-d74f3fd0003a'
  functionDurableTaskDataContributor: '2cc2f2cb-7d79-4b5c-aa81-68014ee64363'
  sandboxFoundryReader: 'fbb65a30-6d98-4cac-ac1b-d2f940bc559e'
  apimCognitiveServicesOpenAiUser: 'aaaf4f95-61da-4b53-b1f1-40d228c9147d'
  deployerStorageBlobContributor: 'de7eec46-02c7-4b7a-ac7d-b2eecb81938a'
  deployerStorageBlobOwner: '43bbeeb1-fac2-4426-a550-197d99b32ee8'
  deployerSandboxGroupDataOwner: 'c1f2afbc-e13b-47a2-a1c9-aef769cddaa6'
  deployerDurableTaskDataContributor: '9752a16f-5b21-4dec-b863-ad96a1816b34'
}

var workloadTags = {
  purpose: 'durable-agent-loop-spike'
  owner: 'larohra'
  source: 'azure-functions-agents-runtime'
  environment: 'private-spike'
}

resource workloadResourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: workloadTags
}

resource sharedApimResourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' existing = {
  name: sharedApimResourceGroupName
}

resource sharedApimService 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  scope: sharedApimResourceGroup
  name: sharedApimServiceName
}

resource sharedApimSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' existing = {
  parent: sharedApimService
  name: apimSubscriptionName
}

module identities './modules/identities.bicep' = {
  name: 'durable-loop-identities'
  scope: workloadResourceGroup
  params: {
    location: location
    functionIdentityName: functionIdentityName
    sandboxIdentityName: sandboxIdentityName
  }
}

module monitoring './modules/monitoring.bicep' = {
  name: 'durable-loop-monitoring'
  scope: workloadResourceGroup
  params: {
    location: location
    logAnalyticsName: logAnalyticsName
    applicationInsightsName: applicationInsightsName
  }
}

module storage './modules/storage.bicep' = {
  name: 'durable-loop-storage'
  scope: workloadResourceGroup
  params: {
    location: location
    storageAccountName: storageAccountName
    deploymentStorageContainerName: deploymentStorageContainerName
    durableContentContainerName: durableContentContainerName
  }
}

module foundry './modules/foundry.bicep' = {
  name: 'durable-loop-foundry'
  scope: workloadResourceGroup
  params: {
    location: location
    accountName: foundryAccountName
    projectName: foundryProjectName
    modelDeploymentName: foundryModelDeploymentName
    modelName: foundryModelName
    modelVersion: foundryModelVersion
    modelCapacity: foundryModelCapacity
  }
}

module sandboxGroup './modules/sandbox-group.bicep' = {
  name: 'durable-loop-sandbox-group'
  scope: workloadResourceGroup
  params: {
    location: location
    sandboxGroupName: sandboxGroupName
    sandboxIdentityResourceId: identities.outputs.sandboxIdentityResourceId
  }
}

module durableTaskScheduler './modules/durable-task-scheduler.bicep' = {
  name: 'durable-loop-durable-task-scheduler'
  scope: workloadResourceGroup
  params: {
    location: location
    schedulerName: durableTaskSchedulerName
    taskHubName: durableTaskHubName
    tags: workloadTags
  }
}

module apim './modules/apim.bicep' = {
  name: 'durable-loop-apim-children'
  scope: sharedApimResourceGroup
  params: {
    apimServiceName: sharedApimServiceName
    workloadResourceGroupName: resourceGroupName
    applicationInsightsName: applicationInsightsName
    foundryAccountName: foundryAccountName
    modelApiName: modelApiName
    modelControlApiName: modelControlApiName
    mcpApiName: mcpApiName
    productName: apimProductName
    subscriptionName: apimSubscriptionName
    loggerName: apimLoggerName
    instrumentationKeyNamedValueName: apimInstrumentationKeyNamedValueName
    tokensPerMinute: apimTokensPerMinute
  }
  dependsOn: [
    foundry
    monitoring
  ]
}

module functionApp './modules/function-app.bicep' = {
  name: 'durable-loop-function-app'
  scope: workloadResourceGroup
  params: {
    location: location
    functionPlanName: functionPlanName
    functionAppName: functionAppName
    functionIdentityResourceId: identities.outputs.functionIdentityResourceId
    functionIdentityClientId: identities.outputs.functionIdentityClientId
    storageAccountName: storageAccountName
    deploymentStorageContainerName: deploymentStorageContainerName
    durableContentBlobUri: 'https://${storageAccountName}.blob.${environment().suffixes.storage}'
    durableContentContainerName: durableContentContainerName
    durableTaskSchedulerEndpoint: durableTaskScheduler.outputs.schedulerEndpoint
    durableTaskHubName: durableTaskHubName
    applicationInsightsName: applicationInsightsName
    foundryProjectEndpoint: 'https://${foundryAccountName}.services.ai.azure.com/api/projects/${foundryProjectName}'
    foundryModelDeploymentName: foundryModelDeploymentName
    apimModelBaseUrl: 'https://${sharedApimServiceName}.azure-api.net/${modelApiName}/openai/v1'
    apimModelControlUrl: 'https://${sharedApimServiceName}.azure-api.net/${modelControlApiName}'
    apimMcpUrl: 'https://${sharedApimServiceName}.azure-api.net/${mcpApiName}'
    apimSubscriptionKey: sharedApimSubscription.listSecrets().primaryKey
    sandboxGroupResourceId: sandboxGroup.outputs.sandboxGroupResourceId
    sandboxRegion: location
    featureGateSettingName: featureGateSettingName
    featureGateEnabled: featureGateEnabled
    enableSensitiveData: enableSensitiveData
  }
  dependsOn: [
    apim
    foundry
    monitoring
    storage
  ]
}

module rbac './modules/rbac.bicep' = {
  name: 'durable-loop-rbac'
  scope: workloadResourceGroup
  params: {
    storageAccountName: storageAccountName
    applicationInsightsName: applicationInsightsName
    foundryAccountName: foundryAccountName
    sandboxGroupName: sandboxGroupName
    durableTaskSchedulerName: durableTaskSchedulerName
    durableTaskHubName: durableTaskHubName
    functionPrincipalId: identities.outputs.functionIdentityPrincipalId
    sandboxPrincipalId: identities.outputs.sandboxIdentityPrincipalId
    apimPrincipalId: sharedApimService.identity.principalId!
    deployerPrincipalId: deployerPrincipalId
    deployerPrincipalType: deployerPrincipalType
    assignmentNames: roleAssignmentNames
  }
  dependsOn: [
    foundry
    functionApp
    sandboxGroup
    storage
    durableTaskScheduler
  ]
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP_NAME string = workloadResourceGroup.name
output AZURE_FUNCTION_NAME string = functionApp.outputs.functionAppName
output FOUNDRY_PROJECT_ENDPOINT string = 'https://${foundryAccountName}.services.ai.azure.com/api/projects/${foundryProjectName}'
output FOUNDRY_MODEL string = foundryModelDeploymentName
output APIM_MODEL_BASE_URL string = 'https://${sharedApimServiceName}.azure-api.net/${modelApiName}/openai/v1'
output APIM_MODEL_CONTROL_URL string = 'https://${sharedApimServiceName}.azure-api.net/${modelControlApiName}'
output APIM_MCP_URL string = 'https://${sharedApimServiceName}.azure-api.net/${mcpApiName}'
output SANDBOX_GROUP_RESOURCE_ID string = sandboxGroup.outputs.sandboxGroupResourceId
output DURABLE_TASK_SCHEDULER_NAME string = durableTaskScheduler.outputs.schedulerName
output DURABLE_TASK_HUB_NAME string = durableTaskScheduler.outputs.taskHubName
output DURABLE_TASK_SCHEDULER_ENDPOINT string = durableTaskScheduler.outputs.schedulerEndpoint
