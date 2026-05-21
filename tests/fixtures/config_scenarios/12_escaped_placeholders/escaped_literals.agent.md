---
name: Escaped Literals
description: "Keep $$API_TOKEN and %%TENANT_ID%% literal for %TEAM%."
trigger:
  type: http_trigger
  args:
    route: escaped-literals
    methods: ["POST"]
    auth_level: function
agent_configuration:
  provider: openai
  openai:
    model: $AGENT_MODEL
metadata:
  literal_dollar: "$$API_TOKEN"
  literal_percent: "%%TENANT_ID%%"
  mixed: "team-%TEAM%-uses-$$API_TOKEN-and-%%TENANT_ID%%"
---

Render literal examples: $$API_TOKEN and %%TENANT_ID%%.

Still resolve normal placeholders: model $AGENT_MODEL, contact $CONTACT_EMAIL.
