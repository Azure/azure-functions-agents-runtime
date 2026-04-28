# azure-functions-agents (Experimental)

> **⚠️ This is an experimental package.** The APIs described here are under active development and subject to change.

A markdown-first programming model for building AI agents on Azure Functions with the [GitHub Copilot SDK](https://github.com/github/copilot-sdk).

- **Build agents with markdown** — write instructions, configure triggers, and bind tools in  `.agent.md` files
- **Run on any Azure Functions trigger** — trigger agents on timer, queue, blob, HTTP, Event Hub, Service Bus, Cosmos DB, and more
- **Connect to 1,400+ services** — Azure API Connections let agents trigger on and perform actions across Office 365, Teams, SQL, Salesforce, SAP, and hundreds of other connectors — no custom code required
- **Extend with MCP servers** — plug in remote HTTP MCP servers for additional capabilities
- **Build custom tools in plain Python** — when connectors and MCP aren't enough, drop a `.py` file in `tools/` and pull in any package you need
- **Automatic HTTP and MCP endpoints** — optionally expose your agent as an HTTP chat API and MCP server with no extra code
- **Serverless with built-in session management** — scales to zero, persists multi-turn conversations across instances on Azure Files

## Installation

### From a GitHub release (`.whl`)

Install directly from the release URL:

```bash
pip install https://github.com/anthonychu/azure-functions-agents/releases/download/v0.7.1/azure_functions_agents-0.7.1-py3-none-any.whl
```

### From the GitHub repo

```bash
pip install azure-functions-agents @ git+https://github.com/anthonychu/azure-functions-agents.git
```

### With connector tools support

Connector tools (Teams, Office 365, SQL, Salesforce, etc.) require an optional extra:

```bash
# From release URL
pip install "azure-functions-agents[connectors] @ https://github.com/anthonychu/azure-functions-agents/releases/download/v0.7.1/azure_functions_agents-0.7.1-py3-none-any.whl"

# From repo
pip install "azure-functions-agents[connectors] @ git+https://github.com/anthonychu/azure-functions-agents.git"
```

## GitHub Token

The Copilot SDK requires a GitHub Personal Access Token (PAT) to authenticate with the GitHub Copilot API.

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens?type=beta) and click **Generate new token** (fine-grained)
2. Give the token a name (e.g. `azure-functions-agents`)
3. Under **Permissions**, select **Add permissions**, then select **Copilot requests**, and set it to **Read-only**
4. Click **Generate token** and copy the value

Set it as the `GITHUB_TOKEN` environment variable (or pass it via `local.settings.json` / `azd env set` when deploying).

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

> The app root is auto-detected from `AzureWebJobsScriptRoot` (set by `func start` and the Azure Functions host). You can override it with `create_function_app(app_root=Path(__file__).parent)` or the `COPILOT_APP_ROOT` env var.

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
https://github.com/anthonychu/azure-functions-agents/releases/download/v0.7.1/azure_functions_agents-0.7.1-py3-none-any.whl
```

Or use any other install method from the [Installation](#installation) section.

### 5. Start Azurite (local storage emulator)

The MCP server endpoint and non-HTTP triggers (timer, queue, blob, etc.) require a storage account. Locally, use [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) via Docker:

```bash
docker run -d --name azurite -p 10000:10000 -p 10001:10001 -p 10002:10002 \
  mcr.microsoft.com/azure-storage/azurite \
  azurite --skipApiVersionCheck --blobHost 0.0.0.0 --queueHost 0.0.0.0 --tableHost 0.0.0.0
```

Then set the storage connection string in `local.settings.json`:

```json
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AzureWebJobsStorage": "UseDevelopmentStorage=true"
  }
}
```

### 6. Run locally

```bash
func start
```

Your agent is now running at `http://localhost:7071/` with a built-in chat UI, HTTP API (`/agent/chat`, `/agent/chatstream`), and MCP server (`/runtime/webhooks/mcp`).

## Features

### `main.agent.md`

Define an agent with a markdown file. When `main.agent.md` is present, the runtime automatically registers:

- **Chat UI** — built-in single-page web interface at the app root
- **HTTP APIs** — `POST /agent/chat` (JSON) and `POST /agent/chatstream` (SSE)
- **MCP server** — `/runtime/webhooks/mcp` for VS Code, Claude Desktop, etc.
- **Session persistence** — multi-turn conversations stored on Azure Files

### Event-driven agents (`<name>.agent.md`)

Define event-triggered agents with `.agent.md` files. Each file corresponds to a single Azure Function. Supported trigger types:

- **Event triggers** — timer, queue, blob, Event Hub, Service Bus, Cosmos DB, Teams, Office 365, etc.
- **HTTP triggers** — expose agents as REST API endpoints with structured JSON responses via `response_example`

### Shared capabilities
- **Markdown-first** — agent instructions, trigger config, and tool bindings in `.agent.md` files
- **Skills** — reusable prompt modules from `SKILL.md` files
- **Custom tools** — drop a `.py` file in `tools/` and it becomes a callable tool
- **Connector tools** — dynamically generated tools from Azure API Connections
- **MCP servers** — connect to external MCP servers for additional tools
- **Sandbox** — code execution via Azure Container Apps dynamic sessions with Playwright web browsing support

## Agent File Format (`.agent.md`)

Agent files use YAML frontmatter + markdown body:

```yaml
---
name: Agent Name
description: What this agent does

# Optional: connector tools
tools_from_connections:
  - connection_id: $SQL_CONNECTION_ID
    prefix: sales_db      # optional

# Optional: code interpreter
execution_sandbox:
  session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

# For triggered agents only (not `main.agent.md`):
trigger:
  type: timer_trigger      # or queue_trigger, teams.new_channel_message_trigger, etc.
  schedule: "0 0 9 * * *"  # trigger-specific params passed as kwargs

logger: true               # optional, default true
substitute_variables: true # optional, default true — inline $VAR / %VAR% replacement in body

# For HTTP-triggered agents: expected response format
response_example: |        # optional — agent returns structured JSON matching this example
  {
    "summary": "A brief summary",
    "keywords": ["keyword1", "keyword2"]
  }
---

Agent instructions in markdown...
```

### Multiple functions from markdown

- **`main.agent.md`** — creates HTTP chat, MCP, and UI endpoints. No other triggers are supported in this file.
- **`<name>.agent.md`** — creates an event-triggered Azure Function. Exactly one trigger per file. The filename (minus `.agent.md`) becomes the function name.

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

#### Frontmatter values

String values in `trigger.*` (except `type`), `tools_from_connections[].connection_id`, and `execution_sandbox.session_pool_management_endpoint` support `$VAR` or `%VAR%` syntax (full-string match only).

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

To disable substitution for an agent, set `substitute_variables: false` in the frontmatter:

```yaml
---
name: My Agent
substitute_variables: false
---

Instructions with literal $VAR references that should not be replaced.
```

## What `main.agent.md` Enables

When a `main.agent.md` file exists in your app root, the runtime automatically registers:

### Chat UI

A built-in single-page chat interface served at the app root (`/`). No frontend code needed — just open `http://localhost:7071/` locally or `https://<your-app>.azurewebsites.net/` when deployed.

On first load, you'll be prompted for the base URL and a function key (for deployed apps). These are stored in browser local storage and can be changed via the gear icon.

### HTTP Chat API

Two POST endpoints for programmatic access:

- **`POST /agent/chat`** — JSON request/response. Returns `session_id`, `response`, and `tool_calls`.
- **`POST /agent/chatstream`** — streaming Server-Sent Events (SSE). Returns incremental text chunks, tool execution events, and a final message.

Pass `x-ms-session-id` header to continue a conversation across requests. If omitted, a new session is created automatically.

### MCP Server

An MCP-compatible endpoint at `/runtime/webhooks/mcp` that any MCP client (VS Code, Claude Desktop, etc.) can connect to. Requires the MCP extension system key in the `x-functions-key` header when deployed.

### Without `main.agent.md`

If there's no `main.agent.md`, the HTTP chat, MCP, and UI endpoints are all disabled. The app only runs triggered functions.

## MCP Server Configuration

You can give your agent access to external MCP servers by creating an `mcp.json` file in the app root. Only **HTTP remote servers** are supported.

```json
{
  "servers": {
    "microsoft-learn": {
      "type": "http",
      "url": "https://learn.microsoft.com/api/mcp"
    }
  }
}
```

Tools from configured MCP servers are automatically available to the agent at runtime. Each server entry supports:

- **`type`** — `"http"` (required)
- **`url`** — the MCP server endpoint URL
- **`headers`** — optional HTTP headers (e.g. for authentication)
- **`tools`** — optional array of tool name patterns to allow (default: `["*"]`)

## Samples

See the [`samples/`](samples/) directory for complete, deployable example apps.

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
# Output: dist/azure_functions_agents-0.7.1-py3-none-any.whl
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE.md](LICENSE.md).
