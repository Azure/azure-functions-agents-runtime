---
name: Blob Watcher
description: Reacts to new text blobs uploaded to the uploads container.
trigger:
  type: blob_trigger
  args:
    path: "uploads/{name}.txt"
    connection: "AzureWebJobsStorage"
logger: true
---

A new blob has been uploaded. Summarize its contents in one short sentence.
