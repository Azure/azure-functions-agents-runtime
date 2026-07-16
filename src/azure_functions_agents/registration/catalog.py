"""Read-only, process-wide index of every resolved agent + its capabilities.

Built once by the ``app.py`` composition root (pass 1, before any
``FunctionApp`` mutation — FRD 0006 §4.2) and threaded read-only into
request handlers so a coordinator's ``delegate_<slug>`` tools can look up
*any* specialist by identity slug at request time, without holding a live
reference to a mutable MAF ``Agent`` (those are built fresh per request —
see ``runner.build_subagent_tools``).

Immutability here is intentionally shallow, not deep. The mapping itself
(``AgentCatalog``) cannot have entries added, removed, or replaced, and
``CatalogEntry`` cannot be rebound to point at different ``resolved``/
``capabilities`` objects — both are enforced structurally. But the
``ResolvedAgent`` (a plain, non-frozen ``pydantic.BaseModel``) and
``AgentCapabilities`` (a plain, non-frozen ``dataclass``) objects an entry
*points to* are otherwise ordinary mutable Python objects, as are the lists
they hold (e.g. ``AgentCapabilities.filtered_user_tools``). Nothing in this
codebase mutates them after ``build_catalog()`` runs — the catalog is built
once per process and only ever read afterwards — but that is a convention
this module relies on, not a guarantee the types themselves enforce. Callers
that need a defensive copy of a mutable field (e.g. before handing a tool
list to a MAF ``Agent`` constructor) should copy it explicitly rather than
assume the catalog protects them from accidental in-place mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from ..config import ResolvedAgent
from .capabilities import AgentCapabilities


@dataclass(frozen=True)
class CatalogEntry:
    """One agent's identity + resolved capability bundle.

    The entry itself is frozen (its two fields cannot be reassigned to
    different objects), but see the module docstring: the ``resolved`` and
    ``capabilities`` objects it points to are ordinary mutable objects, not
    deeply immutable snapshots.
    """

    resolved: ResolvedAgent
    capabilities: AgentCapabilities


# Keyed by agent identity slug (``ResolvedAgent.slug``). A ``MappingProxyType``
# so handlers can only read the mapping itself — no entry can be added,
# removed, or replaced out from under a concurrent request. This does not
# extend to the contents of each ``CatalogEntry``; see the module docstring.
AgentCatalog = MappingProxyType[str, CatalogEntry]


def build_catalog(entries: dict[str, CatalogEntry]) -> AgentCatalog:
    """Freeze ``entries`` (slug -> :class:`CatalogEntry`) into an :data:`AgentCatalog`."""
    return MappingProxyType(dict(entries))
