---
name: Azure OpenAI API Key Agent
description: Uses Azure OpenAI API-key authentication.
agent_configuration:
  provider: azure_openai
  azure_openai:
    model: gpt-4.1
    azure_endpoint: https://azure-openai.example.test
    api_version: "2024-10-21"
    api_key: live-api-key
---

You are an Azure OpenAI agent authenticated with an API key.
