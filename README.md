# azure-functions-agents (Experimental)

> **⚠️ This is an experimental package.** The APIs described here are under active development and subject to change.

A markdown-first programming model for building AI agents on Azure Functions, powered by the [Microsoft Agent Framework (MAF)](https://github.com/microsoft/agent-framework).

- **Build agents with markdown** — write instructions, configure triggers, and bind tools in `.agent.md` files
- **Run on any Azure Functions trigger** — trigger agents on timer, queue, blob, HTTP, Event Hub, Service Bus, Cosmos DB, and more
- **Connect to 1,400+ services** — Azure API Connections let agents trigger on and perform actions across Office 365, Teams, SQL, Salesforce, SAP, and hundreds of other connectors — no custom code required
- **Extend with MCP servers** — plug in remote HTTP MCP servers and stdio MCP servers for additional capabilities
- **Build custom tools in plain Python** — drop a `.py` file in `tools/`, decorate functions with `@tool`, and pull in any package you need
- **Automatic HTTP and MCP endpoints** — optionally expose your agent as an HTTP chat API and MCP server with no extra code
- **Serverless with built-in session management** — scales to zero, persists multi-turn conversations on Azure Files
- **Pluggable model providers** — bring OpenAI, Azure OpenAI, or Azure AI Foundry credentials and the runtime auto-detects the right client

## Installation

The package is published on PyPI as **`azurefunctions-agents-runtime`**.

```bash
pip install azurefunctions-agents-runtime
```

Add it to your function app's `requirements.txt`:

```
azurefunctions-agents-runtime
```

### With connector tools support

Connector tools (Teams, Office 365, SQL, Salesforce, etc.) require an optional extra:

```bash
pip install "azurefunctions-agents-runtime[connectors]"
```


## Model Provider Configuration

The runtime uses Microsoft Agent Framework, which supports OpenAI, Azure OpenAI, and Azure AI Foundry as inference back-ends. Auto-detection picks the first provider whose env vars are set, in this order:

1. `AZURE_OPENAI_ENDPOINT` → Azure OpenAI
2. `FOUNDRY_PROJECT_ENDPOINT` → Azure AI Foundry
3. `OPENAI_API_KEY` → OpenAI

You can pin the provider explicitly with `MAF_PROVIDER=openai|azure_openai|foundry`.

| Provider          | Required env vars                                                                            | Notes                                                                              |
| ----------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| OpenAI            | `OPENAI_API_KEY`, optional `MAF_MODEL` (default `gpt-4o-mini`)                               |                                                                                    |
| Azure OpenAI      | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, optional `AZURE_OPENAI_API_VERSION`      | If `AZURE_OPENAI_API_KEY` is omitted the SDK uses `DefaultAzureCredential` (AAD).  |
| Azure AI Foundry  | `FOUNDRY_PROJECT_ENDPOINT`, optional `FOUNDRY_MODEL`                                         | Always uses `DefaultAzureCredential`.                                              |

`MAF_MODEL` overrides the per-provider default when set.

### Plugging in a custom client manager

Provider auto-detection lives behind a small interface. To integrate a new chat client, implement `ClientManager` and register your instance once at startup:

```python
from azure_functions_agents import ClientManager, set_client_manager

class MyCustomClientManager(ClientManager):
    def resolve_model(self, model_override=None):
        return model_override or "my-model"
    def build_chat_client(self, model_override=None):
        return MyChatClient(model=self.resolve_model(model_override))
    async def close(self):
        ...

set_client_manager(MyCustomClientManager())
```

Once set, every call to `run_agent` / `run_agent_stream` and every triggered agent uses your client.

## Quick Start

### 1. Create the agent file

Create `main.agent.md`:

```markdown
---
name: My Agent
description: A helpful assistant
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

### 3. Create `host.json`

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

### 4. Create `requirements.txt`

```
azurefunctions-agents-runtime
```

Or use `azurefunctions-agents-runtime[connectors]` to enable the optional connector tools extra.

### 5. Set the model provider

For local development with OpenAI:

```json
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "OPENAI_API_KEY": "sk-...",
    "MAF_MODEL": "gpt-4o-mini"
  }
}
```

### 6. Start Azurite (local storage emulator)

The MCP server endpoint and non-HTTP triggers (timer, queue, blob, etc.) require a storage account. Locally, use [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) via Docker:

```bash
docker run -d --name azurite -p 10000:10000 -p 10001:10001 -p 10002:10002 \
  mcr.microsoft.com/azure-storage/azurite \
  azurite --skipApiVersionCheck --blobHost 0.0.0.0 --queueHost 0.0.0.0 --tableHost 0.0.0.0
```

### 7. Run locally

```bash
func start
```

Your agent is now running at `http://localhost:7071/` with a built-in chat UI, HTTP API (`/agent/chat`, `/agent/chatstream`), and MCP server (`/runtime/webhooks/mcp`).

## Features

**Architecture overview:** see [`docs/architecture.md`](docs/architecture.md) for the module map and data flow pipeline.

### `main.agent.md`

Define an agent with a markdown file. When `main.agent.md` is present, the runtime automatically registers:

- **Chat UI** — built-in single-page web interface at the app root
- **HTTP APIs** — `POST /agent/chat` (JSON) and `POST /agent/chatstream` (SSE)
- **MCP server** — `/runtime/webhooks/mcp` for VS Code, Claude Desktop, etc.
- **Session persistence** — multi-turn conversations stored on Azure Files via MAF's `FileHistoryProvider`

Non-main agents can also opt into their own chat UI and HTTP debug endpoints with `debug.chat: true` (or `debug: true`), served at `/agents/{slug}/`, `/agents/{slug}/chat`, and `/agents/{slug}/chatstream`, where `{slug}` is derived from the `.agent.md` filename (not the display `name:` field). See [`docs/front-matter-spec.md#function-name-resolution`](docs/front-matter-spec.md#function-name-resolution).

### Event-driven agents (`<name>.agent.md`)

Define event-triggered agents with `.agent.md` files. Each file corresponds to a single Azure Function. Supported trigger types:

- **Event triggers** — timer, queue, blob, Event Hub, Service Bus, Cosmos DB, Teams, Office 365, etc.
- **HTTP triggers** — expose agents as REST API endpoints with structured JSON responses via `response_example`

### Shared capabilities
- **Markdown-first** — agent instructions, trigger config, and tool bindings in `.agent.md` files
- **Skills** — reusable prompt modules from `*.md` files under `skills/`
- **Custom tools** — drop a `.py` file in `tools/`, decorate functions with `@tool`, and they become callable
- **Connector tools** — dynamically generated tools from Azure API Connections
- **MCP servers** — connect to external MCP servers (HTTP or stdio) for additional tools
- **Sandbox** — Python code execution via Azure Container Apps dynamic sessions

## Agent File Format (`.agent.md`)

Agent files use YAML frontmatter + markdown body:

```yaml
---
name: Agent Name
description: What this agent does

# Optional: system tools (connectors and code execution)
system_tools:
  tools_from_connections:
    - connection_id: $SQL_CONNECTION_ID
      prefix: sales_db      # optional
  execute_in_sessions:
    session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

# For triggered agents only (not `main.agent.md`):
trigger:
  type: timer_trigger      # or queue_trigger, teams.new_channel_message_trigger, etc.
  args:
    schedule: "0 0 9 * * *"  # trigger-specific params passed as kwargs

logger: true               # optional, default true
substitute_variables: true # optional, default true — env-var replacement in frontmatter + body

# For HTTP-triggered agents: expected response format
response_example: |        # optional — agent returns structured JSON matching this example
  {
    "summary": "A brief summary",
    "keywords": ["keyword1", "keyword2"]
  }
---

Agent instructions in markdown...
```

> **Note**: Earlier preview releases supported a `runtime: copilot|maf` frontmatter field. As of 1.0.0 only Microsoft Agent Framework is used and the field is ignored (with a one-time warning per agent file). Remove it from your `.agent.md` files.

### Multiple functions from markdown

- **`main.agent.md`** — creates HTTP chat, MCP, and UI endpoints. No other triggers are supported in this file.
- **`<name>.agent.md`** — creates an event-triggered Azure Function. Exactly one trigger per file. With `debug.chat: true` (or `debug: true`), it also serves `/agents/{slug}/`, `/agents/{slug}/chat`, and `/agents/{slug}/chatstream`. The sanitized filename stem becomes the base Azure Function name. If two agent files sanitize to the same name (for example, `daily-report.agent.md` and `daily_report.agent.md`), the runtime auto-suffixes the Azure Function name (`_2`, `_3`, ...) so both register cleanly. The non-main debug slug (used for `/agents/{slug}/` routes) is **not** auto-suffixed, so slug collisions still require renaming one of the files. The frontmatter `name:` field is display-only. See [`docs/front-matter-spec.md#function-name-resolution`](docs/front-matter-spec.md#function-name-resolution) and [`docs/front-matter-spec.md#debug`](docs/front-matter-spec.md#debug).

When a triggered function runs, the agent's markdown body is used as the system instructions. The prompt sent to the agent includes the trigger type and the serialized binding data:

```
Triggered by: service_bus_queue_trigger

Trigger data:
```json
{"body": "...", "message_id": "...", ...}
```​
```

This applies to all trigger types, including timers (whose data includes fields like `past_due`).

For a complete reference of all supported triggers and their parameters, see [docs/triggers.md](docs/triggers.md).

### Trigger type resolution

| Format | Resolves to | Example |
|---|---|---|
| `http_trigger` | `app.route(...)` with structured JSON response | `http_trigger` |
| No dots | `app.<type>(...)` | `timer_trigger`, `queue_trigger` |
| Dots | Connector library method | `teams.new_channel_message_trigger` |
| `connectors.` prefix | Explicit connector method | `connectors.generic_trigger` |

### HTTP-triggered agents

HTTP-triggered agents expose REST API endpoints that accept JSON input and return structured JSON output. Use `response_example` in the frontmatter to define the expected response format:

```yaml
---
name: Summarize
trigger:
  type: http_trigger
  args:
    route: summarize
    methods: ["POST"]
    auth_level: FUNCTION     # ANONYMOUS | FUNCTION | ADMIN (default: FUNCTION)
response_example: |
  {
    "summary": "A brief summary of the content",
    "keywords": ["keyword1", "keyword2"],
    "sentiment": "positive"
  }
---

Analyze the provided content and return a structured summary.
```

The agent receives the HTTP request body as input and is instructed to return JSON matching the example. If `response_example` is omitted, the raw agent text is returned as `text/plain`.

`response_schema` (JSON Schema) is also supported as an alternative to `response_example` for advanced use cases.

### Environment variable substitution

`docs/front-matter-spec.md#environment-variable-substitution` is the authoritative reference. In short, the runtime resolves `$VAR` and `%VAR%` placeholders in every string value in `agents.config.yaml`, every string value in agent frontmatter, and inline in the markdown body (outside fenced code blocks). Missing variables are left as literal placeholders.

#### Agent instructions (markdown body)

Variable references in the agent's markdown body are replaced **inline** with environment variable values at load time. Both `$VAR_NAME` and `%VAR_NAME%` syntaxes are supported:

```markdown
---
name: Notifier
---

Send a daily summary email to $TO_EMAIL.
Post a message to the %TEAM_NAME% team's General channel.
```

If `TO_EMAIL=alice@example.com` and `TEAM_NAME=Engineering` are set in the environment, the agent instructions become:

> Send a daily summary email to alice@example.com.
> Post a message to the Engineering team's General channel.

If a referenced variable is not set, the original `$VAR_NAME` or `%VAR_NAME%` text is left unchanged.

Text inside fenced code blocks (`` ``` ``) is **not** substituted, so documentation examples in your instructions are preserved.

To disable both frontmatter and body substitution for an agent, set `substitute_variables: false` in the frontmatter:

```yaml
---
name: My Agent
substitute_variables: false
---

Instructions with literal $VAR references that should not be replaced.
```

> **Note**: `substitute_variables` itself is read before env-var substitution. It must be a literal boolean (`true` or `false`). Setting `substitute_variables: $MY_FLAG` will not be resolved and defaults to `true`.

## Custom Python tools

Drop a `.py` file in `tools/` and decorate functions with `@tool`. The runtime auto-discovers them at import time and adds them to every agent.

```python
# tools/my_tools.py
from azure_functions_agents import tool

@tool
def reverse_string(text: str) -> str:
    """Reverse the input string."""
    return text[::-1]
```

`@tool` is re-exported from `agent_framework`. Functions can be sync or async; types in the signature feed MAF's automatic JSON-Schema generation. Tools that need richer schemas can be declared with `agent_framework.FunctionTool` directly.

## What `main.agent.md` Enables

When a `main.agent.md` file exists in your app root, the runtime automatically registers:

### Chat UI

A built-in single-page chat interface served at `/` for the main agent, and at `/agents/{slug}/` for any non-main agent with `debug.chat: true` (or `debug: true`). For non-main agents, `{slug}` comes from the `.agent.md` filename after sanitization, not from the display `name:` field. No frontend code needed — just open `http://localhost:7071/` locally or `https://<your-app>.azurewebsites.net/` when deployed. See [`docs/front-matter-spec.md#function-name-resolution`](docs/front-matter-spec.md#function-name-resolution).

On first load, you'll be prompted for the base URL and a function key (for deployed apps). These are stored in browser local storage and can be changed via the gear icon.

### HTTP Chat API

POST endpoints for programmatic access:

- **Main agent:** `POST /agent/chat` and `POST /agent/chatstream`
- **Non-main agent with `debug.chat: true`:** `POST /agents/{slug}/chat` and `POST /agents/{slug}/chatstream` (`{slug}` comes from the `.agent.md` filename after sanitization)

The JSON endpoint returns `session_id`, `response`, and `tool_calls`. The streaming endpoint uses Server-Sent Events (SSE) with `session`, `delta`, `intermediate`, `tool_start`, `tool_end`, `done`, and `error` events.

Pass `x-ms-session-id` header to continue a conversation across requests. If omitted, a new session is created automatically.

### MCP Server

An MCP-compatible endpoint at `/runtime/webhooks/mcp` that any MCP client (VS Code, Claude Desktop, etc.) can connect to. Requires the MCP extension system key in the `x-functions-key` header when deployed.

### Without `main.agent.md`

If there's no `main.agent.md`, the root (`/`) chat UI, `/agent/*` chat APIs, and `/runtime/webhooks/mcp` endpoint are disabled. The app still runs triggered functions, and non-main agents can still opt into per-agent chat surfaces with `debug.chat: true` (or `debug: true`). See [`docs/front-matter-spec.md#debug`](docs/front-matter-spec.md#debug).

## MCP Server Configuration

You can give your agent access to external MCP servers by creating an `mcp.json` file in the app root. Both **HTTP (Streamable)** remote servers and **stdio** servers are supported.

```json
{
  "servers": {
    "microsoft-learn": {
      "type": "http",
      "url": "https://learn.microsoft.com/api/mcp"
    },
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
    }
  }
}
```

Tools from configured MCP servers are automatically available to the agent at runtime. Each server entry supports:

- **`type`** — `"http"` (Streamable HTTP transport) or `"stdio"`
- **`url`** — the MCP server endpoint URL (HTTP only)
- **`command`** + **`args`** + **`env`** — process spec (stdio only)
- **`headers`** — optional HTTP headers (e.g. for authentication; HTTP only)
- **`tools`** — optional array of tool name patterns to allow (default: `["*"]`)

> **Note**: SSE-transport MCP servers (`type: "sse"`) are no longer supported. Use the Streamable HTTP transport (`type: "http"`) instead.

## Session storage

Multi-turn conversations are persisted as JSONL files using MAF's `FileHistoryProvider`. Storage path resolution:

- When `CONTAINER_NAME` is set (Functions container) → `/code-assistant-session/agent-sessions/{session_id}.jsonl`
- Otherwise: `{AZURE_FUNCTIONS_AGENTS_CONFIG_DIR}/agent-sessions/{session_id}.jsonl`, defaulting to `~/.azure-functions-agents/agent-sessions/`

Session ids must match `^[A-Za-z0-9._-]{1,128}$` — anything else is rejected at the API boundary.

> **Single-process scope**: A per-session `asyncio.Lock` serializes concurrent turns within a single Function instance. The contract is "one active turn per session id". Multi-instance distributed locking is intentionally out of scope.

## Samples

See the [`samples/`](samples/) directory for complete, deployable example apps:

- [`basic-chat`](samples/basic-chat) — minimal chat agent with sandbox
- [`daily-azure-report`](samples/daily-azure-report) — timer-triggered agent that emails a daily Azure status report
- [`daily-tech-news-email`](samples/daily-tech-news-email) — timer-triggered agent that scrapes news and emails a digest

## Deployment Notes

### Required Azure App Settings

Set the model provider env vars described above (e.g. `OPENAI_API_KEY` and `MAF_MODEL`, or `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_DEPLOYMENT`).

When the agent uses connector tools or `execution_sandbox`, the function app's **system-assigned or user-assigned Managed Identity** must be enabled and granted access to the AI Gateway / Logic App connector resource — otherwise `DefaultAzureCredential` will fail to obtain an ARM token at startup.

### Optional config overrides

| Setting | Purpose |
|---|---|
| `AZURE_FUNCTIONS_AGENTS_APP_ROOT` | Override the app root used to discover `*.agent.md`, `tools/`, `skills/`, and `mcp.json` |
| `AZURE_FUNCTIONS_AGENTS_CONFIG_DIR` | Override the directory used for session storage (legacy alias `CODE_ASSISTANT_CONFIG_PATH` still accepted) |
| `AGENT_TIMEOUT` | Per-call timeout in seconds (default `900`) |
| `MAF_PROVIDER` | Pin the model provider (`openai`/`azure_openai`/`foundry`) and skip auto-detection |

## Development

```bash
# Clone the repo
git clone https://github.com/anthonychu/azure-functions-agents.git
cd azure-functions-agents

# Install in development mode
pip install -e ".[connectors]"

# Build a wheel
pip install build
python -m build --wheel
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE.md](LICENSE.md).
