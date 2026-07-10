<!-- AUTO-GENERATED FROM PYDANTIC MODELS - DO NOT EDIT MANUALLY -->
<!-- Generated from: src/azure_functions_agents/config/schema.py -->
<!-- To regenerate: python eng/scripts/generate_config_reference.py -->

# Front Matter Reference

API reference for Azure Functions agent configuration properties. For examples and detailed usage patterns, see [front-matter-spec.md](./front-matter-spec.md).

---

## Global Configuration (`agents.config.yaml`)

Optional file in the root directory. All properties are optional.

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `system_tools` | object | No | `{}` | System-level tools configuration. [Details](#global-system_tools) |
| `model` | string | No | Resolved from env/provider | Default LLM model identifier for all agents |
| `timeout` | number | No | `900` | Default execution timeout in seconds |
| `tools` | object | No | `{}` | Global tool filtering configuration. [Details](#global-tools) |

### Global: `system_tools`

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `dynamic_sessions_code_interpreter` | object | No | `{}` | ACA Dynamic Sessions code interpreter configuration. [Details](#global-system_tools-dynamic_sessions_code_interpreter) |

### Global: `system_tools.dynamic_sessions_code_interpreter`

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `endpoint` | string | **Yes** | N/A | ACA session pool endpoint URL. Supports env var substitution. |
| `client_id` | string | No | `null` | Optional managed identity client ID for multi-identity Function Apps |

### Global: `tools`

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `exclude` | string[] | No | `[]` | Tool names to exclude globally from all agents |

**See:** [Front Matter Spec - Global Configuration](./front-matter-spec.md#global-configuration-agentsconfigyaml)

---

## Agent Front Matter (`.agent.md`)

YAML front matter at the top of each agent markdown file.

### Required Properties

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `name` | string | **Yes** | N/A | Display name for the agent. Does not control function name or route. |
| `description` | string | **Yes** | N/A | Brief description of the agent's purpose |
| `trigger` | object | **Conditional** | N/A | Required unless at least one `builtin_endpoints` value is enabled. [Details](#agent-trigger) |

### Optional Properties

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `builtin_endpoints` | boolean \| object | No | `false` | Enable built-in chat UI, chat API, and/or MCP tool endpoints. [Details](#agent-builtin_endpoints) |
| `model` | string | No | Inherited from global | Override LLM model for this agent |
| `timeout` | number | No | Inherited from global | Override execution timeout (seconds) for this agent |
| `logger` | boolean | No | `true` | Enable/disable response logging for triggered agents |
| `substitute_variables` | boolean | No | `true` | Enable/disable environment variable substitution |
| `system_tools` | object | No | Inherited | Opt out of system tools. [Details](#agent-system_tools) |
| `mcp` | boolean \| object | No | `true` (inherit all) | MCP server filtering. [Details](#agent-mcp) |
| `skills` | boolean \| object | No | Inherit all | Skill filtering. [Details](#agent-skills) |
| `tools` | boolean \| object | No | Inherit all | Custom tool filtering. [Details](#agent-tools) |
| `workflows` | object | No | `null` | Dynamic Workflow enablement and filtering. [Details](./front-matter-spec.md#workflows) |
| `input_schema` | object | No | `null` | JSON Schema for HTTP request validation |
| `response_schema` | object | No | `null` | JSON Schema for response validation |
| `response_example` | string | No | `null` | Example response structure (multiline string) |
| `metadata` | object | No | `{}` | Additional metadata for organization. Free-form. |

### Agent: `trigger`

**Required** unless at least one `builtin_endpoints` value is enabled. Only one trigger per agent file.

**Structure:**
```yaml
trigger:
  type: <trigger_type>
  args: <type_specific_configuration>
```

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `type` | string | **Yes** | N/A | Trigger type identifier. See [Supported Trigger Types](#supported-trigger-types) |
| `args` | object | No | `{}` | Type-specific configuration. See [Supported Trigger Types](#supported-trigger-types) |

**See:** [Front Matter Spec - trigger](./front-matter-spec.md#trigger), [Triggers Reference](./triggers.md)

### Agent: `builtin_endpoints`

Enable built-in endpoints for interactive testing, programmatic access, and agent composition.

**When set to `true`:** Enables all built-in endpoints (`debug_chat_ui`, `chat_api`, `mcp`)

**When set to `false`:** Disables all built-in endpoints (default)

**When set to object:**

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `debug_chat_ui` | boolean | No | `false` | Enable browser-based chat UI at `/agents/{slug}/` plus backing chat APIs |
| `chat_api` | boolean | No | `false` | Enable REST API endpoints (`/agents/{slug}/chat`, `/agents/{slug}/chatstream`) |
| `mcp` | boolean | No | `false` | Expose agent as MCP tool on shared runtime MCP transport |

**Note:** `debug_chat_ui: true` automatically enables `chat_api: true`

**See:** [Front Matter Spec - builtin_endpoints](./front-matter-spec.md#builtin_endpoints)

### Agent: `system_tools`

Opt out of system-level tools configured globally.

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `dynamic_sessions_code_interpreter` | boolean | No | `null` | Set to `false` to opt out of code execution capabilities |

**See:** [Front Matter Spec - system_tools](./front-matter-spec.md#system_tools)

### Agent: `mcp`

Filter MCP servers discovered from `mcp.json`.

**When set to `true` or omitted:** Inherit all discovered MCP servers (default)

**When set to `false`:** Disable all MCP servers for this agent

**When set to object:**

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `exclude` | string[] | No | `[]` | MCP server names to exclude. Must match servers in `mcp.json`. |

**See:** [Front Matter Spec - mcp](./front-matter-spec.md#mcp)

### Agent: `skills`

Filter skills auto-discovered from `skills/` directory.

**When omitted:** Inherit all discovered skills (default)

**When set to `false`:** Disable all skills for this agent

**When set to object:**

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `exclude` | string[] | No | `[]` | Skill names to exclude. Matched against `SKILL.md` `name` field. |

**See:** [Front Matter Spec - skills](./front-matter-spec.md#skills)

### Agent: `tools`

Filter custom tools auto-discovered from `tools/` directory.

**When omitted:** Inherit all discovered tools (default)

**When set to `false`:** Disable all custom tools for this agent

**When set to object:**

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `exclude` | string[] | No | `[]` | Tool names to exclude (in addition to global excludes) |

**See:** [Front Matter Spec - tools](./front-matter-spec.md#tools)

---

## Supported Trigger Types

Each trigger type has a required `type` field and an optional type-specific `args` object.

### `http_trigger`

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `route` | string | **Yes** | N/A | URL path for the HTTP endpoint |
| `methods` | string[] | No | `["POST"]` | Array of HTTP methods (GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS) |
| `auth_level` | string | No | `"function"` | One of: `anonymous`, `function`, `admin` |

### `timer_trigger`

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `schedule` | string | **Yes** | N/A | NCRONTAB expression (6 fields or 5 fields with seconds prepended) |

### `queue_trigger`

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `queue_name` | string | **Yes** | N/A | Azure Queue Storage queue name |
| `connection` | string | **Yes** | N/A | App setting or setting collection for connection |

### `blob_trigger`

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `path` | string | **Yes** | N/A | Blob path pattern (e.g., `"uploads/{name}.txt"`) |
| `connection` | string | No | `"AzureWebJobsStorage"` | App setting name for connection string |

### `event_grid_trigger`

No configuration properties. Receives Event Grid events.

### `service_bus_queue_trigger`

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `queue_name` | string | **Yes** | N/A | Service Bus queue name |
| `connection` | string | **Yes** | N/A | App setting or setting collection for connection |

### `service_bus_topic_trigger`

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `topic_name` | string | **Yes** | N/A | Service Bus topic name |
| `subscription_name` | string | **Yes** | N/A | Service Bus subscription name |
| `connection` | string | **Yes** | N/A | App setting or setting collection for connection |

### `connector_trigger`

No configuration properties. Receives Connector events.

**See:** [Front Matter Spec - trigger](./front-matter-spec.md#trigger), [Triggers Reference](./triggers.md)

---

## Configuration Precedence

### Runtime Settings (model, timeout)

Resolution order (first defined wins):
1. Agent front matter (explicit override)
2. Global `agents.config.yaml`
3. Environment variables (`AZURE_FUNCTIONS_AGENTS_MODEL`, `AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS`)
4. Provider-specific environment variables (for model only)
5. Framework defaults

### Capabilities (MCP servers, skills, tools)

1. **Discovery:** Auto-discovered from `mcp.json`, `skills/`, and `tools/` directories
2. **Global filtering:** Applied from `agents.config.yaml` (tools only)
3. **Agent filtering:** Applied per-agent using exclude lists in front matter

**See:** [Front Matter Spec - Configuration Precedence](./front-matter-spec.md#configuration-precedence)

---

## Environment Variable Substitution

Applies to all string values in `agents.config.yaml`, `mcp.json`, and agent `.agent.md` files (front matter and markdown body).

**Supported syntaxes:**
- `$IDENT` — e.g., `$API_KEY`
- `%IDENT%` — e.g., `%API_KEY%`

**Escape sequences:**
- `$$IDENT` → literal `$IDENT`
- `%%IDENT%%` → literal `%IDENT%`

**Identifier rules:** Must match `[A-Za-z_][A-Za-z0-9_]*`

**Resolution:** `os.environ.get(IDENT, original_placeholder)`. Unset variables remain as literal placeholders.

**Disable per-agent:** Set `substitute_variables: false` in agent front matter.

**See:** [Front Matter Spec - Environment Variable Substitution](./front-matter-spec.md#environment-variable-substitution)

---

## Validation Rules

### Required Properties

**Agent Front Matter:**
- `name` (always required)
- `description` (always required)
- `trigger` (required unless at least one `builtin_endpoints` value is enabled)

**Global Configuration:**
- No required properties (entire file is optional)

### Key Constraints

1. **One trigger per file** — Only one trigger can be specified per `.agent.md` file
2. **Trigger structure** — Must have `type` field; `args` is optional for triggers with no configuration
3. **CRON expressions** — Timer schedules must be valid NCRONTAB (6-field or 5-field with seconds prepended)
4. **HTTP methods** — Must be valid HTTP verbs
5. **Auth levels** — Must be one of: `anonymous`, `function`, `admin`
6. **JSON Schema validation** — `input_schema` and `response_schema` must be valid JSON Schema
7. **Exclude lists** — MCP server names must match `mcp.json` entries; tool and skill names are best-effort validated

**See:** [Front Matter Spec - Validation Rules](./front-matter-spec.md#validation-rules)

---

## File Naming Conventions

- **Global configuration:** `agents.config.yaml` (root directory)
- **Agent files:** `{agent-name}.agent.md` (root or `agents/` folder)
- **Skills:** `skills/{skill-name}/SKILL.md`
- **Custom tools:** `tools/{tool-name}.py`

### Function Name Resolution

Two identifiers are derived from the agent filename (not from the `name` field):

1. **Azure Function name** — Used for host indexing and admin APIs
2. **Built-in endpoint slug** — Used for `/agents/{slug}/` routes and MCP tool names

**Sanitization rules:**
- Start with filename stem (remove `.agent.md`)
- Replace characters outside `[A-Za-z0-9_]` with `_`
- Trim leading/trailing underscores
- Prefix `fn_` if result starts with a digit
- Append `_2`, `_3`, etc. for collision resolution

**Example:** `daily-report.agent.md` → function name `daily_report`, endpoint slug `daily_report`

**See:** [Front Matter Spec - File Naming Conventions](./front-matter-spec.md#file-naming-conventions)

---

## Additional Resources

- **[Front Matter Specification](./front-matter-spec.md)** — Complete guide with examples and patterns
- **[Triggers Reference](./triggers.md)** — Detailed trigger documentation
- **[Architecture](./architecture.md)** — System design and pipeline stages
- **[Samples](../samples/)** — Working examples
