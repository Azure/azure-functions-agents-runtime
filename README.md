# azurefunctions-agents-runtime (Preview)

> **Public preview.** The features described here are available for preview use and may change before general availability.

A markdown-first programming model for building AI agents on Azure Functions, powered by the [Microsoft Agent Framework (MAF)](https://github.com/microsoft/agent-framework).

- **Build agents with markdown** — write instructions, configure triggers, and bind tools in `.agent.md` files
- **Run on any Azure Functions trigger** — trigger agents on timer, queue, blob, HTTP, Event Hub, Service Bus, Cosmos DB, and more
- **Connect to 1,400+ services** — use connector-backed MCP servers to let agents act through Office 365, Teams, SQL, Salesforce, SAP, and hundreds of other connectors
- **Extend with MCP servers** — plug in remote HTTP MCP servers, including MCP servers backed by connectors
- **Build custom tools in plain Python** — drop a `.py` file in `tools/`, decorate functions with `@tool`, and pull in any package you need
- **Automatic HTTP and MCP endpoints** — optionally expose your agent as an HTTP chat API and MCP server with no extra code
- **Serverless with built-in session management** — scales to zero, persists multi-turn conversations in Azure Blob Storage
- **Pluggable model providers** — bring OpenAI, Azure OpenAI, or Microsoft Foundry credentials and the runtime auto-detects the right client

## Installation

The package is published on PyPI as **`azurefunctions-agents-runtime`**.

```bash
pip install azurefunctions-agents-runtime
```

Add it to your function app's `requirements.txt`:

```
azurefunctions-agents-runtime
```

## Model Provider Configuration

The runtime uses Microsoft Agent Framework, which supports OpenAI, Azure OpenAI, and Microsoft Foundry as inference back-ends. Auto-detection picks the first provider whose env vars are set, in this order:

1. `AZURE_OPENAI_ENDPOINT` → Azure OpenAI
2. `FOUNDRY_PROJECT_ENDPOINT` → Microsoft Foundry
3. `OPENAI_API_KEY` → OpenAI

You can pin the provider explicitly with `MAF_PROVIDER=openai|azure_openai|foundry`.

| Provider          | Required env vars                                                                            | Notes                                                                                                                                 |
| ----------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| OpenAI            | `OPENAI_API_KEY`, optional `MAF_MODEL` (default `gpt-4o-mini`)                               | `MAF_MODEL` applies directly for OpenAI.                                                                                              |
| Azure OpenAI      | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, optional `AZURE_OPENAI_API_VERSION`      | `AZURE_OPENAI_DEPLOYMENT` takes precedence over `MAF_MODEL`. If `AZURE_OPENAI_API_KEY` is omitted the SDK uses `DefaultAzureCredential` (AAD); set `AZURE_CLIENT_ID` in multi-identity Function Apps. |
| Microsoft Foundry | `FOUNDRY_PROJECT_ENDPOINT`, optional `FOUNDRY_MODEL`                                         | `FOUNDRY_MODEL` takes precedence over `MAF_MODEL`. Uses `DefaultAzureCredential`; set `AZURE_CLIENT_ID` in multi-identity Function Apps. |

Model resolution precedence is: explicit requested model > provider-specific env (`AZURE_OPENAI_DEPLOYMENT` for Azure OpenAI, `FOUNDRY_MODEL` for Foundry) > `MAF_MODEL` > provider default.

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

Connector-backed tools are exposed through MCP servers in `mcp.json`, and connector-triggered apps use the Azure Functions Connector Extension through the Functions extension bundle. No package extra is required.

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
- **Session persistence** — multi-turn conversations stored in Azure Blob Storage via the runtime's `BlobHistoryProvider`, reusing the function app's `AzureWebJobsStorage` account

Non-main agents can also opt into their own chat UI and HTTP debug endpoints with `debug.chat: true` (or `debug: true`), served at `/agents/{slug}/`, `/agents/{slug}/chat`, and `/agents/{slug}/chatstream`, where `{slug}` is derived from the `.agent.md` filename (not the display `name:` field). See [`docs/front-matter-spec.md#function-name-resolution`](docs/front-matter-spec.md#function-name-resolution).

### Event-driven agents (`<name>.agent.md`)

Define event-triggered agents with `.agent.md` files. Each file corresponds to a single Azure Function. Supported trigger types:

- **Event triggers** — timer, queue, blob, Event Hub, Service Bus, Cosmos DB, Teams, Office 365, etc.
- **HTTP triggers** — expose agents as REST API endpoints; add `response_example` or `response_schema` for validated JSON responses

### Shared capabilities
- **Markdown-first** — agent instructions, trigger config, and tool bindings in `.agent.md` files
- **Skills** — progressive-disclosure prompt modules under `skills/<name>/SKILL.md` (loaded on demand via MAF's `SkillsProvider`)
- **Custom tools** — drop a `.py` file in `tools/`, decorate functions with `@tool`, and they become callable
- **Connector-backed MCP tools** — call Office 365, Teams, SQL, Salesforce, SAP, and other connectors through HTTP MCP servers
- **MCP servers** — connect to external remote HTTP MCP servers for additional tools
- **Sandbox** — Python code execution via Azure Container Apps dynamic sessions; if no explicit sandbox session id is supplied, each invocation gets a fresh GUID-backed session

## Agent File Format (`.agent.md`)

Agent files use YAML frontmatter + markdown body:

```yaml
---
name: Agent Name
description: What this agent does

# Optional: system tools (code execution)
system_tools:
  execute_in_sessions:
    session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

# For triggered agents only (not `main.agent.md`):
trigger:
  type: timer_trigger      # or queue_trigger, connector_trigger, etc.
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
- **`<name>.agent.md`** — creates an event-triggered Azure Function. Exactly one trigger per file. With `debug.chat: true` (or `debug: true`), it also serves `/agents/{slug}/`, `/agents/{slug}/chat`, and `/agents/{slug}/chatstream`. The sanitized filename stem becomes the base Azure Function name. If two agent files sanitize to the same name (for example, `daily-report.agent.md` and `daily_report.agent.md`), the runtime auto-suffixes both the Azure Function name and the non-main debug slug (`_2`, `_3`, ...), keeping them paired in practice (`daily_report_2` ↔ `/agents/daily_report_2/`). The frontmatter `name:` field is display-only. See [`docs/front-matter-spec.md#function-name-resolution`](docs/front-matter-spec.md#function-name-resolution) and [`docs/front-matter-spec.md#debug`](docs/front-matter-spec.md#debug).

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
| `http_trigger` | Runtime HTTP adapter over `app.route(...)` | `http_trigger` |
| No dots | `app.<type>(...)` | `timer_trigger`, `queue_trigger` |
| `connector_trigger` | `app.connector_trigger(...)` | `connector_trigger` |

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

`docs/front-matter-spec.md#environment-variable-substitution` is the authoritative reference. In short, the runtime resolves `$VAR` and `%VAR%` placeholders inline in every string value in `agents.config.yaml`, `mcp.json`, agent frontmatter values, and the markdown body (outside fenced code blocks). Missing variables are left as literal placeholders.

#### Agent instructions (markdown body)

Variable references are resolved inline at load time anywhere string values are supported. Both `$VAR_NAME` and `%VAR_NAME%` syntaxes are supported, where the identifier must match `[A-Za-z_][A-Za-z0-9_]*`:

```markdown
---
name: Notifier
description: Sends updates to $TEAM_NAME
system_tools:
  execute_in_sessions:
    session_pool_management_endpoint: "https://$HOST/api"
---

Send a daily summary email to $TO_EMAIL.
Post a message to the %TEAM_NAME% team's General channel.
```

If `HOST=contoso.internal`, `TO_EMAIL=alice@example.com`, and `TEAM_NAME=Engineering` are set in the environment, those values resolve inline:

> `session_pool_management_endpoint: "https://contoso.internal/api"`
>
> Send a daily summary email to alice@example.com.
>
> Post a message to the Engineering team's General channel.

If a referenced variable is not set, the original `$VAR_NAME` or `%VAR_NAME%` text is left unchanged.

The runtime does **not** substitute dictionary keys, `${FOO}` brace syntax, identifiers starting with a digit such as `$9PORT`, or text inside fenced code blocks (`` ``` ``), so documentation examples in your instructions are preserved.

For the `$IDENT` syntax, identifiers that include characters outside `[A-Za-z0-9_]` (for example `$VAR-NAME`) are matched greedily up to the first invalid character — so `$VAR-NAME` resolves to `<value-of-VAR>-NAME` when `VAR` is set, and stays `$VAR-NAME` when `VAR` is unset. The `%IDENT%` syntax requires a closing `%` immediately after the identifier, so tokens like `%VAR-NAME%` remain fully literal. Quote or escape the surrounding text if you need a `$IDENT` token to remain literal.

To disable substitution for an agent's frontmatter values and markdown body, set `substitute_variables: false` in the frontmatter:

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

You can give your agent access to external MCP servers by creating an `mcp.json` file in the app root. Only remote HTTP MCP servers are supported. The `type` field is optional — when omitted, an entry with a `url` is treated as HTTP. When `type` is specified it must be `"http"` or `"streamable-http"`; any other transport (e.g. `stdio`, `sse`) is rejected with a warning.

String values in `mcp.json` support inline environment-variable substitution with both `$VAR` and `%VAR%`. Eligible fields include `url`, `headers` values, `type`, `tools` entries, and Azure identity auth values such as `auth.scope` and `auth.client_id`. Dictionary keys such as server names, environment-variable names, and header names are not substituted.

```json
{
  "servers": {
    "microsoft-learn": {
      "type": "http",
      "url": "https://$MCP_HOST/api",
      "headers": {
        "Authorization": "Bearer $LEARN_MCP_TOKEN"
      }
    },
    "custom-api": {
      "type": "streamable-http",
      "url": "https://example.com/mcp",
      "headers": {
        "Authorization": "Bearer $MCP_TOKEN"
      }
    },
    "office365-outlook": {
      "type": "http",
      "url": "$O365_MCP_SERVER_URL",
      "tools": ["office365_SendEmailV2"],
      "load_prompts": false,
      "auth": {
        "scope": "https://apihub.azure.com/.default",
        "client_id": "$O365_MCP_CLIENT_ID"
      }
    }
  }
}
```

Tools from configured MCP servers are automatically available to the agent at runtime. Each server entry supports:

- **`type`** — optional. When set, must be `"http"` or `"streamable-http"`. When omitted, an entry with a `url` is treated as HTTP.
- **`url`** — the MCP server endpoint URL (required)
- **`headers`** — optional HTTP headers (e.g. for authentication)
- **`tools`** — optional array of tool name patterns to allow (default: `["*"]`)
- **`load_tools`** — optional boolean controlling whether tools are loaded from the MCP server (default: `true`)
- **`load_prompts`** — optional boolean controlling whether prompts are loaded from the MCP server (default: `true`). Set this to `false` for MCP servers that do not implement `prompts/list`.
- **`auth`** — optional Azure Identity authentication configuration. Set `auth.scope` to the token scope required by the MCP server. The runtime uses `DefaultAzureCredential` to acquire the token.

By default, MCP auth follows the app-wide identity selection: `AZURE_CLIENT_ID` when set, otherwise the system-assigned identity/default Azure credential chain. To choose a user-assigned managed identity for a single MCP server without changing the app-wide identity, set `auth.client_id` in that server's `mcp.json` entry. If the configured client ID is empty or an unresolved placeholder, the runtime falls back to the app-wide identity selection.

> **Note**: Entries without a `url`, with unresolved placeholders in `url`, or with a `type` other than `"http"` / `"streamable-http"`, are ignored with a warning. Use the remote HTTP transport instead.

## Session storage

Multi-turn conversations are persisted as JSON Lines, one record per message:

- **Deployed apps (recommended).** When `AzureWebJobsStorage` is configured —
  as either a connection string or the identity-based
  `AzureWebJobsStorage__blobServiceUri` setting that `azd` provisions —
  history is written to **Azure Blob Storage** via the runtime's
  `BlobHistoryProvider`. One Append Blob per session is stored under
  `agent-sessions/{session_id}.jsonl` inside the
  `azure-functions-agents` container (override with
  `AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER`). No file share, no storage
  account key, no mount path; the same identity that the function app
  already uses for `AzureWebJobsStorage` reads and writes sessions. In
  multi-identity Function Apps, set `AZURE_CLIENT_ID` so
  `DefaultAzureCredential` selects the intended managed identity.
- **Local dev fallback.** When neither `AzureWebJobsStorage` nor
  `AzureWebJobsStorage__blobServiceUri` is set, history falls back to MAF's
  `FileHistoryProvider` writing to
  `{AZURE_FUNCTIONS_AGENTS_CONFIG_DIR}/agent-sessions/{session_id}.jsonl`,
  defaulting to `~/.azure-functions-agents/agent-sessions/`.

Session ids must match `^[A-Za-z0-9._-]{1,128}$` — anything else is rejected at the API boundary.

> **Single-process scope**: A per-session `asyncio.Lock` serializes concurrent turns within a single Function instance. The contract is "one active turn per session id". Multi-instance distributed locking is intentionally out of scope.

## Samples

See the [`samples/`](samples/) directory for complete, deployable example apps:

- [`basic-chat`](samples/basic-chat) — minimal chat agent with sandbox
- [`daily-azure-report`](samples/daily-azure-report) — timer-triggered agent that emails a daily Azure status report
- [`daily-tech-news-email`](samples/daily-tech-news-email) — timer-triggered agent that scrapes news and emails a digest
- [`outlook-reply-agent`](samples/outlook-reply-agent) — connector-triggered agent that drafts replies to incoming Office 365 Outlook email

## Deployment Notes

### Required Azure App Settings

Set the model provider env vars described above (e.g. `OPENAI_API_KEY` and `MAF_MODEL`, `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_DEPLOYMENT`, or `FOUNDRY_PROJECT_ENDPOINT` + `FOUNDRY_MODEL`). For Azure OpenAI and Microsoft Foundry, the provider-specific deployment/model setting takes precedence over `MAF_MODEL`.

When the agent uses connector-backed MCP servers, connector triggers, or `execution_sandbox`, the function app's **system-assigned or user-assigned Managed Identity** must be enabled and granted access to the target resource — otherwise `DefaultAzureCredential` will fail to obtain a token. In multi-identity Function Apps, set `AZURE_CLIENT_ID` so the runtime uses the intended managed identity for Azure OpenAI, Foundry, blob-backed session storage, ACA Dynamic Sessions, and ARM/data-plane connector calls. For an individual MCP server, set `auth.client_id` in `mcp.json` to choose a different managed identity just for that server.

### Optional config overrides

| Setting | Purpose |
|---|---|
| `AZURE_FUNCTIONS_AGENTS_APP_ROOT` | Override the app root used to discover `*.agent.md`, `tools/`, `skills/`, and `mcp.json` |
| `AZURE_FUNCTIONS_AGENTS_CONFIG_DIR` | Override the directory used for session storage |
| `AGENT_TIMEOUT` | Per-call timeout in seconds (default `900`) |
| `MAF_PROVIDER` | Pin the model provider (`openai`/`azure_openai`/`foundry`) and skip auto-detection |
| `MAF_REASONING_EFFORT` | Reasoning effort for supported reasoning models (default `high`; valid values include `none`, `low`, `medium`, `high`, `xhigh`) |
| `MAF_REASONING_SUMMARY` | Reasoning summary mode for supported reasoning models (default `concise`; valid values are `auto`, `concise`, `detailed`) |

## Development

```bash
# Clone the repo
git clone https://github.com/Azure/azure-functions-agents-runtime.git
cd azure-functions-agents-runtime

# Install in development mode
pip install -e .

# Build a wheel
pip install build
python -m build --wheel
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE.md](LICENSE.md).
