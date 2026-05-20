---
name: Queue Worker
description: Processes work items off a storage queue.
trigger:
  type: queue_trigger
  args:
    name: "work-items"
    connection: "AzureWebJobsStorage"
---

Process each queue message and acknowledge completion.
