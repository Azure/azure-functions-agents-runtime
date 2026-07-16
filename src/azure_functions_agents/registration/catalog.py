"""Immutable, process-wide index of every resolved agent + its capabilities.

Built once by the ``app.py`` composition root (pass 1, before any
``FunctionApp`` mutation — FRD 0006 §4.2) and threaded read-only into
request handlers so a coordinator's ``delegate_<slug>`` tools can look up
*any* specialist by identity slug at request time, without holding a live
reference to a mutable MAF ``Agent`` (those are built fresh per request —
see ``runner.build_subagent_tools``).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from ..config import ResolvedAgent
from .capabilities import AgentCapabilities


@dataclass(frozen=True)
class CatalogEntry:
    """One agent's immutable identity + resolved capability bundle."""

    resolved: ResolvedAgent
    capabilities: AgentCapabilities


# Keyed by agent identity slug (``ResolvedAgent.slug``). A ``MappingProxyType``
# so handlers can only read the catalog, never mutate it out from under a
# concurrent request.
AgentCatalog = MappingProxyType[str, CatalogEntry]


def build_catalog(entries: dict[str, CatalogEntry]) -> AgentCatalog:
    """Freeze ``entries`` (slug -> :class:`CatalogEntry`) into an :data:`AgentCatalog`."""
    return MappingProxyType(dict(entries))
