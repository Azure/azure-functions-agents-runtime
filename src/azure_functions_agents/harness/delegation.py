"""Rebuild the immutable delegation catalog from delivered application content."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..config.schema import ResolvedAgent
from ..project_composition import compose_project
from ..registration.catalog import AgentCatalog
from . import _ensure_sandbox


class DelegationReconstructionError(Exception):
    """Delivered agent content cannot form one unambiguous immutable catalog."""


def rebuild_agent_catalog(application_root: Path) -> AgentCatalog:
    """Reconstruct the existing runtime catalog from a verified delivered tree."""
    _ensure_sandbox()
    composition = compose_project(application_root, validate_runtime=False)
    validate_delegation_graph(composition.resolved_agents)
    return composition.catalog


def validate_delegation_graph(resolved_agents: Sequence[ResolvedAgent]) -> None:
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
