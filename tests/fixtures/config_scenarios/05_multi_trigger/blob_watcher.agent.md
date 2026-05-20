---
name: Blob Watcher
description: Reacts to new uploads in the documents container.
trigger:
  type: blob_trigger
  args:
    path: "uploads/{name}.txt"
    connection: "AzureWebJobsStorage"
---

Inspect each new blob and summarize its contents.
