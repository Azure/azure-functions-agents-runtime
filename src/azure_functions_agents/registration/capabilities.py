"""Capability filtering for resolved agents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from .._function_tool import WorkflowTool
from .._logger import logger
from .._slug import delegate_tool_name
from .._tool_descriptor import ToolDescriptor, ToolInput, describe_tools
from ..config import ResolvedAgent
from ..discovery.mcp import MCPServerDescriptor
from ..discovery.skills import SkillDescriptor, describe_skill_catalog, describe_skill_paths

if TYPE_CHECKING:
    from ..harness._harness_binding import AppHarness

# Hardcoded (not imported from system_tools.sandbox) to avoid pulling in
# that module's heavy optional deps (aiohttp, azure.identity) — matches the
# lazy-import convention used by `_build_web_request_tools` below. The
# sandbox tool's name is fixed as "execute_python" by its `@tool` decorator.
SANDBOX_TOOL_NAME = "execute_python"


@dataclass(frozen=True)
class AgentCapabilities:
    """Resolved capability bundle for one agent — passed through to the runner."""

    filtered_user_tools: tuple[ToolDescriptor, ...] | None = None
    filtered_workflow_tools: tuple[WorkflowTool, ...] = ()
    filtered_mcp_tools: tuple[MCPServerDescriptor, ...] | None = None
    enabled_skill_paths: tuple[Path, ...] = ()
    web_request_tools: tuple[ToolDescriptor, ...] | None = None
    skills: tuple[SkillDescriptor, ...] = ()
    skill_catalog: tuple[SkillDescriptor, ...] = ()
    _harness: AppHarness | None = field(default=None, repr=False, compare=False)

    @classmethod
    def create(
        cls,
        *,
        filtered_user_tools: Sequence[ToolInput] | None = None,
        filtered_workflow_tools: Sequence[WorkflowTool] = (),
        filtered_mcp_tools: Sequence[MCPServerDescriptor] | None = None,
        enabled_skill_paths: Sequence[Path] = (),
        web_request_tools: Sequence[ToolInput] | None = None,
        skills: Sequence[SkillDescriptor] | None = None,
        skill_catalog: Sequence[SkillDescriptor] = (),
        _harness: AppHarness | None = None,
    ) -> AgentCapabilities:
        approved = (
            describe_skill_paths(enabled_skill_paths) if skills is None else tuple(skills)
        )
        catalog = _merge_skill_descriptors(skill_catalog, approved)
        return cls(
            filtered_user_tools=(
                None if filtered_user_tools is None else describe_tools(filtered_user_tools)
            ),
            filtered_workflow_tools=tuple(filtered_workflow_tools),
            filtered_mcp_tools=(
                None if filtered_mcp_tools is None else tuple(filtered_mcp_tools)
            ),
            enabled_skill_paths=tuple(skill.path for skill in approved),
            web_request_tools=(
                None if web_request_tools is None else describe_tools(web_request_tools)
            ),
            skills=approved,
            skill_catalog=catalog,
            _harness=_harness,
        )


def _merge_skill_descriptors(
    discovered: Sequence[SkillDescriptor], approved: Sequence[SkillDescriptor]
) -> tuple[SkillDescriptor, ...]:
    catalog = list(discovered)
    for skill in approved:
        if skill not in catalog:
            catalog.append(skill)
    return tuple(catalog)


def _select_skill_roots(
    catalog: Sequence[SkillDescriptor], paths: Sequence[Path]
) -> tuple[SkillDescriptor, ...]:
    roots = tuple(path.resolve() for path in paths)
    return tuple(
        skill
        for path in roots
        for skill in catalog
        if skill.path == path
    )


def with_runtime_skill_paths(
    capabilities: AgentCapabilities,
    skill_paths: list[Path] | tuple[Path, ...],
) -> AgentCapabilities:
    """Return direct-role capabilities augmented with runtime-owned skills."""
    runtime_catalog = describe_skill_catalog(skill_paths)
    runtime_skills = _select_skill_roots(runtime_catalog, skill_paths)
    project_skills = capabilities.skills or describe_skill_paths(capabilities.enabled_skill_paths)
    approved = _merge_skill_descriptors(project_skills, runtime_skills)
    return replace(
        capabilities,
        enabled_skill_paths=tuple(skill.path for skill in approved),
        skills=approved,
        skill_catalog=_merge_skill_descriptors(
            capabilities.skill_catalog,
            _merge_skill_descriptors(runtime_catalog, approved),
        ),
    )


def _filter_tools_by_name(
    tools: Sequence[ToolInput], exclude_names: set[str]
) -> tuple[ToolDescriptor, ...]:
    return tuple(tool for tool in describe_tools(tools) if tool.name not in exclude_names)


def _workflows_enabled(resolved: ResolvedAgent) -> bool:
    return resolved.workflows is not None and resolved.workflows.enabled


def _workflow_exclude_names(resolved: ResolvedAgent) -> set[str]:
    if resolved.workflows is None:
        return set()
    return set(resolved.workflows.exclude)


def _build_web_request_tools(resolved: ResolvedAgent) -> tuple[ToolDescriptor, ...]:
    """Build the stateless ``web_request`` descriptor once per enabled agent."""
    if resolved.tools_disabled or resolved.web_request_config is None:
        return ()
    # Imported lazily so registration import cost stays low when the tool is unused.
    web_request_module = import_module("azure_functions_agents.system_tools.web_request")
    return describe_tools(web_request_module.create_web_request_tools(resolved.web_request_config))


def build_capabilities(
    resolved: ResolvedAgent,
    *,
    discovered_user_tools: Sequence[ToolInput],
    discovered_workflow_tools: Sequence[WorkflowTool] | None = None,
    discovered_mcp_tools: Mapping[str, MCPServerDescriptor],
    discovered_skills: Mapping[str, Path],
    discovered_skill_descriptors: Sequence[SkillDescriptor] | None = None,
) -> AgentCapabilities:
    """Apply resolved capability filters and return the final runner inputs."""
    exclude_names = set(resolved.tool_filter.exclude or [])

    if resolved.tools_disabled:
        filtered_user_tools: tuple[ToolDescriptor, ...] = ()
    else:
        filtered_user_tools = _filter_tools_by_name(discovered_user_tools, exclude_names)

    workflow_tools = list(discovered_workflow_tools or [])
    if _workflows_enabled(resolved):
        workflow_exclude_names = _workflow_exclude_names(resolved)
        known_workflow_names = {tool.name for tool in workflow_tools}
        unknown_workflow_names = workflow_exclude_names - known_workflow_names
        if unknown_workflow_names:
            logger.warning(
                "%s: workflows.exclude contains unknown workflow tool name(s): %s",
                resolved.source_file or "<unknown>",
                sorted(unknown_workflow_names),
            )
        filtered_workflow_tools = tuple(
            tool for tool in workflow_tools if tool.name not in workflow_exclude_names
        )
    else:
        filtered_workflow_tools = ()

    if resolved.mcp_disabled:
        filtered_mcp_tools: tuple[MCPServerDescriptor, ...] = ()
    else:
        filtered_mcp_tools = tuple(
            discovered_mcp_tools[name]
            for name in resolved.enabled_mcp_names
            if name in discovered_mcp_tools
        )

    skill_catalog = (
        tuple(discovered_skill_descriptors)
        if discovered_skill_descriptors is not None
        else tuple(
            SkillDescriptor.create(name=name, description="", path=path)
            for name, path in discovered_skills.items()
        )
    )
    skills_by_name = {skill.name: skill for skill in skill_catalog}
    if resolved.skills_disabled:
        approved_skills: tuple[SkillDescriptor, ...] = ()
    else:
        approved_skills = tuple(
            skills_by_name[name]
            for name in resolved.enabled_skills_names
            if name in skills_by_name
        )

    return AgentCapabilities.create(
        filtered_user_tools=filtered_user_tools,
        filtered_workflow_tools=filtered_workflow_tools,
        filtered_mcp_tools=filtered_mcp_tools,
        skills=approved_skills,
        skill_catalog=skill_catalog,
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
    names = {tool.name for tool in capabilities.filtered_user_tools or ()}
    names.update(tool.name for tool in capabilities.filtered_mcp_tools or ())
    names.update(tool.name for tool in capabilities.filtered_workflow_tools)
    names.update(tool.name for tool in capabilities.web_request_tools or ())
    if resolved.sandbox_config is not None and not resolved.tools_disabled:
        names.add(SANDBOX_TOOL_NAME)
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
