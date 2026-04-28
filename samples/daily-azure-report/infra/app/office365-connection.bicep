param connectionName string
param location string = resourceGroup().location
param tags object = {}

resource office365Connection 'Microsoft.Web/connections@2016-06-01' = {
  name: connectionName
  location: location
  tags: tags
  properties: {
    displayName: 'Office 365 Outlook'
    api: {
      id: subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'office365')
    }
  }
}

output connectionId string = office365Connection.id
output connectionName string = office365Connection.name
