targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment which is used to generate a short unique hash used in all resources.')
param environmentName string

@minLength(1)
@description('Primary location for all resources.')
@allowed([
  'australiaeast'
  'centralus'
  'eastus'
  'eastus2'
  'northeurope'
  'westeurope'
  'westus'
  'westus3'
])
@metadata({
  azd: {
    type: 'location'
  }
})
param location string

@description('Subscription the deployed portal scans by default (PORTAL_SUBSCRIPTION_ID). Empty uses the built-in default.')
param portalSubscriptionId string = ''

@description('MSAL first-party app (client) ID the SPA signs in with. Empty uses the Polaris default.')
param msalClientId string = ''

@description('MSAL authority. Empty uses the /organizations default.')
param msalAuthority string = ''

@description('Optional. Exact resource group name to create. When empty, defaults to "rg-<environmentName>".')
param resourceGroupName string = ''

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }

// Empty overrides (azd substitutes '' for unset env vars) fall back to defaults.
var effectivePortalSubscriptionId = empty(portalSubscriptionId)
  ? '1a839f1f-10b2-4613-95ad-0800a22abbf2'
  : portalSubscriptionId
var effectiveMsalClientId = empty(msalClientId)
  ? '0ceccceb-9c05-4953-9193-d94f9daa18d3'
  : msalClientId
var effectiveMsalAuthority = empty(msalAuthority)
  ? 'https://login.microsoftonline.com/organizations'
  : msalAuthority

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: !empty(resourceGroupName) ? resourceGroupName : '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

module portal './app/container-app.bicep' = {
  name: 'portal'
  scope: rg
  params: {
    location: location
    tags: tags
    logAnalyticsName: '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
    containerRegistryName: '${abbrs.containerRegistryRegistries}${resourceToken}'
    managedIdentityName: '${abbrs.managedIdentityUserAssignedIdentities}portal-${resourceToken}'
    managedEnvironmentName: '${abbrs.appManagedEnvironments}${resourceToken}'
    containerAppName: '${abbrs.appContainerApps}portal-${resourceToken}'
    portalSubscriptionId: effectivePortalSubscriptionId
    msalClientId: effectiveMsalClientId
    msalAuthority: effectiveMsalAuthority
  }
}

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = portal.outputs.containerRegistryLoginServer
output AZURE_RESOURCE_GROUP string = rg.name
output PORTAL_URI string = 'https://${portal.outputs.containerAppFqdn}'
