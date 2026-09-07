param storageAccountName string
param applicationInsightsName string
param foundryAccountName string
param sandboxGroupName string
param functionPrincipalId string
param sandboxPrincipalId string
param apimPrincipalId string
param deployerPrincipalId string
param deployerPrincipalType string
param assignmentNames object

var storageBlobDataOwnerRoleId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var storageQueueDataContributorRoleId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var cognitiveServicesOpenAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var sandboxGroupDataOwnerRoleId = 'c24cf47c-5077-412d-a19c-45202126392c'
var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' existing = {
  name: foundryAccountName
}

#disable-next-line BCP081
resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' existing = {
  name: sandboxGroupName
}

resource functionStorageBlobOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.functionStorageBlobOwner
  scope: storageAccount
  properties: {
    principalId: functionPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwnerRoleId)
  }
}

resource functionStorageQueueContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.functionStorageQueueContributor
  scope: storageAccount
  properties: {
    principalId: functionPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageQueueDataContributorRoleId
    )
  }
}

resource functionStorageTableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.functionStorageTableContributor
  scope: storageAccount
  properties: {
    principalId: functionPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageTableDataContributorRoleId
    )
  }
}

resource functionMonitoringMetricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.functionMonitoringMetricsPublisher
  scope: applicationInsights
  properties: {
    principalId: functionPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      monitoringMetricsPublisherRoleId
    )
  }
}

resource functionCognitiveServicesUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.functionCognitiveServicesUser
  scope: foundryAccount
  properties: {
    principalId: functionPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
  }
}

resource functionCognitiveServicesOpenAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.functionCognitiveServicesOpenAiUser
  scope: foundryAccount
  properties: {
    principalId: functionPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesOpenAiUserRoleId
    )
  }
}

resource functionSandboxGroupDataOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.functionSandboxGroupDataOwner
  scope: sandboxGroup
  properties: {
    principalId: functionPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sandboxGroupDataOwnerRoleId)
  }
}

resource sandboxFoundryReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.sandboxFoundryReader
  scope: foundryAccount
  properties: {
    principalId: sandboxPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
  }
}

resource apimCognitiveServicesOpenAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.apimCognitiveServicesOpenAiUser
  scope: foundryAccount
  properties: {
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesOpenAiUserRoleId
    )
  }
}

resource deployerStorageBlobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.deployerStorageBlobContributor
  scope: storageAccount
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataContributorRoleId
    )
  }
}

resource deployerStorageBlobOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.deployerStorageBlobOwner
  scope: storageAccount
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataOwnerRoleId
    )
  }
}

resource deployerSandboxGroupDataOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: assignmentNames.deployerSandboxGroupDataOwner
  scope: sandboxGroup
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sandboxGroupDataOwnerRoleId)
  }
}
