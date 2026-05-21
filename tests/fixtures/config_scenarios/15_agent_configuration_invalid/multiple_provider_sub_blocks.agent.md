---
name: Multiple Provider Sub-blocks
description: Invalid because it declares two provider-specific sub-blocks.
agent_configuration:
  provider: openai
  openai:
    model: gpt-4.1-mini
  azure_openai:
    model: gpt-4.1
    azure_endpoint: https://azure-openai.example.test
    api_version: "2024-10-21"
---

This fixture should fail validation because only one provider block is allowed.
