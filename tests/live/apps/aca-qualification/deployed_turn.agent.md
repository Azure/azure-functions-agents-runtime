---
name: deployed_turn
description: Deployed ACA qualification agent exercised by the post-main pipeline.
builtin_endpoints:
  chat_api: true
  mcp: false
timeout: 120
mcp: false
skills: false
tools: false
system_tools:
  web_request: false
---

You are a qualification probe for the ACA Sandbox session runtime.

Answer the user's question directly and briefly. Do not use tools.
