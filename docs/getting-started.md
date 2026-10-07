# Getting started

## Model provider configuration

The runtime uses Microsoft Agent Framework by default, which supports Microsoft Foundry, Azure OpenAI, and OpenAI as inference back-ends. The public preview quickstart and samples use **Microsoft Foundry** as the primary path, pinned with `AZURE_FUNCTIONS_AGENTS_PROVIDER=foundry`.

| Provider | `AZURE_FUNCTIONS_AGENTS_PROVIDER` | Required env vars | Notes |
| --- | --- | --- | --- |
| Microsoft Foundry | `foundry` | `FOUNDRY_PROJECT_ENDPOINT`; optional `FOUNDRY_MODEL` | Recommended quickstart/sample path. Uses `DefaultAzureCredential`; run `az login` locally and set `AZURE_CLIENT_ID` in multi-identity Function Apps. If no model is configured, the runtime uses `gpt-4o-mini`. |
| Azure OpenAI | `azure_openai` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, optional `AZURE_OPENAI_API_VERSION` | Alternative Azure-hosted provider. `AZURE_OPENAI_DEPLOYMENT` takes precedence over `AZURE_FUNCTIONS_AGENTS_MODEL`. If `AZURE_OPENAI_API_KEY` is omitted the SDK uses `DefaultAzureCredential` (AAD). |
| OpenAI | `openai` | `OPENAI_API_KEY`, optional `AZURE_FUNCTIONS_AGENTS_MODEL` (default `gpt-4o-mini`) | Alternative non-Azure provider. `AZURE_FUNCTIONS_AGENTS_MODEL` applies directly for OpenAI. |

If `AZURE_FUNCTIONS_AGENTS_PROVIDER` is unset, auto-detection picks the first provider whose env vars are set, in this order: `AZURE_OPENAI_ENDPOINT` → `FOUNDRY_PROJECT_ENDPOINT` → `OPENAI_API_KEY`. Set `AZURE_FUNCTIONS_AGENTS_PROVIDER` to make the provider choice intentional.

Model resolution precedence is: explicit requested model > provider-specific env (`FOUNDRY_MODEL` for Foundry, `AZURE_OPENAI_DEPLOYMENT` for Azure OpenAI) > `AZURE_FUNCTIONS_AGENTS_MODEL` > runtime fallback (`gpt-4o-mini`).

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
azurefunctions-agents-runtime[monitor]
```

Connector-backed tools are exposed through MCP servers in `mcp.json`, and connector-triggered apps use the Azure Functions Connector Extension through the Functions extension bundle. No package extra is required for connectors.

> Use `azurefunctions-agents-runtime` (without the extra) instead if you don't want to export traces to Azure Monitor / Application Insights.

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

The existing `AzureWebJobsStorage` setting selects Blob-backed session storage,
even on a local host. `AzureWebJobsStorage__blobServiceUri` is the Entra-based
alternative. Local session files are used only when neither is configured;
configured Blob failures surface rather than falling back to local disk.

### 8. Run locally

```bash
func start
```

Your agent is now running at `http://localhost:7071/agents/main/` with a built-in chat UI, HTTP API (`/agents/main/chat`, `/agents/main/chatstream`), and MCP tool exposed through the Functions MCP endpoint (`/runtime/webhooks/mcp`).

## Call an agent from your Function

When your application owns the route or trigger, omit the agent's `trigger`
and `builtin_endpoints`, then inject it by filename-derived slug:

```python
from azurefunctions.extensions.http.fastapi import Request, Response
from azure_functions_agents import HostedSkill, create_function_app

app = create_function_app()


@app.route(route="summarize", methods=["POST"])
@app.hosted_skill(arg_name="skill", agent_name="summarizer")
async def summarize(req: Request, skill: HostedSkill) -> Response:
  result = await skill.run((await req.body()).decode("utf-8"))
  return Response(result.content, media_type="text/plain")
```

Use `skill.run()` for one result or `skill.stream()` for structured events.
See the [hybrid HostedSkill sample](../samples/hybrid-hosted-skill/).

## Where to go next

- [Front matter spec](front-matter-spec.md) — full `.agent.md` field reference, triggers, built-in endpoints, subagents, and environment variable substitution, with narrative examples
- [Front matter reference](front-matter-reference.md) — auto-generated, plain field-by-field reference (handy for quick lookups)
- [Triggers](triggers.md) — supported trigger types and payload shapes
- [Architecture](architecture.md) — how the runtime discovers, translates, and registers agents
- [Copilot preview operations](copilot-preview-operations.md) — separate default-off, local-only preview; this quickstart uses MAF and its existing history provider
- The repository [README](https://github.com/Azure/azure-functions-agents-runtime#readme) also covers custom Python tools, built-in endpoint routes, and multi-agent delegation in more depth
