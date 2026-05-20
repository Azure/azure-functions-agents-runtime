# Azure Functions Agents - Configuration Specification

## Overview

Azure Functions agents use a **two-tier configuration system**:

1. **Global Configuration** (`agents.config.yaml`) — Infrastructure and runtime defaults
2. **Agent-Specific Configuration** (`.agent.md` front matter) — Agent behavior, triggers, and capability filtering

Each agent is defined in a `.agent.md` file with YAML front matter followed by markdown instructions. The front matter configures the agent-specific behavior, while the markdown body contains the agent's system prompt.

### Configuration Model

**Global configuration defines infrastructure and defaults:**
- Skills (auto-discovered from `skills/` directory)
- Custom tools (auto-discovered from `tools/` directory)
- System tools (`system_tools`)
  - Code execution sandbox configuration
  - Connector tools
- Default runtime settings (model, timeout)

**MCP server discovery:**
- MCP servers (defined in `mcp.json` or `.vscode/mcp.json`)

**Agent front matter:**
- **Inherits all discovered capabilities by default**
- Can apply **exclude lists** to filter out unwanted MCP servers, skills, or tools
- Can **override** runtime settings (model, timeout)
- Must define **trigger** (how the agent is invoked)
- Can enable **HTTP/MCP endpoints** for testing and composition

### Configuration Precedence

For runtime settings (model, timeout):
1. **Agent front matter** — Explicit overrides in `.agent.md` files
2. **Global configuration** — Values in `agents.config.yaml`
3. **Environment variables** — App settings and env vars
4. **Framework defaults** — Built-in default values

For capabilities (MCP, skills, tools):
1. **Auto-discovered** — MCP servers from `mcp.json` or `.vscode/mcp.json`, plus skills and tools from their directories
2. **Filtered per-agent** using exclude lists in agent front matter

### Quick Reference: Required vs Optional

| Level | Required Properties | Optional Properties |
|-------|-------------------|-------------------|
| **Global** (`agents.config.yaml`) | None (entire file is optional) | `system_tools`, `model`, `timeout`, `tools` |
| **Agent** (`.agent.md` front matter) | `name`, `description`, `trigger`* | `debug`, `model`, `timeout`, `system_tools`, `mcp`, `skills`, `tools`, `input_schema`, `response_schema`, `response_example`, `metadata` |


---

## Configuration Files

### Global Configuration (`agents.config.yaml`)
Optional file in the root directory that defines shared infrastructure and runtime defaults for all agents.

**Required properties:** None (entire file is optional)

**Supported properties:**
- `system_tools` — Object containing system-level tools configuration
  - `execute_in_sessions` — Object with code execution sandbox configuration
  - `tools_from_connections` — Array of connector configurations
- `model` — String specifying default LLM model identifier
- `timeout` — Number specifying default execution timeout in seconds
- `tools` — Object for tool filtering configuration

**Note:** MCP servers (from `mcp.json` or `.vscode/mcp.json`), skills (from `skills/` directory), and custom tools (from `tools/` directory) are automatically discovered. Agents can filter them out using exclude lists.

**Key principle:** `agents.config.yaml` defines shared runtime configuration. Agents filter discovered capabilities and choose what they use.

### Agent Configuration (`.agent.md` front matter)
YAML front matter at the top of each agent file.

**Required properties:**
- `name` — String, display name for the agent
- `description` — String, brief description of the agent's purpose
- `trigger` — Object defining how the agent is invoked (optional for `main.agent.md` only)

**Optional properties:**
- `debug` — Object or boolean for enabling debugging/testing endpoints
- `model` — String to override global default model
- `timeout` — Number to override global default timeout
- `system_tools` — Object to opt out of system tools
- `mcp` — Boolean or object to inherit, disable, or exclude MCP servers
- `skills` — Object with exclude lists or false to filter skills
- `tools` — Object with exclude lists or false to filter tools
- `input_schema` — Object, JSON Schema for HTTP request validation
- `response_schema` — Object, JSON Schema for response validation
- `response_example` — String, example response for documentation
- `metadata` — Object, additional organizational metadata


**File structure:**
```
/
  agents.config.yaml          # Optional: Global defaults
  *.agent.md               # Agents (require trigger)
  ...
```

---

## Field Reference

Fields are organized into categories based on how they can be used:

### Field Categories

**Infrastructure (Discovered capabilities, filtered in agents):**
- `mcp` — MCP servers discovered from `mcp.json` or `.vscode/mcp.json`, filtered in agents
- `skills` — Auto-discovered from `skills/` directory, exclude lists (agent only)
- `tools` — Auto-discovered from `tools/` directory, exclude lists (agent only)
- `system_tools` — System-level tools and capabilities (global configuration, agent opt-out)
  - `execute_in_sessions` — Code execution sandbox
  - `tools_from_connections` — Connector-based tools

**Runtime Settings (Global defaults, overridable in agents):**
- `model` — LLM selection
- `timeout` — Execution time limit

**Agent-Specific (Agent front matter only):**
- `name`, `description` — Agent identity (required)
- `trigger` — Invocation method (required for non-main agents)
- `debug` — Debugging and testing endpoints
- `input_schema`, `response_schema`, `response_example` — HTTP validation
- `metadata` — Organizational metadata

---

### Required Fields (Agent Front Matter Only)

**Summary:** Every `.agent.md` file must have `name`, `description`, and `trigger` (except `main.agent.md` where `trigger` is optional).

#### `name`
- **Type:** `string`
- **Typical location:** Agent only (required)
- **Description:** Display name for the agent. This is used for chat UI labels, descriptions, logs, and documentation, but it does **not** control any registered Azure Function name, route slug, or MCP/debug identifier. See [File Naming Conventions](#file-naming-conventions).
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
- **Description:** Defines how the agent is invoked. Required for all agents except `main.agent.md`. If `main.agent.md` omits `trigger`, it uses the default HTTP trigger settings.
- **Structure:** `type` field specifies the trigger type, `args` contains type-specific configuration
- **Important:** Only **one trigger per agent file** is allowed

#### **HTTP Trigger**
```yaml
trigger:
  type: http_trigger
  args:
    route: string          # Required. URL path for the endpoint
    methods: string[]      # Optional. Array of HTTP methods. Defaults to ["POST"]
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

#### `debug`
- **Type:** `object`
- **Location:** Agent only (front matter)
- **Can override:** N/A (agent-specific only)
- **Default:** All disabled (`false`) for regular agents; all enabled (`true`) for `main.agent.md`
- **Description:** Enables debugging and testing endpoints for the agent. Useful for development, testing, and agent composition.

**Structure:**
```yaml
debug:
  chat: boolean   # Enable chat UI plus chat/chatstream APIs
  http: boolean   # Enable REST API endpoints even without the chat UI
  mcp: boolean    # Enable MCP tool registration for agent-to-agent calls
```

**Endpoint Details:**

**`chat: true`** — Interactive Chat UI
- **Routes by agent type:** `{slug}` below is the sanitized filename-based value described in [Function name resolution](#function-name-resolution).

  | Agent file | UI (`GET`) | Chat (`POST`) | Streaming (`POST`) | MCP tool when `debug.mcp: true` |
  | --- | --- | --- | --- | --- |
  | `main.agent.md` | `/` | `/agent/chat` | `/agent/chatstream` | Registers the `main` MCP tool through the shared runtime MCP webhook |
  | Any other `.agent.md` with `debug.chat: true` | `/agents/{slug}/` | `/agents/{slug}/chat` | `/agents/{slug}/chatstream` | Registers an MCP tool named `{slug}` through the shared runtime MCP webhook |
- **Purpose:** Browser-based chat interface for manual testing and interaction
- **Behavior:** Also registers the backing REST endpoints the built-in page calls, so `debug.chat: true` is self-sufficient
- **Use case:** Test any agent (timer, queue, HTTP) via a web UI during development

**`http: true`** — REST API Endpoints
- **Routes:** Registers the same `POST` routes shown above for the relevant agent type, but without the chat UI page
- **Behavior:** Useful when you want programmatic access without exposing the chat page
- **Request body:** `{"prompt": "your question or instruction"}`
- **Response:** JSON with `session_id`, `response`, `tool_calls`, etc.
- **Use case:** Programmatic access to the agent, integration testing, API clients

**`mcp: true`** — MCP Tool Registration
- **Tool name:** Derived from the sanitized agent filename slug described in [Function name resolution](#function-name-resolution) (for example, `daily_azure_report.agent.md` → `daily_azure_report`)
- **Tool description:** From agent `description` field
- **Tool trigger:** `mcpToolTrigger`
- **Input:** `{"prompt": "string"}`
- **Output:** JSON response from the agent
- **Route behavior:** Does not create a per-agent `/agents/{slug}` MCP route; it registers a tool on the shared runtime MCP transport
- **Use case:** Enable agent-to-agent communication — other agents can invoke this agent as a tool

**Examples:**

**Enable all debug endpoints:**
```yaml
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"

debug:
  chat: true   # Enable UI for manual testing
  http: true   # Enable REST API for integration tests
  mcp: true    # Expose as MCP tool for other agents
```

**Enable only HTTP API (no UI, no MCP):**
```yaml
trigger:
  type: queue_trigger
  args:
    queue_name: "tasks"

debug:
  http: true   # Enable REST API only
```

**Enable only MCP tool (for agent composition):**
```yaml
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"

debug:
  mcp: true   # Expose as tool for other agents to call
```

**Shorthand for enabling all:**
```yaml
debug: true   # Equivalent to chat: true, http: true, mcp: true
```

**Shorthand for disabling all:**
```yaml
debug: false  # Equivalent to chat: false, http: false, mcp: false (default)
```

---

#### `model`
- **Type:** `string`
- **Location:** Global (`agents.config.yaml`) for default, Agent (front matter) for override
- **Can override:** Yes
- **Description:** Specifies which LLM to use for the agent. Valid model identifiers depend on the active provider.
- **Precedence:** Agent front matter → Global `agents.config.yaml` → provider-specific env (`AZURE_OPENAI_DEPLOYMENT` for Azure OpenAI, `FOUNDRY_MODEL` for Foundry) → `MAF_MODEL` → provider default

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
- **Location:** Global (`agents.config.yaml`) for default, Agent (front matter) for override
- **Can override:** Yes
- **Description:** Maximum execution time in seconds for the agent.
- **Precedence:** Agent front matter → Global `agents.config.yaml` → `COPILOT_AGENT_TIMEOUT` env var → `900` seconds (default)

**Global default:**
```yaml
timeout: 900  # 15 minutes
```

**Agent override:**
```yaml
timeout: 60  # 1 minute for fast agent
```

---

#### `system_tools`
- **Type:** `object`
- **Location:** Global (`agents.config.yaml`) for configuration, Agent (front matter) for opt-out
- **Description:** Configures system-level tools and capabilities provided by the Azure Functions agent runtime. Defined globally, inherited by all agents, with opt-out capability at the agent level.

**Structure:**
```yaml
system_tools:
  execute_in_sessions:      # Code execution sandbox configuration
    session_pool_management_endpoint: string
  tools_from_connections:   # Connector-based tools
    - connection_id: string
```

---

##### `system_tools.execute_in_sessions`
- **Type:** `object` (global), `boolean` (agent)
- **Description:** Configures Python code execution environment using Azure Container Apps dynamic sessions. All agents inherit sandbox access by default. Agents can opt out by setting to `false`.

**Global configuration (in `agents.config.yaml`):**
```yaml
system_tools:
  execute_in_sessions:
    session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT
```

**Agent opt-out (in agent front matter):**
```yaml
---
name: Simple Agent
description: An agent that doesn't need code execution

system_tools:
  execute_in_sessions: false  # Opt out of code execution capabilities
---
```

**Note:** When the runtime has no explicit session id to bind to the ACA sandbox, each invocation gets a fresh GUID-backed sandbox session instead of sharing a default session. Managed identity auth for the ACA sandbox honors `AZURE_CLIENT_ID` in multi-identity Function Apps.

**Note:** Future versions may support multiple sandbox types with exclude lists similar to MCP servers, skills, and tools.

---

##### `system_tools.tools_from_connections`
- **Type:** `array`
- **Description:** Loads connector-based tools (e.g., Office 365, Outlook, SharePoint) from Azure Logic App connectors as dynamic tools. All agents inherit these tools by default.
- **Status:** ⚠️ Under review — May be deprecated in favor of MCP-based connector integration

**Global configuration (in `agents.config.yaml`):**
```yaml
system_tools:
  tools_from_connections:
    - connection_id: $O365_CONNECTION_ID
    - connection_id: $OUTLOOK_CONNECTION_ID
```

**Note:** Connector auth uses `DefaultAzureCredential`; set `AZURE_CLIENT_ID` in multi-identity Function Apps to select the intended managed identity.

**Note:** This field enables dynamic tool generation from connector APIs. Connector-backed MCP servers are defined in `mcp.json` or `.vscode/mcp.json` and participate in the standard MCP discovery flow, which provides better standardization and discoverability. The future direction between these two approaches is under consideration.

---

#### `tools`
- **Type:** `object`
- **Location:** Global (`agents.config.yaml`) for configuration, Agent (front matter) for filtering
- **Description:** Controls which custom tools (auto-discovered from the `tools/` directory) are available to agents. Use global config to set defaults, agent config to apply exclude lists.

**Global configuration (optional) - Set defaults:**
```yaml
tools:
  exclude: ["bash", "execute_shell"]  # Exclude dangerous tools by default
```

**Agent filtering - Use exclude lists:**
```yaml
# Exclude specific tools (in addition to global excludes)
tools:
  exclude: ["web_fetch", "http_request"]
```

**Disable all tools for an agent:**
```yaml
tools: false
```

**Note:** Agents inherit all globally available custom tools by default. Use `exclude` to filter out unwanted tools.

---

#### `mcp`
- **Type:** `boolean` or `object`
- **Location:** Agent (front matter) for filtering
- **Description:** MCP server filtering. MCP servers are discovered from `mcp.json` or `.vscode/mcp.json`. Agents inherit all discovered servers by default. Use `false` to disable MCP for an agent, or use `exclude` to hide specific servers.

**Default behavior - Inherit all discovered servers:**
```yaml
# Omit `mcp`, set it to null, or use:
mcp: true
```

**Agent filtering - Use exclude lists:**
```yaml
# Exclude specific MCP servers
mcp:
  exclude: ["custom-api", "experimental-server"]
```

**Disable all MCP servers for an agent:**
```yaml
mcp: false
```

**Note:** `mcp.exclude` entries must match MCP servers discovered from `mcp.json` or `.vscode/mcp.json`. See [MCP documentation](https://modelcontextprotocol.io/) for server definitions.

---

#### `skills`
- **Type:** `object` or `boolean`
- **Location:** Agent (front matter) for filtering only
- **Description:** Skill filtering configuration. Each skill lives in its own subdirectory under `skills/` with a `SKILL.md` file declaring `name:` and `description:` in YAML frontmatter. Skill names must match `^[a-z0-9]([a-z0-9]*-[a-z0-9])*[a-z0-9]*$` (lowercase letters, digits, and single hyphens). At runtime the discovered skills are exposed through MAF's `SkillsProvider`, which gives the agent `load_skill` / `read_skill_resource` tools that operate scoped to the skill directory.

**SKILL.md format (one per skill directory):**
```markdown
---
name: my-skill
description: One sentence the LLM uses to decide whether to load this skill.
---

# My Skill

Skill body — instructions, examples, references to in-directory resources.
```

**Agent filtering - Use exclude lists:**
```yaml
# Exclude specific skills (matched against the SKILL.md `name` field)
skills:
  exclude: ["security-review", "compliance-checker"]
```

**Disable all skills for an agent:**
```yaml
skills: false
```

**Note:** All skills under `skills/` are auto-discovered and available to all agents by default. Use `exclude` to filter out unwanted skills.

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

Environment variable substitution is resolved against the Azure Functions process environment. On Azure, Application Settings are exposed to the function host as environment variables, so placeholders can refer to either local environment variables or deployed app settings.

**Scope**

Inline substitution applies to all string values in:
1. `agents.config.yaml`
2. `mcp.json`
3. `.vscode/mcp.json`
4. Agent `*.agent.md` frontmatter values
5. Agent `*.agent.md` markdown body

For the markdown body, text inside fenced code blocks (` ``` `) is preserved and is not substituted.

**Supported syntaxes**
- `$IDENT` — for example, `Authorization: Bearer $TOKEN`
- `%IDENT%` — for example, `base_url: "https://%HOST%/api"`

To keep placeholder-like text literal while leaving substitution enabled, escape it by doubling the placeholder sigil:
- `$$IDENT` renders as literal `$IDENT`
- `%%IDENT%%` renders as literal `%IDENT%`

Identifiers must match `[A-Za-z_][A-Za-z0-9_]*`. A full-string value such as `default_timeout: "$DEFAULT_TIMEOUT"` is also substituted.

**Resolution**

Each placeholder is resolved with `os.environ.get(IDENT, original_placeholder)`. If a referenced environment variable is not set, the original placeholder text is left literal. String-typed fields keep that literal value; non-string fields still undergo normal schema validation, so entries such as `timeout: $TIMEOUT` raise a validation error.

**What is not substituted**
- Dictionary / object keys are never substituted; only values are substituted. For example, `"$KEY": "value"` keeps `"$KEY"` as the literal key.
- Escaped placeholders stay literal: `$$TOKEN` becomes `$TOKEN`, and `%%HOST%%` becomes `%HOST%`.
- `${FOO}` brace syntax is not supported because `{` immediately after `$` does not match the identifier regex.
- Identifiers starting with a digit, such as `$9PORT`, do not match the supported syntax and remain literal.
- For `$IDENT`, identifiers that include characters outside `[A-Za-z0-9_]` are matched up to the first invalid character. For example, `$VAR-NAME` becomes `<value-of-VAR>-NAME` when `VAR` is set, and remains `$VAR-NAME` when `VAR` is unset.
- For `%IDENT%`, the closing `%` must immediately follow the identifier, so tokens like `%VAR-NAME%` remain fully literal regardless of whether `VAR` is set.
- Text inside markdown fenced code blocks remains literal. This code-block exception applies only to the markdown body, not to YAML or JSON string values.

Set `substitute_variables: false` in an agent's frontmatter to disable both frontmatter substitution and markdown body substitution for that agent. The flag is per-agent, defaults to `true`, and has no effect on the app-wide `agents.config.yaml`, `mcp.json`, or `.vscode/mcp.json` files.

> **Note**: `substitute_variables` itself is read before env-var substitution. It must be a literal boolean (`true` or `false`). Setting `substitute_variables: $MY_FLAG` will not be resolved and defaults to `true`.

**Example:**
```yaml
---
name: Notifier
model: $AGENT_MODEL
substitute_variables: false
response_example: $RESPONSE_TEMPLATE
---

Send a daily summary email to $TO_EMAIL.
```

With `substitute_variables: false`, `model`, `response_example`, and `$TO_EMAIL` in the body all remain literal.

**Common patterns:**
- `$ACA_SESSION_POOL_ENDPOINT` — Session pool endpoint
- `$SUBSCRIPTION_ID` — Azure subscription ID
- `$O365_CONNECTION_ID` — Office 365 connection resource ID
- `$API_ENDPOINT` — Service endpoint URL
- `$TO_EMAIL` — Recipient email address
- `$STORAGE_CONNECTION` — Storage account connection string

---

## Complete Examples

### Example 1: Multi-Agent Application with Global Configuration

This example demonstrates the recommended pattern: define shared runtime configuration in `agents.config.yaml`, discover MCP servers from `mcp.json`, and filter capabilities per-agent as needed.

**Global Configuration (`agents.config.yaml`):**
```yaml
# Shared infrastructure
system_tools:
  execute_in_sessions:
    session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT
  tools_from_connections:
    - connection_id: $O365_CONNECTION_ID

# Global defaults
model: gpt-4o
timeout: 900

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
*Note: This agent inherits shared runtime defaults plus all discovered capabilities (sandbox, connectors, MCP servers, auto-discovered skills and tools).*

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
*Note: This agent inherits shared runtime defaults plus all discovered capabilities.*

**Daily Report Agent (`daily_report.agent.md`):**
```yaml
---
name: Daily Azure Report
description: Lists resources created or changed in the last 24 hours and emails a report

trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"
---

When triggered, list all resources in subscription $SUBSCRIPTION_ID, filter for changes in the last 24 hours, and email a report to $TO_EMAIL.
```
*Note: This agent inherits shared runtime defaults plus all discovered capabilities.*

**Timer Agent with HTTP and MCP Endpoints (`scheduled_task.agent.md`):**
```yaml
---
name: Scheduled Task
description: A timer-triggered agent with HTTP and MCP access for testing

trigger:
  type: timer_trigger
  args:
    schedule: "0 0 * * * *"  # Every hour

debug:
  chat: true   # Enable chat UI for manual testing
  http: true   # Enable REST API endpoints for integration tests
  mcp: true    # Expose as MCP tool for other agents
---

Run scheduled Azure resource checks. Can be triggered on schedule, via HTTP endpoints, or called as a tool by other agents.
```

This creates:
- Timer trigger: Runs every hour automatically
- Chat UI: `GET /` for browser-based testing
- HTTP endpoints: `POST /agent/chat`, `POST /agent/chatstream` for programmatic access
- MCP tool: `scheduled_task` tool callable by other agents

### Example 2: Simple Single-Agent Application

**Global Configuration (`agents.config.yaml`):**
```yaml
system_tools:
  execute_in_sessions:
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

This example shows how to override runtime settings and filter capabilities per-agent. Assume `mcp.json` or `.vscode/mcp.json` includes an `experimental-server` entry.

**Global Configuration (`agents.config.yaml`):**
```yaml
system_tools:
  execute_in_sessions:
    session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

model: gpt-4o
timeout: 900
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
system_tools:
  execute_in_sessions: false  # Opt out of code execution for security/performance
mcp:
  exclude: ["experimental-server"]  # Exclude a discovered MCP server
skills:
  exclude: ["admin-tools"]  # Exclude specific skills
---

You are a fast agent optimized for simple queries.
```
*Note: This agent overrides runtime settings (model, timeout), opts out of the sandbox, and excludes specific MCP servers and skills.*

### Example 4: Agent Using Exclude Pattern

Assume `mcp.json` defines the `microsoft-learn`, `azure-devops`, `github-copilot`, and `custom-api` servers used in this example.

**Global Configuration (`agents.config.yaml`):**
```yaml
system_tools:
  execute_in_sessions:
    session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

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

**No global configuration file** (`agents.config.yaml` omitted)

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

### Required Properties

**Agent Front Matter (`.agent.md`):**
1. **`name`** — Must always be present (string)
2. **`description`** — Must always be present (string)
3. **`trigger`** — Required for all agents except `main.agent.md` (object with `type` field)

**Global Configuration (`agents.config.yaml`):**
- **No required properties** — The entire file is optional

### Supported Properties

**Global Configuration (`agents.config.yaml`) — Exact property names:**
- `system_tools` (object)
  - `execute_in_sessions` (object)
  - `tools_from_connections` (array)
- `model` (string)
- `timeout` (number)
- `tools` (object)

**Agent Front Matter (`.agent.md`) — All properties from Field Reference section**

### Field Validation Rules

1. **Single trigger per file:** Only one trigger can be specified per `.agent.md` file
2. **Trigger structure:** When specified, trigger must have `type` field; `args` field is optional for triggers with no configuration
3. **Trigger type-specific validation:** Each trigger type validates its own required fields in the `args` section
4. **Environment variables:** Inline `$VAR` and `%VAR%` placeholders in supported string values may be backed by environment variables or Azure Application Settings; if no value is defined, the literal placeholder is preserved
5. **CRON expressions:** Timer trigger schedules must be valid 6-field CRON expressions
6. **HTTP methods:** Must be valid HTTP verbs (GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS)
7. **Auth levels:** Must be one of: `anonymous`, `function`, `admin`
8. **Schema validation:** `input_schema` and `response_schema` must be valid JSON Schema (draft-07 or later)
9. **Model names:** Must be valid Copilot SDK model identifiers (e.g., `claude-sonnet-4`, `gpt-4o`, `o1`, `o1-mini`)
10. **Timeout limits:** Must be positive numbers; consider Azure Functions timeout limits (5 min for Consumption, 30 min for Premium)
11. **Tool references:** Tools in `tools.exclude` must exist in the `tools/` directory
12. **MCP server references:** Servers in `mcp.exclude` must be defined in MCP configuration discovered from `mcp.json` or `.vscode/mcp.json`
13. **Skill references:** Skills in `skills.exclude` must exist as directories under `skills/`
15. **Configuration file location:** `agents.config.yaml` must be in the same directory as agent `.md` files

---

## File Naming Conventions

- **Global configuration:** `agents.config.yaml` (in root directory)
- **Main agent:** `main.agent.md` (optional) — Special agent with HTTP chat UI and MCP tool enabled by default
- **Named agents:** `{agent-name}.agent.md` (e.g., `daily_azure_report.agent.md`)
- **Skills:** `skills/{skill-name}/SKILL.md`

### Function name resolution

For non-main agents, two related identifiers are derived from the source filename. The frontmatter `name:` field remains display-only and is never used for either identifier.

- **Azure Function name** (used for host indexing and `admin/functions/{name}` URLs):
  - Start with the agent filename stem (remove `.agent.md`).
  - Sanitize it for Azure Functions registration:
    - Replace characters outside `[A-Za-z0-9_]` with `_`
    - Trim leading/trailing underscores
    - Prefix `fn_` if the result would otherwise start with a digit
  - If another agent in the same `create_function_app()` call already uses that sanitized name, append `_2`, `_3`, and so on until the name is unique.
  - Example: `daily-report.agent.md` → `daily_report`; if `daily_report.agent.md` also exists, the second Azure Function name becomes `daily_report_2`.

- **Debug slug** (used for `/agents/{slug}/`, `/agents/{slug}/chat`, `/agents/{slug}/chatstream`, and the MCP tool name exposed when `debug.mcp: true`):
  - Uses the same filename sanitization rules.
  - Uses the same collision handling as Azure Function names: if another agent in the same `create_function_app()` call already uses that sanitized slug, append `_2`, `_3`, and so on until the slug is unique.
  - In practice, the debug slug stays paired with the allocated Azure Function name for the same agent (for example, `daily_report_2` maps to `/agents/daily_report_2/`).
  - Example: `daily-report.agent.md` → `/agents/daily_report/`; if `daily_report.agent.md` also exists, the second debug slug becomes `/agents/daily_report_2/`.

In other words, the display `name:` field is never used to derive registered Azure Function names, routes, or runtime identifiers; it is presentation-only. See also [`name`](#name).

**Main agent behavior:**
The `main.agent.md` file is special:
- **Debug endpoints enabled by default** (`debug: true`):
  - `GET /` — Chat UI page
  - `POST /agent/chat` — Non-streaming chat endpoint
  - `POST /agent/chatstream` — Streaming chat endpoint (SSE)
  - MCP tool registration — Tool name derived from the sanitized filename slug (`main` for `main.agent.md`), exposed as `mcpToolTrigger`
- **No trigger required** — Uses HTTP by default; can be omitted from front matter

Other agents require an explicit `trigger` definition and have `debug: false` (all debug endpoints disabled) by default.

**Example project structure:**
```
/
  agents.config.yaml           # Global configuration
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
