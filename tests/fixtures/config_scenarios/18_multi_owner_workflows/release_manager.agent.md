---
name: Release Manager
description: Owns release workflows
builtin_endpoints:
  chat_api: true
workflows:
  enabled: true
  exclude: [incident_evidence]
  subagents:
    - agent: release_reviewer
---
Handle releases.

