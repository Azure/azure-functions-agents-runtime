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

Substitution is disabled for this agent, so the text $DEPLOY_REGION and
%ALERT_CHANNEL% must remain exactly as written and not be replaced.
