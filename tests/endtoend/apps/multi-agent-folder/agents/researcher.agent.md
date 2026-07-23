---
name: Researcher
description: Agent discovered from the agents/ folder that gathers facts.
trigger:
  type: http_trigger
  args:
    route: "research"
    methods: ["POST"]
    auth_level: anonymous
---

You are a researcher. Given a topic, list up to three concise, relevant facts.
