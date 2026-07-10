"""Pydantic schemas for global, agent, and resolved runtime configuration."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class McpFilter(BaseModel):
    """Agent-level MCP exclude list override."""

    model_config = ConfigDict(extra="forbid")

    exclude: list[str] = Field(default_factory=list)


class SkillsFilter(BaseModel):
    """Agent-level skills exclude list override."""

    model_config = ConfigDict(extra="forbid")

    exclude: list[str] = Field(default_factory=list)


class ToolsFilter(BaseModel):
    """Agent/global tool filtering settings for discovered tools."""

    model_config = ConfigDict(extra="forbid")

    exclude: list[str] = Field(default_factory=list)


class BuiltinEndpointsConfig(BaseModel):
    """Concrete built-in endpoint toggles for debug chat UI, chat API, and MCP exposure."""

    model_config = ConfigDict(extra="forbid")

    debug_chat_ui: bool = False
    chat_api: bool = False
    mcp: bool = False

    @model_validator(mode="after")
    def debug_chat_ui_requires_chat_api(self) -> BuiltinEndpointsConfig:
        if self.debug_chat_ui:
            self.chat_api = True
        return self


class TriggerSpec(BaseModel):
    """Trigger definition parsed from agent frontmatter."""

    model_config = ConfigDict(extra="forbid")

    type: str
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("type must be non-empty")
        return trimmed


class DynamicSessionsCodeInterpreterConfig(BaseModel):
    """Configuration for the ACA Dynamic Sessions-backed code interpreter."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str
    client_id: str | None = None


class SystemToolsConfig(BaseModel):
    """Global system tool configuration shared across agents."""

    model_config = ConfigDict(extra="forbid")

    dynamic_sessions_code_interpreter: DynamicSessionsCodeInterpreterConfig | None = None


class SystemToolsAgentOverride(BaseModel):
    """Agent-level system tool overrides, primarily sandbox opt-out."""

    model_config = ConfigDict(extra="forbid")

    dynamic_sessions_code_interpreter: bool | None = None


class GlobalConfig(BaseModel):
    """Top-level agents.config.yaml schema."""

    model_config = ConfigDict(extra="forbid")

    system_tools: SystemToolsConfig | None = None
    model: str | None = None
    timeout: float | None = None
    tools: ToolsFilter | None = None


class AgentSpec(BaseModel):
    """Raw per-agent specification parsed from frontmatter plus markdown body."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    trigger: TriggerSpec | None = None
    builtin_endpoints: bool | BuiltinEndpointsConfig | None = None
    model: str | None = None
    timeout: float | None = None
    logger: bool | None = None
    substitute_variables: bool = True
    system_tools: SystemToolsAgentOverride | None = None
    mcp: bool | McpFilter | None = None
    skills: bool | SkillsFilter | None = None
    tools: bool | ToolsFilter | None = None
    input_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    response_example: str | None = None
    metadata: dict[str, Any] | None = None
    instructions: str = ""
    source_file: str | None = None
    is_main: bool = False


class ResolvedAgent(BaseModel):
    """Fully merged agent configuration consumed by registration/runtime layers."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    trigger: TriggerSpec | None
    instructions: str
    is_main: bool
    builtin_endpoints: BuiltinEndpointsConfig
    model: str | None
    timeout: float
    enabled_mcp_names: list[str]
    enabled_skills_names: list[str]
    mcp_exclude_names: list[str] = Field(default_factory=list)
    skills_exclude_names: list[str] = Field(default_factory=list)
    tool_exclude_names: list[str] = Field(default_factory=list)
    tool_filter: ToolsFilter
    tools_disabled: bool = False
    skills_disabled: bool = False
    mcp_disabled: bool = False
    sandbox_config: DynamicSessionsCodeInterpreterConfig | None
    input_schema: dict[str, Any] | None
    response_schema: dict[str, Any] | None
    response_example: str | None
    substitute_variables: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_file: str | None = None


GlobalConfig.model_rebuild()
AgentSpec.model_rebuild()
ResolvedAgent.model_rebuild()


# Trigger type documentation metadata
# Used by eng/scripts/generate_config_reference.py for generating trigger reference docs.
# Each trigger type maps to its field specifications and optional notes.
# Format: "trigger_type": {"fields": {field_name: (type, required, default, description)}, "note": "..."}
TRIGGER_TYPES: dict[str, dict[str, Any]] = {
    "http_trigger": {
        "fields": {
            "route": ("string", True, "N/A", "URL path for the HTTP endpoint"),
            "methods": ("string[]", False, '`["POST"]`', "Array of HTTP methods (GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS)"),
            "auth_level": ("string", False, '`"function"`', "One of: `anonymous`, `function`, `admin`"),
        }
    },
    "timer_trigger": {
        "fields": {
            "schedule": ("string", True, "N/A", "NCRONTAB expression (6 fields or 5 fields with seconds prepended)"),
        }
    },
    "queue_trigger": {
        "fields": {
            "queue_name": ("string", True, "N/A", "Azure Queue Storage queue name"),
            "connection": ("string", True, "N/A", "App setting or setting collection for connection"),
        }
    },
    "blob_trigger": {
        "fields": {
            "path": ("string", True, "N/A", 'Blob path pattern (e.g., `"uploads/{name}.txt"`)'),
            "connection": ("string", False, '`"AzureWebJobsStorage"`', "App setting name for connection string"),
        }
    },
    "event_grid_trigger": {
        "fields": {},
        "note": "No configuration properties. Receives Event Grid events.",
    },
    "service_bus_queue_trigger": {
        "fields": {
            "queue_name": ("string", True, "N/A", "Service Bus queue name"),
            "connection": ("string", True, "N/A", "App setting or setting collection for connection"),
        }
    },
    "service_bus_topic_trigger": {
        "fields": {
            "topic_name": ("string", True, "N/A", "Service Bus topic name"),
            "subscription_name": ("string", True, "N/A", "Service Bus subscription name"),
            "connection": ("string", True, "N/A", "App setting or setting collection for connection"),
        }
    },
    "connector_trigger": {
        "fields": {},
        "note": "No configuration properties. Receives Connector events.",
    },
}


# Field description metadata for documentation generation
# Used by eng/scripts/generate_config_reference.py to enhance generated docs.
# These descriptions complement or override Pydantic field metadata and may include
# markdown formatting and internal document links.

GLOBAL_CONFIG_DESCRIPTIONS: dict[str, str] = {
    "system_tools": "System-level tools configuration. [Details](#global-system_tools)",
    "model": "Default LLM model identifier for all agents",
    "timeout": "Default execution timeout in seconds",
    "tools": "Global tool filtering configuration. [Details](#global-tools)",
}

GLOBAL_CONFIG_DEFAULTS: dict[str, str] = {
    "system_tools": "`{}`",
    "model": "Resolved from env/provider",
    "timeout": "`900`",
    "tools": "`{}`",
}

SYSTEM_TOOLS_CONFIG_DESCRIPTIONS: dict[str, str] = {
    "dynamic_sessions_code_interpreter": "ACA Dynamic Sessions code interpreter configuration. [Details](#global-system_tools-dynamic_sessions_code_interpreter)",
}

DYNAMIC_SESSIONS_DESCRIPTIONS: dict[str, str] = {
    "endpoint": "ACA session pool endpoint URL. Supports env var substitution.",
    "client_id": "Optional managed identity client ID for multi-identity Function Apps",
}

TOOLS_FILTER_DESCRIPTIONS: dict[str, str] = {
    "exclude": "Tool names to exclude globally from all agents",
}

AGENT_SPEC_REQUIRED_DESCRIPTIONS: dict[str, str] = {
    "name": "Display name for the agent. Does not control function name or route.",
    "description": "Brief description of the agent's purpose",
    "trigger": "Required unless at least one `builtin_endpoints` value is enabled. [Details](#agent-trigger)",
}

AGENT_SPEC_OPTIONAL_DESCRIPTIONS: dict[str, str] = {
    "builtin_endpoints": "Enable built-in chat UI, chat API, and/or MCP tool endpoints. [Details](#agent-builtin_endpoints)",
    "model": "Override LLM model for this agent",
    "timeout": "Override execution timeout (seconds) for this agent",
    "logger": "Enable/disable response logging for triggered agents",
    "substitute_variables": "Enable/disable environment variable substitution",
    "system_tools": "Opt out of system tools. [Details](#agent-system_tools)",
    "mcp": "MCP server filtering. [Details](#agent-mcp)",
    "skills": "Skill filtering. [Details](#agent-skills)",
    "tools": "Custom tool filtering. [Details](#agent-tools)",
    "input_schema": "JSON Schema for HTTP request validation",
    "response_schema": "JSON Schema for response validation",
    "response_example": "Example response structure (multiline string)",
    "metadata": "Additional metadata for organization. Free-form.",
}

TRIGGER_SPEC_DESCRIPTIONS: dict[str, str] = {
    "type": "Trigger type identifier. See [Supported Trigger Types](#supported-trigger-types)",
    "args": "Type-specific configuration. See [Supported Trigger Types](#supported-trigger-types)",
}

BUILTIN_ENDPOINTS_DESCRIPTIONS: dict[str, str] = {
    "debug_chat_ui": "Enable browser-based chat UI at `/agents/{slug}/` plus backing chat APIs",
    "chat_api": "Enable REST API endpoints (`/agents/{slug}/chat`, `/agents/{slug}/chatstream`)",
    "mcp": "Expose agent as MCP tool on shared runtime MCP transport",
}

SYSTEM_TOOLS_AGENT_DESCRIPTIONS: dict[str, str] = {
    "dynamic_sessions_code_interpreter": "Set to `false` to opt out of code execution capabilities",
}

MCP_FILTER_DESCRIPTIONS: dict[str, str] = {
    "exclude": "MCP server names to exclude. Must match servers in `mcp.json`.",
}

SKILLS_FILTER_DESCRIPTIONS: dict[str, str] = {
    "exclude": "Skill names to exclude. Matched against `SKILL.md` `name` field.",
}

AGENT_TOOLS_FILTER_DESCRIPTIONS: dict[str, str] = {
    "exclude": "Tool names to exclude (in addition to global excludes)",
}
