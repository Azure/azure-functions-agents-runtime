---
name: Unset API Key Agent
description: Removes the inherited API key and switches to managed identity.
agent_configuration:
  azure_openai:
    api_key:
    managed_identity_client_id: cid
---

Unset the inherited API key.
