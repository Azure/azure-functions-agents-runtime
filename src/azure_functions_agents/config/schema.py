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


class SubagentRef(BaseModel):
    """A coordinator's reference to one specialist agent it may delegate to.

    Object form only (no string shorthand) — see FRD 0006 §5 Decision #16.
    ``agent`` is the specialist's identity slug (its file stem, sanitized;
    see :mod:`azure_functions_agents._slug`). ``when`` is an optional
    routing hint surfaced to the coordinator model as the ``delegate_<slug>``
    tool's description; if omitted, the specialist's own ``description`` is
    used instead (resolved once the specialist is known, not here).
    """

    model_config = ConfigDict(extra="forbid")

    agent: str
    when: str | None = None

    @field_validator("agent")
    @classmethod
    def validate_agent(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("agent must be non-empty")
        return trimmed


class DynamicSessionsCodeInterpreterConfig(BaseModel):
    """Configuration for the ACA Dynamic Sessions-backed code interpreter."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str
    client_id: str | None = None


class WebRequestConfig(BaseModel):
    """Configuration for the built-in, default-on ``web_request`` system tool.

    v1 is intentionally minimal: an exact-host allowlist plus operator caps.
    See ``docs/frds/0005-web-request-system-tool.md`` for the full (future)
    surface — per-host auth, wildcard hosts, and redirect following are v2.
    """

    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str] | None = None
    require_https: bool = True
    timeout_seconds: float | None = None
    max_response_bytes: int | None = None
    max_request_bytes: int | None = None


class SystemToolsConfig(BaseModel):
    """Global system tool configuration shared across agents."""

    model_config = ConfigDict(extra="forbid")

    dynamic_sessions_code_interpreter: DynamicSessionsCodeInterpreterConfig | None = None
    web_request: WebRequestConfig | bool | None = None


class SystemToolsAgentOverride(BaseModel):
    """Agent-level system tool overrides, primarily sandbox opt-out."""

    model_config = ConfigDict(extra="forbid")

    dynamic_sessions_code_interpreter: bool | None = None
    web_request: bool | None = None


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
    workflows: dict[str, Any] | None = None
    subagents: list[SubagentRef] | None = None
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
    # Identity slug (derived from source_file's stem; see _slug.py). Defaulted
    # rather than required because many tests construct ResolvedAgent directly,
    # bypassing config.merge.compose(), which is the only code path that
    # actually computes it.
    slug: str = ""
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
    workflows: dict[str, Any] | None = None
    subagents: list[SubagentRef] = Field(default_factory=list)
    tools_disabled: bool = False
    skills_disabled: bool = False
    mcp_disabled: bool = False
    sandbox_config: DynamicSessionsCodeInterpreterConfig | None
    web_request_config: WebRequestConfig | None = None
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
