# Azure Functions Agents - Configuration Specification

## Overview

Azure Functions agents use a **two-tier configuration system**:

1. **Global Configuration** (`agents.app.yaml`) — Infrastructure and capabilities available to all agents
2. **Agent-Specific Configuration** (`.agent.md` front matter) — Agent behavior, triggers, and capability filtering

Each agent is defined in a `.agent.md` file with YAML front matter followed by markdown instructions. The front matter configures the agent-specific behavior, while the markdown body contains the agent's system prompt.

### Configuration Model

**Global configuration defines infrastructure:**
- MCP servers (defined in `mcp.json`, referenced in `agents.app.yaml`)
- Skills (auto-discovered from `skills/` directory)
- Custom tools (auto-discovered from `tools/` directory)
- Code execution sandbox configuration
- Connector tools
- Default runtime settings (model, timeout)

**Agent front matter:**
- **Inherits all auto-discovered capabilities by default**
- Can apply **allow/deny lists** to filter which MCP servers, skills, or tools are available
- Can **override** runtime settings (model, timeout)
- Must define **trigger** (how the agent is invoked)
- Can enable **HTTP/MCP endpoints** for testing and composition

### Configuration Precedence

For runtime settings (model, timeout):
1. **Agent front matter** — Explicit overrides in `.agent.md` files
2. **Global configuration** — Values in `agents.app.yaml`
3. **Environment variables** — App settings and env vars
4. **Framework defaults** — Built-in default values

For capabilities (MCP, skills, tools):
1. **Auto-discovered and referenced** — MCP servers referenced in `agents.app.yaml` (defined in `mcp.json`), skills and tools auto-discovered from their directories
2. **Filtered per-agent** using allow/deny lists in agent front matter

---

## Configuration Files

### Global Configuration (`agents.app.yaml`)
Optional file in `src/` directory that defines infrastructure and capabilities available to all agents.

**What goes in global configuration:**
- MCP server references (servers defined in `mcp.json`)
- Code execution sandbox endpoints
- Connector tool configurations
- Default runtime settings (model, timeout)

**Note:** Skills (from `skills/` directory) and custom tools (from `tools/` directory) are automatically discovered and do not need to be listed in global configuration. Agents can filter them using allow/deny lists.

**Key principle:** Global config defines **what's available**. Agents filter **what they use**.

### Agent Configuration (`.agent.md` front matter)
YAML front matter at the top of each agent file.

**What goes in agent front matter:**
- `name` and `description` (required)
- `trigger` — How the agent is invoked (required, except for `main.agent.md`)
- `enable-http` / `enable-mcp` — Enable testing/composition endpoints
- **Allow/deny lists** for `mcp`, `skills`, `tools` (filters global capabilities)
- **Runtime overrides** for `model`, `timeout`
- **HTTP-specific** fields like `input_schema`, `response_schema`, `response_example`

**Special file: `main.agent.md`**
- Optional primary agent with HTTP chat UI and MCP tool enabled by default
- No `trigger` required (uses HTTP endpoints automatically)
- See [File Naming Conventions](#file-naming-conventions) for details

**File structure:**
```
src/
  agents.app.yaml          # Optional: Global defaults
  main.agent.md            # Optional: Main agent (HTTP + MCP enabled)
  another_agent.agent.md   # Other agents (require trigger)
  ...
```

---

## Field Reference

Fields are organized into categories based on how they can be used:

### Field Categories

**Infrastructure (Global only, filtered in agents):**
- `mcp` — MCP server references (global) or allow/deny lists (agent)
- `skills` — Auto-discovered from `skills/` directory, allow/deny lists (agent only)
- `tools` — Auto-discovered from `tools/` directory, allow/deny lists (agent only)
- `execution_sandbox` — Sandbox configuration (global), opt-out (agent)
- `tools_from_connections` — Connector tools (global only)

**Runtime Settings (Global defaults, overridable in agents):**
- `model` — LLM selection
- `timeout` — Execution time limit

**Agent-Specific (Agent front matter only):**
- `name`, `description` — Agent identity (required)
- `trigger` — Invocation method (required for non-main agents)
- `enable-http`, `enable-mcp` — Testing/composition endpoints
- `input_schema`, `response_schema`, `response_example` — HTTP validation
- `metadata` — Organizational metadata

---

### Required Fields (Agent Front Matter Only)

#### `name`
- **Type:** `string`
- **Typical location:** Agent only (required)
- **Description:** Display name for the agent
- **Example:** `"Daily Azure Report"`

#### `description`
- **Type:** `string`
- **Typical location:** Agent only (required)
- **Description:** Brief description of the agent's purpose (used for agent selection, logging, and documentation)
- **Example:** `"Lists resources created or changed in the last 24 hours and emails a report"`

---

### Optional Fields

#### `trigger`
- **Type:** `object`
- **Typical location:** Agent only
- **Can override:** N/A (agent-specific only)
- **Description:** Defines how the agent is invoked. If omitted, defaults to HTTP trigger with default settings.
- **Structure:** `type` field specifies the trigger type, `args` contains type-specific configuration
- **Important:** Only **one trigger per agent file** is allowed

#### **HTTP Trigger** (default)
```yaml
trigger:
  type: http_trigger
  args:
    route: string          # Optional. Custom route path. Defaults to function name
    methods: string[]      # Optional. Array of HTTP methods. Defaults to ["GET", "POST"]
    auth_level: string     # Optional. One of: anonymous, function, admin. Defaults to function
```

**Example:**
```yaml
trigger:
  type: http_trigger
  args:
    route: "resource-summary"
    methods: ["POST"]
    auth_level: function
```

#### **Timer Trigger**
```yaml
trigger:
  type: timer_trigger
  args:
    schedule: string       # Required. CRON expression (6-field format: second minute hour day month day-of-week)
```

#### **Queue Trigger**
```yaml
trigger:
  type: queue_trigger
  args:
    name: string           # Required. Queue name
    connection: string     # Optional. App setting name for connection string. Defaults to AzureWebJobsStorage
```

#### **Blob Trigger**
```yaml
trigger:
  type: blob_trigger
  args:
    path: string           # Required. Blob path pattern (e.g., "uploads/{name}.txt")
    connection: string     # Optional. App setting name for connection string. Defaults to AzureWebJobsStorage
```

#### **Event Grid Trigger**
```yaml
trigger:
  type: event_grid_trigger
```

#### **Service Bus Trigger**
```yaml
trigger:
  type: service_bus_trigger
  args:
    queue_name: string           # Required if using queue. Queue name
    topic_name: string           # Required if using topic. Topic name
    subscription_name: string    # Required if using topic. Subscription name
    connection: string           # Optional. App setting name for connection string
```

---

#### `enable-http`
- **Type:** `boolean`
- **Typical location:** Agent only
- **Can override:** N/A (agent-specific only)
- **Default:** `false`
- **Description:** Enable HTTP chat endpoints for the agent. When enabled, creates interactive chat endpoints for testing and usage.
- **Applies to:** All agents (especially useful for non-HTTP triggered agents)

**Endpoints created when enabled:**
- `POST /agent/chat` — Non-streaming chat endpoint
- `POST /agent/chatstream` — Server-sent events streaming chat endpoint
- `GET /` — Chat UI page

**Example:**
```yaml
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"
enable-http: true  # Enable HTTP chat endpoints for manual testing
```

**Use case:** Test timer or queue-triggered agents via HTTP during development without waiting for the schedule or adding messages to queues. Also useful for providing a chat interface to any agent.

---

#### `enable-mcp`
- **Type:** `boolean`
- **Typical location:** Agent only
- **Can override:** N/A (agent-specific only)
- **Default:** `false`
- **Description:** Enable MCP (Model Context Protocol) tool registration for the agent. When enabled, the agent is exposed as an MCP tool that can be called by other agents or MCP clients.
- **Applies to:** All agents

**MCP tool created when enabled:**
- Tool name: Derived from agent `name` field (e.g., "Daily Azure Report" → `daily_azure_report`)
- Tool description: From agent `description` field
- Tool trigger type: `mcpToolTrigger`
- Input: `{"prompt": "string"}` — The prompt to send to the agent
- Output: JSON with `session_id`, `response`, `response_intermediate`, `tool_calls`

**Example:**
```yaml
name: Daily Azure Report
description: Lists resources created or changed in the last 24 hours and emails a report
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"
enable-mcp: true  # Expose as MCP tool
```

**Use case:** Allow other agents to invoke this agent as a tool, enabling agent-to-agent communication and composition.

**MCP tool registration example:**
```
daily_azure_report: mcpToolTrigger
```

---

#### `model`
- **Type:** `string`
- **Location:** Global (`agents.app.yaml`) for default, Agent (front matter) for override
- **Can override:** Yes
- **Description:** Specifies which LLM to use for the agent. Valid model identifiers include `claude-sonnet-4`, `gpt-4o`, `gpt-4o-mini`, `o1`, `o1-mini`.
- **Precedence:** Agent front matter → Global `agents.app.yaml` → `COPILOT_MODEL` env var → `"claude-sonnet-4"` (default)

**Global default:**
```yaml
model: gpt-4o
```

**Agent override:**
```yaml
model: gpt-4o-mini  # Use faster model for this agent
```

**Note:** Model parameters (temperature, max_tokens, etc.) are configured globally via environment variables or SDK configuration, not in the front matter.

---

#### `timeout`
- **Type:** `number`
- **Location:** Global (`agents.app.yaml`) for default, Agent (front matter) for override
- **Can override:** Yes
- **Description:** Maximum execution time in seconds for the agent.
- **Precedence:** Agent front matter → Global `agents.app.yaml` → `COPILOT_AGENT_TIMEOUT` env var → `900` seconds (default)

**Global default:**
```yaml
timeout: 900  # 15 minutes
```

**Agent override:**
```yaml
timeout: 60  # 1 minute for fast agent
```

---

#### `execution_sandbox`
- **Type:** `object` (global), `boolean` (agent)
- **Location:** Global (`agents.app.yaml`) for configuration, Agent (front matter) for opt-out
- **Can override:** No, but agents can opt out by setting to `false`
- **Description:** Configures Python code execution environment using Azure Container Apps dynamic sessions. Defined globally in `agents.app.yaml`. All agents inherit sandbox access by default. Agents can opt out by setting `execution_sandbox: false`.

**Global configuration (in `agents.app.yaml`):**
```yaml
execution_sandbox:
  session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT
```

**Agent opt-out (in agent front matter):**
```yaml
---
name: Simple Agent
description: An agent that doesn't need code execution

execution_sandbox: false  # Opt out of code execution capabilities
---
```

**Note:** Future versions may support multiple sandbox types with allow/deny lists similar to MCP servers, skills, and tools.

---

#### `tools_from_connections`
- **Type:** `array`
- **Location:** Global only (`agents.app.yaml`)
- **Can override:** No (global infrastructure only)
- **Description:** Loads connector-based tools (e.g., Office 365, Outlook, SharePoint) from Azure Logic App connectors as dynamic tools. All agents inherit these tools.
- **Status:** ⚠️ Under review — May be deprecated in favor of MCP-based connector integration

**Example:**
```yaml
tools_from_connections:
  - connection_id: $O365_CONNECTION_ID
  - connection_id: $OUTLOOK_CONNECTION_ID
```

**Note:** This field enables dynamic tool generation from connector APIs. An alternative approach is to use connectors via their MCP (Model Context Protocol) servers through the `mcp` field, which provides better standardization and discoverability. The future direction between these two approaches is under consideration.

**MCP alternative:**
```yaml
mcp:
  - office365-connector  # Use connector via MCP instead
```

---

#### `tools`
- **Type:** `object`
- **Location:** Global (`agents.app.yaml`) for configuration, Agent (front matter) for filtering
- **Description:** Controls which tools are available. All tools from `tools/` directory and built-in tools are auto-discovered. Use global config to set defaults, agent config to apply allow/deny lists.

**Global configuration (optional) - Set defaults:**
```yaml
tools:
  exclude: ["bash", "execute_shell"]  # Exclude dangerous tools by default
```

**Agent filtering - Override with allow/deny lists:**
```yaml
# Include only specific tools
tools:
  include: ["azure_rest", "send_email"]

# Exclude specific tools (in addition to global excludes)
tools:
  exclude: ["web_fetch"]

# Only custom tools (no built-ins)
tools:
  custom_only: true
```

**Note:** Agents inherit all globally available tools by default. Use `include`/`exclude` to filter.

---

#### `mcp`
- **Type:** `array` or `object`
- **Location:** Global (`agents.app.yaml`) for references, Agent (front matter) for filtering
- **Description:** MCP server configuration. MCP servers are defined in `mcp.json`. In `agents.app.yaml`, list which servers are available to agents. Agents inherit all listed servers by default or can use allow/deny lists to filter.

**Global configuration (in `agents.app.yaml`) - List available servers:**
```yaml
mcp:
  - microsoft-learn
  - azure-devops
  - custom-api
```
*Note: These server names must be defined in `mcp.json`. See [MCP documentation](https://modelcontextprotocol.io/) for server definitions.*

**Agent filtering - Override with allow/deny lists:**
```yaml
# Include only specific MCP servers (shorthand)
mcp:
  - microsoft-learn

# Include only specific MCP servers (explicit)
mcp:
  include: ["microsoft-learn", "azure-devops"]

# Exclude specific MCP servers
mcp:
  exclude: ["custom-api"]
```

**Disable all MCP servers for an agent:**
```yaml
mcp: []
# or
mcp:
  include: []
```

**Note:** Agents inherit all globally defined MCP servers by default. Use `include`/`exclude` to filter.

---

#### `skills`
- **Type:** `array`, `object`, or `boolean`
- **Location:** Agent (front matter) for filtering only
- **Description:** Skills configuration. All skills in `skills/` directory are automatically discovered and available to all agents by default. Use allow/deny lists in agent front matter to filter which skills each agent can access.

**Agent filtering - Override with allow/deny lists:**
```yaml
# Include only specific skills (shorthand)
skills:
  - azure-resources

# Include only specific skills (explicit)
skills:
  include: ["azure-resources", "cost-optimization"]

# Exclude specific skills
skills:
  exclude: ["security-review"]
```

**Disable all skills for an agent:**
```yaml
skills: false
# or
skills: []
# or
skills:
  include: []
```

**Note:** All skills in the `skills/` directory are auto-discovered and available to all agents by default. Use `include`/`exclude` to filter.

---

#### `response_example`
- **Type:** `string` (multiline)
- **Typical location:** Agent only
- **Can override:** N/A (agent-specific only)
- **Description:** Example response structure for HTTP-triggered agents. Used for documentation and to guide output format.

**Example:**
```yaml
response_example: |
  {
    "total_resources": 42,
    "by_type": {
      "Microsoft.Web/sites": 5
    }
  }
```

---

#### `response_schema`
- **Type:** `object`
- **Typical location:** Agent only
- **Can override:** N/A (agent-specific only)
- **Description:** JSON Schema for validating agent outputs. More formal than `response_example`.

**Example:**
```yaml
response_schema:
  type: object
  required: ["total_resources", "by_type"]
  properties:
    total_resources:
      type: integer
```

---

#### `input_schema`
- **Type:** `object`
- **Typical location:** Agent only
- **Can override:** N/A (agent-specific only)
- **Description:** JSON Schema for validating incoming HTTP requests before invoking the agent
- **Only applicable to:** HTTP-triggered agents

**Example:**
```yaml
input_schema:
  type: object
  required: ["subscription_id"]
  properties:
    subscription_id:
      type: string
      pattern: "^[0-9a-f-]+$"
```

---

#### `metadata`
- **Type:** `object`
- **Typical location:** Agent only
- **Can override:** N/A (agent-specific only)
- **Description:** Additional metadata for organization, discoverability, and governance. Fields are free-form.

**Common fields:**
```yaml
metadata:
  version: string
  owner: string
  tags: string[]
  documentation_url: string
  support_contact: string
```

**Example:**
```yaml
metadata:
  version: "1.2.0"
  owner: "platform-team@company.com"
  tags: ["production", "cost-optimization"]
```

---

## Environment Variable Substitution

Use `$VARIABLE_NAME` syntax in any field value for runtime substitution from app settings or environment variables.

**Common patterns:**
- `$ACA_SESSION_POOL_ENDPOINT` — Session pool endpoint
- `$SUBSCRIPTION_ID` — Azure subscription ID
- `$O365_CONNECTION_ID` — Office 365 connection resource ID
- `$TO_EMAIL` — Recipient email address
- `$STORAGE_CONNECTION` — Storage account connection string

---

## Complete Examples

### Example 1: Multi-Agent Application with Global Configuration

This example demonstrates the recommended pattern: define all infrastructure globally and filter per-agent as needed.

**Global Configuration (`agents.app.yaml`):**
```yaml
# Shared infrastructure
execution_sandbox:
  session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

# Connector tools (may be replaced by MCP in future)
tools_from_connections:
  - connection_id: $O365_CONNECTION_ID

# Global defaults
model: gpt-4o
timeout: 900

# Available MCP servers
mcp:
  - microsoft-learn
  - azure-devops

# Global tool configuration
tools:
  exclude: ["bash", "execute_shell"]
```

**Chat Agent (`chat.agent.md`):**
```yaml
---
name: Chat Assistant
description: A helpful assistant with Python code execution capabilities
---

You are a helpful assistant. If you need to get up to date information, browse the web for it.
```
*Note: This agent inherits all global capabilities (sandbox, connectors, MCP servers, auto-discovered skills and tools, model, timeout).*

**Resource Summary Agent (`resource_summary.agent.md`):**
```yaml
---
name: Resource Summary
description: Returns a structured summary of Azure resources

trigger:
  type: http_trigger
  args:
    route: "resource-summary"
    methods: ["POST"]
    auth_level: function

skills:
  - azure-resources  # Filter: only use azure-resources (from global config)

input_schema:
  type: object
  required: ["subscription_id"]
  properties:
    subscription_id:
      type: string
      pattern: "^[0-9a-f-]+$"

response_schema:
  type: object
  required: ["total_resources", "by_type"]
  properties:
    total_resources:
      type: integer
    by_type:
      type: object
    by_location:
      type: object
---

Given the subscription ID in the request body, list all resources and return a structured summary.
```
*Note: This agent filters global skills to only `azure-resources`. Inherits all other global capabilities.*

**Daily Report Agent (`daily_report.agent.md`):**
```yaml
---
name: Daily Azure Report
description: Lists resources created or changed in the last 24 hours and emails a report

trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"

mcp:
  - microsoft-learn  # Include only: microsoft-learn (shorthand for include)

skills:
  - azure-resources  # Include only: azure-resources (shorthand for include)
---

When triggered, list all resources in subscription $SUBSCRIPTION_ID, filter for changes in the last 24 hours, and email a report to $TO_EMAIL.
```
*Note: This agent filters global MCP servers to only `microsoft-learn` and global skills to only `azure-resources`. Inherits all other global capabilities.*

**Timer Agent with HTTP and MCP Endpoints (`scheduled_task.agent.md`):**
```yaml
---
name: Scheduled Task
description: A timer-triggered agent with HTTP and MCP access for testing

trigger:
  type: timer_trigger
  args:
    schedule: "0 0 * * * *"  # Every hour

enable-http: true  # Enable HTTP chat endpoints for manual testing
enable-mcp: true   # Expose as MCP tool for other agents

skills:
  - azure-resources
---

Run scheduled Azure resource checks. Can be triggered on schedule, via HTTP endpoints, or called as a tool by other agents.
```

This creates:
- Timer trigger: Runs every hour automatically
- HTTP endpoints: `POST /agent/chat`, `POST /agent/chatstream` for testing
- MCP tool: `scheduled_task` tool callable by other agents

### Example 2: Simple Single-Agent Application

**Global Configuration (`agents.app.yaml`):**
```yaml
execution_sandbox:
  session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

model: claude-sonnet-4
timeout: 600
```

**Agent (`main.agent.md`):**
```yaml
---
name: Chat Assistant
description: A helpful assistant with Python code execution capabilities
---

You are a helpful assistant. If you need to run Python code or perform calculations, use the code execution sandbox.
```

### Example 3: Agent with Runtime Overrides and Capability Filtering

This example shows how to override runtime settings and filter capabilities per-agent.

**Global Configuration (`agents.app.yaml`):**
```yaml
execution_sandbox:
  session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

model: gpt-4o
timeout: 900

mcp:
  - microsoft-learn
  - azure-devops
```

**Agent with Overrides (`fast_agent.agent.md`):**
```yaml
---
name: Fast Agent
description: A fast agent that uses a different model

trigger:
  type: http_trigger

# Runtime overrides
model: gpt-4o-mini  # Override: use faster model instead of global default
timeout: 60         # Override: shorter timeout instead of global default

# Capability filters
execution_sandbox: false  # Opt out of code execution for security/performance
mcp: []            # Disable all MCP servers (empty include list)
skills: false      # Disable all skills
---

You are a fast agent optimized for simple queries.
```
*Note: This agent overrides runtime settings (model, timeout) and opts out of the sandbox and filters out all MCP servers and skills for maximum performance.*

### Example 4: Agent Using Exclude Pattern

**Global Configuration (`agents.app.yaml`):**
```yaml
execution_sandbox:
  session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

mcp:
  - microsoft-learn
  - azure-devops
  - github-copilot
  - custom-api

tools:
  exclude: ["bash", "execute_shell"]  # Exclude dangerous tools globally
```

**Agent with Exclusions (`basic_agent.agent.md`):**
```yaml
---
name: Basic Agent
description: A basic agent that excludes certain capabilities

trigger:
  type: http_trigger

# Exclude specific capabilities (inherit all others)
mcp:
  exclude: ["custom-api"]  # Use all MCP servers except custom-api

skills:
  exclude: ["compliance-checker", "security-review"]  # Exclude these auto-discovered skills

tools:
  exclude: ["web_fetch"]  # Also exclude web_fetch (in addition to global excludes)
---

You are a basic agent with most capabilities but some exclusions for security.
```
*Note: This agent demonstrates the exclude pattern, which is consistent across `mcp`, `skills`, and `tools`.*

### Example 5: Minimal Configuration

**No global configuration file** (`agents.app.yaml` omitted)

**Agent (`main.agent.md`):**
```yaml
---
name: Azure Assistant
description: An interactive assistant for exploring Azure resources
---

Help the user explore resources in subscription $SUBSCRIPTION_ID.
```

All configuration uses framework defaults (HTTP trigger, default model, etc.)

---

## Validation Rules

1. **Required fields:** `name` and `description` must always be present in agent front matter
2. **Single trigger per file:** Only one trigger can be specified per `.agent.md` file
3. **Trigger structure:** When specified, trigger must have `type` field; `args` field is optional for triggers with no configuration
4. **Trigger type-specific validation:** Each trigger type validates its own required fields in the `args` section
5. **Environment variables:** `$VARIABLE_NAME` references must be defined in app settings at runtime
6. **CRON expressions:** Timer trigger schedules must be valid 6-field CRON expressions
7. **HTTP methods:** Must be valid HTTP verbs (GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS)
8. **Auth levels:** Must be one of: `anonymous`, `function`, `admin`
9. **Schema validation:** `input_schema` and `response_schema` must be valid JSON Schema (draft-07 or later)
10. **Model names:** Must be valid Copilot SDK model identifiers (e.g., `claude-sonnet-4`, `gpt-4o`, `o1`, `o1-mini`)
11. **Timeout limits:** Must be positive numbers; consider Azure Functions timeout limits (5 min for Consumption, 30 min for Premium)
12. **Tool references:** Tools in `tools.include` must exist in `tools/` directory or be built-in tools
13. **MCP server references:** Servers in `mcp` array must be defined in `mcp.json`
14. **Skill references:** Skills in `skills` array must exist as directories under `skills/`
15. **Configuration file location:** `agents.app.yaml` must be in the same directory as agent `.md` files (typically `src/`)

---

## File Naming Conventions

- **Global configuration:** `agents.app.yaml` (in `src/` directory)
- **Main agent:** `main.agent.md` (optional) — Special agent with HTTP chat UI and MCP tool enabled by default
- **Named agents:** `{agent-name}.agent.md` (e.g., `daily_azure_report.agent.md`)
- **Skills:** `skills/{skill-name}/SKILL.md`

**Main agent behavior:**
The `main.agent.md` file is special:
- **HTTP endpoints enabled by default** (`enable-http: true`):
  - `GET /` — Chat UI page
  - `POST /agent/chat` — Non-streaming chat endpoint
  - `POST /agent/chatstream` — Streaming chat endpoint (SSE)
- **MCP tool enabled by default** (`enable-mcp: true`):
  - Tool name derived from `name` field in front matter
  - Exposed as `mcpToolTrigger` for other agents to call
- **No trigger required** — Uses HTTP by default; can be omitted from front matter

Other agents require an explicit `trigger` definition and have `enable-http: false` and `enable-mcp: false` by default.

**Example project structure:**
```
src/
  agents.app.yaml           # Global configuration
  main.agent.md             # Default HTTP agent
  daily_report.agent.md     # Timer-triggered agent
  resource_summary.agent.md # Custom HTTP agent
  function_app.py           # Python Functions entry point
  host.json
  requirements.txt
  skills/
    azure-resources/
      SKILL.md
    cost-optimization/
      SKILL.md
  tools/
    azure_rest.py
```

---

## Resources

- **Trigger Reference:** [`triggers.md`](./triggers.md) — Detailed documentation for all trigger types
- **Sample Projects:** [`../samples/`](../samples/) — Working examples demonstrating various agent patterns