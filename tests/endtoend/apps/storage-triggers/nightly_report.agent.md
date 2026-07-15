---
name: Nightly Report
description: Timer agent that runs on a daily schedule and can also be triggered on demand.
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"
    run_on_startup: false
logger: true
---

When triggered, generate a brief one-line nightly status report.
