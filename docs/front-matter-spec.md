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

## Configuration Files

### Global Configuration (`agents.app.yaml`)
Optional file in `src/` directory that defines shared settings for all agents. Use this to set defaults and shared infrastructure (execution sandbox, connectors, model, timeout, etc.).

**Key principle:** Any field in `agents.app.yaml` can be overridden in individual agent front matter.

### Agent Configuration (`.agent.md` front matter)
YAML front matter at the top of each agent file. Use this for agent-specific settings, trigger configuration, and to override global defaults.

**File structure:**
```
src/
  agents.app.yaml          # Optional: Global defaults
  *.agent.md               # Agent with front matter
  ...
```

---

## Field Reference

**All fields** documented below can be used in either `agents.app.yaml` (global defaults) or `.agent.md` front matter (agent-specific). Agent front matter always takes precedence.

Each field includes:
- **Typical location:** Where this field is commonly used
- **Can override:** Whether agent front matter can override global config

### Required Fields

These fields are **required in every `.agent.md` file** and cannot be set globally.

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

#### `enable-debug-http`
- **Type:** `boolean`
- **Typical location:** Agent only
- **Can override:** N/A (agent-specific only)
- **Default:** `false`
- **Description:** Automatically create an HTTP debug endpoint for non-HTTP agents (timer, queue, etc.). Alternative to defining an explicit HTTP trigger.
- **Applies to:** Non-HTTP triggered agents

**Example:**
```yaml
enable-debug-http: true  # Creates debug HTTP endpoint for testing
```

---

#### `model`
- **Type:** `string` or `object`
- **Typical location:** Global (shared default) or Agent (override)
- **Can override:** Yes
- **Default:** Value of `COPILOT_MODEL` env var, or `"claude-sonnet-4"`
- **Description:** Specifies which LLM to use. Agent value overrides global value, which overrides env var.

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

---

#### `timeout`
- **Type:** `number`
- **Typical location:** Global (shared default) or Agent (override)
- **Can override:** Yes
- **Default:** Value of `COPILOT_AGENT_TIMEOUT` env var, or `900` (15 minutes)
- **Description:** Maximum execution time in seconds for the agent.

**Example:**
```yaml
timeout: 300  # 5 minutes
```

---

#### `execution_sandbox`
- **Type:** `object`
- **Typical location:** Global (shared infrastructure) or Agent (override)
- **Can override:** Yes
- **Description:** Configures Python code execution environment using Azure Container Apps dynamic sessions

**Example:**
```yaml
execution_sandbox:
  session_pool_management_endpoint: $ACA_SESSION_POOL_ENDPOINT
```

---

#### `tools_from_connections`
- **Type:** `array`
- **Typical location:** Global (shared infrastructure) or Agent (override)
- **Can override:** Yes
- **Description:** Loads connector-based tools (e.g., Office 365, Outlook, SharePoint) from Azure Logic App connectors

**Example:**
```yaml
tools_from_connections:
  - connection_id: $O365_CONNECTION_ID
  - connection_id: $OUTLOOK_CONNECTION_ID
```

---

#### `tools`
- **Type:** `object`
- **Typical location:** Global (shared default) or Agent (override)
- **Can override:** Yes
- **Description:** Controls which tools are available. By default, all tools from `tools/` directory and built-in tools are loaded.

**Structure:**
```yaml
tools:
  include: string[]      # Optional. Only load these specific tools
  exclude: string[]      # Optional. Block these tools from being loaded
  only_custom: boolean   # Optional. If true, only load custom tools from tools/, no built-ins
```

**Examples:**
```yaml
# Include only specific tools
tools:
  include: ["azure_rest", "send_email"]

# Exclude specific tools
tools:
  exclude: ["web_fetch", "bash", "execute_shell"]

# Only custom tools
tools:
  only_custom: true
```

---

#### `mcp`
- **Type:** `array` or `object`
- **Typical location:** Global (all available servers) or Agent (allow-list filter)
- **Can override:** Yes (agent value filters/overrides global)
- **Description:** MCP servers configuration. In `agents.app.yaml`, defines all available MCP servers. In agent front matter, acts as an allow-list filter.

**Global configuration - Define all available servers:**
```yaml
mcp:
  - microsoft-learn
  - azure-devops
```

**Agent front matter - Filter to specific servers:**
```yaml
mcp:
  - microsoft-learn  # Only load this MCP server
```

**Object syntax (inline definition):**
```yaml
mcp:
  custom-api:
    type: http
    url: https://api.example.com/mcp
```

---

#### `skills`
- **Type:** `array` or `boolean`
- **Typical location:** Global (all available skills) or Agent (allow-list filter)
- **Can override:** Yes (agent value filters/overrides global)
- **Description:** Skills configuration. In `agents.app.yaml`, defines all available skills. In agent front matter, acts as an allow-list filter.

**Global configuration - Define all available skills:**
```yaml
skills:
  - azure-resources
  - cost-optimization
```

**Agent front matter - Filter to specific skills:**
```yaml
skills:
  - azure-resources  # Only load this skill
```

**Disable all skills:**
```yaml
skills: false
# or
skills: []
```

---

#### `retry`
- **Type:** `object`
- **Typical location:** Global (shared default) or Agent (override)
- **Can override:** Yes
- **Default:** No automatic retries
- **Description:** Configures automatic retry behavior for failed agent executions

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
```

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

**Chat Agent (`chat.agent.md`):**
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

## Resources

- **Trigger Reference:** [`triggers.md`](./triggers.md) — Detailed documentation for all trigger types
- **Sample Projects:** [`../samples/`](../samples/) — Working examples demonstrating various agent patterns