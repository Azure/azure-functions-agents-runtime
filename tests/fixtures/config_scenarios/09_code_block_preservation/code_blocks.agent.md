---
name: Code Block Preserver
description: Agent whose instructions contain fenced code blocks that must remain literal.
trigger:
  type: http_trigger
  args:
    route: "code-blocks"
---

You assist developers. The deployment region is $DEPLOY_REGION and the alert channel is %ALERT_CHANNEL%.

Sample shell snippet (this block must NOT be substituted):

```bash
export AZURE_OPENAI_KEY=$AZURE_OPENAI_KEY
echo "Region: %DEPLOY_REGION%"
curl https://api.example.test/$ENDPOINT
```

After the fence, substitution resumes: contact $ONCALL_USER for escalation.

```yaml
secret: $DO_NOT_TOUCH
queue: %DO_NOT_TOUCH%
```

Final line outside any fence: subscription is $SUBSCRIPTION_ID.
