---
name: No Fetch
description: HTTP agent that opts out of the default-on web_request tool.
trigger:
  type: http_trigger
  args:
    route: "no-fetch"
    methods: ["POST"]
    auth_level: anonymous
system_tools:
  web_request: false
---

Answer using only your own knowledge, in one short sentence.
