---
name: OpenAI Provider Agent
description: Declares an OpenAI provider block with sampling controls.
agent_configuration:
  provider: openai
  model: gpt-4.1-mini
  temperature: 0.2
  top_p: 0.9
  max_tokens: 256
  openai:
    base_url: https://api.openai.example.test/v1
---

You are an OpenAI-backed assistant. Keep responses concise and practical.
