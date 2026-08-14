---
name: Incident Commander
description: Owns incident workflows
builtin_endpoints:
  chat_api: true
workflows:
  enabled: true
  exclude: [release_evidence]
  subagents:
    - agent: incident_analyst
---
Handle incidents.

