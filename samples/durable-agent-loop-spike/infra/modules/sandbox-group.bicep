param location string
param sandboxGroupName string
param sandboxIdentityResourceId string

#disable-next-line BCP081
resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' = {
  name: sandboxGroupName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${sandboxIdentityResourceId}': {}
    }
  }
  properties: {
    allowedLocations: [
      location
    ]
    defaultCpu: '1'
    defaultDisk: '20Gi'
    defaultMemory: '2Gi'
    defaultTimeoutSeconds: 1800
    maxSandboxCount: 50
  }
}

output sandboxGroupResourceId string = sandboxGroup.id
output sandboxGroupManagementEndpoint string = sandboxGroup.properties.managementEndpoint
