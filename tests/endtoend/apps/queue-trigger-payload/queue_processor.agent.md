---
name: Queue Payload Processor
description: >
  Processes JSON messages from a storage queue. Purpose-built to exercise the
  queue-message trigger-data serialization: the agent receives a structured JSON
  payload containing body, body_encoding, body_json, id, and dequeue_count.
trigger:
  type: queue_trigger
  args:
    queue_name: "queue-payload-input"
    connection: "AzureWebJobsStorage"
logger: true
---

You receive a structured JSON message from a storage queue. The trigger data
includes a `body` field (the raw message text) and a `body_json` field (the
parsed JSON object when the body is valid JSON). Respond in a single sentence
that confirms you received the message and names the value of `body_json.order`.
