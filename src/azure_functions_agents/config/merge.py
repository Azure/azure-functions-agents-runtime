"""Merge global and agent config into resolved runtime configuration."""

from __future__ import annotations

import os

from azure_functions_agents.config.schema import (
    AgentSpec,
    DebugConfig,
    ExecuteInSessionsConfig,
    GlobalConfig,
    McpFilter,
    ResolvedAgent,
    SkillsFilter,
    ToolsFilter,
    ToolsFromConnectionEntry,
)

DEFAULT_TIMEOUT = 900.0


def _resolve_debug(spec: AgentSpec) -> DebugConfig:
    if isinstance(spec.debug, DebugConfig):
        return spec.debug
    if spec.debug is True:
        return DebugConfig(chat=True, http=True, mcp=True)
    if spec.debug is False:
        return DebugConfig(chat=False, http=False, mcp=False)
    if spec.is_main:
        return DebugConfig(chat=True, http=True, mcp=True)
    return DebugConfig(chat=False, http=False, mcp=False)


def _resolve_model(spec: AgentSpec, global_config: GlobalConfig) -> str | None:
    return spec.model or global_config.model or os.environ.get("MAF_MODEL")


def _resolve_timeout(spec: AgentSpec, global_config: GlobalConfig) -> float:
    if spec.timeout is not None:
        return spec.timeout
    if global_config.timeout is not None:
        return global_config.timeout
    env_timeout = os.environ.get("AGENT_TIMEOUT")
    if env_timeout is not None:
        try:
            return float(env_timeout)
        except ValueError:
            pass
    return DEFAULT_TIMEOUT


def _resolve_sandbox(
    spec: AgentSpec, global_config: GlobalConfig
) -> ExecuteInSessionsConfig | None:
    if spec.system_tools and spec.system_tools.execute_in_sessions is False:
        return None
    if global_config.system_tools:
        return global_config.system_tools.execute_in_sessions
    return None


def _resolve_connectors(global_config: GlobalConfig) -> list[ToolsFromConnectionEntry]:
    if global_config.system_tools is None:
        return []
    return list(global_config.system_tools.tools_from_connections)


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
    custom_only = spec_tools.custom_only
    if global_tools_filter is not None:
        merged_excludes.update(global_tools_filter.exclude)
        custom_only = custom_only or global_tools_filter.custom_only
    return ToolsFilter(exclude=sorted(merged_excludes), custom_only=custom_only), False


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
        debug=_resolve_debug(spec),
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
        connector_specs=_resolve_connectors(global_config),
        input_schema=spec.input_schema,
        response_schema=spec.response_schema,
        response_example=spec.response_example,
        substitute_variables=spec.substitute_variables,
        metadata=metadata,
        source_file=spec.source_file,
    )

    return resolved
