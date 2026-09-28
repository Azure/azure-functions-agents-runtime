---
name: Copilot preview
description: A minimal local native-session and custom-tool example.
builtin_endpoints:
  chat_api: true
  debug_chat_ui: false
  mcp: false
  http_auth: anonymous
trigger:
  type: http_trigger
  args:
    route: preview
    methods: [POST]
    http_auth: anonymous
mcp: false
skills: false
tools: true
workflows:
  enabled: false
system_tools:
  web_request: false
  dynamic_sessions_code_interpreter: false
---
You are a small receipt assistant. When the user supplies a tag, call
make_receipt exactly once. Reply only with the receipt returned by the tool.
When asked to recall the previous receipt, use the conversation and previous
tool result without calling any tool. Never invent a receipt.
