---
name: Locked Down
description: Disables tools, skills, and MCP entirely to run on model knowledge only.
trigger:
  type: http_trigger
  args:
    route: "locked-down"
    methods: ["POST"]
    auth_level: anonymous
tools: false
skills: false
mcp: false
---

You have no external tools, skills, or MCP servers. Answer using only your own
knowledge, in one short sentence.
