"""Compose one application's validated, immutable agent catalog."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ._source_marker import source_marker
from .config.loader import load_agent_specs, load_global_config
from .config.merge import compose
from .config.schema import AgentSpec, GlobalConfig, ResolvedAgent
from .config.validation import (
    validate_resolved_agent,
    validate_session_runtime,
    validate_subagent_references,
    validate_workflow_subagent_references,
)
from .discovery.mcp import MCPDiscoveryResult, clear_mcp_cache, discover_mcp_servers
from .discovery.skills import SkillDiscoveryResult, clear_skills_cache, discover_skills
from .discovery.tools import ProjectTools, clear_tool_discovery_cache, discover_project_tools
from .registration.capabilities import build_capabilities, validate_subagent_tool_names
from .registration.catalog import AgentCatalog, CatalogEntry, build_catalog


@dataclass(frozen=True, slots=True)
class ProjectComposition:
    """The ordinary discovery, validation, and catalog result for one project."""

    application_root: Path
    global_config: GlobalConfig
    agent_specs: tuple[AgentSpec, ...]
    tool_result: ProjectTools
    mcp_result: MCPDiscoveryResult
    skill_result: SkillDiscoveryResult
    resolved_agents: tuple[ResolvedAgent, ...]
    referenced_slugs: frozenset[str]
    catalog: AgentCatalog


def compose_project(
    application_root: Path,
    *,
    strict: bool = False,
    refresh_discovery: bool = False,
    default_model: str | None = None,
    validate_runtime: bool = True,
) -> ProjectComposition:
    """Run the shared ordinary project-composition pipeline."""
    root = Path(application_root).resolve()
    if refresh_discovery:
        clear_tool_discovery_cache()
        clear_mcp_cache()
        clear_skills_cache()
    global_config = load_global_config(root)
    agent_specs = load_agent_specs(root, strict=strict)
    tool_result = discover_project_tools(root)
    mcp_result = discover_mcp_servers(root)
    skill_result = discover_skills(root)

    mcp_names = list(mcp_result.servers)
    skill_names = list(skill_result.skills)
    resolved_agents = [
        compose(
            spec,
            global_config,
            discovered_mcp_names=mcp_names,
            discovered_skill_names=skill_names,
        )
        for spec in agent_specs
    ]
    if default_model is not None:
        resolved_agents = [
            resolved
            if spec.model is not None or global_config.model is not None
            else resolved.model_copy(update={"model": default_model})
            for spec, resolved in zip(agent_specs, resolved_agents, strict=True)
        ]
    if validate_runtime:
        validate_session_runtime(global_config, resolved_agents)

    known_slugs = require_unique_slugs(resolved_agents)
    referenced_slugs: set[str] = set()
    for resolved in resolved_agents:
        validate_subagent_references(resolved, known_slugs=known_slugs)
        validate_workflow_subagent_references(resolved, known_slugs=known_slugs)
        referenced_slugs.update(reference.agent for reference in resolved.subagents)
        if resolved.workflows is not None:
            referenced_slugs.update(reference.agent for reference in resolved.workflows.subagents)

    entries: dict[str, CatalogEntry] = {}
    for resolved in resolved_agents:
        validate_resolved_agent(
            resolved,
            discovered_mcp_names=mcp_names,
            discovered_skills=skill_names,
            is_referenced_as_subagent=resolved.slug in referenced_slugs,
        )
        capabilities = build_capabilities(
            resolved,
            discovered_user_tools=tool_result.user_tools,
            discovered_workflow_tools=tool_result.workflow_tools,
            discovered_mcp_tools=mcp_result.servers,
            discovered_skills=skill_result.skills,
        )
        validate_subagent_tool_names(resolved, capabilities)
        entries[resolved.slug] = CatalogEntry(resolved=resolved, capabilities=capabilities)

    return ProjectComposition(
        application_root=root,
        global_config=global_config,
        agent_specs=tuple(agent_specs),
        tool_result=tool_result,
        mcp_result=mcp_result,
        skill_result=skill_result,
        resolved_agents=tuple(resolved_agents),
        referenced_slugs=frozenset(referenced_slugs),
        catalog=build_catalog(entries),
    )


def require_unique_slugs(resolved_agents: list[ResolvedAgent]) -> set[str]:
    """Return the complete slug index or raise on a collision."""
    sources_by_slug: dict[str, list[str]] = {}
    for resolved in resolved_agents:
        sources_by_slug.setdefault(resolved.slug, []).append(source_marker(resolved.source_file))

    for slug, sources in sorted(sources_by_slug.items()):
        if len(sources) > 1:
            listed = ", ".join(sorted(sources))
            raise ValueError(
                f"Duplicate agent slug {slug!r} is used by {len(sources)} source "
                f"files: {listed}. Agent identity slugs must be globally unique "
                "across the app (a slug doubles as the registered function "
                "name, the `/agents/<slug>/` built-in endpoint route, and the "
                "`delegate_<slug>` tool name). Rename one of the colliding "
                "source files (e.g. its file stem) to resolve this. See "
                "docs/front-matter-spec.md#subagents."
            )
    return set(sources_by_slug)
