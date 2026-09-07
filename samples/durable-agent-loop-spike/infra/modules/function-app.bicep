param location string
param functionPlanName string
param functionAppName string
param functionIdentityResourceId string
param functionIdentityClientId string
param storageAccountName string
param deploymentStorageContainerName string
param durableContentBlobUri string
param durableContentContainerName string
param applicationInsightsName string
param foundryProjectEndpoint string
param foundryModelDeploymentName string
param apimModelBaseUrl string
param apimModelControlUrl string
param apimMcpUrl string

@secure()
param apimSubscriptionKey string

param sandboxGroupResourceId string
param sandboxRegion string
param featureGateSettingName string
param featureGateEnabled bool
param enableSensitiveData bool

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

resource functionPlan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: functionPlanName
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
    zoneRedundant: false
  }
}

resource functionApp 'Microsoft.Web/sites@2024-11-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${functionIdentityResourceId}': {}
    }
  }
  properties: {
    clientAffinityEnabled: false
    functionAppConfig: {
      deployment: {
        storage: {
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: functionIdentityResourceId
          }
          type: 'blobContainer'
          value: '${storageAccount.properties.primaryEndpoints.blob}${deploymentStorageContainerName}'
        }
      }
      runtime: {
        name: 'python'
        version: '3.13'
      }
      scaleAndConcurrency: {
        alwaysReady: []
        instanceMemoryMB: 2048
        maximumInstanceCount: 10
      }
    }
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    serverFarmId: functionPlan.id
    siteConfig: {
      alwaysOn: false
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
    }
  }
}

resource functionAppSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: functionApp
  name: 'appsettings'
  properties: union(
    {
      FUNCTIONS_WORKER_RUNTIME: 'python'
      AzureWebJobsStorage__credential: 'managedidentity'
      AzureWebJobsStorage__clientId: functionIdentityClientId
      AzureWebJobsStorage__blobServiceUri: storageAccount.properties.primaryEndpoints.blob
      AzureWebJobsStorage__queueServiceUri: storageAccount.properties.primaryEndpoints.queue
      AzureWebJobsStorage__tableServiceUri: storageAccount.properties.primaryEndpoints.table
      AzureWebJobsStorage__fileServiceUri: storageAccount.properties.primaryEndpoints.file
      APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'Authorization=AAD;ClientId=${functionIdentityClientId}'
      APPLICATIONINSIGHTS_CONNECTION_STRING: applicationInsights.properties.ConnectionString
      AZURE_CLIENT_ID: functionIdentityClientId
      AZURE_FUNCTIONS_AGENTS_PROVIDER: 'foundry'
      FOUNDRY_PROJECT_ENDPOINT: foundryProjectEndpoint
      FOUNDRY_MODEL: foundryModelDeploymentName
      AZURE_FUNCTIONS_AGENTS_APIM_MODEL_BASE_URL: apimModelBaseUrl
      AZURE_FUNCTIONS_AGENTS_APIM_MODEL_CONTROL_URL: apimModelControlUrl
      AZURE_FUNCTIONS_AGENTS_APIM_MODEL: foundryModelDeploymentName
      AZURE_FUNCTIONS_AGENTS_APIM_MCP_URL: apimMcpUrl
      AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY: apimSubscriptionKey
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_SANDBOX_GROUP_RESOURCE_ID: sandboxGroupResourceId
      AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION: sandboxRegion
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_ALLOWED_HOSTS: '${replace(replace(environment().resourceManager, 'https://', ''), '/', '')},www.example.com'
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_BUNDLE_ROOT: 'sandbox_bundle'
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_BLOB_URI: durableContentBlobUri
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_CONTAINER: durableContentContainerName
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_CLIENT_ID: functionIdentityClientId
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_BACKGROUND_MODEL_ENABLED: 'false'
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_RETAINED_SANDBOX_ENABLED: 'false'
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_FAULT_INJECTION_ENABLED: 'false'
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_APP_OWNED_SANDBOXES: '10'
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_RETAINED_SANDBOX_AUTO_DELETE_SECONDS: '86400'
      AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_SANDBOX_REAPER_AGE_SECONDS: '600'
      AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT: 'medium'
      AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY: 'concise'
      ENABLE_SENSITIVE_DATA: string(enableSensitiveData)
      ENABLE_MULTIPLATFORM_BUILD: 'true'
      PYTHON_ENABLE_INIT_INDEXING: '1'
    },
    {
      '${featureGateSettingName}': string(featureGateEnabled)
    }
  )
}

output functionAppName string = functionApp.name
output functionAppResourceId string = functionApp.id
