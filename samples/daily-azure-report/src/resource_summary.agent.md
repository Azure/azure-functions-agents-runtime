---
name: Resource Summary
description: Returns a structured summary of Azure resources.

trigger:
  type: http_trigger
  route: resource-summary
  methods: ["POST"]
  auth_level: FUNCTION

response_example: |
  {
    "total_resources": 42,
    "by_type": {
      "Microsoft.Web/sites": 5,
      "Microsoft.Storage/storageAccounts": 3
    },
    "by_location": {
      "eastus2": 20,
      "westus": 10
    }
  }
---

Given the subscription ID in the request body, use the azure_rest tool to list all resources and return a structured summary with counts by resource type and location.
