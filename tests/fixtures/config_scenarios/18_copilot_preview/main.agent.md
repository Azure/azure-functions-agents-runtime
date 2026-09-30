---
name: Minimal native preview
description: Explicitly disabled unsupported capabilities.
model: gpt-4.1-mini
builtin_endpoints:
  chat_api: true
  debug_chat_ui: false
  mcp: false
mcp: false
skills: false
tools: false
system_tools:
  web_request: false
  dynamic_sessions_code_interpreter: false
workflows:
  enabled: false
---
Answer briefly without tools.
