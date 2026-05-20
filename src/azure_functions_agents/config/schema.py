"""Pydantic schemas for global, agent, and resolved runtime configuration."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class DebugConfig(BaseModel):
    """Concrete debug surface toggles for chat, HTTP, and MCP exposure."""

    model_config = ConfigDict(extra="forbid")

    chat: bool = False
    http: bool = False
    mcp: bool = False


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


class ExecuteInSessionsConfig(BaseModel):
    """Global sandbox execution configuration for execute-in-sessions tools."""

    model_config = ConfigDict(extra="forbid")

    session_pool_management_endpoint: str


class ToolsFromConnectionEntry(BaseModel):
    """A connection-backed tool discovery entry from agents.config.yaml."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str
    prefix: str | None = None


class SystemToolsConfig(BaseModel):
    """Global system tool configuration shared across agents."""

    model_config = ConfigDict(extra="forbid")

    execute_in_sessions: ExecuteInSessionsConfig | None = None
    tools_from_connections: list[ToolsFromConnectionEntry] = Field(default_factory=list)


class SystemToolsAgentOverride(BaseModel):
    """Agent-level system tool overrides, primarily sandbox opt-out."""

    model_config = ConfigDict(extra="forbid")

    execute_in_sessions: bool | None = None


class GlobalConfig(BaseModel):
    """Top-level agents.config.yaml schema."""

    model_config = ConfigDict(extra="forbid")

    mcp: list[str] = Field(default_factory=list)
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
    debug: bool | DebugConfig | None = None
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
    debug: DebugConfig
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
    sandbox_config: ExecuteInSessionsConfig | None
    connector_specs: list[ToolsFromConnectionEntry]
    input_schema: dict[str, Any] | None
    response_schema: dict[str, Any] | None
    response_example: str | None
    substitute_variables: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_file: str | None = None


GlobalConfig.model_rebuild()
AgentSpec.model_rebuild()
ResolvedAgent.model_rebuild()
