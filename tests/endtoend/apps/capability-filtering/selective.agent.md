---
name: Selective Filters
description: Inherits capabilities but excludes one MCP server, one skill, and one tool.
trigger:
  type: http_trigger
  args:
    route: "selective"
    methods: ["POST"]
    auth_level: anonymous
mcp:
  exclude:
    - internal-api
skills:
  exclude:
    - style-guide
tools:
  exclude:
    - add_numbers
---

Use whatever tools, skills, and MCP servers are available to you to answer the user.
