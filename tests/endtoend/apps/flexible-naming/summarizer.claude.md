---
name: Summarizer
description: Agent discovered from summarizer.claude.md (flexible *.claude.md naming).
trigger:
  type: http_trigger
  args:
    route: "summarizer"
    methods: ["POST"]
    auth_level: anonymous
---

You are a summarization assistant. Summarize the user's input in one sentence.
