param location string
param schedulerName string
param taskHubName string
param tags object

resource scheduler 'Microsoft.DurableTask/schedulers@2026-05-01-preview' = {
  name: schedulerName
  location: location
  tags: tags
  properties: {
    ipAllowlist: [
      '0.0.0.0/0'
    ]
    publicNetworkAccess: 'Enabled'
    sku: {
      name: 'Consumption'
    }
  }
}

resource taskHub 'Microsoft.DurableTask/schedulers/taskHubs@2026-05-01-preview' = {
  parent: scheduler
  name: taskHubName
  properties: {
    capabilities: []
  }
}

output schedulerEndpoint string = scheduler.properties.endpoint
output schedulerName string = scheduler.name
output taskHubName string = taskHub.name
