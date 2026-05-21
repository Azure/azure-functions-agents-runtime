---
name: Azure OpenAI User Assigned MI Agent
description: Uses Azure OpenAI with a user-assigned managed identity.
agent_configuration:
  provider: azure_openai
  azure_openai:
    model: gpt-4.1
    azure_endpoint: https://azure-openai.example.test
    api_version: "2024-10-21"
    managed_identity_client_id: "11111111-1111-1111-1111-111111111111"
---

You are an Azure OpenAI agent authenticated with a user-assigned managed identity.
