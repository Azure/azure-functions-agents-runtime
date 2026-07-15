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

You keep the glossary skill, the reverse_text tool, and the Microsoft Learn MCP
server, but you no longer have the style-guide skill, the add_numbers tool, or the
internal-api MCP server. Use what remains to answer the user.
