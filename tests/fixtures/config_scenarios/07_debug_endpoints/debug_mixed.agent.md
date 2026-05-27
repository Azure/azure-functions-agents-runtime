---
name: Debug Mixed
description: Agent that pins individual debug surfaces via the object form.
trigger:
  type: http_trigger
  args:
    route: "debug-mixed"
debug_endpoints:
  chat_ui: true
  chat_api: true
  mcp: false
---

You are an agent with chat and http debug surfaces enabled but MCP exposure off.
