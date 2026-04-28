---
name: azure-resources
description: Query and manage Azure resources using the ARM REST API via the azure_rest custom tool. Use when listing, filtering, or inspecting Azure resources, resource groups, deployments, or subscription details.
---

# Azure Resources

## Tools available

### azure_rest
A custom tool that makes authenticated requests to the Azure Resource Manager REST API. It uses the function app's managed identity — no tokens or credentials needed.

Parameters:
- `path` (required) — ARM REST API path relative to `https://management.azure.com`. Must include `api-version` as a query parameter.
- `method` (optional, default GET) — HTTP method.
- `body` (optional) — JSON request body for POST/PUT/PATCH.
- `query` (optional) — JMESPath expression to filter the response, similar to `az --query`. Use this to reduce response size and extract only the fields you need. Examples:
  - `value[].{name: name, type: type, location: location}` — extract specific fields from a resource list
  - `value[?type=='Microsoft.Web/sites'].name` — filter by resource type
  - `value | length(@)` — count results

### Microsoft Learn MCP server
Use the Microsoft Learn tools (`microsoft_docs_search`, `microsoft_docs_fetch`) to look up correct ARM REST API paths, api-versions, query parameters, and response schemas when you're unsure.

## Common API paths

### List all resources in a subscription
```
GET /subscriptions/{subscriptionId}/resources?api-version=2021-04-01
```
Response includes `createdTime`, `changedTime`, `name`, `type`, `location`, `resourceGroup` for each resource.

### List resource groups
```
GET /subscriptions/{subscriptionId}/resourcegroups?api-version=2021-04-01
```

### List resources in a specific resource group
```
GET /subscriptions/{subscriptionId}/resourceGroups/{resourceGroupName}/resources?api-version=2021-04-01
```

### Get a specific resource by ID
```
GET /subscriptions/{subscriptionId}/resourceGroups/{rg}/providers/{namespace}/{type}/{name}?api-version={version}
```
Note: api-version varies by resource type. Use Microsoft Learn to look up the correct version.

### List deployments
```
GET /subscriptions/{subscriptionId}/resourcegroups/{resourceGroupName}/providers/Microsoft.Resources/deployments?api-version=2021-04-01
```

## Tips
- The `api-version` query parameter is **required** for all ARM calls. If unsure of the version, search Microsoft Learn.
- To filter recently changed resources, list all resources and filter client-side by `createdTime` and `changedTime` timestamps (ISO 8601 format).
- Large subscriptions may return paginated results with a `nextLink` property. Follow it to get additional pages.
- RBAC on the managed identity controls what the tool can access. Reader role allows listing and reading all resources.
