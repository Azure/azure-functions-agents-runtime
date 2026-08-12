---
name: order-review
description: Review order payloads for fulfillment readiness and operational risk.
---

# Order review

Use `summarize_order_quantities` before assessing an order with line items.

Flag missing SKUs, non-positive quantities, unusually large totals, and details that
prevent fulfillment. Keep recommendations concise and never claim an external action
completed without a confirming tool result.
