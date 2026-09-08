---
name: UI And MCP Agent
description: HTTP agent that also enables the debug chat UI and MCP tool registration.
trigger:
  type: http_trigger
  args:
    route: "ui-and-mcp"
    methods: ["POST"]
    auth_level: anonymous
builtin_endpoints:
  debug_chat_ui: true
  mcp: true
---

You are a demo agent. Respond to the user's message in one short sentence.
