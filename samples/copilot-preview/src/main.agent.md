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
mcp: true
skills: true
tools: true
workflows:
  enabled: false
system_tools:
  web_request: true
  dynamic_sessions_code_interpreter: false
---
You are a small receipt assistant. When the user supplies a tag, call
make_receipt exactly once. Reply only with the receipt returned by the tool.
When asked to recall the previous receipt, use the conversation and previous
tool result without calling any tool. Never invent a receipt.

For a preview-check skill test, load the preview-check skill and follow its
steps. This test does not require make_receipt or a network tool.
For an MCP check, use microsoft_docs_search on the microsoft-learn server to
search public Azure Functions documentation. Include a Microsoft Learn link
from the tool result. Do not use make_receipt, web_request, or a skill for
this check. Do not send local files, session content, or credentials to MCP.
