---
name: Resource Summary
description: HTTP-triggered structured summary endpoint.
trigger:
  type: http_trigger
  args:
    route: "resource-summary"
    methods: ["POST"]
    auth_level: function
---

Return a structured resource summary for the supplied subscription id.
