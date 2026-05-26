---
name: Top Level Model Unset Agent
description: Unsets the inherited model so post-merge validation fails (model is required).
agent_configuration:
  model:
---

Unsetting the only `model` placement triggers the post-merge required-field error.
