"""Per-session workflow context registry + instance-ID ownership scheme.

Two concerns live here:

1. **Per-turn registry.** Workflow tool handlers need the Durable
   Functions ``client`` the Functions host injected into the chat
   handler via ``durable_client_input`` and the owning agent name.
   ContextVars turn out to be unusable: the copilot SDK dispatches tool
   calls onto the RPC reader task via :func:`asyncio.ensure_future`, so
   context set inside the chat handler does NOT propagate into tool
   handlers. Instead we keep a module-level registry keyed by
   ``session_id`` — the chat handler registers a row before calling
   ``run_copilot_agent``, unregisters it in ``finally``, and the tool
   handlers look up the row via :attr:`ToolInvocation.session_id`.
   Concurrent turns on the same ``session_id`` are possible; to keep a
   late-arriving turn from evicting a newer registration,
   :func:`register_workflow_session` returns an opaque token and
   :func:`unregister_workflow_session` is a no-op unless that token
   still owns the slot.

2. **Instance-ID ownership.** Every workflow started via
   ``start_workflow`` receives an instance ID whose leading
   :data:`SESSION_PREFIX_LEN` hex characters are ``sha256(session_id)``.
   Ownership is enforced by prefix match on the workflow ID, which is
   stable across Durable's lifecycle and does not depend on the
   orchestration input being preserved post-completion. Hashing keeps
   the raw ``session_id`` out of Durable-visible metadata (defense in
   depth for repo-wide ``session_id`` hygiene; M5 builds on this).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, Optional


SESSION_PREFIX_LEN = 12


def session_instance_prefix(session_id: str) -> str:
    """Return the fixed-length hash prefix embedded in every workflow ID
    started by ``session_id``.

    Workflow ownership is enforced by comparing this prefix against the
    Durable instance_id: any workflow whose ID does not start with the
    calling session's prefix is treated as nonexistent for that session.
    Hashing keeps the raw ``session_id`` out of Durable-visible metadata.
    """
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:SESSION_PREFIX_LEN]


def new_workflow_instance_id(session_id: str) -> str:
    """Generate a fresh workflow instance ID for ``session_id``.

    Shape: ``{12-hex-session-hash}-{32-hex-uuid}``. The leading prefix is
    reproducible (same session → same prefix) and is used for ownership
    checks; the uuid suffix keeps each workflow unique.
    """
    return f"{session_instance_prefix(session_id)}-{uuid.uuid4().hex}"


def session_owns_workflow(session_id: str, workflow_id: str) -> bool:
    if not session_id or not workflow_id:
        return False
    return workflow_id.startswith(session_instance_prefix(session_id) + "-")


@dataclass(frozen=True)
class WorkflowSessionContext:
    """Per-in-flight-request state needed by workflow tools."""

    session_id: str
    agent_name: str
    durable_client: Any  # azure.durable_functions.DurableOrchestrationClient
    token: str


_registry: Dict[str, WorkflowSessionContext] = {}
_lock = Lock()


def register_workflow_session(
    session_id: str,
    agent_name: str,
    durable_client: Any,
) -> str:
    """Register the per-session context for the duration of a chat turn.

    Returns an opaque token the caller passes to
    :func:`unregister_workflow_session` in its ``finally`` block.
    """
    token = uuid.uuid4().hex
    with _lock:
        _registry[session_id] = WorkflowSessionContext(
            session_id=session_id,
            agent_name=agent_name,
            durable_client=durable_client,
            token=token,
        )
    return token


def unregister_workflow_session(session_id: str, token: str) -> None:
    """Remove the row for ``session_id``, but only if ``token`` still owns it.

    Safe to call multiple times and safe to call when a later turn has
    already replaced our slot — in both cases this is a no-op.
    """
    with _lock:
        existing = _registry.get(session_id)
        if existing is not None and existing.token == token:
            _registry.pop(session_id, None)


def get_workflow_session(session_id: Optional[str]) -> Optional[WorkflowSessionContext]:
    if not session_id:
        return None
    with _lock:
        return _registry.get(session_id)


__all__ = [
    "SESSION_PREFIX_LEN",
    "WorkflowSessionContext",
    "get_workflow_session",
    "new_workflow_instance_id",
    "register_workflow_session",
    "session_instance_prefix",
    "session_owns_workflow",
    "unregister_workflow_session",
]
