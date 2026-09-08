"""Per-workflow-agent-session context registry and instance-ID isolation scheme.

Two concerns live here:

1. **Compatibility registry.** Production workflow tools receive a request-local
   context directly. The process-local registry remains for callers of the
   original helper API. Its registration token is private bookkeeping and is
   not part of workflow session state.

2. **Instance-ID isolation.** Every workflow started via
   ``start_workflow`` receives an instance ID whose leading
   :data:`AGENT_SESSION_PREFIX_LEN` hex characters are a SHA-256 prefix
   over the workflow agent slug and session ID.
   Isolation is enforced by prefix match on the workflow ID, which is
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

from azure.durable_functions import DurableFunctionsClient

AGENT_SESSION_PREFIX_LEN = 32
# Compatibility alias retained for callers that imported the original constant.
# Its value follows the current agent/session format, not the legacy 12-hex format.
SESSION_PREFIX_LEN = AGENT_SESSION_PREFIX_LEN


def session_instance_prefix(workflow_agent_slug: str, session_id: str) -> str:
    """Return the fixed-length workflow-agent/session prefix embedded in workflow IDs.

    Workflow isolation is enforced by comparing this prefix against the
    Durable instance_id: any workflow whose ID does not start with the
    calling workflow-agent/session prefix is treated as nonexistent.
    Hashing keeps the raw ``session_id`` out of Durable-visible metadata.
    """
    digest = hashlib.sha256()
    for value in (workflow_agent_slug, session_id):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()[:AGENT_SESSION_PREFIX_LEN]


def new_workflow_instance_id(workflow_agent_slug: str, session_id: str) -> str:
    """Generate a fresh workflow instance ID for a workflow-agent/session pair.

    Shape: ``{32-hex-agent-session-hash}-{32-hex-uuid}``.
    """
    return f"{session_instance_prefix(workflow_agent_slug, session_id)}-{uuid.uuid4().hex}"


def workflow_matches_agent_session(
    workflow_agent_slug: str,
    session_id: str,
    workflow_id: str,
) -> bool:
    if not workflow_agent_slug or not session_id or not workflow_id:
        return False
    return workflow_id.startswith(
        session_instance_prefix(workflow_agent_slug, session_id) + "-"
    )


@dataclass(frozen=True)
class WorkflowSessionContext:
    """Per-in-flight-request state needed by workflow tools."""

    workflow_agent_slug: str
    session_id: str
    agent_name: str
    durable_client: DurableFunctionsClient


@dataclass(frozen=True)
class _WorkflowSessionRegistration:
    context: WorkflowSessionContext
    token: str


_registry: dict[tuple[str, str], _WorkflowSessionRegistration] = {}
_lock = Lock()


def register_workflow_session(
    workflow_agent_slug: str,
    session_id: str,
    agent_name: str,
    durable_client: DurableFunctionsClient,
) -> str:
    """Register the per-session context for the duration of a chat turn.

    Returns an opaque token the caller passes to
    :func:`unregister_workflow_session` in its ``finally`` block.
    """
    token = uuid.uuid4().hex
    context = WorkflowSessionContext(
        workflow_agent_slug=workflow_agent_slug,
        session_id=session_id,
        agent_name=agent_name,
        durable_client=durable_client,
    )
    with _lock:
        _registry[(workflow_agent_slug, session_id)] = _WorkflowSessionRegistration(
            context=context,
            token=token,
        )
    return token


def unregister_workflow_session(
    workflow_agent_slug: str,
    session_id: str,
    token: str,
) -> None:
    """Remove the row for ``session_id``, but only if ``token`` still owns it.

    Safe to call multiple times and safe to call when a later turn has
    already replaced our slot — in both cases this is a no-op.
    """
    with _lock:
        key = (workflow_agent_slug, session_id)
        existing = _registry.get(key)
        if existing is not None and existing.token == token:
            _registry.pop(key, None)


def get_workflow_session(
    workflow_agent_slug: str | None,
    session_id: str | None,
) -> WorkflowSessionContext | None:
    if not workflow_agent_slug or not session_id:
        return None
    with _lock:
        registration = _registry.get((workflow_agent_slug, session_id))
        return registration.context if registration is not None else None


__all__ = [
    "AGENT_SESSION_PREFIX_LEN",
    "SESSION_PREFIX_LEN",
    "WorkflowSessionContext",
    "get_workflow_session",
    "new_workflow_instance_id",
    "register_workflow_session",
    "session_instance_prefix",
    "unregister_workflow_session",
    "workflow_matches_agent_session",
]
