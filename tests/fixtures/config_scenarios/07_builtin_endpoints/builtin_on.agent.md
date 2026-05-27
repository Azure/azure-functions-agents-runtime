---
name: Builtin Shorthand On
description: Agent that uses the shorthand builtin_endpoints=true to enable all built-in endpoints.
trigger:
  type: http_trigger
  args:
    route: "builtin-on"
builtin_endpoints: true
---

You are an agent that has all built-in endpoints enabled via shorthand.
