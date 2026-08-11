"""Per-owner-session workflow context registry + instance-ID ownership scheme.

Two concerns live here:

1. **Compatibility registry.** Production workflow tools receive a request-local
   context directly. The process-local registry remains for callers of the
   original helper API. Its registration token is private bookkeeping and is
   not part of workflow session state.

2. **Instance-ID ownership.** Every workflow started via
   ``start_workflow`` receives an instance ID whose leading
   :data:`OWNER_SESSION_PREFIX_LEN` hex characters are a SHA-256 prefix
   over the owner slug and session ID.
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
from typing import Any

OWNER_SESSION_PREFIX_LEN = 32
# Compatibility alias retained for callers that imported the original constant.
# Its value follows the current owner/session format, not the legacy 12-hex format.
SESSION_PREFIX_LEN = OWNER_SESSION_PREFIX_LEN


def session_instance_prefix(owner_slug: str, session_id: str) -> str:
    """Return the fixed-length owner/session prefix embedded in workflow IDs.

    Workflow ownership is enforced by comparing this prefix against the
    Durable instance_id: any workflow whose ID does not start with the
    calling owner/session prefix is treated as nonexistent.
    Hashing keeps the raw ``session_id`` out of Durable-visible metadata.
    """
    digest = hashlib.sha256()
    for value in (owner_slug, session_id):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()[:OWNER_SESSION_PREFIX_LEN]


def new_workflow_instance_id(owner_slug: str, session_id: str) -> str:
    """Generate a fresh workflow instance ID for an owner/session pair.

    Shape: ``{32-hex-owner-session-hash}-{32-hex-uuid}``.
    """
    return f"{session_instance_prefix(owner_slug, session_id)}-{uuid.uuid4().hex}"


def session_owns_workflow(
    owner_slug: str,
    session_id: str,
    workflow_id: str,
) -> bool:
    if not owner_slug or not session_id or not workflow_id:
        return False
    return workflow_id.startswith(
        session_instance_prefix(owner_slug, session_id) + "-"
    )


@dataclass(frozen=True)
class WorkflowSessionContext:
    """Per-in-flight-request state needed by workflow tools."""

    owner_slug: str
    session_id: str
    agent_name: str
    durable_client: Any  # azure.durable_functions.DurableOrchestrationClient


@dataclass(frozen=True)
class _WorkflowSessionRegistration:
    context: WorkflowSessionContext
    token: str


_registry: dict[tuple[str, str], _WorkflowSessionRegistration] = {}
_lock = Lock()


def register_workflow_session(
    owner_slug: str,
    session_id: str,
    agent_name: str,
    durable_client: Any,
) -> str:
    """Register the per-session context for the duration of a chat turn.

    Returns an opaque token the caller passes to
    :func:`unregister_workflow_session` in its ``finally`` block.
    """
    token = uuid.uuid4().hex
    context = WorkflowSessionContext(
        owner_slug=owner_slug,
        session_id=session_id,
        agent_name=agent_name,
        durable_client=durable_client,
    )
    with _lock:
        _registry[(owner_slug, session_id)] = _WorkflowSessionRegistration(
            context=context,
            token=token,
        )
    return token


def unregister_workflow_session(
    owner_slug: str,
    session_id: str,
    token: str,
) -> None:
    """Remove the row for ``session_id``, but only if ``token`` still owns it.

    Safe to call multiple times and safe to call when a later turn has
    already replaced our slot — in both cases this is a no-op.
    """
    with _lock:
        key = (owner_slug, session_id)
        existing = _registry.get(key)
        if existing is not None and existing.token == token:
            _registry.pop(key, None)


def get_workflow_session(
    owner_slug: str | None,
    session_id: str | None,
) -> WorkflowSessionContext | None:
    if not owner_slug or not session_id:
        return None
    with _lock:
        registration = _registry.get((owner_slug, session_id))
        return registration.context if registration is not None else None


__all__ = [
    "OWNER_SESSION_PREFIX_LEN",
    "SESSION_PREFIX_LEN",
    "WorkflowSessionContext",
    "get_workflow_session",
    "new_workflow_instance_id",
    "register_workflow_session",
    "session_instance_prefix",
    "session_owns_workflow",
    "unregister_workflow_session",
]
