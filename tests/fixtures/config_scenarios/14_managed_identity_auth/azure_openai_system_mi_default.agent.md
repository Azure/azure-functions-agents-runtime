---
name: Azure OpenAI System MI Agent
description: Uses Azure OpenAI with the default system-assigned credential flow.
agent_configuration:
  provider: azure_openai
  model: gpt-4.1
  azure_openai:
    azure_endpoint: https://azure-openai.example.test
    api_version: "2024-10-21"
---

You are an Azure OpenAI agent that relies on the default managed identity chain.
