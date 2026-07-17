---
name: Entra Secured Agent
description: Chat agent whose HTTP API requires a valid Entra ID (Azure AD) token.

builtin_endpoints:
  chat_api: true
  http_auth:
    mode: entra
    # tenant_id and allowed_audiences are read from the environment variables
    # set by the Bicep deployment:
    #   AZURE_FUNCTIONS_AGENTS_ENTRA_TENANT_ID
    #   AZURE_FUNCTIONS_AGENTS_ENTRA_AUDIENCES
    # You can also pin them inline here instead, for example:
    # entra:
    #   tenant_id: "<tenant-guid>"
    #   allowed_audiences: ["api://agents"]
---

You are a helpful assistant. Answer questions clearly and concisely.
