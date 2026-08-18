---
name: ACA Live Model Turn
description: Minimal no-tools agent used only by the opt-in ACA real-turn qualification.
trigger:
  type: http_trigger
  args:
    route: aca-live-model-turn
    methods: ["POST"]
    auth_level: function
mcp: false
skills: false
tools: false
system_tools:
  web_request: false
---

Return a short acknowledgement. Do not call tools or access external resources.
