---
name: Azure OpenAI Provider Agent
description: Declares an Azure OpenAI provider block with explicit auth settings.
agent_configuration:
  provider: azure_openai
  temperature: 0.4
  top_p: 0.85
  max_tokens: 512
  azure_openai:
    model: gpt-4.1
    azure_endpoint: https://azure-openai.example.test
    api_version: "2024-10-21"
    api_key: azure-openai-key
---

You are an Azure OpenAI-backed assistant. Prefer Azure terminology in your answers.
