"""Workflow-safe tool registry (M1 step 3c).

Neutral home for the registry of tools the workflow orchestrator can
dispatch via its activity. Sits between :mod:`.engine` (which looks up
handlers at activity-execution time) and :mod:`.integration` (which
turns discovered workflow tools into the effective tool set + addendum) so neither
has to import the other.

Three concepts kept distinct:

- **Registered**: known to the engine, dispatchable. Name → handler +
  description + ``public`` flag.
- **Public**: included in the effective workflow tool set unless filtered.
  Internal helpers like ``__echo`` are registered with ``public=False`` so
  they don't leak into agent-visible plans by accident.
- **Effective allowlist**: the set the running app actually permits in
  plans. Computed by :mod:`.integration` per-app and stashed via
  :func:`set_app_config`; read by ``start_workflow`` when validating
  a plan.

Reserved names (the LLM-facing workflow-management tools themselves)
can never be registered — workflow nodes must never reach back into
the workflow control plane.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkflowToolEntry:
    """One row in the registry.

    ``handler`` runs inside the orchestrator's activity. It must be a
    plain (synchronous) callable taking a ``dict`` of args and returning
    a JSON-serializable value. Async handlers are rejected at
    registration time — supporting them needs a wrapper that doesn't
    exist yet, and silently returning a coroutine to the activity would
    surface as a confusing serialization error later.
    """

    name: str
    description: str
    handler: Callable[[dict[str, Any]], Any]
    public: bool


# Names of LLM-facing workflow-management tools — these can never be
# workflow node targets. Kept here (not in tools.py) to avoid pulling
# the agent-facing workflow tool layer into modules that don't otherwise need it.
RESERVED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "start_workflow",
        "get_workflow_status",
        "list_workflows",
        "cancel_workflow",
        "terminate_workflow",
    }
)


_REGISTRY: dict[str, WorkflowToolEntry] = {}
_APP_ALLOWLIST: frozenset[str] | None = None


def register_workflow_tool(
    name: str,
    description: str,
    handler: Callable[[dict[str, Any]], Any],
    *,
    public: bool = True,
) -> None:
    """Register a workflow-safe tool.

    Raises :class:`ValueError` on collision with an existing entry, on a
    name that collides with a reserved workflow-management tool, or on
    an obviously-wrong handler shape (async functions are rejected so
    the orchestrator's activity can stay synchronous in M1).
    """
    if not isinstance(name, str) or not name:
        raise ValueError("workflow tool name must be a non-empty string")
    if name in RESERVED_TOOL_NAMES:
        raise ValueError(
            f"workflow tool name {name!r} is reserved for the workflow "
            "control plane and cannot be used as a workflow node target"
        )
    if name in _REGISTRY:
        raise ValueError(f"workflow tool {name!r} is already registered")
    if not callable(handler):
        raise ValueError(
            f"workflow tool {name!r}: handler must be a callable taking a "
            "dict of args and returning a JSON-serializable value"
        )
    if inspect.iscoroutinefunction(handler):
        raise ValueError(
            f"workflow tool {name!r}: async handlers are not supported; "
            "register a synchronous wrapper instead"
        )
    _REGISTRY[name] = WorkflowToolEntry(
        name=name, description=description, handler=handler, public=public
    )


def get_handler(name: str) -> Callable[[dict[str, Any]], Any] | None:
    entry = _REGISTRY.get(name)
    return entry.handler if entry is not None else None


def get_entry(name: str) -> WorkflowToolEntry | None:
    return _REGISTRY.get(name)


def public_tool_names() -> frozenset[str]:
    """Return the set of registered tools that should be agent-visible
    by default (when frontmatter does not narrow the allowlist).
    """
    return frozenset(name for name, entry in _REGISTRY.items() if entry.public)


def all_registered_names() -> frozenset[str]:
    return frozenset(_REGISTRY)


def set_app_config(allowed_tools: frozenset[str]) -> None:
    """Stash the effective per-app allowlist computed by
    :mod:`.integration`. ``start_workflow`` reads this when validating
    submitted plans.

    M1 has exactly one main agent so a single module-level value is
    sufficient. Per-agent allowlists land with the M3 registry refactor
    — at which point this should be replaced with a per-agent lookup.
    """
    global _APP_ALLOWLIST
    _APP_ALLOWLIST = frozenset(allowed_tools)


def get_app_config() -> frozenset[str] | None:
    """Return the effective per-app allowlist, or ``None`` if workflows
    were never enabled for this app (in which case ``start_workflow``
    should never have been registered).
    """
    return _APP_ALLOWLIST


def reset() -> None:
    """Clear all registered tools and the app allowlist.

    Test-only hook. Production code should never need this — the
    registry is built once at app load and read from then on.
    """
    _REGISTRY.clear()
    global _APP_ALLOWLIST
    _APP_ALLOWLIST = None


__all__ = [
    "RESERVED_TOOL_NAMES",
    "WorkflowToolEntry",
    "all_registered_names",
    "get_app_config",
    "get_entry",
    "get_handler",
    "public_tool_names",
    "register_workflow_tool",
    "reset",
    "set_app_config",
]
