---
name: Azure Reporter
description: Reports on subscription %SUBSCRIPTION_ID% and emails $TO_EMAIL.
model: $AGENT_MODEL_OVERRIDE
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"
    run_on_start: true
---

You are an Azure assistant. The current subscription is $SUBSCRIPTION_ID and the report should be emailed to %TO_EMAIL%.
