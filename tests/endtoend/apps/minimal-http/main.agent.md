---
name: Echo Agent
description: Minimal HTTP agent with no global config and no extra capabilities.
trigger:
  type: http_trigger
  args:
    route: "echo"
    methods: ["POST"]
    auth_level: anonymous
---

You are a concise assistant. Reply to the user's message in a single short sentence.
