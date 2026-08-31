---
name: retry-policy-e2e
description: Start the canonical retry-policy E2E workflow for order ORD-1001.
---

# Retry policy E2E

1. Call `read_skill_resource` to read `references/order-recovery-plan.json`.
2. Pass the parsed JSON to `start_workflow` unchanged.
3. Return the workflow id and do not poll.

The fixed resource intentionally requests five attempts and a 30-second timeout.
The `reserve_inventory` tool declares three attempts and a five-second timeout,
so the E2E test can verify decorator precedence after execution.
