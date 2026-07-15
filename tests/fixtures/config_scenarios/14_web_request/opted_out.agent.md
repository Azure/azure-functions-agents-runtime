---
name: Opted Out Agent
description: Agent that explicitly opts out of the default-on web_request tool.
trigger:
  type: http_trigger
  args:
    route: "opted-out"
    methods: ["POST"]
    auth_level: function
system_tools:
  web_request: false
---

You are restricted from making outbound web requests.
