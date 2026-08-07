"""Rebuild the immutable delegation catalog from delivered application content."""

from __future__ import annotations

from pathlib import Path

from ..config.loader import load_agent_specs, load_global_config
from ..config.merge import compose
from ..config.schema import ResolvedAgent
from ..config.validation import validate_resolved_agent, validate_subagent_references
from ..discovery.mcp import discover_mcp_servers
from ..discovery.skills import discover_skills
from ..discovery.tools import discover_project_tools
from ..registration.capabilities import build_capabilities, validate_subagent_tool_names
from ..registration.catalog import AgentCatalog, CatalogEntry, build_catalog
from . import _ensure_sandbox


class DelegationReconstructionError(Exception):
    """Delivered agent content cannot form one unambiguous immutable catalog."""


def rebuild_agent_catalog(application_root: Path) -> AgentCatalog:
    """Reconstruct the existing runtime catalog from a verified delivered tree."""

    _ensure_sandbox()
    root = Path(application_root).resolve()
    global_config = load_global_config(root)
    agent_specs = load_agent_specs(root)
    tools = discover_project_tools(root)
    mcp = discover_mcp_servers(root)
    skills = discover_skills(root)
    resolved_agents = [
        compose(
            spec,
            global_config,
            discovered_mcp_names=list(mcp.servers),
            discovered_skill_names=list(skills.skills),
        )
        for spec in agent_specs
    ]
    known_slugs = _require_unique_slugs(resolved_agents)
    referenced_slugs: set[str] = set()
    for resolved in resolved_agents:
        validate_subagent_references(resolved, known_slugs=known_slugs)
        referenced_slugs.update(reference.agent for reference in resolved.subagents)

    entries: dict[str, CatalogEntry] = {}
    for resolved in resolved_agents:
        validate_resolved_agent(
            resolved,
            discovered_mcp_names=list(mcp.servers),
            discovered_skills=list(skills.skills),
            is_referenced_as_subagent=resolved.slug in referenced_slugs,
        )
        capabilities = build_capabilities(
            resolved,
            discovered_user_tools=tools.user_tools,
            discovered_workflow_tools=tools.workflow_tools,
            discovered_mcp_tools=mcp.servers,
            discovered_skills=skills.skills,
        )
        validate_subagent_tool_names(resolved, capabilities)
        entries[resolved.slug] = CatalogEntry(resolved=resolved, capabilities=capabilities)
    return build_catalog(entries)


def validate_delegation_graph(resolved_agents: list[ResolvedAgent]) -> None:
    """Reject cyclic or deeper-than-one static delegation graphs for sandbox execution."""

    references = {
        resolved.slug: tuple(reference.agent for reference in resolved.subagents)
        for resolved in resolved_agents
    }
    for root, children in references.items():
        for child in children:
            grandchildren = references.get(child, ())
            if grandchildren:
                raise DelegationReconstructionError(
                    "Sandbox delegation supports one coordinator-to-specialist level."
                )
            if child == root:
                raise DelegationReconstructionError("Sandbox delegation graph is cyclic.")
    _ensure_acyclic_references(references)


def _ensure_acyclic_references(references: dict[str, tuple[str, ...]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(slug: str) -> None:
        if slug in visiting:
            raise DelegationReconstructionError("Sandbox delegation graph is cyclic.")
        if slug in visited:
            return
        visiting.add(slug)
        for child in references.get(slug, ()):
            visit(child)
        visiting.remove(slug)
        visited.add(slug)

    for slug in references:
        visit(slug)


def _require_unique_slugs(resolved_agents: list[ResolvedAgent]) -> set[str]:
    seen: set[str] = set()
    for resolved in resolved_agents:
        if resolved.slug in seen:
            raise DelegationReconstructionError(
                "Delivered agent content has duplicate identity slugs."
            )
        seen.add(resolved.slug)
    return seen
