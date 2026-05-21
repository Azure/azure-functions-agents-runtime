---
name: Credential Extra Passthrough
description: Invalid because YAML cannot carry a credential object.
agent_configuration:
  provider: azure_openai
  azure_openai:
    model: gpt-4.1
    azure_endpoint: https://azure-openai.example.test
    api_version: "2024-10-21"
    credential: some-string
---

This fixture should fail validation because credential passthrough is forbidden in YAML.
