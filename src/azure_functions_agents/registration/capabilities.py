"""Capability filtering for resolved agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

from .._function_tool import WorkflowTool
from .._slug import delegate_tool_name
from ..config import ResolvedAgent
from ..discovery.mcp import MCPTool

# Hardcoded (not imported from system_tools.sandbox) to avoid pulling in
# that module's heavy optional deps (aiohttp, azure.identity) — matches the
# lazy-import convention used by `_build_web_request_tools` below. The
# sandbox tool's name is fixed as "execute_python" by its `@tool` decorator.
_SANDBOX_TOOL_NAME = "execute_python"


@dataclass
class AgentCapabilities:
    """Resolved capability bundle for one agent — passed through to the runner."""

    filtered_user_tools: list[Any] | None = None
    filtered_workflow_tools: list[WorkflowTool] = field(default_factory=list)
    filtered_mcp_tools: list[MCPTool] | None = None
    enabled_skill_paths: list[Path] = field(default_factory=list)
    web_request_tools: list[Any] | None = None


def _tool_name(tool: object) -> str:
    name = getattr(tool, "name", "") or ""
    return str(name)


def _filter_tools_by_name(tools: list[Any], exclude_names: set[str]) -> list[Any]:
    if not exclude_names:
        return list(tools)
    return [tool for tool in tools if _tool_name(tool) not in exclude_names]


def _workflows_enabled(resolved: ResolvedAgent) -> bool:
    block = resolved.workflows
    return isinstance(block, dict) and block.get("enabled") is True


def _workflow_exclude_names(resolved: ResolvedAgent) -> set[str]:
    block = resolved.workflows
    if not isinstance(block, dict):
        return set()
    raw = block.get("exclude")
    if not isinstance(raw, list):
        return set()
    return {name for name in raw if isinstance(name, str)}


def _build_web_request_tools(resolved: ResolvedAgent) -> list[Any]:
    """Build the (stateless) ``web_request`` tool once per agent, or ``[]`` when disabled."""
    if resolved.tools_disabled or resolved.web_request_config is None:
        return []
    # Imported lazily so registration import cost stays low when the tool is unused.
    web_request_module = import_module("azure_functions_agents.system_tools.web_request")
    return list(web_request_module.create_web_request_tools(resolved.web_request_config))


def build_capabilities(
    resolved: ResolvedAgent,
    *,
    discovered_user_tools: list[Any],
    discovered_workflow_tools: list[WorkflowTool] | None = None,
    discovered_mcp_tools: dict[str, MCPTool],
    discovered_skills: dict[str, Path],
) -> AgentCapabilities:
    """Apply resolved capability filters and return the final runner inputs."""
    exclude_names = set(resolved.tool_filter.exclude or [])

    if resolved.tools_disabled:
        filtered_user_tools: list[Any] = []
    else:
        filtered_user_tools = _filter_tools_by_name(list(discovered_user_tools), exclude_names)

    workflow_tools = list(discovered_workflow_tools or [])
    if getattr(resolved, "is_main", False) and _workflows_enabled(resolved):
        workflow_exclude_names = _workflow_exclude_names(resolved)
        filtered_workflow_tools = [
            tool for tool in workflow_tools if tool.name not in workflow_exclude_names
        ]
    else:
        filtered_workflow_tools = []

    if resolved.mcp_disabled:
        filtered_mcp_tools: list[MCPTool] = []
    else:
        filtered_mcp_tools = [
            discovered_mcp_tools[name]
            for name in resolved.enabled_mcp_names
            if name in discovered_mcp_tools
        ]

    if resolved.skills_disabled:
        enabled_skill_paths: list[Path] = []
    else:
        # Filter to enabled skills
        enabled_skill_paths = [
            discovered_skills[name]
            for name in resolved.enabled_skills_names
            if name in discovered_skills
        ]

    return AgentCapabilities(
        filtered_user_tools=filtered_user_tools,
        filtered_workflow_tools=filtered_workflow_tools,
        filtered_mcp_tools=filtered_mcp_tools,
        enabled_skill_paths=enabled_skill_paths,
        web_request_tools=_build_web_request_tools(resolved),
    )


def existing_tool_names(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> set[str]:
    """Collect every tool name already in play for ``resolved`` before delegation.

    Used by :func:`validate_subagent_tool_names` for fail-fast collision
    checks. Skill tools are excluded (not exposed as top-level tools by
    name). Covers each MCP server's configured name but not its individual
    remote functions (unknown at composition time) — MAF's ``Agent.run()``
    independently rejects those collisions once expanded.
    """
    names = {_tool_name(tool) for tool in capabilities.filtered_user_tools or []}
    names.update(_tool_name(tool) for tool in capabilities.filtered_mcp_tools or [])
    names.update(_tool_name(tool) for tool in capabilities.filtered_workflow_tools or [])
    names.update(_tool_name(tool) for tool in capabilities.web_request_tools or [])
    if resolved.sandbox_config is not None and not resolved.tools_disabled:
        names.add(_SANDBOX_TOOL_NAME)
    names.discard("")
    return names


def validate_subagent_tool_names(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> None:
    """Fail fast when a ``delegate_<slug>`` tool name would collide.

    Collisions between two different specialists' own ``delegate_<slug>``
    names are structurally impossible (agent slugs are globally unique —
    FRD 0007 §5 Decision #17), so this only needs to check the auto-derived
    name against the coordinator's *own* other tools.
    """
    if not resolved.subagents:
        return
    taken = existing_tool_names(resolved, capabilities)
    source_file = resolved.source_file or "<unknown>"
    for ref in resolved.subagents:
        tool_name = delegate_tool_name(ref.agent)
        if tool_name in taken:
            raise ValueError(
                f"{Path(source_file)}: field `subagents`: The auto-derived "
                f"tool name `{tool_name}` (for delegating to `{ref.agent}`) "
                "collides with an existing tool of the same name on this "
                "agent. Rename the colliding tool, or remove/rename the "
                "conflicting agent's source file, to resolve this. See "
                "docs/front-matter-spec.md#subagents."
            )
