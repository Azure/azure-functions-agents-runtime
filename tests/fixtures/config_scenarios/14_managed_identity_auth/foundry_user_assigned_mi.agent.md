---
name: Foundry User Assigned MI Agent
description: Uses Foundry with a user-assigned managed identity.
agent_configuration:
  provider: foundry
  foundry:
    model: gpt-4.1-nano
    project_endpoint: https://foundry.example.test/api/projects/demo
    managed_identity_client_id: "22222222-2222-2222-2222-222222222222"
---

You are a Foundry agent authenticated with a user-assigned managed identity.
