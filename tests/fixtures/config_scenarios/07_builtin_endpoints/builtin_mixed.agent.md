---
name: Builtin Mixed
description: Agent that pins individual built-in endpoints via the object form.
trigger:
  type: http_trigger
  args:
    route: "builtin-mixed"
builtin_endpoints:
  debug_chat_ui: true
  chat_api: true
  mcp: false
---

You are an agent with debug chat UI and HTTP chat API enabled but MCP exposure off.
