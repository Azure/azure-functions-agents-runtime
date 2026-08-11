# Getting started

## Model provider configuration

The runtime uses Microsoft Agent Framework, which supports Microsoft Foundry, Azure OpenAI, and OpenAI as inference back-ends. The public preview quickstart and samples use **Microsoft Foundry** as the primary path, pinned with `AZURE_FUNCTIONS_AGENTS_PROVIDER=foundry`.

| Provider | `AZURE_FUNCTIONS_AGENTS_PROVIDER` | Required env vars | Notes |
| --- | --- | --- | --- |
| Microsoft Foundry | `foundry` | `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL` | Recommended quickstart/sample path. Uses `DefaultAzureCredential`; run `az login` locally and set `AZURE_CLIENT_ID` in multi-identity Function Apps. |
| Azure OpenAI | `azure_openai` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, optional `AZURE_OPENAI_API_VERSION` | Alternative Azure-hosted provider. `AZURE_OPENAI_DEPLOYMENT` takes precedence over `AZURE_FUNCTIONS_AGENTS_MODEL`. If `AZURE_OPENAI_API_KEY` is omitted the SDK uses `DefaultAzureCredential` (AAD). |
| OpenAI | `openai` | `OPENAI_API_KEY`, optional `AZURE_FUNCTIONS_AGENTS_MODEL` (default `gpt-4o-mini`) | Alternative non-Azure provider. `AZURE_FUNCTIONS_AGENTS_MODEL` applies directly for OpenAI. |

If `AZURE_FUNCTIONS_AGENTS_PROVIDER` is unset, auto-detection picks the first provider whose env vars are set, in this order: `AZURE_OPENAI_ENDPOINT` → `FOUNDRY_PROJECT_ENDPOINT` → `OPENAI_API_KEY`. Set `AZURE_FUNCTIONS_AGENTS_PROVIDER` to make the provider choice intentional.

Model resolution precedence is: explicit requested model > provider-specific env (`FOUNDRY_MODEL` for Foundry, `AZURE_OPENAI_DEPLOYMENT` for Azure OpenAI) > `AZURE_FUNCTIONS_AGENTS_MODEL` > provider default.

## Quick start

### 1. Create the agent file

Create `main.agent.md`:

```markdown
---
name: My Agent
description: A helpful assistant

builtin_endpoints: true
---

You are a helpful assistant. Answer questions concisely.
```

### 2. Create the function app entry point

Create `function_app.py`:

```python
from azure_functions_agents import create_function_app

app = create_function_app()
```

> The app root is auto-detected from `AzureWebJobsScriptRoot` (set by `func start` and the Azure Functions host). You can override it with `create_function_app(app_root=Path(__file__).parent)` or the `AZURE_FUNCTIONS_AGENTS_APP_ROOT` env var.

### 3. Create `agents.config.yaml`

```yaml
# Default runtime configuration
model: $FOUNDRY_MODEL
timeout: 900
```

### 4. Create `host.json`

```json
{
  "version": "2.0",
  "extensions": {
    "http": {
      "routePrefix": ""
    }
  },
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.*, 5.0.0)"
  }
}
```

### 5. Create `requirements.txt`

```
azurefunctions-agents-runtime
```

Connector-backed tools are exposed through MCP servers in `mcp.json`, and connector-triggered apps use the Azure Functions Connector Extension through the Functions extension bundle. No package extra is required.

### 6. Set the model provider

For local development with Microsoft Foundry, sign in with `az login`, then create `local.settings.json`:

```json
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "AZURE_FUNCTIONS_AGENTS_PROVIDER": "foundry",
    "FOUNDRY_PROJECT_ENDPOINT": "https://<project-name>.<region>.services.ai.azure.com/api/projects/<project-name>",
    "FOUNDRY_MODEL": "gpt-5.4"
  }
}
```

### 7. Start Azurite (local storage emulator)

The MCP server endpoint and non-HTTP triggers (timer, queue, blob, etc.) require a storage account. Locally, use [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) via Docker:

```bash
docker run -d --name azurite -p 10000:10000 -p 10001:10001 -p 10002:10002 \
  mcr.microsoft.com/azure-storage/azurite \
  azurite --skipApiVersionCheck --blobHost 0.0.0.0 --queueHost 0.0.0.0 --tableHost 0.0.0.0
```

### 8. Run locally

```bash
func start
```

Your agent is now running at `http://localhost:7071/agents/main/` with a built-in chat UI, HTTP API (`/agents/main/chat`, `/agents/main/chatstream`), and MCP tool exposed through the Functions MCP endpoint (`/runtime/webhooks/mcp`).

## Where to go next

- [Front matter spec](front-matter-spec.md) — full `.agent.md` field reference, triggers, built-in endpoints, subagents, and environment variable substitution
- [Triggers](triggers.md) — supported trigger types and payload shapes
- [Architecture](architecture.md) — how the runtime discovers, translates, and registers agents
- The repository [README](https://github.com/Azure/azure-functions-agents-runtime#readme) also covers custom Python tools, built-in endpoint routes, and multi-agent delegation in more depth
