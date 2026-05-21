---
name: MCP Consumer
description: Uses mcp.json discovery to enable github and filesystem servers.
trigger:
  type: http_trigger
  args:
    route: "mcp-consumer"
---

You can call tools from the github and filesystem MCP servers.
