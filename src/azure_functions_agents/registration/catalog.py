"""Read-only agent catalog with independently frozen capability inventories."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from ..config import ResolvedAgent
from .capabilities import AgentCapabilities


@dataclass(frozen=True)
class CatalogEntry:
    """One resolved agent and its immutable capability inventory."""

    resolved: ResolvedAgent
    capabilities: AgentCapabilities


type AgentCatalog = MappingProxyType[str, CatalogEntry]


def build_catalog(entries: dict[str, CatalogEntry]) -> AgentCatalog:
    """Freeze ``entries`` (slug -> :class:`CatalogEntry`) into an :data:`AgentCatalog`."""
    frozen: dict[str, CatalogEntry] = {}
    for slug, entry in entries.items():
        capabilities = entry.capabilities
        frozen[slug] = CatalogEntry(
            resolved=entry.resolved,
            capabilities=AgentCapabilities.create(
                filtered_user_tools=capabilities.filtered_user_tools,
                filtered_workflow_tools=capabilities.filtered_workflow_tools,
                filtered_mcp_tools=capabilities.filtered_mcp_tools,
                enabled_skill_paths=capabilities.enabled_skill_paths,
                web_request_tools=capabilities.web_request_tools,
                skills=capabilities.skills or None,
                skill_catalog=capabilities.skill_catalog,
                _harness=capabilities._harness,
            ),
        )
    return MappingProxyType(frozen)
