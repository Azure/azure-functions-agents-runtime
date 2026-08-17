"""Read a validated ACA checkpoint conversation without a storage-provider dependency."""

from __future__ import annotations

from dataclasses import dataclass

from .._history_presentation import (
    decode_history_jsonl,
    filter_excluded_history_messages,
    present_history_messages,
)
from ..journal_paths import checkpoint_conversation_path
from ..session_state import (
    ConcurrencyConflictError,
    OwnerContext,
    SessionRowNotFoundError,
    owner_partition,
)
from ..transport.transport_models import SandboxFileNotFoundError, SandboxFileOperationError
from .readiness import (
    ActivatedSession,
    SessionActivationGoneError,
    SessionActivationNotFoundError,
    SessionActivationSetupTimeoutError,
    SessionActivationUnavailableError,
    SessionActivationUntrustedError,
    SessionRuntimeBinding,
    SetupDeadline,
    _quarantine_detected_binding,
    _within_setup_budget,
    activate_session,
)

MAX_CHECKPOINT_CONVERSATION_BYTES = 4 * 1024 * 1024


class SessionHistoryError(RuntimeError):
    """Base class for a history read that has no successful transcript."""


class SessionHistoryNotFoundError(SessionHistoryError):
    """The owner does not have a session binding to read."""


class SessionHistoryGoneError(SessionHistoryError):
    """The session's history horizon has ended permanently."""


class SessionHistoryUnavailableError(SessionHistoryError):
    """A verified session's checkpoint cannot be safely read now."""


@dataclass(frozen=True, slots=True)
class SessionHistoryRead:
    """The presentation-ready history and whether activation resumed its backing."""

    messages: list[dict[str, str]]
    truncated: bool
    resumed: bool


async def read_session_history(
    runtime: SessionRuntimeBinding,
    owner: OwnerContext,
    session_id: str,
    setup_deadline: SetupDeadline,
) -> SessionHistoryRead:
    """Read the owner-authorized latest complete ACA checkpoint conversation."""
    partition = owner_partition(owner)
    await runtime.reconcile_session(partition, session_id)

    activated: ActivatedSession | None = None
    try:
        try:
            activated = await activate_session(
                runtime,
                owner,
                session_id,
                setup_deadline,
                allow_create=False,
            )
        except SessionActivationGoneError as exc:
            raise SessionHistoryGoneError("Session history is gone.") from exc
        except (SessionActivationUntrustedError, SessionActivationUnavailableError) as exc:
            raise SessionHistoryUnavailableError("Session history is unavailable.") from exc
        except SessionActivationNotFoundError as exc:
            raise SessionHistoryNotFoundError("Session was not found for this owner.") from exc
        except SessionActivationSetupTimeoutError as exc:
            raise SessionHistoryUnavailableError("Session history is unavailable.") from exc

        if activated.checkpoint_name is None:
            if activated.session.checkpoint_expectation == "none":
                return SessionHistoryRead(
                    messages=[],
                    truncated=False,
                    resumed=activated.resumed,
                )
            raise SessionHistoryUnavailableError("Session history is unavailable.")

        messages, truncated = await _read_checkpoint_conversation(activated, setup_deadline)
        return SessionHistoryRead(
            messages=messages,
            truncated=truncated,
            resumed=activated.resumed,
        )
    finally:
        if activated is not None:
            await activated.handle.close()


async def _read_checkpoint_conversation(
    activated: ActivatedSession,
    setup_deadline: SetupDeadline,
) -> tuple[list[dict[str, str]], bool]:
    checkpoint_name = activated.checkpoint_name
    if checkpoint_name is None:
        raise SessionHistoryUnavailableError("Session history is unavailable.")
    path = checkpoint_conversation_path(checkpoint_name)
    try:
        stat = await _within_setup_budget(activated.handle.stat_file(path), setup_deadline)
    except (SandboxFileNotFoundError, SandboxFileOperationError, SessionActivationSetupTimeoutError) as exc:
        raise SessionHistoryUnavailableError("Session history is unavailable.") from exc

    if (
        stat.is_directory
        or stat.size is None
        or stat.size < 0
        or stat.size > MAX_CHECKPOINT_CONVERSATION_BYTES
    ):
        await _quarantine_checkpoint(activated)
        raise SessionHistoryUnavailableError("Session history is unavailable.")

    try:
        content = await _within_setup_budget(activated.handle.read_file(path), setup_deadline)
    except (SandboxFileNotFoundError, SandboxFileOperationError, SessionActivationSetupTimeoutError) as exc:
        raise SessionHistoryUnavailableError("Session history is unavailable.") from exc

    if len(content) > MAX_CHECKPOINT_CONVERSATION_BYTES:
        await _quarantine_checkpoint(activated)
        raise SessionHistoryUnavailableError("Session history is unavailable.")

    try:
        decoded = decode_history_jsonl(content, source="ACA checkpoint conversation")
    except (UnicodeDecodeError, ValueError) as exc:
        await _quarantine_checkpoint(activated)
        raise SessionHistoryUnavailableError("Session history is unavailable.") from exc
    return present_history_messages(filter_excluded_history_messages(decoded))


async def _quarantine_checkpoint(activated: ActivatedSession) -> None:
    try:
        await _quarantine_detected_binding(
            activated.store,
            activated.session,
            activated.etag,
            reason="checkpoint_corrupt",
        )
    except ConcurrencyConflictError:
        await _raise_stale_quarantine_outcome(activated)


async def _raise_stale_quarantine_outcome(activated: ActivatedSession) -> None:
    try:
        current = await activated.store.get_session(
            activated.partition,
            activated.session.session_id,
        )
    except SessionRowNotFoundError:
        raise SessionHistoryUnavailableError("Session history is unavailable.") from None

    session = current.record
    if (
        session.owner_partition != activated.partition
        or session.session_id != activated.session.session_id
    ):
        raise SessionHistoryUnavailableError("Session history is unavailable.")
    if (
        session.status in {"tombstoned", "deleted"}
        or session.generation != activated.session.generation
        or (session.digest_kind, session.digest)
        != (activated.session.digest_kind, activated.session.digest)
    ):
        raise SessionHistoryGoneError("Session history is gone.")
    raise SessionHistoryUnavailableError("Session history is unavailable.")
