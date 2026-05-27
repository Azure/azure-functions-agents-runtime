# Daily Azure Report

A multi-agent Azure Functions app that monitors your Azure subscription. Includes a timer-triggered agent that emails a daily resource change report and an HTTP-triggered agent that returns a structured resource summary as JSON.

| Trigger | Built-in Endpoints | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|---|
| Timer + HTTP | ✅ main UI/API/MCP | ✅ azure_rest | ✅ Office 365 Outlook | ✅ MS Learn + Office 365 Outlook | ✅ azure-resources | | ✅ |

## Features

- **Timer trigger** — `daily_azure_report` runs daily at 15:00 UTC, emails a report of resources created or changed in the last 24 hours
- **HTTP trigger** — `resource_summary` at `POST /resource-summary` returns a structured JSON summary of all resources by type and location
- **Custom `azure_rest` tool** — makes authenticated ARM REST API calls using the function app's managed identity, with JMESPath query support
- **Office 365 Outlook connector** — provisions a v2 connection under a Connector Gateway and exposes the send-email operation through an MCP server
- **Microsoft Learn MCP server** — gives the agent access to Azure documentation for looking up correct API paths and versions
- **`azure-resources` skill** — packages ARM REST API knowledge (paths, api-versions, tips) so the agent instructions can focus on the job, not the technical details
- **Interactive assistant endpoints** — `main.agent.md` explicitly enables the built-in chat UI/API/MCP tool at `/agents/main/` for ad-hoc Azure queries
- **Variable substitution** — subscription ID and recipient email configured via environment variables. Substitution applies to all config string values (agent instructions, `agents.config.yaml`, `mcp.json`)

## Prerequisites

- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- An Azure subscription

## Deploy

1. **Set environment variables:**

   ```bash
   cd samples/daily-azure-report
   azd init
  azd env set AZURE_LOCATION eastus2
   azd env set TO_EMAIL <recipient@example.com>
   ```

  `AZURE_LOCATION` is restricted to regions that support Azure Functions Flex Consumption, Microsoft.Web Connector Gateways, and the sample's default Microsoft Foundry `gpt-5.4` Global Standard deployment: `centralus`, `eastus`, `eastus2`, `northcentralus`, `southcentralus`, and `westus`.

2. **Deploy to Azure:**

   ```bash
   azd up
   ```

    This provisions all resources (Function App, Microsoft Foundry, storage, Connector Gateway, Office 365 Outlook v2 connection, connection access policies for the Function App identity and deployer, and MCP server config) and deploys the code. The subscription ID is automatically detected from the deployment. The managed identity is granted Reader access on the subscription for querying resources.

  3. **Authenticate the Office 365 Outlook connection:**

    Open the deployed Office 365 Outlook connection in the Azure portal and complete authentication. The deployment output includes `O365_CONNECTION_ID`; after signing in, the connection status should be `Connected`:

    ```bash
    az resource show --ids "$(azd env get-value O365_CONNECTION_ID)" --query properties.overallStatus -o tsv
    ```

  4. **Verify:**

   The timer fires daily at 15:00 UTC. To test immediately, trigger the function with curl:

   ```bash
   # Get the master key
   az functionapp keys list -g <resource-group> -n <function-app-name> --query "masterKey" -o tsv

   # Trigger the function
   curl -X POST "https://<function-app-name>.azurewebsites.net/admin/functions/daily_azure_report" \
     -H "x-functions-key: <master-key>" \
     -H "Content-Type: application/json" \
     -d '{}'
   ```

## Run Locally

Follow the [shared local development guide](../README.md#run-locally) in the samples directory. This sample requires Azure credentials and email configuration.

### Local settings

Required:

- `FOUNDRY_PROJECT_ENDPOINT`: your Microsoft Foundry project endpoint
- `FOUNDRY_MODEL`: model deployment name (e.g. `gpt-5.4`)
- `SUBSCRIPTION_ID`: Azure subscription ID (for querying resources)
- `O365_MCP_SERVER_URL`: Office 365 Outlook MCP server URL
- `TO_EMAIL`: recipient email address

Optional:

- `AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT`: optional reasoning effort for supported Foundry reasoning models
- `AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY`: optional reasoning summary mode for supported Foundry reasoning models
- `O365_MCP_CLIENT_ID`: managed identity client ID for the Office 365 Outlook MCP server; defaults to the app-wide identity selection
- `ACA_SESSION_POOL_ENDPOINT`: if set, enables code execution features; if empty, agents work but lose advanced capabilities

If `O365_MCP_CLIENT_ID` is set, only the Office 365 Outlook MCP server uses that managed identity. If it is empty, the MCP server uses the app-wide identity selection: `AZURE_CLIENT_ID` when set, otherwise the system-assigned identity/default Azure credential chain.

Without `SUBSCRIPTION_ID`:

- The `azure_rest` tool cannot authenticate to query Azure resources
- Both timer and HTTP agents fail

Without `O365_MCP_SERVER_URL`:

- The timer agent cannot send the daily report email

### Testing locally

**Trigger the daily report manually:**

**Bash:**

```bash
curl -X POST http://localhost:7071/admin/functions/daily_azure_report \
  -H "Content-Type: application/json" \
  -d '{}'
```

**PowerShell:**

```powershell
Invoke-WebRequest -Uri "http://localhost:7071/admin/functions/daily_azure_report" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{}'
```

**Query resources via HTTP:**

**Bash:**

```bash
curl -X POST http://localhost:7071/resource-summary \
  -H "Content-Type: application/json" \
  -d '{}'
```

**PowerShell:**

```powershell
Invoke-WebRequest -Uri "http://localhost:7071/resource-summary" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{}'
```

## How It Works

### Agents

- [`daily_azure_report.agent.md`](src/daily_azure_report.agent.md) — timer-triggered agent that lists resources changed in the last 24 hours and emails a report
- [`resource_summary.agent.md`](src/resource_summary.agent.md) — HTTP-triggered agent at `POST /resource-summary` that returns a structured JSON summary of resources by type and location
- [`main.agent.md`](src/main.agent.md) — interactive chat agent for ad-hoc Azure queries via `/agents/main/`, `/agents/main/chat`, and the built-in MCP tool

### Shared capabilities

- [`tools/azure_rest.py`](src/tools/azure_rest.py) — custom tool for authenticated ARM REST API calls with JMESPath query filtering
- [`mcp.json`](src/mcp.json) — Microsoft Learn MCP server for Azure documentation lookups and the Office 365 Outlook MCP server provisioned by Bicep for sending email
- [`skills/azure-resources/SKILL.md`](src/skills/azure-resources/SKILL.md) — ARM REST API knowledge (paths, api-versions, tips)
- When the timer fires, the agent:
  1. Calls the `azure_rest` tool to list resources in the subscription
  2. Filters for resources created or modified in the last 24 hours
  3. Formats a summary report as an HTML email
  4. Sends the report to the configured recipient via the Office 365 Outlook MCP server
- The HTTP agent at `/resource-summary` accepts a JSON body with `subscription_id` and returns a structured summary:

  ```json
  {"total_resources": 239, "by_type": {...}, "by_location": {...}}
  ```

- `$SUBSCRIPTION_ID` and `$TO_EMAIL` in the agent instructions are replaced with actual values at load time. Inline `$VAR` and `%VAR%` substitution applies to all config string values
- `SUBSCRIPTION_ID` is automatically set from the deployment subscription — no manual input needed
