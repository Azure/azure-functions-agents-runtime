---
name: Builtin Shorthand On
description: Agent that uses the shorthand builtin_endpoints=true to enable chat surfaces.
trigger:
  type: http_trigger
  args:
    route: "builtin-on"
builtin_endpoints: true
---

You are an agent that has built-in chat surfaces enabled via shorthand.
