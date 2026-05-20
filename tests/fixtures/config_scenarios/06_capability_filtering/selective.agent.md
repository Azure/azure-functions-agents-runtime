---
name: Selective Filters
description: Uses exclude lists across mcp, skills, and tools, and pins custom_only.
trigger:
  type: http_trigger
  args:
    route: "selective"
mcp:
  exclude:
    - experimental-server
    - custom-api
skills:
  exclude:
    - compliance-checker
    - security-review
tools:
  exclude:
    - web_fetch
---

You are an agent that prefers internal tools only.
