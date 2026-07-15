#!/usr/bin/env python3
"""Generate docs/front-matter-reference.md from Pydantic schema models.

This script introspects the Pydantic models in src/azure_functions_agents/config/schema.py
and generates a markdown API reference document.

Usage:
    python eng/scripts/generate_config_reference.py [--check]

    --check: Verify the generated docs match the committed version (exits non-zero if different)
"""

import argparse
import sys
from pathlib import Path
from typing import Any, get_args, get_origin

# Add src to path for imports - import schema module directly to avoid package side effects
repo_root = Path(__file__).parent.parent.parent
schema_module_path = repo_root / "src" / "azure_functions_agents" / "config"
sys.path.insert(0, str(schema_module_path))

# Import directly from schema module to avoid triggering full package imports
import importlib.util
spec = importlib.util.spec_from_file_location("schema", schema_module_path / "schema.py")
schema = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schema)

from pydantic import BaseModel
from pydantic.fields import FieldInfo

# Extract models from the dynamically loaded schema module
AgentSpec = schema.AgentSpec
BuiltinEndpointsConfig = schema.BuiltinEndpointsConfig
DynamicSessionsCodeInterpreterConfig = schema.DynamicSessionsCodeInterpreterConfig
GlobalConfig = schema.GlobalConfig
McpFilter = schema.McpFilter
SkillsFilter = schema.SkillsFilter
SystemToolsAgentOverride = schema.SystemToolsAgentOverride
SystemToolsConfig = schema.SystemToolsConfig
ToolsFilter = schema.ToolsFilter
TriggerSpec = schema.TriggerSpec
TRIGGER_TYPES = schema.TRIGGER_TYPES
WebRequestConfig = schema.WebRequestConfig


def format_type(field_info: FieldInfo, field_name: str) -> str:
    """Format field type annotation as a readable string."""
    annotation = field_info.annotation

    # Handle Union types (e.g., str | None, bool | object)
    origin = get_origin(annotation)
    if origin is type(None) or (hasattr(annotation, "__origin__") and annotation.__origin__ is type(None)):
        # This shouldn't happen but handle it
        return "null"

    # Get string representation
    type_str = str(annotation)

    # Clean up ForwardRef patterns early
    import re
    type_str = re.sub(r"ForwardRef\('([^']+)'[^)]*\)", r"\1", type_str)
    
    # Clean up common patterns
    type_str = type_str.replace("typing.", "")
    type_str = type_str.replace("<class '", "").replace("'>", "")
    type_str = type_str.replace("builtins.", "")
    type_str = type_str.replace("NoneType", "None")

    # Handle nested model types FIRST (before other replacements) - replace with "object"
    model_class_names = [
        "BuiltinEndpointsConfig", "BuiltinEndpointegersConfig",  # Handle both (typo variant too)
        "DynamicSessionsCodeInterpreterConfig",
        "EndpointAuthConfig",
        "EntraAuthConfig",
        "McpFilter",
        "SkillsFilter",
        "ToolsFilter",
        "SystemToolsConfig",
        "SystemToolsAgentOverride",
        "TriggerSpec",
    ]
    for class_name in model_class_names:
        if class_name in type_str:
            # Found a model class - need to handle Union case
            if " | " in type_str:
                parts = type_str.split(" | ")
                new_parts = []
                for part in parts:
                    if any(cn in part for cn in model_class_names):
                        new_parts.append("object")
                    elif part not in ("None", "null"):
                        new_parts.append(part)
                type_str = " | ".join(new_parts)
            else:
                type_str = "object"
            break

    # Remove module prefixes (schema., etc.)
    type_str = re.sub(r'\b[a-z_]+\.', '', type_str)  # Remove all module prefixes
    
    # Clean up any remaining artifacts like trailing quotes and parentheses
    type_str = re.sub(r"['\",]+\s*is_class=True\)", "", type_str)
    type_str = type_str.replace("'", "").replace('"', '')

    # Simplify common types BEFORE handling unions
    type_str = type_str.replace("str", "string")
    type_str = type_str.replace("int", "integer")
    type_str = type_str.replace("float", "number")
    type_str = type_str.replace("bool", "boolean")
    type_str = type_str.replace("dict[string, Any]", "object")
    type_str = type_str.replace("list[string]", "string[]")
    type_str = type_str.replace("dict[str, Any]", "object")
    type_str = type_str.replace("list[str]", "string[]")

    # Handle Union types shown as X | Y
    if " | " in type_str:
        parts = type_str.split(" | ")
        # Remove None/null from unions and filter empty/whitespace parts
        parts = [p.strip() for p in parts if p.strip() and p.strip() not in ("None", "null")]
        
        if len(parts) == 0:
            # All parts were None/null - shouldn't happen but fallback
            type_str = "object"
        elif len(parts) == 1:
            # Only one type left after filtering None
            type_str = parts[0]
        else:
            # Multiple types - join with escaped pipe for markdown tables
            type_str = " \\| ".join(parts)

    return type_str


def get_default_value(field_info: FieldInfo, field_name: str = "", field_type: str = "") -> str:
    """Get default value as a string for documentation."""
    if field_info.is_required():
        return "N/A"

    # Check for default_factory first (Pydantic v2)
    if hasattr(field_info, 'default_factory') and field_info.default_factory is not None:
        factory = field_info.default_factory
        factory_str = str(factory)
        if "list" in factory_str or factory == list:
            return "`[]`"
        if "dict" in factory_str or factory == dict:
            return "`{}`"

    default = field_info.default
    
    # Handle Pydantic's special undefined value
    if default is not None and "Pydantic" in str(type(default)):
        # Check type to infer default
        if "[]" in field_type or "list" in field_type.lower() or field_name == "exclude":
            return "`[]`"
        if "object" in field_type.lower():
            return "`{}`"
        return "`{}`"  # Fallback for undefined objects
    
    if default is None:
        # For None defaults, infer semantic default from type
        # Arrays should default to []
        if "[]" in field_type or "list" in field_type.lower() or field_name == "exclude":
            return "`[]`"
        # Objects should default to {}
        if "object" in field_type.lower() or field_name in ("system_tools", "tools", "mcp", "skills"):
            return "`{}`"
        return "`null`"
    
    if callable(default):
        # Factory function - try to infer the result
        if "dict" in str(default):
            return "`{}`"
        if "list" in str(default):
            return "`[]`"
        return "N/A"
    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    if isinstance(default, str):
        return f'`"{default}"`'
    if isinstance(default, (int, float)):
        return f"`{default}`"
    return str(default)


def is_required(field_info: FieldInfo) -> bool:
    """Check if a field is required."""
    return field_info.is_required()


def generate_model_table(
    model: type[BaseModel], prefix: str = "", descriptions: dict[str, str] | None = None,
    custom_defaults: dict[str, str] | None = None
) -> list[str]:
    """Generate a markdown table for a Pydantic model's fields."""
    lines = []
    lines.append("| Property | Type | Required | Default | Description |")
    lines.append("|----------|------|----------|---------|-------------|")

    descriptions = descriptions or {}
    custom_defaults = custom_defaults or {}

    for field_name, field_info in model.model_fields.items():
        # Skip internal/computed fields
        if field_name in ("instructions", "source_file", "is_main", "enabled_mcp_names", 
                         "enabled_skills_names", "mcp_exclude_names", "skills_exclude_names",
                         "tool_exclude_names", "tool_filter", "tools_disabled", "skills_disabled",
                         "mcp_disabled", "sandbox_config"):
            continue

        field_type = format_type(field_info, field_name)
        required = "**Yes**" if is_required(field_info) else "No"
        
        # Use custom default if provided, otherwise compute it
        if field_name in custom_defaults:
            default = custom_defaults[field_name]
        else:
            default = get_default_value(field_info, field_name, field_type)
        
        description = descriptions.get(field_name, field_info.description or "")

        lines.append(f"| `{field_name}` | {field_type} | {required} | {default} | {description} |")

    return lines


# Custom descriptions for fields (enhances docstrings)
GLOBAL_CONFIG_DESCRIPTIONS = {
    "system_tools": "System-level tools configuration. [Details](#global-system_tools)",
    "model": "Default LLM model identifier for all agents",
    "timeout": "Default execution timeout in seconds",
    "tools": "Global tool filtering configuration. [Details](#global-tools)",
}

GLOBAL_CONFIG_DEFAULTS = {
    "system_tools": "`{}`",
    "model": "Resolved from env/provider",
    "timeout": "`900`",
    "tools": "`{}`",
}

SYSTEM_TOOLS_CONFIG_DESCRIPTIONS = {
    "dynamic_sessions_code_interpreter": "ACA Dynamic Sessions code interpreter configuration. [Details](#global-system_tools-dynamic_sessions_code_interpreter)",
    "web_request": "Outbound HTTP request tool configuration. Enabled by default; set to `false` to disable app-wide. [Details](#global-system_tools-web_request)",
}

DYNAMIC_SESSIONS_DESCRIPTIONS = {
    "endpoint": "ACA session pool endpoint URL. Supports env var substitution.",
    "client_id": "Optional managed identity client ID for multi-identity Function Apps",
}

WEB_REQUEST_DESCRIPTIONS = {
    "allowed_hosts": "Exact-match allowlist of hostnames the tool may call. Omit to allow any public host (still subject to the SSRF floor).",
    "require_https": "Require `https://` URLs. Set to `false` to also allow `http://`.",
    "timeout_seconds": "Per-request timeout in seconds, clamped to a runtime-defined ceiling (120 s).",
    "max_response_bytes": "Maximum response body size read before truncating, clamped to a runtime-defined ceiling (10 MB).",
    "max_request_bytes": "Maximum request body size accepted, clamped to a runtime-defined ceiling (10 MB).",
}

TOOLS_FILTER_DESCRIPTIONS = {
    "exclude": "Tool names to exclude globally from all agents",
}

AGENT_SPEC_REQUIRED_DESCRIPTIONS = {
    "name": "Display name for the agent. Does not control function name or route.",
    "description": "Brief description of the agent's purpose",
    "trigger": "Required unless at least one `builtin_endpoints` value is enabled. [Details](#agent-trigger)",
}

AGENT_SPEC_OPTIONAL_DESCRIPTIONS = {
    "builtin_endpoints": "Enable built-in chat UI, chat API, and/or MCP tool endpoints. [Details](#agent-builtin_endpoints)",
    "model": "Override LLM model for this agent",
    "timeout": "Override execution timeout (seconds) for this agent",
    "logger": "Enable/disable response logging for triggered agents",
    "substitute_variables": "Enable/disable environment variable substitution",
    "system_tools": "Opt out of system tools. [Details](#agent-system_tools)",
    "mcp": "MCP server filtering. [Details](#agent-mcp)",
    "skills": "Skill filtering. [Details](#agent-skills)",
    "tools": "Custom tool filtering. [Details](#agent-tools)",
    "workflows": "Dynamic Workflow enablement and filtering. [Details](./front-matter-spec.md#workflows)",
    "input_schema": "JSON Schema for HTTP request validation",
    "response_schema": "JSON Schema for response validation",
    "response_example": "Example response structure (multiline string)",
    "metadata": "Additional metadata for organization. Free-form.",
}

TRIGGER_SPEC_DESCRIPTIONS = {
    "type": "Trigger type identifier. See [Supported Trigger Types](#supported-trigger-types)",
    "args": "Type-specific configuration. See [Supported Trigger Types](#supported-trigger-types)",
}

BUILTIN_ENDPOINTS_DESCRIPTIONS = {
    "debug_chat_ui": "Enable browser-based chat UI at `/agents/{slug}/` plus backing chat APIs",
    "chat_api": "Enable REST API endpoints (`/agents/{slug}/chat`, `/agents/{slug}/chatstream`)",
    "mcp": "Expose agent as MCP tool on shared runtime MCP transport",
}

SYSTEM_TOOLS_AGENT_DESCRIPTIONS = {
    "dynamic_sessions_code_interpreter": "Set to `false` to opt out of code execution capabilities",
    "web_request": "Set to `false` to opt out of the default-on `web_request` tool for this agent",
}

MCP_FILTER_DESCRIPTIONS = {
    "exclude": "MCP server names to exclude. Must match servers in `mcp.json`.",
}

SKILLS_FILTER_DESCRIPTIONS = {
    "exclude": "Skill names to exclude. Matched against `SKILL.md` `name` field.",
}

AGENT_TOOLS_FILTER_DESCRIPTIONS = {
    "exclude": "Tool names to exclude (in addition to global excludes)",
}


def generate_markdown() -> str:
    """Generate the complete front-matter-reference.md content."""
    lines = [
        "<!-- AUTO-GENERATED FROM PYDANTIC MODELS - DO NOT EDIT MANUALLY -->",
        "<!-- Generated from: src/azure_functions_agents/config/schema.py -->",
        "<!-- To regenerate: python eng/scripts/generate_config_reference.py -->",
        "",
        "# Front Matter Reference",
        "",
        "API reference for Azure Functions agent configuration properties. For examples and detailed usage patterns, see [front-matter-spec.md](./front-matter-spec.md).",
        "",
        "---",
        "",
        "## Global Configuration (`agents.config.yaml`)",
        "",
        "Optional file in the root directory. All properties are optional.",
        "",
    ]

    # Global config table
    lines.extend(generate_model_table(GlobalConfig, descriptions=GLOBAL_CONFIG_DESCRIPTIONS, custom_defaults=GLOBAL_CONFIG_DEFAULTS))
    lines.extend(["", "### Global: `system_tools`", ""])
    lines.extend(generate_model_table(SystemToolsConfig, descriptions=SYSTEM_TOOLS_CONFIG_DESCRIPTIONS))
    lines.extend(["", "### Global: `system_tools.dynamic_sessions_code_interpreter`", ""])
    lines.extend(generate_model_table(DynamicSessionsCodeInterpreterConfig, descriptions=DYNAMIC_SESSIONS_DESCRIPTIONS))
    lines.extend(["", "### Global: `system_tools.web_request`", ""])
    lines.extend(generate_model_table(WebRequestConfig, descriptions=WEB_REQUEST_DESCRIPTIONS,
                                      custom_defaults={"allowed_hosts": "`null`"}))
    lines.extend(["", "### Global: `tools`", ""])
    lines.extend(generate_model_table(ToolsFilter, descriptions=TOOLS_FILTER_DESCRIPTIONS))

    lines.extend([
        "",
        "**See:** [Front Matter Spec - Global Configuration](./front-matter-spec.md#global-configuration-agentsconfigyaml)",
        "",
        "---",
        "",
        "## Agent Front Matter (`.agent.md`)",
        "",
        "YAML front matter at the top of each agent markdown file.",
        "",
        "### Required Properties",
        "",
    ])

    # Agent required properties
    lines.append("| Property | Type | Required | Default | Description |")
    lines.append("|----------|------|----------|---------|-------------|")
    for field in ["name", "description", "trigger"]:
        field_info = AgentSpec.model_fields[field]
        field_type = format_type(field_info, field)
        required = "**Conditional**" if field == "trigger" else "**Yes**"
        lines.append(f"| `{field}` | {field_type} | {required} | N/A | {AGENT_SPEC_REQUIRED_DESCRIPTIONS[field]} |")

    lines.extend(["", "### Optional Properties", ""])

    # Agent optional properties
    lines.append("| Property | Type | Required | Default | Description |")
    lines.append("|----------|------|----------|---------|-------------|")
    for field_name, field_info in AgentSpec.model_fields.items():
        if field_name in ("name", "description", "trigger", "instructions", "source_file", "is_main"):
            continue
        field_type = format_type(field_info, field_name)
        default = get_default_value(field_info)
        description = AGENT_SPEC_OPTIONAL_DESCRIPTIONS.get(field_name, "")
        
        # Special handling for inherited defaults
        if field_name == "model":
            default = "Inherited from global"
        elif field_name == "timeout":
            default = "Inherited from global"
        elif field_name == "logger":
            default = "`true`"
        elif field_name == "metadata":
            default = "`{}`"
        elif field_name == "builtin_endpoints":
            default = "`false`"
        elif field_name == "system_tools":
            default = "Inherited"
        elif field_name == "mcp":
            default = "`true` (inherit all)"
        elif field_name in ("skills", "tools"):
            default = "Inherit all"

        lines.append(f"| `{field_name}` | {field_type} | No | {default} | {description} |")

    # Agent trigger details
    lines.extend([
        "",
        "### Agent: `trigger`",
        "",
        "**Required** unless at least one `builtin_endpoints` value is enabled. Only one trigger per agent file.",
        "",
        "**Structure:**",
        "```yaml",
        "trigger:",
        "  type: <trigger_type>",
        "  args: <type_specific_configuration>",
        "```",
        "",
    ])
    lines.extend(generate_model_table(TriggerSpec, descriptions=TRIGGER_SPEC_DESCRIPTIONS))
    lines.extend([
        "",
        "**See:** [Front Matter Spec - trigger](./front-matter-spec.md#trigger), [Triggers Reference](./triggers.md)",
        "",
    ])

    # Agent builtin_endpoints details
    lines.extend([
        "### Agent: `builtin_endpoints`",
        "",
        "Enable built-in endpoints for interactive testing, programmatic access, and agent composition.",
        "",
        "**When set to `true`:** Enables all built-in endpoints (`debug_chat_ui`, `chat_api`, `mcp`)",
        "",
        "**When set to `false`:** Disables all built-in endpoints (default)",
        "",
        "**When set to object:**",
        "",
    ])
    lines.extend(generate_model_table(BuiltinEndpointsConfig, descriptions=BUILTIN_ENDPOINTS_DESCRIPTIONS))
    lines.extend([
        "",
        "**Note:** `debug_chat_ui: true` automatically enables `chat_api: true`",
        "",
        "**See:** [Front Matter Spec - builtin_endpoints](./front-matter-spec.md#builtin_endpoints)",
        "",
    ])

    # Agent system_tools details
    lines.extend([
        "### Agent: `system_tools`",
        "",
        "Opt out of system-level tools configured globally.",
        "",
    ])
    lines.extend(generate_model_table(SystemToolsAgentOverride, descriptions=SYSTEM_TOOLS_AGENT_DESCRIPTIONS))
    lines.extend([
        "",
        "**See:** [Front Matter Spec - system_tools](./front-matter-spec.md#system_tools)",
        "",
    ])

    # Agent mcp details
    lines.extend([
        "### Agent: `mcp`",
        "",
        "Filter MCP servers discovered from `mcp.json`.",
        "",
        "**When set to `true` or omitted:** Inherit all discovered MCP servers (default)",
        "",
        "**When set to `false`:** Disable all MCP servers for this agent",
        "",
        "**When set to object:**",
        "",
    ])
    lines.extend(generate_model_table(McpFilter, descriptions=MCP_FILTER_DESCRIPTIONS))
    lines.extend([
        "",
        "**See:** [Front Matter Spec - mcp](./front-matter-spec.md#mcp)",
        "",
    ])

    # Agent skills details
    lines.extend([
        "### Agent: `skills`",
        "",
        "Filter skills auto-discovered from `skills/` directory.",
        "",
        "**When omitted:** Inherit all discovered skills (default)",
        "",
        "**When set to `false`:** Disable all skills for this agent",
        "",
        "**When set to object:**",
        "",
    ])
    lines.extend(generate_model_table(SkillsFilter, descriptions=SKILLS_FILTER_DESCRIPTIONS))
    lines.extend([
        "",
        "**See:** [Front Matter Spec - skills](./front-matter-spec.md#skills)",
        "",
    ])

    # Agent tools details
    lines.extend([
        "### Agent: `tools`",
        "",
        "Filter custom tools auto-discovered from `tools/` directory.",
        "",
        "**When omitted:** Inherit all discovered tools (default)",
        "",
        "**When set to `false`:** Disable all custom tools for this agent",
        "",
        "**When set to object:**",
        "",
    ])
    lines.extend(generate_model_table(ToolsFilter, descriptions=AGENT_TOOLS_FILTER_DESCRIPTIONS))
    lines.extend([
        "",
        "**See:** [Front Matter Spec - tools](./front-matter-spec.md#tools)",
        "",
        "---",
        "",
        "## Supported Trigger Types",
        "",
        "Each trigger type has a required `type` field and an optional type-specific `args` object.",
        "",
    ])

    # Generate trigger types section
    for trigger_type, config in TRIGGER_TYPES.items():
        lines.append(f"### `{trigger_type}`")
        lines.append("")

        if "note" in config and not config["fields"]:
            lines.append(config["note"])
            lines.append("")
        elif config["fields"]:
            lines.append("| Property | Type | Required | Default | Description |")
            lines.append("|----------|------|----------|---------|-------------|")
            for field_name, (ftype, required, default, desc) in config["fields"].items():
                req_str = "**Yes**" if required else "No"
                lines.append(f"| `{field_name}` | {ftype} | {req_str} | {default} | {desc} |")
            lines.append("")
            if "note" in config:
                lines.append(config["note"])
                lines.append("")

    lines.extend([
        "**See:** [Front Matter Spec - trigger](./front-matter-spec.md#trigger), [Triggers Reference](./triggers.md)",
        "",
        "---",
        "",
        "## Configuration Precedence",
        "",
        "### Runtime Settings (model, timeout)",
        "",
        "Resolution order (first defined wins):",
        "1. Agent front matter (explicit override)",
        "2. Global `agents.config.yaml`",
        "3. Environment variables (`AZURE_FUNCTIONS_AGENTS_MODEL`, `AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS`)",
        "4. Provider-specific environment variables (for model only)",
        "5. Framework defaults",
        "",
        "### Capabilities (MCP servers, skills, tools)",
        "",
        "1. **Discovery:** Auto-discovered from `mcp.json`, `skills/`, and `tools/` directories",
        "2. **Global filtering:** Applied from `agents.config.yaml` (tools only)",
        "3. **Agent filtering:** Applied per-agent using exclude lists in front matter",
        "",
        "**See:** [Front Matter Spec - Configuration Precedence](./front-matter-spec.md#configuration-precedence)",
        "",
        "---",
        "",
        "## Environment Variable Substitution",
        "",
        "Applies to all string values in `agents.config.yaml`, `mcp.json`, and agent `.agent.md` files (front matter and markdown body).",
        "",
        "**Supported syntaxes:**",
        "- `$IDENT` — e.g., `$API_KEY`",
        "- `%IDENT%` — e.g., `%API_KEY%`",
        "",
        "**Escape sequences:**",
        "- `$$IDENT` → literal `$IDENT`",
        "- `%%IDENT%%` → literal `%IDENT%`",
        "",
        "**Identifier rules:** Must match `[A-Za-z_][A-Za-z0-9_]*`",
        "",
        "**Resolution:** `os.environ.get(IDENT, original_placeholder)`. Unset variables remain as literal placeholders.",
        "",
        "**Disable per-agent:** Set `substitute_variables: false` in agent front matter.",
        "",
        "**See:** [Front Matter Spec - Environment Variable Substitution](./front-matter-spec.md#environment-variable-substitution)",
        "",
        "---",
        "",
        "## Validation Rules",
        "",
        "### Required Properties",
        "",
        "**Agent Front Matter:**",
        "- `name` (always required)",
        "- `description` (always required)",
        "- `trigger` (required unless at least one `builtin_endpoints` value is enabled)",
        "",
        "**Global Configuration:**",
        "- No required properties (entire file is optional)",
        "",
        "### Key Constraints",
        "",
        "1. **One trigger per file** — Only one trigger can be specified per `.agent.md` file",
        "2. **Trigger structure** — Must have `type` field; `args` is optional for triggers with no configuration",
        "3. **CRON expressions** — Timer schedules must be valid NCRONTAB (6-field or 5-field with seconds prepended)",
        "4. **HTTP methods** — Must be valid HTTP verbs",
        "5. **Auth levels** — Must be one of: `anonymous`, `function`, `admin`",
        "6. **JSON Schema validation** — `input_schema` and `response_schema` must be valid JSON Schema",
        "7. **Exclude lists** — MCP server names must match `mcp.json` entries; tool and skill names are best-effort validated",
        "",
        "**See:** [Front Matter Spec - Validation Rules](./front-matter-spec.md#validation-rules)",
        "",
        "---",
        "",
        "## File Naming Conventions",
        "",
        "- **Global configuration:** `agents.config.yaml` (root directory)",
        "- **Agent files:** `{agent-name}.agent.md` (root or `agents/` folder)",
        "- **Skills:** `skills/{skill-name}/SKILL.md`",
        "- **Custom tools:** `tools/{tool-name}.py`",
        "",
        "### Function Name Resolution",
        "",
        "Two identifiers are derived from the agent filename (not from the `name` field):",
        "",
        "1. **Azure Function name** — Used for host indexing and admin APIs",
        "2. **Built-in endpoint slug** — Used for `/agents/{slug}/` routes and MCP tool names",
        "",
        "**Sanitization rules:**",
        "- Start with filename stem (remove `.agent.md`)",
        "- Replace characters outside `[A-Za-z0-9_]` with `_`",
        "- Trim leading/trailing underscores",
        "- Prefix `fn_` if result starts with a digit",
        "- Append `_2`, `_3`, etc. for collision resolution",
        "",
        '**Example:** `daily-report.agent.md` → function name `daily_report`, endpoint slug `daily_report`',
        "",
        "**See:** [Front Matter Spec - File Naming Conventions](./front-matter-spec.md#file-naming-conventions)",
        "",
        "---",
        "",
        "## Additional Resources",
        "",
        "- **[Front Matter Specification](./front-matter-spec.md)** — Complete guide with examples and patterns",
        "- **[Triggers Reference](./triggers.md)** — Detailed trigger documentation",
        "- **[Architecture](./architecture.md)** — System design and pipeline stages",
        "- **[Samples](../samples/)** — Working examples",
    ])

    return "\n".join(lines) + "\n"


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Generate front-matter-reference.md")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check if generated docs match committed version (CI mode)",
    )
    args = parser.parse_args()

    output_path = repo_root / "docs" / "front-matter-reference.md"
    generated_content = generate_markdown()

    if args.check:
        # Check mode: verify generated content matches committed version
        if not output_path.exists():
            print(f"ERROR: {output_path} does not exist", file=sys.stderr)
            return 1

        existing_content = output_path.read_text(encoding="utf-8")
        if existing_content != generated_content:
            print(
                f"ERROR: Generated docs do not match {output_path}",
                file=sys.stderr,
            )
            print("Run: python eng/scripts/generate_config_reference.py", file=sys.stderr)
            return 1

        print(f"OK: {output_path.relative_to(repo_root)} is up to date")
        return 0

    # Generate mode: write the file
    output_path.write_text(generated_content, encoding="utf-8")
    print(f"Generated {output_path.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
