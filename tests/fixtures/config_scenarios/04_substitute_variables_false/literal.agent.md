---
name: Literal Agent
description: Demonstrates substitute_variables=false keeping placeholders literal.
agent_configuration:
  provider: openai
  model: $AGENT_MODEL
  openai: {}
substitute_variables: false
trigger:
  type: http_trigger
  args:
    route: "literal"
    methods: ["POST"]
    auth_level: function
response_example: $RESPONSE_TEMPLATE
---

Send the daily summary to $TO_EMAIL using the %REPORT_FORMAT% template.
