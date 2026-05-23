# Daily Tech News Email

A timer-triggered agent that fetches the day's top tech news headlines, summarizes them, and emails a digest using an Office 365 Outlook MCP server.

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| Timer | | | ✅ Office 365 Outlook | | ✅ | |

## Features

- **Timer trigger** — runs daily at 15:00 UTC
- **Code execution** — uses ACA Dynamic Sessions to fetch tech news from public RSS feeds and Hacker News
- **Office 365 Outlook MCP server** — sends the email using managed identity auth
- **Variable substitution** — recipient email address configured via `$TO_EMAIL` environment variable. Substitution applies to all config string values (agent instructions, `agents.config.yaml`, `mcp.json`)

## Prerequisites

- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- a Microsoft Foundry project with a model deployment (e.g. `gpt-5.4`)
- An Azure subscription

## Deploy

1. **Set environment variables:**

   ```bash
   cd samples/daily-tech-news-email
   azd init
    azd env set FOUNDRY_PROJECT_ENDPOINT <your-foundry-project-endpoint>
    azd env set FOUNDRY_MODEL gpt-5.4
    azd env set O365_MCP_SERVER_URL <your-office365-outlook-mcp-url>
   azd env set TO_EMAIL <recipient@example.com>
   ```

2. **Deploy to Azure:**

   ```bash
   azd up
   ```

    This provisions all resources (Function App, storage, and ACA session pool) and deploys the code.

3. **Verify:**

   The timer fires daily at 15:00 UTC. To test immediately, trigger the function with curl:

   ```bash
   # Get the master key
   az functionapp keys list -g <resource-group> -n <function-app-name> --query "masterKey" -o tsv

   # Trigger the function
   curl -X POST "https://<function-app-name>.azurewebsites.net/admin/functions/daily_tech_news" \
     -H "x-functions-key: <master-key>" \
     -H "Content-Type: application/json" \
     -d '{}'
   ```

## Run Locally

Follow the [shared local development guide](../README.md#run-locally) in the samples directory. This sample requires additional setup for timers and email delivery.

### Local settings

Required:

- `FOUNDRY_PROJECT_ENDPOINT`: your Microsoft Foundry project endpoint
- `FOUNDRY_MODEL`: model deployment name (e.g. `gpt-5.4`)
- `ACA_SESSION_POOL_ENDPOINT`: needed for code execution (fetching news)
- `O365_MCP_SERVER_URL`: Office 365 Outlook MCP server URL
- `TO_EMAIL`: recipient email address

Optional:

- `MAF_REASONING_EFFORT`: reasoning effort for supported Foundry reasoning models; defaults to `high`
- `MAF_REASONING_SUMMARY`: reasoning summary mode for supported Foundry reasoning models; defaults to `concise`
- `O365_MCP_CLIENT_ID`: managed identity client ID for the Office 365 Outlook MCP server; defaults to the app-wide identity selection

If `O365_MCP_CLIENT_ID` is set, only the Office 365 Outlook MCP server uses that managed identity. If it is empty, the MCP server uses the app-wide identity selection: `AZURE_CLIENT_ID` when set, otherwise the system-assigned identity/default Azure credential chain.

Without `ACA_SESSION_POOL_ENDPOINT`:

- The timer still fires, but the agent cannot fetch news (execute_python unavailable)
- Email sending may fail if the agent cannot build the news summary

Without `O365_MCP_SERVER_URL`:

- The agent cannot send email

### Testing locally

Since this is timer-triggered, you can manually invoke it:

**Bash:**

```bash
# In a new terminal, get the function host's endpoint
# Timer functions are triggered via HTTP admin endpoint
curl -X POST http://localhost:7071/admin/functions/daily_tech_news \
  -H "Content-Type: application/json" \
  -d '{}'
```

**PowerShell:**

```powershell
Invoke-WebRequest -Uri "http://localhost:7071/admin/functions/daily_tech_news" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{}'
```

## How It Works

- [`daily_tech_news.agent.md`](src/daily_tech_news.agent.md) defines the agent with a timer trigger, code execution sandbox, and Office 365 Outlook MCP email tool
- [`mcp.json`](src/mcp.json) configures the Office 365 Outlook MCP server and limits the exposed tool set to `office365_SendEmailV2`
- When the timer fires, the agent:
  1. Uses `execute_python` to fetch tech news from public RSS feeds and Hacker News
  2. Summarizes the top stories into an HTML email
  3. Calls the Office 365 Outlook MCP email tool to deliver the summary to the configured recipient
- The `$TO_EMAIL` variable in the agent instructions is replaced with the actual email address at load time. Inline `$VAR` and `%VAR%` substitution applies to all config string values
