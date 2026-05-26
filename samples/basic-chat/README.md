# Basic Chat

An HTTP chat agent with a built-in web UI, streaming API, MCP server endpoint, Microsoft Foundry, and Python code execution via ACA Dynamic Sessions.

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| HTTP | | | | | ✅ | ✅ |

## Features

- **Chat UI** — built-in single-page interface at the app root
- **HTTP API** — `POST /agent/chat` (JSON) and `POST /agent/chatstream` (SSE)
- **MCP server** — `/runtime/webhooks/mcp` for connecting from VS Code, Claude Desktop, etc.
- **Microsoft Foundry** — provisions an AI Services account, Foundry project, and `gpt-5.4` deployment
- **Code execution** — sandboxed Python via ACA Dynamic Sessions with Playwright support
- **Session persistence** — multi-turn conversations stored in Azure Blob Storage

## Prerequisites

- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- An Azure subscription

## Deploy

1. **Set environment variables:**

   ```bash
   cd samples/basic-chat
   azd init
   azd env set AZURE_LOCATION eastus2
   ```

   `AZURE_LOCATION` is restricted to regions that support both Azure Functions Flex Consumption and the sample's default Microsoft Foundry `gpt-5.4` Global Standard deployment: `brazilsouth`, `canadacentral`, `canadaeast`, `centralus`, `eastus`, `eastus2`, `northcentralus`, `southcentralus`, `westus`, and `westus3`.

2. **Deploy to Azure:**

   ```bash
   azd up
   ```

3. **Open the chat UI:**

   Navigate to the Function App URL shown in the deployment output (`https://<app-name>.azurewebsites.net/`).

## Run Locally

Follow the [shared local development guide](../README.md#run-locally) in the samples directory. This sample has minimal additional requirements.

### Local settings

- `MAF_PROVIDER`: set to `foundry`
- `FOUNDRY_PROJECT_ENDPOINT`: required for local runs — your Microsoft Foundry project endpoint
- `FOUNDRY_MODEL`: required for local runs — model deployment name (for example, `gpt-5.4`)
- `ACA_SESSION_POOL_ENDPOINT`: optional; if empty, chat works but code execution (Python/Playwright) is unavailable

### Testing endpoints

Once `func start` is running:

- **Chat UI:** `http://localhost:7071/`
- **Chat API:** `POST http://localhost:7071/agent/chat` with JSON body `{"prompt": "..."}`
- **Streaming API:** `POST http://localhost:7071/agent/chatstream` (Server-Sent Events)
- **MCP webhook:** `http://localhost:7071/runtime/webhooks/mcp` (for VS Code, Claude Desktop)

## How It Works

- [`main.agent.md`](src/main.agent.md) defines the agent with code execution sandbox support
- The Bicep template creates a Microsoft Foundry project and `gpt-5.4` deployment for cloud runs
- The framework registers HTTP chat endpoints, an MCP server, and a built-in chat UI
- The agent can answer questions and run Python code in a secure sandbox when needed
