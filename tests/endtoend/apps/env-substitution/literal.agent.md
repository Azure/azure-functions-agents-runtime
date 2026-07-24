---
name: Literal Agent
description: Opts out of substitution so placeholders stay literal.
substitute_variables: false
trigger:
  type: http_trigger
  args:
    route: "literal"
    methods: ["POST"]
    auth_level: anonymous
---

Escalate incidents in $DEPLOY_REGION to the %ALERT_CHANNEL% channel.
