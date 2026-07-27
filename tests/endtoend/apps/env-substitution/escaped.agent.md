---
name: Escaped And Fenced
description: "Keeps $$API_TOKEN and %%TENANT_ID%% literal while still resolving $CONTACT_EMAIL."
trigger:
  type: http_trigger
  args:
    route: "escaped"
    methods: ["POST"]
    auth_level: anonymous
---

Store the $$API_TOKEN for tenant %%TENANT_ID%%.

Contact $CONTACT_EMAIL in region $DEPLOY_REGION.

```bash
export API_TOKEN=$API_TOKEN
echo "Channel: %ALERT_CHANNEL%"
curl https://api.example.test/$ENDPOINT
```

Escalate to the %ALERT_CHANNEL% channel.
