---
name: order-review
description: Review order payloads for fulfillment readiness and operational risk.
---

# Order review

Treat `summary` and `review_signals` as trusted outputs from deterministic application
code. Use `summarize_order_quantities` only when those prepared fields are absent.

Explain how the signals affect fulfillment, identify operational details still needed,
and prioritize human-review actions. Keep recommendations concise and never claim an
external action completed without a confirming tool result.
