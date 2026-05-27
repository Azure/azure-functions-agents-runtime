"""Merge global and agent config into resolved runtime configuration."""

from __future__ import annotations

from azure_functions_agents.config.env import runtime_env_value
from azure_functions_agents.config.schema import (
    AgentSpec,
    DebugConfig,
    DynamicSessionsCodeInterpreterConfig,
    GlobalConfig,
    McpFilter,
    ResolvedAgent,
    SkillsFilter,
    ToolsFilter,
)

DEFAULT_TIMEOUT = 900.0


def _resolve_debug(spec: AgentSpec) -> DebugConfig:
    debug_endpoints = spec.debug_endpoints
    if isinstance(debug_endpoints, DebugConfig):
        return debug_endpoints
    if debug_endpoints is True:
        if spec.is_main:
            return DebugConfig(chat_ui=True, chat_api=True, mcp=True)
        return DebugConfig(chat_ui=True, chat_api=True, mcp=False)
    if debug_endpoints is False:
        return DebugConfig(chat_ui=False, chat_api=False, mcp=False)
    if spec.is_main:
        return DebugConfig(chat_ui=True, chat_api=True, mcp=True)
    return DebugConfig(chat_ui=False, chat_api=False, mcp=False)


def _resolve_model(spec: AgentSpec, global_config: GlobalConfig) -> str | None:
    env_model = runtime_env_value("AZURE_FUNCTIONS_AGENTS_MODEL")
    return spec.model or global_config.model or env_model or None


def _resolve_timeout(spec: AgentSpec, global_config: GlobalConfig) -> float:
    if spec.timeout is not None:
        return spec.timeout
    if global_config.timeout is not None:
        return global_config.timeout
    env_timeout = runtime_env_value("AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS")
    if env_timeout:
        try:
            return float(env_timeout)
        except ValueError:
            pass
    return DEFAULT_TIMEOUT


def _resolve_sandbox(
    spec: AgentSpec, global_config: GlobalConfig
) -> DynamicSessionsCodeInterpreterConfig | None:
    if spec.system_tools and spec.system_tools.dynamic_sessions_code_interpreter is False:
        return None
    if global_config.system_tools:
        return global_config.system_tools.dynamic_sessions_code_interpreter
    return None


def apply_mcp_filter(
    global_mcp: list[str], spec_mcp: bool | McpFilter | None
) -> tuple[list[str], bool]:
    if spec_mcp is False:
        return [], True
    if spec_mcp is None or spec_mcp is True:
        return list(global_mcp), False
    excluded_names = set(spec_mcp.exclude)
    return ([name for name in global_mcp if name not in excluded_names], False)


def apply_skills_filter(
    discovered_skills: list[str], spec_skills: bool | SkillsFilter | None
) -> tuple[list[str], bool]:
    if spec_skills is False:
        return [], True
    if spec_skills is None or spec_skills is True:
        return list(discovered_skills), False
    excluded_names = set(spec_skills.exclude)
    return ([name for name in discovered_skills if name not in excluded_names], False)


def apply_tools_filter(
    spec_tools: bool | ToolsFilter | None,
    global_tools_filter: ToolsFilter | None,
) -> tuple[ToolsFilter, bool]:
    if spec_tools is False:
        return ToolsFilter(), True
    if spec_tools is None or spec_tools is True:
        if global_tools_filter is not None:
            return global_tools_filter.model_copy(deep=True), False
        return ToolsFilter(), False

    merged_excludes = set(spec_tools.exclude)
    if global_tools_filter is not None:
        merged_excludes.update(global_tools_filter.exclude)
    return ToolsFilter(exclude=sorted(merged_excludes)), False


def compose(
    spec: AgentSpec,
    global_config: GlobalConfig,
    *,
    discovered_mcp_names: list[str] | None = None,
    discovered_skill_names: list[str] | None = None,
) -> ResolvedAgent:
    """Top-level merge function called by the app orchestrator."""
    available_mcp = list(discovered_mcp_names or [])
    enabled_mcp, mcp_disabled = apply_mcp_filter(available_mcp, spec.mcp)

    skill_pool = list(discovered_skill_names or [])
    enabled_skills, skills_disabled = apply_skills_filter(skill_pool, spec.skills)

    tool_filter, tools_disabled = apply_tools_filter(spec.tools, global_config.tools)

    metadata = dict(spec.metadata or {})
    if spec.logger is not None:
        metadata["logger"] = spec.logger

    resolved = ResolvedAgent(
        name=spec.name,
        description=spec.description,
        trigger=spec.trigger,
        instructions=spec.instructions,
        is_main=spec.is_main,
        debug_endpoints=_resolve_debug(spec),
        model=_resolve_model(spec, global_config),
        timeout=_resolve_timeout(spec, global_config),
        enabled_mcp_names=enabled_mcp,
        enabled_skills_names=enabled_skills,
        mcp_exclude_names=list(spec.mcp.exclude) if isinstance(spec.mcp, McpFilter) else [],
        skills_exclude_names=list(spec.skills.exclude)
        if isinstance(spec.skills, SkillsFilter)
        else [],
        tool_exclude_names=list(tool_filter.exclude),
        tool_filter=tool_filter,
        tools_disabled=tools_disabled,
        skills_disabled=skills_disabled,
        mcp_disabled=mcp_disabled,
        sandbox_config=_resolve_sandbox(spec, global_config),
        input_schema=spec.input_schema,
        response_schema=spec.response_schema,
        response_example=spec.response_example,
        substitute_variables=spec.substitute_variables,
        metadata=metadata,
        source_file=spec.source_file,
    )

    return resolved
