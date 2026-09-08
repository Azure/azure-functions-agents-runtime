param name string
param location string = resourceGroup().location
param tags object = {}
param applicationInsightsName string = ''
param appServicePlanId string
param appSettings object = {}
param runtimeName string
param runtimeVersion string
param serviceName string = 'api'
param storageAccountName string
param deploymentStorageContainerName string
param instanceMemoryMB int = 2048
param maximumInstanceCount int = 100
param identityId string = ''
param identityClientId string = ''

@description('Client ID of the Entra app registration for App Service Authentication. Empty disables Easy Auth setup.')
param entraClientId string = ''

@description('OpenID issuer URL for the Entra app registration (e.g. https://login.microsoftonline.com/<tenant>/v2.0).')
param entraOpenIdIssuer string = ''

@description('Allowed token audiences enforced by App Service Authentication.')
param entraAllowedAudiences array = []

var applicationInsightsIdentity = 'ClientId=${identityClientId};Authorization=AAD'

var baseAppSettings = {
  AzureWebJobsStorage__credential: 'managedidentity'
  AzureWebJobsStorage__clientId: identityClientId
  AzureWebJobsStorage__blobServiceUri: stg.properties.primaryEndpoints.blob
  AzureWebJobsStorage__queueServiceUri: stg.properties.primaryEndpoints.queue
  AzureWebJobsStorage__tableServiceUri: stg.properties.primaryEndpoints.table
  AzureWebJobsStorage__fileServiceUri: stg.properties.primaryEndpoints.file
  APPLICATIONINSIGHTS_AUTHENTICATION_STRING: applicationInsightsIdentity
  APPLICATIONINSIGHTS_CONNECTION_STRING: applicationInsights.properties.ConnectionString
}

var allAppSettings = union(appSettings, baseAppSettings)

resource stg 'Microsoft.Storage/storageAccounts@2022-09-01' existing = {
  name: storageAccountName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

module api 'br/public:avm/res/web/site:0.15.1' = {
  name: '${serviceName}-flex-consumption'
  params: {
    kind: 'functionapp,linux'
    name: name
    location: location
    tags: union(tags, { 'azd-service-name': serviceName })
    serverFarmResourceId: appServicePlanId
    managedIdentities: {
      userAssignedResourceIds: [
        identityId
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${stg.properties.primaryEndpoints.blob}${deploymentStorageContainerName}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identityId
          }
        }
      }
      scaleAndConcurrency: {
        instanceMemoryMB: instanceMemoryMB
        maximumInstanceCount: maximumInstanceCount
      }
      runtime: {
        name: runtimeName
        version: runtimeVersion
      }
    }
    siteConfig: {
      alwaysOn: false
    }
    appSettingsKeyValuePairs: allAppSettings
  }
}

// App Service Authentication (Easy Auth) for the entra-secured agent. Configured
// in "allow anonymous" mode so the API-key agent still works: unauthenticated
// requests reach the function, but any supplied Entra token is validated by the
// platform and surfaced as X-MS-CLIENT-PRINCIPAL. The runtime's entra agent fails
// closed when that principal is absent.
resource siteAuth 'Microsoft.Web/sites/config@2023-12-01' = if (!empty(entraClientId)) {
  name: '${name}/authsettingsV2'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      requireAuthentication: false
      unauthenticatedClientAction: 'AllowAnonymous'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: entraOpenIdIssuer
          clientId: entraClientId
        }
        validation: {
          allowedAudiences: entraAllowedAudiences
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
  dependsOn: [
    api
  ]
}

output SERVICE_API_NAME string = api.outputs.name
