param connectorGatewayName string
param connectionName string = 'office365-outlook'
param mcpServerConfigName string = 'Office-365-Outlook-draft-replies'
param location string = resourceGroup().location
param tags object = {}
param managedIdentityPrincipalId string
param deployerPrincipalId string
param tenantId string

resource connectorGateway 'Microsoft.Web/connectorGateways@2026-05-01-preview' = {
  name: connectorGatewayName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

resource office365Connection 'Microsoft.Web/connectorGateways/connections@2026-05-01-preview' = {
  parent: connectorGateway
  name: connectionName
  properties: {
    connectorName: 'office365'
    displayName: 'Office 365 Outlook Connection'
  }
}

resource office365ConnectionAccessPolicy 'Microsoft.Web/connectorGateways/connections/accessPolicies@2026-05-01-preview' = {
  parent: office365Connection
  name: managedIdentityPrincipalId
  properties: {
    principal: {
      type: 'ActiveDirectory'
      identity: {
        objectId: managedIdentityPrincipalId
        tenantId: tenantId
      }
    }
  }
}

resource office365ConnectionDeployerAccessPolicy 'Microsoft.Web/connectorGateways/connections/accessPolicies@2026-05-01-preview' = {
  parent: office365Connection
  name: deployerPrincipalId
  properties: {
    principal: {
      type: 'ActiveDirectory'
      identity: {
        objectId: deployerPrincipalId
        tenantId: tenantId
      }
    }
  }
}

resource office365ConnectionGatewayAccessPolicy 'Microsoft.Web/connectorGateways/connections/accessPolicies@2026-05-01-preview' = {
  parent: office365Connection
  name: 'connectorGateway-msi'
  properties: {
    principal: {
      type: 'ActiveDirectory'
      identity: {
        objectId: connectorGateway.identity.principalId
        tenantId: tenantId
      }
    }
  }
}

resource office365McpServerConfig 'Microsoft.Web/connectorGateways/mcpserverconfigs@2026-05-01-preview' = {
  parent: connectorGateway
  name: mcpServerConfigName
  properties: {
    state: 'Enabled'
    description: 'Office 365 Outlook draft-reply actions for the Outlook reply agent sample.'
    connectors: [
      {
        name: 'office365'
        connectionName: office365Connection.name
        displayName: 'Office 365 Outlook'
        description: ''
        operations: [
          {
            name: 'DraftEmail'
            displayName: 'Draft an email message'
            description: 'This operation drafts an email message.'
            userParameters: []
            agentParameters: [
              {
                name: 'messageId'
                schema: {
                  type: 'string'
                  description: 'Optional source message ID to draft a reply against.'
                }
              }
              {
                name: 'draftType'
                schema: {
                  type: 'string'
                  description: 'Optional draft type. Use Reply when drafting a reply to an existing message.'
                }
              }
              {
                name: 'comment'
                schema: {
                  type: 'string'
                  description: 'Plain-text reply body for Reply drafts. Use this for the generated visible reply content when draftType is Reply; do not include HTML tags or Markdown.'
                }
              }
              {
                name: 'draftMessage'
                schema: {
                  type: 'object'
                  description: 'Draft message envelope. Include To, Subject, and Body for both new drafts and Reply drafts. For Reply drafts, duplicate the plain-text comment in Body and put the visible reply body in comment.'
                  properties: {
                    To: {
                      type: 'string'
                      format: 'email'
                      description: 'Specify email addresses separated by semicolons like someone@contoso.com'
                    }
                    Subject: {
                      type: 'string'
                      description: 'Specify the subject of the mail'
                    }
                    Body: {
                      type: 'string'
                      format: 'html'
                      description: 'Specify the body of the draft mail'
                    }
                  }
                  required: [
                    'To'
                    'Subject'
                    'Body'
                  ]
                }
              }
            ]
          }
        ]
      }
    ]
    policies: []
    settings: {
      textOnlyContent: true
    }
  }
}

output connectorGatewayName string = connectorGateway.name
output connectionId string = office365Connection.id
output connectionAccessPolicyId string = office365ConnectionAccessPolicy.id
output deployerConnectionAccessPolicyId string = office365ConnectionDeployerAccessPolicy.id
output connectorGatewayConnectionAccessPolicyId string = office365ConnectionGatewayAccessPolicy.id
output mcpEndpointUrl string = office365McpServerConfig.properties.mcpEndpointUrl
