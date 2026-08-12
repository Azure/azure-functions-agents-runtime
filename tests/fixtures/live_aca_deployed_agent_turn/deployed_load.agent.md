---
name: Deployed ACA Load Qualification Agent
description: Load-only fixture agent for the manual persistent ACA qualification.
builtin_endpoints:
  chat_api: true
  mcp: false
  http_auth:
    mode: entra
    entra:
      tenant_id: $AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_ENTRA_TENANT_ID
      allowed_audiences:
        - $AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE
      allowed_client_ids:
        - $AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TEST_INVOKER_CLIENT_ID
model: $AZURE_OPENAI_DEPLOYMENT
timeout: 480
mcp: false
skills: false
tools: true
system_tools:
  web_request: false
---

For a load qualification request, call `qualification_hold` exactly once, then return a brief
acknowledgement. Do not access web, MCP, or external resources.
