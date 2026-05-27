---
name: Debug Shorthand On
description: Agent that uses the shorthand debug_endpoints=true to enable chat surfaces.
trigger:
  type: http_trigger
  args:
    route: "debug-on"
debug_endpoints: true
---

You are an agent that has chat debugging enabled via shorthand.
