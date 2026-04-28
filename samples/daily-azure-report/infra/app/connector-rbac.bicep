param connectionName string
param managedIdentityPrincipalId string

// Contributor role — grants the managed identity access to invoke the connector
var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'

resource connection 'Microsoft.Web/connections@2016-06-01' existing = {
  name: connectionName
}

resource connectorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(connection.id, managedIdentityPrincipalId, contributorRoleId)
  scope: connection
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}
