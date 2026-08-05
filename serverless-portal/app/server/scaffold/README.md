# Basic Chat

An endpoint-first chat agent with a built-in web UI, streaming API, MCP tool, Microsoft Foundry, and Python code execution via ACA Dynamic Sessions.

| Trigger | Built-in Endpoints | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|---|
| | ✅ HTTP + MCP | | | | | ✅ | ✅ |

## Features

- **Chat UI** — built-in single-page interface at `/agents/main/`
- **HTTP API** — `POST /agents/main/chat` (JSON) and `POST /agents/main/chatstream` (SSE)
- **MCP tool** — exposed through `/runtime/webhooks/mcp` for connecting from VS Code, Claude Desktop, etc.
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

   Navigate to `https://<app-name>.azurewebsites.net/agents/main/`.

## Run Locally

Follow the [shared local development guide](../README.md#run-locally) in the samples directory. This sample has minimal additional requirements.

### Local settings

- `AZURE_FUNCTIONS_AGENTS_PROVIDER`: set to `foundry`
- `FOUNDRY_PROJECT_ENDPOINT`: required for local runs — your Microsoft Foundry project endpoint
- `FOUNDRY_MODEL`: required for local runs — model deployment name (for example, `gpt-5.4`)
- `ACA_SESSION_POOL_ENDPOINT`: optional; if empty, chat works but code execution (Python/Playwright) is unavailable

### Testing endpoints

Once `func start` is running:

- **Chat UI:** `http://localhost:7071/agents/main/`
- **Chat API:** `POST http://localhost:7071/agents/main/chat` with JSON body `{"prompt": "..."}`
- **Streaming API:** `POST http://localhost:7071/agents/main/chatstream` (Server-Sent Events)
- **MCP webhook:** `http://localhost:7071/runtime/webhooks/mcp` (for VS Code, Claude Desktop)

## How It Works

- [`main.agent.md`](src/main.agent.md) defines the endpoint-only agent with `builtin_endpoints: true` and code execution sandbox support
- The Bicep template creates a Microsoft Foundry project and `gpt-5.4` deployment for cloud runs
- The framework registers built-in HTTP chat endpoints, an MCP tool, and a built-in chat UI
- The agent can answer questions and run Python code in a secure sandbox when needed
