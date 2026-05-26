"""Pydantic schemas for global, agent, and resolved runtime configuration."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from azure_functions_agents.client_manager.providers import (
    PROVIDER_REGISTRY,
    AzureOpenAIConfig,
    FoundryConfig,
    OpenAIConfig,
    ProviderConfigBase,
)


def _normalize_optional_model_value(value: Any) -> Any:
    """Treat empty-string / whitespace-only ``model`` values as unset."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


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


class AgentConfiguration(BaseModel):
    """Provider-agnostic LLM runtime settings shared by global and agent config."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    provider: str
    model: str | None = None
    timeout: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    openai: OpenAIConfig | None = None
    azure_openai: AzureOpenAIConfig | None = None
    foundry: FoundryConfig | None = None

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        normalized = value.strip()
        if normalized in PROVIDER_REGISTRY:
            return normalized

        known = ", ".join(sorted(PROVIDER_REGISTRY))
        raise ValueError(
            f"Unknown provider {value!r}; known providers are: {known}"
        )

    @field_validator("model", mode="before")
    @classmethod
    def normalize_model(cls, value: Any) -> Any:
        return _normalize_optional_model_value(value)

    @model_validator(mode="after")
    def validate_provider_sub_block(self) -> AgentConfiguration:
        all_provider_keys = list(PROVIDER_REGISTRY)
        populated = [key for key in all_provider_keys if getattr(self, key) is not None]

        if self.provider not in populated:
            raise ValueError(
                f"agent_configuration.{self.provider} must be provided when provider is "
                f"{self.provider!r}"
            )

        extras = [key for key in populated if key != self.provider]
        if extras:
            extras_str = ", ".join(repr(k) for k in sorted(extras))
            raise ValueError(
                f"agent_configuration declares provider {self.provider!r} but also has "
                f"unrelated provider sub-block(s): {extras_str}. Only the sub-block "
                f"matching the declared provider is permitted."
            )
        return self

    @model_validator(mode="after")
    def validate_model_present(self) -> AgentConfiguration:
        if not self.model:
            raise ValueError("agent_configuration.model is required.")
        return self

    @property
    def provider_config(self) -> ProviderConfigBase:
        provider_config = getattr(self, self.provider, None)
        if not isinstance(provider_config, ProviderConfigBase):
            raise RuntimeError(
                f"Provider config for {self.provider!r} is unavailable after validation."
            )
        return provider_config


class GlobalConfig(BaseModel):
    """Top-level agents.config.yaml schema."""

    model_config = ConfigDict(extra="forbid")

    system_tools: SystemToolsConfig | None = None
    agent_configuration: AgentConfiguration | None = None
    tools: ToolsFilter | None = None


class AgentSpec(BaseModel):
    """Raw per-agent specification parsed from frontmatter plus markdown body."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    trigger: TriggerSpec | None = None
    debug: bool | DebugConfig | None = None
    agent_configuration: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Raw per-agent agent_configuration mapping. It is merged with global "
            "settings and validated after composition."
        ),
    )
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
    agent_configuration: AgentConfiguration
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
