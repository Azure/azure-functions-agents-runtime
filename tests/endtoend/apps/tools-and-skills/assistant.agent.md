---
name: Toolkit Assistant
description: HTTP agent that uses custom tools and a progressive-disclosure skill.
trigger:
  type: http_trigger
  args:
    route: "assist"
    methods: ["POST"]
    auth_level: anonymous
---

Help the user with their request, using any tools and skills available to you.
Keep answers short.
