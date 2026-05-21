---
name: Foundry System MI Agent
description: Uses Foundry with the default system-assigned credential flow.
agent_configuration:
  provider: foundry
  foundry:
    model: gpt-4.1-nano
    project_endpoint: https://foundry.example.test/api/projects/demo
---

You are a Foundry agent that relies on the default managed identity chain.
