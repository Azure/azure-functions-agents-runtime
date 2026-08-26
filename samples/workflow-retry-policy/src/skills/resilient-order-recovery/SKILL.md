---
name: resilient-order-recovery
description: Recover order ORD-1001 through the canonical retry-policy workflow. Use when a user asks to retry, recover, or complete the delayed sample order.
---

# Resilient order recovery

Recover order `ORD-1001` with the canonical workflow plan stored in
`references/order-recovery-plan.json`.

1. Call `read_skill_resource` to read `references/order-recovery-plan.json`
   from this skill.
2. Pass the parsed JSON to `start_workflow` unchanged. Do not add, remove,
   rename, or move fields.
3. Return the workflow id and do not poll.

The first task stores a simulated inventory-service incident in Azure Blob
Storage. The plan then asks for a longer DAG timeout and five retry attempts on
inventory reservation. The `reserve_inventory` tool author declares a
five-second timeout and three attempts with `@workflow_tool`; those decorator
declarations are authoritative. Preserving the resource unchanged makes that
precedence visible in the final workflow status.
