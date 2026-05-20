---
name: Debug Shorthand Off
description: Agent that explicitly disables debug via shorthand=false.
trigger:
  type: http_trigger
  args:
    route: "debug-off"
debug: false
---

You are an agent with debug surfaces explicitly disabled.
