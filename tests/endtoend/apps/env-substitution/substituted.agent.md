---
name: Substituted Agent
description: Deploys to %DEPLOY_REGION% and pages $CONTACT_EMAIL.
model: $AGENT_MODEL
trigger:
  type: http_trigger
  args:
    route: "substituted"
    methods: ["POST"]
    auth_level: anonymous
---

You operate in region $DEPLOY_REGION and alert the %ALERT_CHANNEL% channel.
