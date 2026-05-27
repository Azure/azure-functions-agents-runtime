---
name: Locked Down
description: Agent that disables tools, skills, and MCP entirely and opts out of sandbox.
trigger:
  type: http_trigger
  args:
    route: "locked-down"
    methods: ["POST"]
    auth_level: function
tools: false
skills: false
mcp: false
system_tools:
  dynamic_sessions_code_interpreter: false
---

You are restricted to no external capabilities. Respond using only the LLM's own knowledge.
