---
name: Debug Shorthand On
description: Agent that uses the shorthand debug=true to enable all surfaces.
trigger:
  type: http_trigger
  args:
    route: "debug-on"
debug: true
---

You are an agent that has debugging fully enabled via shorthand.
