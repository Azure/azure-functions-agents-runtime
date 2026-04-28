# Daily Tech News Email

A timer-triggered agent that fetches the day's top tech news headlines, summarizes them, and emails a digest using the Office 365 connector.

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| Timer | | ✅ Office 365 | | | ✅ | |

## Features

- **Timer trigger** — runs daily at 15:00 UTC
- **Code execution** — uses ACA Dynamic Sessions to fetch tech news from public RSS feeds and Hacker News
- **Office 365 connector** — sends the email via an Azure API Connection
- **Variable substitution** — recipient email address configured via `$TO_EMAIL` environment variable, resolved at load time in the agent instructions

## Prerequisites

- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- A [GitHub Personal Access Token](../../README.md#github-token) with **Copilot** scope
- An Azure subscription

## Deploy

1. **Set environment variables:**

   ```bash
   cd samples/daily-tech-news-email
   azd init
   azd env set GITHUB_TOKEN <your-github-pat>
   azd env set TO_EMAIL <recipient@example.com>
   ```

   Optional:

   ```bash
   azd env set COPILOT_MODEL claude-opus-4.6     # default
   ```

2. **Deploy to Azure:**

   ```bash
   azd up
   ```

   This provisions all resources (Function App, storage, ACA session pool, Office 365 API connection) and deploys the code.

3. **Authenticate the Office 365 connector:**

   After deployment, the Office 365 connection is created but **not yet authenticated**. You need to authorize it manually:

   - Go to the [Azure portal](https://portal.azure.com)
   - Navigate to the resource group created by `azd` (named `rg-<environment-name>`)
   - Find the **API Connection** resource (named `office365-...`)
   - Click **Edit API connection** in the left menu
   - Click **Authorize**, sign in with your Microsoft account, then click **Save**

4. **Verify:**

   The timer fires daily at 15:00 UTC. To test immediately, trigger the function with curl:

   ```bash
   # Get the master key
   az functionapp keys list -g <resource-group> -n <function-app-name> --query "masterKey" -o tsv

   # Trigger the function
   curl -X POST "https://<function-app-name>.azurewebsites.net/admin/functions/daily_tech_news_agent" \
     -H "x-functions-key: <master-key>" \
     -H "Content-Type: application/json" \
     -d '{}'
   ```

## Run Locally

Follow the [shared local development guide](../README.md#run-locally) in the samples directory. This sample requires additional setup for timers and email delivery.

### Local settings

Required:

- `GITHUB_TOKEN` (see shared guide)
- `ACA_SESSION_POOL_ENDPOINT`: needed for code execution (fetching news)
- `TO_EMAIL`: recipient email address
- `O365_CONNECTION_ID`: Office 365 connector ID

Without `ACA_SESSION_POOL_ENDPOINT`:

- The timer still fires, but the agent cannot fetch news (execute_python unavailable)
- Email sending may fail due to missing connector tools

Without `O365_CONNECTION_ID`:

- The agent cannot send email

### Testing locally

Since this is timer-triggered, you can manually invoke it:

**Bash:**

```bash
# In a new terminal, get the function host's endpoint
# Timer functions are triggered via HTTP admin endpoint
curl -X POST http://localhost:7071/admin/functions/daily_tech_news_agent \
  -H "Content-Type: application/json" \
  -d '{}'
```

**PowerShell:**

```powershell
Invoke-WebRequest -Uri "http://localhost:7071/admin/functions/daily_tech_news_agent" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{}'
```

## How It Works

- [`daily_tech_news.agent.md`](src/daily_tech_news.agent.md) defines the agent with a timer trigger, code execution sandbox, and Office 365 connector tools
- The `tools_from_connections` frontmatter references the Office 365 API Connection. At runtime, the framework discovers **all available actions** on the connector (send email, manage contacts, calendar operations, etc.) and exposes them as tools the agent can call. This agent uses the send email action, but any connector action is available without additional configuration.
- When the timer fires, the agent:
  1. Uses `execute_python` to fetch tech news from public RSS feeds and Hacker News
  2. Summarizes the top stories into an HTML email
  3. Calls the Office 365 send email tool to deliver the summary to the configured recipient
- The `$TO_EMAIL` variable in the agent instructions is replaced with the actual email address at load time (via environment variable substitution)
