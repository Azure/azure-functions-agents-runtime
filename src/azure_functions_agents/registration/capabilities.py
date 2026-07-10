"""Capability filtering for resolved agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .._function_tool import WorkflowTool
from ..config import ResolvedAgent
from ..discovery.mcp import MCPTool


@dataclass
class AgentCapabilities:
    """Resolved capability bundle for one agent — passed through to the runner."""

    filtered_user_tools: list[Any] | None = None
    filtered_workflow_tools: list[WorkflowTool] = field(default_factory=list)
    filtered_mcp_tools: list[MCPTool] | None = None
    enabled_skill_paths: list[Path] = field(default_factory=list)


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
    )
