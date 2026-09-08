---
name: Nightly Report
description: Timer agent running every night at 07:00.
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"
    run_on_start: false
---

Generate the nightly report.
