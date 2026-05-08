# Azure Functions Agents - Configuration Specification

## Overview

Azure Functions agents use a **two-tier configuration system**:

1. **Global Configuration** (`agents.app.yaml`) — Shared settings, defaults, and infrastructure configuration for all agents
2. **Agent-Specific Configuration** (`.agent.md` front matter) — Individual agent overrides, trigger definitions, and filtering

Each agent is defined in a `.agent.md` file with YAML front matter followed by markdown instructions. The front matter configures the agent-specific behavior, while the markdown body contains the agent's system prompt. Global configuration in `agents.app.yaml` provides defaults and shared infrastructure settings.

### Configuration Precedence

Settings follow this precedence order (highest to lowest):
1. **Agent front matter** — Explicit values in `.agent.md` files
2. **Global configuration** — Values in `agents.app.yaml`
3. **Environment variables** — App settings and env vars
4. **Framework defaults** — Built-in default values

---

## Global Configuration File (`agents.app.yaml`)

The `agents.app.yaml` file sits in the `src/` directory alongside your `.agent.md` files and defines shared configuration for all agents in the function app.

### Purpose
- Define shared infrastructure (execution sandbox, connector connections)
- Set default runtime configuration (model, timeout)
- Configure available tools, MCP servers, and skills globally
- Reduce duplication across agent files

### Location
```
src/
  agents.app.yaml          # Global configuration
  main.agent.md            # Agent definitions
  daily_report.agent.md
  ...
```

### Example Structure
```yaml
# Shared infrastructure
execution_sandbox:
  session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

tools_from_connections:
  - connection_id: $O365_CONNECTION_ID

# Global defaults
model: gpt-4o
timeout: 900

# Available MCP servers (agents can filter with allow-list)
mcp:
  - microsoft-learn
  - azure-devops

# Available skills (agents can filter with allow-list)
skills:
  - azure-resources
  - cost-optimization
  - security-review

# Global tool configuration
tools:
  exclude: ["bash", "execute_shell"]

# Reliability settings
retry:
  max_attempts: 3
  backoff: exponential
```

---

## Agent Front Matter (`.agent.md`)

Individual agent files use front matter for agent-specific configuration, overrides, and filtering.

### Agent-Specific Fields

#### Required in Every Agent
- `name` — Display name for the agent
- `description` — Brief description of the agent's purpose

#### Agent-Specific Configuration
- `trigger` — How this agent is invoked (HTTP, timer, etc.)
- `enable-debug-http` — Enable debug HTTP endpoint (alternative to explicit trigger)

#### Filtering & Overrides (Optional)
- `model` — Override the global model for this agent
- `mcp` — Allow-list specific MCP servers for this agent (filters global `mcp`)
- `skills` — Allow-list specific skills for this agent (filters global `skills`)
- `timeout` — Override global timeout for this agent

#### Less Common Overrides
- `execution_sandbox` — Override global sandbox configuration
- `tools_from_connections` — Override global connections
- `tools` — Override global tool configuration
- `response_example` — Example output structure (HTTP agents)
- `response_schema` — JSON Schema for output validation (HTTP agents)
- `input_schema` — Validate incoming HTTP requests

---

## Field Summary by Location

### Typically in `agents.app.yaml` (Global)
- `execution_sandbox` — Python code execution environment
- `tools_from_connections` — Connector-based tools (O365, Teams, SQL, etc.)
- `model` — Default LLM for all agents
- `timeout` — Default maximum execution time
- `mcp` — All available MCP servers
- `skills` — All available skills
- `tools` — Global tool configuration
- `retry` — Default retry behavior

### Typically in `.agent.md` Front Matter (Agent-Specific)
- `name` — Agent display name (required)
- `description` — Agent purpose (required)
- `trigger` — Agent invocation method
- `enable-debug-http` — Debug HTTP endpoint
- `mcp` — Filtered MCP servers for this agent (allow-list)
- `skills` — Filtered skills for this agent (allow-list)
- `model` — Override model for this agent
- `response_example` — HTTP response example
- `response_schema` — HTTP response validation
- `input_schema` — HTTP request validation

---

## Field Reference

Below is the complete field reference. Fields can be placed in either `agents.app.yaml` (global) or `.agent.md` front matter (agent-specific), with agent front matter taking precedence.

### Required Fields (Agent Front Matter Only)

### `name`
- **Type:** `string`
- **Description:** Display name for the agent
- **Example:** `"Daily Azure Report"`

### `description`
- **Type:** `string`  
- **Description:** Brief description of the agent's purpose (used for agent selection, logging, and documentation)
- **Example:** `"Lists resources created or changed in the last 24 hours and emails a report"`

---

## Optional Fields

### `trigger`
- **Type:** `object`
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

**Minimal (defaults to GET/POST on function route):**
```yaml
trigger:
  type: http_trigger
```

#### **Timer Trigger**
```yaml
trigger:
  type: timer_trigger
  args:
    schedule: string       # Required. CRON expression (6-field format: second minute hour day month day-of-week)
```

**Example:**
```yaml
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"  # Daily at 7:00 AM UTC
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

### `enable-debug-http`
- **Type:** `boolean`
- **Description:** Automatically create an HTTP debug endpoint for non-HTTP agents (timer, queue, etc.). Alternative to defining an explicit HTTP trigger.
- **Default:** `false`
- **Typical usage:** Agent front matter
- **Applies to:** Non-HTTP triggered agents

**Example:**
```yaml
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 7 * * *"

enable-debug-http: true  # Creates debug HTTP endpoint for testing
```

**Use case:** Test timer or queue-triggered agents via HTTP during development without waiting for the schedule or adding messages to queues.

---

### `execution_sandbox`
- **Type:** `object`
- **Description:** Configures Python code execution environment using Azure Container Apps dynamic sessions
- **Structure:**
```yaml
execution_sandbox:
  session_pool_management_endpoint: string  # Required. ACA session pool endpoint (typically env var)
```

**Example:**
```yaml
execution_sandbox:
  session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT
```

---

### `tools_from_connections`
- **Type:** `array`
- **Description:** Loads connector-based tools (e.g., Office 365, Outlook, SharePoint) from Azure Logic App connectors
- **Structure:**
```yaml
tools_from_connections:
  - connection_id: string  # Required. Connection resource ID (typically env var)
```

**Example:**
```yaml
tools_from_connections:
  - connection_id: $O365_CONNECTION_ID
  - connection_id: $OUTLOOK_CONNECTION_ID
```

---

### `response_example`
- **Type:** `string` (multiline)
- **Description:** Example response structure. Used for documentation and to guide the agent's output format
- **Best Practice:** Use for structured outputs (JSON, XML) from HTTP-triggered agents

**Example:**
```yaml
response_example: |
  {
    "total_resources": 42,
    "by_type": {
      "Microsoft.Web/sites": 5,
      "Microsoft.Storage/storageAccounts": 3
    },
    "by_location": {
      "eastus2": 20,
      "westus": 10
    }
  }
```

---

### `response_schema`
- **Type:** `object`
- **Description:** JSON Schema for validating and structuring agent outputs. More formal than `response_example`
- **Best Practice:** Use for HTTP-triggered agents that require strict output validation

**Example:**
```yaml
response_schema:
  type: object
  required: ["total_resources", "by_type"]
  properties:
    total_resources:
      type: integer
      minimum: 0
    by_type:
      type: object
      additionalProperties:
        type: integer
    by_location:
      type: object
      additionalProperties:
        type: integer
```

---

### `input_schema`
- **Type:** `object`
- **Description:** JSON Schema for validating incoming HTTP requests before invoking the agent
- **Best Practice:** Use to validate request bodies early and provide better error messages
- **Only applicable to:** HTTP-triggered agents

**Example:**
```yaml
input_schema:
  type: object
  required: ["subscription_id"]
  properties:
    subscription_id:
      type: string
      pattern: "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
      description: "Azure subscription ID in UUID format"
    resource_group:
      type: string
      minLength: 1
      maxLength: 90
```

---

### `model`
- **Type:** `string` or `object`
- **Description:** Specifies which LLM to use for this agent. Overrides the global `COPILOT_MODEL` environment variable
- **Default:** Value of `COPILOT_MODEL` env var, or `"claude-sonnet-4"`

**Simple syntax:**
```yaml
model: gpt-4o
```

**Advanced syntax with parameters:**
```yaml
model:
  name: claude-sonnet-4
  temperature: 0.7
  max_tokens: 4000
```

**Use cases:**
- Use faster/cheaper models for simple tasks
- Use advanced models for complex reasoning
- Tune temperature for creative vs deterministic outputs

---

### `timeout`
- **Type:** `number`
- **Description:** Maximum execution time in seconds for the agent. Overrides the global `COPILOT_AGENT_TIMEOUT` environment variable
- **Default:** Value of `COPILOT_AGENT_TIMEOUT` env var, or `900` (15 minutes)

**Example:**
```yaml
timeout: 300  # 5 minutes
```

**Use case:** Prevent long-running agents from consuming excessive resources

---

### `tools`
- **Type:** `object`
- **Description:** Controls which tools are available to the agent. By default, all tools from `tools/` directory and built-in tools are loaded
- **Structure:**

```yaml
tools:
  include: string[]      # Optional. Only load these specific tools
  exclude: string[]      # Optional. Block these tools from being loaded
  only_custom: boolean   # Optional. If true, only load custom tools from tools/, no built-ins
```

**Examples:**

Include only specific tools:
```yaml
tools:
  include: ["azure_rest", "send_email"]
```

Exclude specific tools:
```yaml
tools:
  exclude: ["web_fetch", "bash", "execute_shell"]
```

Only custom tools:
```yaml
tools:
  only_custom: true
```

**Use cases:**
- Security: Restrict tool access for sensitive agents
- Performance: Reduce function schema size
- Clarity: Make agent capabilities explicit

---

### `mcp`
- **Type:** `array` or `object`
- **Description:** MCP servers configuration. In `agents.app.yaml`, defines all available MCP servers. In agent front matter, acts as an allow-list filter to select which global MCP servers this agent can use.
- **Typical usage:** Global config defines all servers; agent front matter filters to specific servers
- **Default:** All servers defined in `agents.app.yaml` or `mcp.json`

**Global configuration (`agents.app.yaml`) - Define all available servers:**
```yaml
mcp:
  - microsoft-learn
  - azure-devops
  - custom-api
```

**Agent front matter - Filter to specific servers (allow-list):**
```yaml
mcp:
  - microsoft-learn  # Only load this MCP server for this agent
```

**Object syntax (inline definition, typically in global config):**
```yaml
mcp:
  custom-api:
    type: http
    url: https://api.example.com/mcp
    tools: ["search", "fetch"]
  local-tool:
    type: local
    command: python
    args: ["-m", "my_mcp_server"]
    tools: ["*"]
```

**Disable all MCP servers for an agent:**
```yaml
mcp: []
```

**Use cases:**
- **Global:** Define all available MCP servers once in `agents.app.yaml`
- **Agent:** Filter to only needed servers to reduce context and improve performance

---

### `skills`
- **Type:** `array` or `boolean`
- **Description:** Skills configuration. In `agents.app.yaml`, defines all available skills. In agent front matter, acts as an allow-list filter to select which global skills this agent should load.
- **Typical usage:** Global config defines all skills; agent front matter filters to relevant skills
- **Default:** All skills defined in `agents.app.yaml` or auto-discovered from `skills/` directory

**Global configuration (`agents.app.yaml`) - Define all available skills:**
```yaml
skills:
  - azure-resources
  - cost-optimization
  - security-review
```

**Agent front matter - Filter to specific skills (allow-list):**
```yaml
skills:
  - azure-resources  # Only load this skill for this agent
```

**Disable all skills for an agent:**
```yaml
skills: []
# or
skills: false
```

**Use cases:**
- **Global:** Define all available skills once in `agents.app.yaml`
- **Agent:** Focus agent context on relevant domain knowledge only

---

### `retry`
- **Type:** `object`
- **Description:** Configures automatic retry behavior for failed agent executions
- **Default:** No automatic retries

**Structure:**
```yaml
retry:
  max_attempts: number     # Required. Maximum number of retry attempts
  backoff: string          # Optional. "linear" or "exponential". Default: "exponential"
  retry_on: string[]       # Optional. Conditions to retry on. Default: ["timeout", "error"]
  initial_delay: number    # Optional. Initial delay in seconds. Default: 1
  max_delay: number        # Optional. Maximum delay in seconds. Default: 60
```

**Example:**
```yaml
retry:
  max_attempts: 3
  backoff: exponential
  retry_on: ["timeout", "rate_limit", "service_unavailable"]
  initial_delay: 2
  max_delay: 30
```

**Use cases:**
- Resilience against transient failures
- Handling rate limits
- Dealing with unreliable external services

---

### `metadata`
- **Type:** `object`
- **Description:** Additional metadata for organization, discoverability, and governance
- **Fields are free-form** but common patterns include:

```yaml
metadata:
  version: string          # Semantic version of the agent
  owner: string           # Team or individual responsible
  tags: string[]          # Categorization tags
  documentation_url: string
  support_contact: string
```

**Example:**
```yaml
metadata:
  version: "1.2.0"
  owner: "platform-team@company.com"
  tags: ["production", "cost-optimization", "azure"]
  documentation_url: "https://wiki.company.com/agents/cost-optimizer"
  support_contact: "platform-team-slack"
```

**Use cases:**
- Agent lifecycle management
- Searchability and categorization
- Compliance and governance
- Support and ownership tracking

---

## Environment Variable Substitution

Use `$VARIABLE_NAME` syntax in any field value for runtime substitution from app settings or environment variables.

**Common patterns:**
- `$ACA_SESSION_POOL_ENDPOINT` — Session pool endpoint
- `$SUBSCRIPTION_ID` — Azure subscription ID
- `$O365_CONNECTION_ID` — Office 365 connection resource ID
- `$TO_EMAIL` — Recipient email address
- `$STORAGE_CONNECTION` — Storage account connection string

### Configuration Precedence

Some fields support both environment variables and front matter configuration. The order of precedence (highest to lowest):

1. **Front matter value** — Explicit value in the `.agent.md` file
2. **Environment variable** — Global configuration via app settings
3. **Default value** — Built-in framework default

**Examples:**

**Model selection:**
1. `model: gpt-4o` in front matter (highest priority)
2. `COPILOT_MODEL` environment variable
3. `claude-sonnet-4` (default)

**Timeout:**
1. `timeout: 300` in front matter (highest priority)
2. `COPILOT_AGENT_TIMEOUT` environment variable
3. `900` seconds / 15 minutes (default)

---

## Complete Examples

### Example 1: Multi-Agent Application with Global Configuration

**Global Configuration (`agents.app.yaml`):**
```yaml
# Shared infrastructure
execution_sandbox:
  session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

tools_from_connections:
  - connection_id: $O365_CONNECTION_ID

# Global defaults
model: gpt-4o
timeout: 900

# Available MCP servers
mcp:
  - microsoft-learn
  - azure-devops

# Available skills
skills:
  - azure-resources
  - cost-optimization
  - security-review

# Global tool configuration
tools:
  exclude: ["bash", "execute_shell"]

# Default retry behavior
retry:
  max_attempts: 3
  backoff: exponential
  retry_on: ["timeout", "error"]
```

**Chat Agent (`main.agent.md`):**
```yaml
---
name: Chat Assistant
description: A helpful assistant with Python code execution capabilities
---

You are a helpful assistant. If you need to get up to date information, browse the web for it.
```

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
  - azure-resources  # Only load azure-resources skill

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
  - microsoft-learn  # Only load Microsoft Learn MCP server

skills:
  - azure-resources  # Only load azure-resources skill
---

When triggered, list all resources in subscription $SUBSCRIPTION_ID, filter for changes in the last 24 hours, and email a report to $TO_EMAIL.
```

### Example 2: Simple Single-Agent Application

**Global Configuration (`agents.app.yaml`):**
```yaml
execution_sandbox:
  session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT

model:
  name: claude-sonnet-4
  temperature: 0.7

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

### Example 3: Agent with Overrides

**Global Configuration (`agents.app.yaml`):**
```yaml
model: gpt-4o
timeout: 900

mcp:
  - microsoft-learn
  - azure-devops

skills:
  - azure-resources
```

**Agent with Overrides (`fast_agent.agent.md`):**
```yaml
---
name: Fast Agent
description: A fast agent that uses a different model

trigger:
  type: http_trigger

model: gpt-4o-mini  # Override global model
timeout: 60         # Override global timeout

mcp: []            # Disable all MCP servers
skills: false      # Disable all skills
---

You are a fast agent optimized for simple queries.
```

### Example 4: Minimal Configuration

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
13. **MCP server references:** Servers in `mcp` array must be defined in `agents.app.yaml`, `mcp.json`, or inline
14. **Skill references:** Skills in `skills` array must exist in `agents.app.yaml` or as directories under `skills/`
15. **Retry attempts:** `retry.max_attempts` must be >= 1 and <= 10 (recommended)
16. **Retry backoff:** `retry.backoff` must be either `"linear"` or `"exponential"`
17. **Configuration file location:** `agents.app.yaml` must be in the same directory as agent `.md` files (typically `src/`)

---

## File Naming Conventions

- **Global configuration:** `agents.app.yaml` (in `src/` directory)
- **Primary agent:** `main.agent.md` or `function_app.agent.md`
- **Named agents:** `{agent-name}.agent.md` (e.g., `daily_azure_report.agent.md`)
- **Skills:** `skills/{skill-name}/SKILL.md`

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

## Skills Front Matter

Skills use a simplified front matter structure:

```yaml
---
name: string        # Required. Skill name
description: string # Required. When this skill should be used
---
```

Skills contain domain-specific knowledge and are referenced by agents but don't have triggers or tool configurations.

---

## Implementation Status

This specification includes both currently implemented features and proposed enhancements. Implementation status by configuration area:

### ✅ Fully Implemented
- **Agent front matter:** `name`, `description`, `trigger` (all types)
- **Capabilities:** `execution_sandbox`, `tools_from_connections`
- **Output:** `response_example`, `response_schema`

### 🚧 In Development / Partial Implementation
- **Global configuration file:** `agents.app.yaml` support
- **Per-agent overrides:** `model`, `timeout` overrides in front matter
- **Allow-list filtering:** `mcp`, `skills` filtering in front matter
- **Tool configuration:** `tools` include/exclude patterns

### 📋 Proposed / Not Yet Implemented
- **Input validation:** `input_schema` for HTTP request validation
- **Reliability:** `retry` configuration with backoff strategies
- **Governance:** `metadata` fields (version, owner, tags)
- **Debug mode:** `enable-debug-http` for non-HTTP agents

**Note:** The global configuration pattern (`agents.app.yaml`) is the intended direction. During implementation, some fields may temporarily remain environment-variable-only before full front matter support is added.
- `retry` — Automatic retry behavior
- `metadata` — Organizational metadata

**Note:** Proposed fields represent the intended direction of the programming model. Implementation requires updates to the `azure-functions-agents` framework.

---

## Resources

- **JSON Schema:** [`front-matter-schema.json`](./front-matter-schema.json) — Formal schema for validation and editor support
- **Trigger Reference:** [`triggers.md`](./triggers.md) — Detailed documentation for all trigger types
- **Sample Projects:** [`../samples/`](../samples/) — Working examples demonstrating various agent patterns