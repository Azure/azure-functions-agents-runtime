---
name: Structured Reporter
description: HTTP agent that validates its request and response with JSON Schema.
trigger:
  type: http_trigger
  args:
    route: "structured-report"
    methods: ["POST"]
    auth_level: anonymous
input_schema:
  type: object
  required:
    - subscription_id
    - report_type
  properties:
    subscription_id:
      type: string
      description: Azure subscription identifier.
    report_type:
      type: string
      enum:
        - cost
        - security
        - inventory
response_schema:
  type: object
  required:
    - status
    - summary
  properties:
    status:
      type: string
      enum:
        - ok
        - error
    summary:
      type: string
    findings:
      type: array
      items:
        type: object
        properties:
          severity:
            type: string
          message:
            type: string
response_example: |
  {
    "status": "ok",
    "summary": "All resources nominal.",
    "findings": []
  }
metadata:
  owner: platform-team
  tags:
    - reporting
    - azure
  cost_center: 4242
  enabled: true
---

You produce a structured report for the requested subscription and report type.
Always respond with JSON that matches the response schema: a `status` of `ok` or
`error`, a `summary` string, and a `findings` array.
