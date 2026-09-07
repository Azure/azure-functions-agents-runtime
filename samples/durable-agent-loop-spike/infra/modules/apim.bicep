param apimServiceName string
param workloadResourceGroupName string
param applicationInsightsName string
param foundryAccountName string
param modelApiName string
param modelControlApiName string
param mcpApiName string
param productName string
param subscriptionName string
param loggerName string
param instrumentationKeyNamedValueName string
param tokensPerMinute int

var modelBackendUrl = 'https://${foundryAccountName}.services.ai.azure.com/'
var mcpBackendUrl = 'https://learn.microsoft.com/api/mcp'
var applicationInsightsScope = resourceGroup(az.subscription().subscriptionId, workloadResourceGroupName)

resource apimService 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimServiceName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  scope: applicationInsightsScope
  name: applicationInsightsName
}

resource modelBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = {
  parent: apimService
  name: modelApiName
  properties: {
    credentials: any({
      managedIdentity: {
        resource: 'https://ai.azure.com/'
      }
    })
    description: 'Pinned backend for the durable agent loop private spike'
    protocol: 'http'
    tls: {
      validateCertificateChain: true
      validateCertificateName: true
    }
    url: modelBackendUrl
  }
}

resource modelApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apimService
  name: modelApiName
  properties: {
    apiRevision: '1'
    description: 'Private durable loop Responses API lane'
    displayName: 'Durable agent loop model'
    path: modelApiName
    protocols: [
      'https'
    ]
    subscriptionKeyParameterNames: {
      header: 'api-key'
      query: 'subscription-key'
    }
    subscriptionRequired: true
  }
}

resource chatCompletionsOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: modelApi
  name: 'chat-completions'
  properties: {
    displayName: 'chat-completions'
    method: 'POST'
    responses: []
    templateParameters: []
    urlTemplate: '/openai/v1/chat/completions'
  }
}

resource responsesCreateOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: modelApi
  name: 'responses-create'
  properties: {
    displayName: 'responses-create'
    method: 'POST'
    responses: []
    templateParameters: []
    urlTemplate: '/openai/v1/responses'
  }
}

resource modelApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: modelApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(
      replace(
        '''
      <policies>
        <inbound>
          <base />
          <set-header name="api-key" exists-action="delete" />
          <set-backend-service backend-id="__MODEL_BACKEND_NAME__" />
          <llm-token-limit counter-key="@(context.Subscription.Id)" tokens-per-minute="__TOKENS_PER_MINUTE__" estimate-prompt-tokens="false" />
          <llm-emit-token-metric namespace="DurableAgentLoopSpike">
            <dimension name="API ID" />
            <dimension name="Subscription ID" />
            <dimension name="Backend ID" />
          </llm-emit-token-metric>
        </inbound>
        <backend>
          <forward-request timeout="600" buffer-response="false" fail-on-error-status-code="true" />
        </backend>
        <outbound>
          <base />
        </outbound>
        <on-error>
          <base />
        </on-error>
      </policies>
    ''',
        '__MODEL_BACKEND_NAME__',
        modelBackend.name
      ),
      '__TOKENS_PER_MINUTE__',
      string(tokensPerMinute)
    )
  }
}

resource modelControlApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apimService
  name: modelControlApiName
  properties: {
    apiRevision: '1'
    description: 'Private bounded background response poll/cancel lane'
    displayName: 'Durable agent loop model control'
    path: modelControlApiName
    protocols: [
      'https'
    ]
    subscriptionKeyParameterNames: {
      header: 'api-key'
      query: 'subscription-key'
    }
    subscriptionRequired: true
  }
}

resource controlResponsesPollOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: modelControlApi
  name: 'responses-poll'
  properties: {
    displayName: 'responses-poll'
    method: 'GET'
    responses: []
    templateParameters: []
    urlTemplate: '/responses'
  }
}

resource controlResponsesCancelOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: modelControlApi
  name: 'responses-cancel'
  properties: {
    displayName: 'responses-cancel'
    method: 'POST'
    responses: []
    templateParameters: []
    urlTemplate: '/responses/cancel'
  }
}

resource modelControlApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: modelControlApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(
      '''
      <policies>
        <inbound>
          <base />
          <set-header name="api-key" exists-action="delete" />
          <check-header name="x-af-response-id" failed-check-httpcode="400" failed-check-error-message="Missing response identifier" ignore-case="false" />
          <set-variable name="responseId" value="@(context.Request.Headers.GetValueOrDefault(&quot;x-af-response-id&quot;, &quot;&quot;))" />
          <choose>
            <when condition="@(!System.Text.RegularExpressions.Regex.IsMatch((string)context.Variables[&quot;responseId&quot;], &quot;^resp_[A-Za-z0-9]{16,160}$&quot;))">
              <return-response>
                <set-status code="400" reason="Invalid response identifier" />
              </return-response>
            </when>
          </choose>
          <set-backend-service backend-id="__MODEL_BACKEND_NAME__" />
          <choose>
            <when condition="@(context.Operation.Id == &quot;responses-poll&quot;)">
              <rewrite-uri template="@(&quot;/openai/v1/responses/&quot; + (string)context.Variables[&quot;responseId&quot;])" copy-unmatched-params="false" />
            </when>
            <when condition="@(context.Operation.Id == &quot;responses-cancel&quot;)">
              <rewrite-uri template="@(&quot;/openai/v1/responses/&quot; + (string)context.Variables[&quot;responseId&quot;] + &quot;/cancel&quot;)" copy-unmatched-params="false" />
            </when>
            <otherwise>
              <return-response>
                <set-status code="404" reason="Not Found" />
              </return-response>
            </otherwise>
          </choose>
          <set-header name="x-af-response-id" exists-action="delete" />
        </inbound>
        <backend>
          <forward-request timeout="120" buffer-response="false" fail-on-error-status-code="true" />
        </backend>
        <outbound>
          <base />
        </outbound>
        <on-error>
          <base />
        </on-error>
      </policies>
    ''',
      '__MODEL_BACKEND_NAME__',
      modelBackend.name
    )
  }
}

resource mcpApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apimService
  name: mcpApiName
  properties: {
    apiRevision: '1'
    description: 'Private read-only Microsoft Learn MCP lane'
    displayName: 'Durable agent loop MCP'
    path: mcpApiName
    protocols: [
      'https'
    ]
    serviceUrl: mcpBackendUrl
    subscriptionKeyParameterNames: {
      header: 'api-key'
      query: 'subscription-key'
    }
    subscriptionRequired: true
  }
}

resource mcpGetOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: mcpApi
  name: 'mcp-get'
  properties: {
    displayName: 'mcp-get'
    method: 'GET'
    responses: []
    templateParameters: []
    urlTemplate: '/'
  }
}

resource mcpPostOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: mcpApi
  name: 'mcp-post'
  properties: {
    displayName: 'mcp-post'
    method: 'POST'
    responses: []
    templateParameters: []
    urlTemplate: '/'
  }
}

resource mcpApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: mcpApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: '''
      <policies>
        <inbound>
          <base />
          <set-header name="api-key" exists-action="delete" />
          <rate-limit calls="120" renewal-period="60" />
        </inbound>
        <backend>
          <forward-request timeout="120" buffer-response="false" fail-on-error-status-code="true" />
        </backend>
        <outbound>
          <base />
        </outbound>
        <on-error>
          <base />
        </on-error>
      </policies>
    '''
  }
}

resource instrumentationKeyNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apimService
  name: instrumentationKeyNamedValueName
  properties: {
    displayName: instrumentationKeyNamedValueName
    secret: true
    tags: [
      'durable-agent-loop'
    ]
    value: applicationInsights.properties.InstrumentationKey
  }
}

resource applicationInsightsLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apimService
  name: loggerName
  properties: {
    credentials: {
      instrumentationKey: '{{${instrumentationKeyNamedValue.name}}}'
    }
    description: 'Durable agent loop API-scoped telemetry'
    isBuffered: false
    loggerType: 'applicationInsights'
    resourceId: applicationInsights.id
  }
}

var diagnosticProperties = {
  alwaysLog: 'allErrors'
  backend: {
    request: {
      body: {
        bytes: 0
      }
      headers: [
        'traceparent'
        'x-af-operation-id'
      ]
    }
    response: {
      body: {
        bytes: 0
      }
      headers: [
        'request-id'
      ]
    }
  }
  frontend: {
    request: {
      body: {
        bytes: 0
      }
      headers: [
        'traceparent'
        'x-af-operation-id'
      ]
    }
    response: {
      body: {
        bytes: 0
      }
      headers: [
        'request-id'
      ]
    }
  }
  httpCorrelationProtocol: 'W3C'
  logClientIp: false
  loggerId: applicationInsightsLogger.id
  metrics: true
  sampling: {
    percentage: 100
    samplingType: 'fixed'
  }
  verbosity: 'information'
}

resource modelDiagnostic 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: modelApi
  name: 'applicationinsights'
  properties: diagnosticProperties
}

resource mcpDiagnostic 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: mcpApi
  name: 'applicationinsights'
  properties: diagnosticProperties
}

resource product 'Microsoft.ApiManagement/service/products@2024-05-01' = {
  parent: apimService
  name: productName
  properties: {
    approvalRequired: false
    description: 'Isolated nonproduction product for durable agent loop qualification'
    displayName: 'Durable agent loop spike'
    state: 'published'
    subscriptionRequired: true
  }
}

resource modelProductAssociation 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = {
  parent: product
  name: modelApi.name
}

resource modelControlProductAssociation 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = {
  parent: product
  name: modelControlApi.name
}

resource mcpProductAssociation 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = {
  parent: product
  name: mcpApi.name
}

resource spikeSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apimService
  name: subscriptionName
  properties: {
    allowTracing: false
    displayName: 'Durable agent loop spike'
    scope: product.id
    state: 'active'
  }
}
