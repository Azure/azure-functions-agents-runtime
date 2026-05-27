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


class DebugConfig(BaseModel):
    """Concrete debug endpoint toggles for chat UI, chat API, and MCP exposure."""

    model_config = ConfigDict(extra="forbid")

    chat_ui: bool = False
    chat_api: bool = False
    mcp: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_names(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if "chat" in normalized:
            if "chat_ui" in normalized:
                raise ValueError("Use either 'chat_ui' or deprecated 'chat', not both")
            normalized["chat_ui"] = normalized.pop("chat")
        if "http" in normalized:
            if "chat_api" in normalized:
                raise ValueError("Use either 'chat_api' or deprecated 'http', not both")
            normalized["chat_api"] = normalized.pop("http")
        return normalized

    @model_validator(mode="after")
    def chat_ui_requires_chat_api(self) -> DebugConfig:
        if self.chat_ui:
            self.chat_api = True
        return self

    @property
    def chat(self) -> bool:
        """Deprecated compatibility alias for ``chat_ui``."""
        return self.chat_ui

    @property
    def http(self) -> bool:
        """Deprecated compatibility alias for ``chat_api``."""
        return self.chat_api


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

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_endpoint(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if "session_pool_management_endpoint" in normalized:
            if "endpoint" in normalized:
                raise ValueError(
                    "Use either 'endpoint' or deprecated 'session_pool_management_endpoint', not both"
                )
            normalized["endpoint"] = normalized.pop("session_pool_management_endpoint")
        return normalized

    @property
    def session_pool_management_endpoint(self) -> str:
        """Deprecated compatibility alias for ``endpoint``."""
        return self.endpoint


class ExecuteInSessionsConfig(DynamicSessionsCodeInterpreterConfig):
    """Deprecated compatibility alias for dynamic sessions code interpreter config."""


class SystemToolsConfig(BaseModel):
    """Global system tool configuration shared across agents."""

    model_config = ConfigDict(extra="forbid")

    dynamic_sessions_code_interpreter: DynamicSessionsCodeInterpreterConfig | None = None
    execute_in_sessions: ExecuteInSessionsConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_conflicting_system_tool_names(cls, data: Any) -> Any:
        if isinstance(data, dict) and (
            "dynamic_sessions_code_interpreter" in data and "execute_in_sessions" in data
        ):
            raise ValueError(
                "Use either 'dynamic_sessions_code_interpreter' or deprecated "
                "'execute_in_sessions', not both"
            )
        return data

    @model_validator(mode="after")
    def normalize_legacy_execute_in_sessions(self) -> SystemToolsConfig:
        if self.execute_in_sessions is None:
            return self
        if self.dynamic_sessions_code_interpreter is not None:
            return self
        self.dynamic_sessions_code_interpreter = (
            DynamicSessionsCodeInterpreterConfig.model_validate(
                self.execute_in_sessions.model_dump()
            )
        )
        return self


class SystemToolsAgentOverride(BaseModel):
    """Agent-level system tool overrides, primarily sandbox opt-out."""

    model_config = ConfigDict(extra="forbid")

    dynamic_sessions_code_interpreter: bool | None = None
    execute_in_sessions: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_conflicting_system_tool_names(cls, data: Any) -> Any:
        if isinstance(data, dict) and (
            "dynamic_sessions_code_interpreter" in data and "execute_in_sessions" in data
        ):
            raise ValueError(
                "Use either 'dynamic_sessions_code_interpreter' or deprecated "
                "'execute_in_sessions', not both"
            )
        return data

    @model_validator(mode="after")
    def normalize_legacy_execute_in_sessions(self) -> SystemToolsAgentOverride:
        if self.execute_in_sessions is None:
            return self
        if self.dynamic_sessions_code_interpreter is not None:
            return self
        self.dynamic_sessions_code_interpreter = self.execute_in_sessions
        return self


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
    debug_endpoints: bool | DebugConfig | None = None
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

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_debug(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if "debug" in normalized:
            if "debug_endpoints" in normalized:
                raise ValueError("Use either 'debug_endpoints' or deprecated 'debug', not both")
            normalized["debug_endpoints"] = normalized.pop("debug")
        return normalized

    @property
    def debug(self) -> bool | DebugConfig | None:
        """Deprecated compatibility alias for ``debug_endpoints``."""
        return self.debug_endpoints


class ResolvedAgent(BaseModel):
    """Fully merged agent configuration consumed by registration/runtime layers."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    trigger: TriggerSpec | None
    instructions: str
    is_main: bool
    debug_endpoints: DebugConfig
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

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_debug(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if "debug" in normalized:
            if "debug_endpoints" in normalized:
                raise ValueError("Use either 'debug_endpoints' or deprecated 'debug', not both")
            normalized["debug_endpoints"] = normalized.pop("debug")
        return normalized

    @property
    def debug(self) -> DebugConfig:
        """Deprecated compatibility alias for ``debug_endpoints``."""
        return self.debug_endpoints


GlobalConfig.model_rebuild()
AgentSpec.model_rebuild()
ResolvedAgent.model_rebuild()
