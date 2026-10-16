---
name: Runtime tool authoring
description: Only runtime descriptors and local public functions register.
model: fixture-model
system_tools:
  web_request: false
tools:
  exclude:
    - fhs_execute_python
workflows:
  enabled: true
  exclude:
    - fhs_start_workflow
builtin_endpoints:
  chat_api: true
  debug_chat_ui: false
  mcp: false
---
Use only the registered runtime tools.
