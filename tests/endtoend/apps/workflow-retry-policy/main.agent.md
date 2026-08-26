---
name: Retry Policy E2E
description: Exercises model-generated Dynamic Workflow retry policy precedence.
builtin_endpoints: true
workflows:
  enabled: true
---

When the user asks to recover order `ORD-1001`, use the
`retry-policy-e2e` skill exactly. Report the workflow id after
`start_workflow` returns and do not poll.
