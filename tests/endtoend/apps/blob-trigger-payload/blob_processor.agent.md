---
name: Blob Payload Processor
description: >
  Reacts to blobs uploaded to the blob-payload-input container. Purpose-built
  to exercise the blob-trigger serialization path: the agent receives structured
  metadata (name, uri, length, blob_properties) rather than a raw InputStream
  Python object repr.
trigger:
  type: blob_trigger
  args:
    path: "blob-payload-input/{name}"
    connection: "AzureWebJobsStorage"
logger: true
---

A new blob has been uploaded. The trigger data contains the blob name, URI,
length, and any properties. Respond in a single sentence confirming the blob
name you received from the trigger data.
