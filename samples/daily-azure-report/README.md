# Daily Azure Report

A multi-agent Azure Functions app that monitors your Azure subscription. Includes a timer-triggered agent that emails a daily resource change report and an HTTP-triggered agent that returns a structured resource summary as JSON.

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| Timer + HTTP | ✅ azure_rest | ✅ Office 365 | ✅ MS Learn | ✅ azure-resources | ✅ | ✅ |

## Features

- **Timer trigger** — `daily_azure_report` runs daily at 15:00 UTC, emails a report of resources created or changed in the last 24 hours
- **HTTP trigger** — `resource_summary` at `POST /resource-summary` returns a structured JSON summary of all resources by type and location
- **Custom `azure_rest` tool** — makes authenticated ARM REST API calls using the function app's managed identity, with JMESPath query support
- **Office 365 connector** — sends the report via email
- **Microsoft Learn MCP server** — gives the agent access to Azure documentation for looking up correct API paths and versions
- **`azure-resources` skill** — packages ARM REST API knowledge (paths, api-versions, tips) so the agent instructions can focus on the job, not the technical details
- **Interactive chat UI** — `main.agent.md` enables the built-in chat interface for ad-hoc Azure queries
- **Sandbox** — `system_tools.execute_in_sessions` in [`src/agents.config.yaml`](src/agents.config.yaml) enables ACA Dynamic Sessions for agents that need code execution
- **Variable substitution** — recipient email and the timer/chat subscription ID can come from environment variables. The `resource_summary` HTTP agent instead reads `subscription_id` from the request body. Substitution applies to all config string values (agent instructions, `agents.config.yaml`, `mcp.json`)

## Prerequisites

- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- an Azure OpenAI resource with a model deployment (e.g. `gpt-5.2`)
- An Azure subscription

## Deploy

1. **Set environment variables:**

   ```bash
   cd samples/daily-azure-report
   azd init
   azd env set AZURE_OPENAI_ENDPOINT <your-azure-openai-endpoint>
   azd env set AZURE_OPENAI_DEPLOYMENT gpt-5.2
   azd env set TO_EMAIL <recipient@example.com>
   ```

2. **Deploy to Azure:**

   ```bash
   azd up
   ```

   This provisions all resources (Function App, storage, Office 365 API connection) and deploys the code. The sample's `prepackage` hook builds the local runtime wheel and refreshes `src\requirements.txt` before zip packaging. The subscription ID is automatically detected from the deployment. The managed identity is granted Reader access on the subscription for querying resources.

3. **Authenticate the Office 365 connector:**

   After deployment, the Office 365 connection is created but **not yet authenticated**. You need to authorize it manually:

   - Go to the [Azure portal](https://portal.azure.com)
   - Navigate to the resource group created by `azd` (named `rg-<environment-name>`)
   - Find the **API Connection** resource (`office365-...`)
   - Click **Edit API connection** in the left menu
   - Click **Authorize**, sign in with your Microsoft account, then click **Save**

   The `azure_rest` custom tool uses the function app's managed identity — no manual authentication needed.

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

- `AZURE_OPENAI_ENDPOINT`: your Azure OpenAI resource endpoint
- `AZURE_OPENAI_DEPLOYMENT`: model deployment name (e.g. `gpt-5.2`)
- `TO_EMAIL`: recipient email address
- `O365_CONNECTION_ID`: Office 365 connector ID

Optional:

- `SUBSCRIPTION_ID`: used by the timer agent and the `main.agent.md` chat agent; the `resource_summary` HTTP agent takes `subscription_id` from the request body instead
- `ACA_SESSION_POOL_ENDPOINT`: if set, enables code execution features; if empty, agents work but lose advanced capabilities

Without `SUBSCRIPTION_ID`:

- The timer agent and the `main.agent.md` chat agent do not know which subscription to query
- The `resource_summary` HTTP agent can still work if each request body supplies `subscription_id`

Without `O365_CONNECTION_ID`:

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
  -d '{"subscription_id":"<subscription-id>"}'
```

**PowerShell:**

```powershell
Invoke-WebRequest -Uri "http://localhost:7071/resource-summary" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"subscription_id":"<subscription-id>"}'
```

## How It Works

### Agents

- [`daily_azure_report.agent.md`](src/daily_azure_report.agent.md) — timer-triggered agent that lists resources changed in the last 24 hours and emails a report
- [`resource_summary.agent.md`](src/resource_summary.agent.md) — HTTP-triggered agent at `POST /resource-summary` that returns a structured JSON summary of resources by type and location
- [`main.agent.md`](src/main.agent.md) — interactive chat agent for ad-hoc Azure queries via the built-in UI

### Shared capabilities

- [`tools/azure_rest.py`](src/tools/azure_rest.py) — custom tool for authenticated ARM REST API calls with JMESPath query filtering
- [`mcp.json`](src/mcp.json) — Microsoft Learn MCP server for Azure documentation lookups
- [`skills/azure-resources/SKILL.md`](src/skills/azure-resources/SKILL.md) — ARM REST API knowledge (paths, api-versions, tips)
- [`src/agents.config.yaml`](src/agents.config.yaml) wires up the Office 365 connector through global `system_tools.tools_from_connections`
- [`src/agents.config.yaml`](src/agents.config.yaml) also enables ACA Dynamic Sessions through global `system_tools.execute_in_sessions`
- When the timer fires, the agent:
  1. Calls the `azure_rest` tool to list resources in the subscription
  2. Filters for resources created or modified in the last 24 hours
  3. Formats a summary report as an HTML email
  4. Sends the report to the configured recipient via the Office 365 connector
- The HTTP agent at `/resource-summary` accepts a JSON body with `subscription_id` and returns a structured summary:

  ```json
  {"total_resources": 239, "by_type": {...}, "by_location": {...}}
  ```

- `$SUBSCRIPTION_ID` and `$TO_EMAIL` in agent instructions are replaced with actual values at load time when those variables are present. Inline `$VAR` and `%VAR%` substitution applies to all config string values
- The deployment infrastructure populates `SUBSCRIPTION_ID` for the timer and chat agents, while the HTTP `resource_summary` endpoint expects callers to send `subscription_id` in the request body
