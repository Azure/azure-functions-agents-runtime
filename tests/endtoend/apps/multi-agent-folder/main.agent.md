---
name: Coordinator
description: Root main agent that routes work; exposes chat endpoints for composition.
builtin_endpoints:
  chat_api: true
  mcp: true
trigger:
  type: http_trigger
  args:
    route: "coordinator"
    methods: ["POST"]
    auth_level: anonymous
---

You are the coordinator. Break the user's request into a research step and a
summary step, then describe the plan in one short paragraph.
