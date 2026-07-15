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

Escaped placeholders stay literal: $$API_TOKEN and %%TENANT_ID%%.

Normal placeholders still resolve: contact $CONTACT_EMAIL in region $DEPLOY_REGION.

The following fenced block must NOT be substituted:

```bash
export API_TOKEN=$API_TOKEN
echo "Channel: %ALERT_CHANNEL%"
curl https://api.example.test/$ENDPOINT
```

After the fence, substitution resumes for the %ALERT_CHANNEL% channel.
