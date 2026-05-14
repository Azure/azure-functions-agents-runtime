"""Capability filtering for resolved agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import ResolvedAgent
from ..discovery.mcp import MCPTool


@dataclass
class AgentCapabilities:
    """Resolved capability bundle for one agent — passed through to the runner."""

    filtered_user_tools: list[Any] | None = None
    filtered_mcp_tools: list[MCPTool] | None = None
    skills_text: str = ""
    use_connector_tools: bool = False


def _tool_name(tool: object) -> str:
    name = getattr(tool, "name", "") or ""
    return str(name)


def _filter_tools_by_name(tools: list[Any], exclude_names: set[str]) -> list[Any]:
    if not exclude_names:
        return list(tools)
    return [tool for tool in tools if _tool_name(tool) not in exclude_names]


def build_capabilities(
    resolved: ResolvedAgent,
    *,
    discovered_user_tools: list[Any],
    builtin_tools: list[Any],
    discovered_mcp_tools: dict[str, MCPTool],
    discovered_skills: dict[str, str],
) -> AgentCapabilities:
    """Apply resolved capability filters and return the final runner inputs."""
    exclude_names = set(resolved.tool_filter.exclude or [])

    if resolved.tools_disabled:
        filtered_user_tools: list[Any] = []
    elif resolved.tool_filter.custom_only:
        filtered_user_tools = _filter_tools_by_name(list(discovered_user_tools), exclude_names)
    else:
        filtered_user_tools = _filter_tools_by_name(
            list(discovered_user_tools) + list(builtin_tools),
            exclude_names,
        )

    if resolved.mcp_disabled:
        filtered_mcp_tools: list[MCPTool] = []
    else:
        filtered_mcp_tools = [
            discovered_mcp_tools[name]
            for name in resolved.enabled_mcp_names
            if name in discovered_mcp_tools
        ]

    if resolved.skills_disabled:
        skills_text = ""
    else:
        skills_parts = [
            f"## skill: {name}\n\n{discovered_skills[name].rstrip()}"
            for name in resolved.enabled_skills_names
            if name in discovered_skills
        ]
        skills_text = "\n\n".join(skills_parts)

    return AgentCapabilities(
        filtered_user_tools=filtered_user_tools,
        filtered_mcp_tools=filtered_mcp_tools,
        skills_text=skills_text,
        use_connector_tools=bool(resolved.connector_specs) and not resolved.tools_disabled,
    )
