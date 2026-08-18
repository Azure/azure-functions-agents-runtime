---
name: Deployed ACA Qualification Agent
description: Minimal no-tools agent used only for a manually qualified deployed Function App turn.
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
timeout: 120
mcp: false
skills: false
tools: false
system_tools:
  web_request: false
---

Return a brief acknowledgement. Do not call tools or access web, MCP, or external resources.
