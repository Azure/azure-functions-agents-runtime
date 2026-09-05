param location string
param functionIdentityName string
param sandboxIdentityName string

resource functionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: functionIdentityName
  location: location
}

resource sandboxIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: sandboxIdentityName
  location: location
}

output functionIdentityResourceId string = functionIdentity.id
output functionIdentityClientId string = functionIdentity.properties.clientId
output functionIdentityPrincipalId string = functionIdentity.properties.principalId
output sandboxIdentityResourceId string = sandboxIdentity.id
output sandboxIdentityPrincipalId string = sandboxIdentity.properties.principalId
