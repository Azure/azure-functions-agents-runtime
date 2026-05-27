"""Merge global and agent config into resolved runtime configuration."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from azure_functions_agents.client_manager.providers import PROVIDER_REGISTRY
from azure_functions_agents.config.schema import (
    AgentConfiguration,
    AgentSpec,
    DebugConfig,
    ExecuteInSessionsConfig,
    GlobalConfig,
    McpFilter,
    ResolvedAgent,
    SkillsFilter,
    ToolsFilter,
)

logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT = 900.0


def _json_merge_patch(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Apply RFC 7396 JSON Merge Patch of ``override`` onto ``base`` and return a new dict."""
    if override is None:
        return deepcopy(base)

    result = deepcopy(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _json_merge_patch(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


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


def _non_empty_provider_sub_blocks(configuration: dict[str, Any] | None) -> list[str]:
    if configuration is None:
        return []

    return [
        provider
        for provider in PROVIDER_REGISTRY
        if isinstance(configuration.get(provider), dict) and bool(configuration.get(provider))
    ]


def _compose_agent_configuration(
    spec: AgentSpec,
    global_config: GlobalConfig,
) -> AgentConfiguration:
    agent_config = spec.agent_configuration
    global_agent_config = global_config.agent_configuration

    if agent_config is None and global_agent_config is None:
        raise ValueError(
            f"Agent {spec.name!r} must declare agent_configuration either at the global "
            "level or per-agent level."
        )

    base_payload = (
        global_agent_config.model_dump(exclude_unset=False, exclude_none=False)
        if global_agent_config is not None
        else {}
    )
    merged_payload = _json_merge_patch(base_payload, agent_config)

    global_provider = global_agent_config.provider if global_agent_config is not None else None
    explicit_provider = (
        merged_payload.get("provider")
        if agent_config is not None and "provider" in agent_config
        else None
    )
    provider = explicit_provider if isinstance(explicit_provider, str) else None
    agent_provider_blocks = _non_empty_provider_sub_blocks(agent_config)
    if provider is None:
        provider = (
            agent_provider_blocks[0]
            if len(agent_provider_blocks) == 1
            else global_provider
        )

    if provider is None:
        raise ValueError(
            f"Agent {spec.name!r} could not resolve agent_configuration.provider."
        )

    agent_switches_provider = (
        global_provider is not None
        and provider != global_provider
        and agent_config is not None
        and ("provider" in agent_config or provider in agent_provider_blocks)
    )
    if agent_switches_provider:
        assert global_provider is not None
        logger.debug(
            "Agent %s overrides global provider %r with %r; dropping the global "
            "provider sub-block during merge.",
            spec.name,
            global_provider,
            provider,
        )
        merged_payload.pop(global_provider, None)

    merged_payload["provider"] = provider

    try:
        return AgentConfiguration.model_validate(merged_payload)
    except ValidationError as exc:
        raise ValueError(str(exc.errors(include_input=False))) from None


def _resolve_sandbox(
    spec: AgentSpec, global_config: GlobalConfig
) -> ExecuteInSessionsConfig | None:
    if spec.system_tools and spec.system_tools.execute_in_sessions is False:
        return None
    if global_config.system_tools:
        return global_config.system_tools.execute_in_sessions
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

    agent_configuration = _compose_agent_configuration(spec, global_config)

    resolved = ResolvedAgent(
        name=spec.name,
        description=spec.description,
        trigger=spec.trigger,
        instructions=spec.instructions,
        is_main=spec.is_main,
        debug=_resolve_debug(spec),
        agent_configuration=agent_configuration,
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
