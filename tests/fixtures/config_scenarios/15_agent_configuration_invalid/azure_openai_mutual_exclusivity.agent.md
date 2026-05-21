---
name: Azure OpenAI Mutual Exclusivity
description: Invalid because api_key and managed_identity_client_id are both set.
agent_configuration:
  provider: azure_openai
  azure_openai:
    model: gpt-4.1
    azure_endpoint: https://azure-openai.example.test
    api_version: "2024-10-21"
    api_key: forbidden-with-managed-identity
    managed_identity_client_id: "33333333-3333-3333-3333-333333333333"
---

This fixture should fail validation because Azure OpenAI auth modes are mutually exclusive.
