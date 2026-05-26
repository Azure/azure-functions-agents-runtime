---
name: Sub-block Model Rejected
description: Invalid because model inside the provider sub-block is rejected.
agent_configuration:
  provider: azure_openai
  azure_openai:
    model: gpt-4o-mini
    azure_endpoint: https://azure-openai.example.test
    api_version: "2024-10-21"
---

<!-- Invalid case: 'model' inside provider sub-block is rejected. See Slice 4 of Option E. -->

This fixture should fail validation because model must be declared only at the top level.
