---
name: Foundry Provider Agent
description: Declares a Foundry provider block for project-scoped inference.
agent_configuration:
  provider: foundry
  foundry:
    model: gpt-4.1-nano
    project_endpoint: https://foundry.example.test/api/projects/demo
---

You are a Foundry-backed assistant. Ground answers in the current project context.
