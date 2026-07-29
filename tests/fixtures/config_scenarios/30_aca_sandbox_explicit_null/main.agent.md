---
name: Main Agent
description: >
  A bare `aca_sandbox:` key -- present in the YAML but with no value, which
  parses to an explicit Python `None`, not an absent key. Must be rejected at
  the schema layer distinctly from `aca_sandbox` being omitted entirely
  (which defaults cleanly to the in-process backend with no error): an
  explicit null previously bypassed AcaSandboxConfig's own required-field
  validation and silently fell back to the default backend (fail-open).
builtin_endpoints:
  chat_api: true
---
Assist the user.
