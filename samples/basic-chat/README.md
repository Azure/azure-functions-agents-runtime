# Basic Chat

An HTTP chat agent with a built-in web UI, streaming API, MCP server endpoint, and Python code execution via ACA Dynamic Sessions.

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| HTTP | | | | | ✅ | ✅ |

## Features

- **Chat UI** — built-in single-page interface at the app root
- **HTTP API** — `POST /agent/chat` (JSON) and `POST /agent/chatstream` (SSE)
- **MCP server** — `/runtime/webhooks/mcp` for connecting from VS Code, Claude Desktop, etc.
- **Code execution** — sandboxed Python via ACA Dynamic Sessions with Playwright support
- **Session persistence** — multi-turn conversations stored on Azure Blob Storage

## Prerequisites

- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- an Azure OpenAI resource with a model deployment (e.g. `gpt-5.2`)
- An Azure subscription

## Deploy

1. **Set environment variables:**

   ```bash
   cd samples/basic-chat
   azd init
   azd env set AZURE_OPENAI_ENDPOINT <your-azure-openai-endpoint>
   azd env set AZURE_OPENAI_DEPLOYMENT gpt-5.2
   ```

2. **Deploy to Azure:**

   ```bash
   azd up
   ```

   The sample's `prepackage` hook builds the local runtime wheel and refreshes `src\requirements.txt` before zip packaging.

3. **Open the chat UI:**

   Navigate to the Function App URL shown in the deployment output (`https://<app-name>.azurewebsites.net/`).

## Run Locally

Follow the [shared local development guide](../README.md#run-locally) in the samples directory. This sample has minimal additional requirements.

### Local settings

- `AZURE_OPENAI_ENDPOINT`: required — your Azure OpenAI resource endpoint
- `AZURE_OPENAI_DEPLOYMENT`: required — model deployment name (e.g. `gpt-5.2`)
- `ACA_SESSION_POOL_ENDPOINT`: optional; if empty, chat works but code execution (Python/Playwright) is unavailable

### Testing endpoints

Once `func start` is running:

- **Chat UI:** `http://localhost:7071/`
- **Chat API:** `POST http://localhost:7071/agent/chat` with JSON body `{"prompt": "..."}`
- **Streaming API:** `POST http://localhost:7071/agent/chatstream` (Server-Sent Events)
- **MCP webhook:** `http://localhost:7071/runtime/webhooks/mcp` (for VS Code, Claude Desktop)

## How It Works

- [`src/agents.config.yaml`](src/agents.config.yaml) enables the ACA Dynamic Sessions sandbox for the app, and [`main.agent.md`](src/main.agent.md) defines the chat agent
- The framework registers HTTP chat endpoints, an MCP server, and a built-in chat UI
- The agent can answer questions and run Python code in a secure sandbox when needed
