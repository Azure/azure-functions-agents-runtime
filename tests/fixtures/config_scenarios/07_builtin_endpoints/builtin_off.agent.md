---
name: Builtin Shorthand Off
description: Agent that explicitly disables built-in endpoints via shorthand=false.
trigger:
  type: http_trigger
  args:
    route: "builtin-off"
builtin_endpoints: false
---

You are an agent with built-in endpoints explicitly disabled.
