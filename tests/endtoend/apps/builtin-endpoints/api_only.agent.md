---
name: API Only Agent
description: Timer agent that also exposes the REST chat API via the object form.
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"
    run_on_startup: false
builtin_endpoints:
  chat_api: true
  debug_chat_ui: false
  mcp: false
---

When triggered, produce a one-line status summary. When called via the chat API,
answer the user's question briefly.
