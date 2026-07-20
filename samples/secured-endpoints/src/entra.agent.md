---
name: Entra Secured Agent
description: Chat agent whose HTTP API requires a valid Entra ID (Azure AD) token.

builtin_endpoints:
  chat_api: true
  http_auth:
    mode: entra
    # tenant_id and allowed_audiences are optional allow-lists. To keep secrets
    # out of source, reference environment variables inline — the runtime
    # resolves $VAR / %VAR% placeholders at load time, so the frontmatter stays
    # the single source of truth. The Bicep deployment sets ENTRA_TENANT_ID /
    # ENTRA_AUDIENCE:
    entra:
      tenant_id: $ENTRA_TENANT_ID
      allowed_audiences: ["$ENTRA_AUDIENCE"]
    # You can also pin the values inline instead, for example:
    # entra:
    #   tenant_id: "<tenant-guid>"
    #   allowed_audiences: ["api://agents"]
---

You are a helpful assistant. Answer questions clearly and concisely.
