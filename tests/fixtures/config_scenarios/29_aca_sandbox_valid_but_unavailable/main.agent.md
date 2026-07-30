---
name: Main Agent
description: >
  An otherwise-fully-valid aca_sandbox configuration (passes rows 1-11):
  used to prove Row 12's host-ABI gate, Row 13's always-hard-fail backstop
  (vacuously satisfied by the unconditional capability gate), and that a
  valid aca_sandbox config parses successfully yet still fails the startup
  capability gate rather than a confusing runtime error at first request.
builtin_endpoints:
  chat_api: true
---
Assist the user.
