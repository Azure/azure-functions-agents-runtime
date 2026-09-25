---
name: Incident Triage Specialist
description: Deterministic process-level A2A test agent.
builtin_endpoints:
  a2a:
    mode: simple
    url: $A2A_PUBLIC_URL
  http_auth:
    mode: anonymous
---

Return a concise incident brief.
