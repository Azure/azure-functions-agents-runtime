---
name: Report Agent
description: An agent in the agents folder for reports
trigger:
  type: timer_trigger
  args:
    schedule: 0 0 * * * *
---
You generate daily reports.
