---
name: Main Agent
description: A main agent bound to a timer trigger, which is not HTTP-shaped (Row 4).
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"
    run_on_start: false
---
Generate the nightly report.
