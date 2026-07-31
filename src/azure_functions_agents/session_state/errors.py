"""Typed, fail-closed errors for the P3b Table-backed session state store.

These are distinct from :class:`~.session_models.SessionStateContractError`
(P3a's pure shape/contract validation). Store errors describe *operational*
outcomes of talking to Azure Table Storage: configuration problems, optimistic
concurrency losses, admission/idempotency conflicts, and service
unavailability. A corrupt stored entity that fails P3a's contract validation
is re-raised here as :class:`CorruptEntityError` so store callers never need
to catch :class:`SessionStateContractError` directly.

Every error in this module is intentionally narrow and carries only the
non-secret context a caller needs to react correctly (e.g. the current
``active_run_id`` for a conflict) -- never raw claims, connection strings, or
idempotency-key plaintext.
"""

from __future__ import annotations


class SessionStateStoreError(Exception):
    """Base class for all P3b session-state store errors."""


class StateStoreConfigurationError(SessionStateStoreError):
    """``AzureWebJobsStorage`` is missing or unusable for Table access.

    Raised instead of silently falling back to any in-memory/local state --
    the store always fails closed when it cannot resolve a supported,
    credential-bearing connection to Azure Table Storage.
    """


class StateStoreUnavailableError(SessionStateStoreError):
    """The Table service call failed for an operational reason.

    Covers authentication/authorization failures (401/403), throttling (429),
    and server errors (5xx/network) -- anything that is not a specific,
    typed outcome below. ``status_code`` is ``None`` when the failure never
    reached the point of an HTTP response (e.g. a transport error).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RowNotFoundError(SessionStateStoreError):
    """Base class for a durable row that does not exist (or is not visible)."""


class SessionRowNotFoundError(RowNotFoundError):
    """No session row exists for the given owner partition/session_id."""


class RunRowNotFoundError(RowNotFoundError):
    """No run row exists for the given owner partition/session_id/run_id."""


class RowAlreadyExistsError(SessionStateStoreError):
    """A create-only write collided with a row that already exists."""


class ConcurrencyConflictError(SessionStateStoreError):
    """An ETag-guarded write lost a race against another writer.

    The row changed between the caller's read and its conditional write.
    Callers should re-read the current row and retry if appropriate; this is
    not necessarily a business-logic conflict (see :class:`ActiveRunConflictError`
    and :class:`IdempotencyConflictError` for the two admission-specific
    conflicts that get their own typed, business-meaningful errors).
    """


class GenerationConflictError(SessionStateStoreError):
    """A write attempted to move ``generation`` backward, or an invalid rebind.

    Equal generation is legal for ordinary same-backing mutations; only a
    state-preserving backing rebind may strictly increase it. Any other
    transition (including a decrease) is rejected -- generation rollback is
    always treated as a rollback/security failure, never silently accepted.
    """


class ActiveRunConflictError(SessionStateStoreError):
    """The session already has a different active run.

    Raised both when a pre-flight read finds ``active_run_id`` already set
    (distinct idempotency key, or no key at all) and when a race is lost
    during the admission transaction (after a safe re-read confirms who won).
    """

    def __init__(self, message: str, *, active_run_id: str) -> None:
        super().__init__(message)
        self.active_run_id = active_run_id


class IdempotencyConflictError(SessionStateStoreError):
    """The same idempotency key was reused with a different request payload."""

    def __init__(self, message: str, *, existing_run_id: str) -> None:
        super().__init__(message)
        self.existing_run_id = existing_run_id


class TerminalStateConflictError(SessionStateStoreError):
    """A run already has a *different* terminal status than the one proposed.

    Terminal status is monotonic: re-adopting the SAME terminal outcome is an
    idempotent no-op (safe for inline resubmit, opportunistic cleanup, and the
    reconciler timer to all call), but proposing a different terminal outcome
    for an already-terminal run is a contradiction and is rejected.
    """


class CorruptEntityError(SessionStateStoreError):
    """A stored entity failed P3a's typed validation when read back.

    Never coerced into a default/empty result -- a corrupt row is a fail-closed
    condition surfaced to the caller, chained from the underlying
    :class:`~.session_models.SessionStateContractError`.
    """


__all__ = [
    "ActiveRunConflictError",
    "ConcurrencyConflictError",
    "CorruptEntityError",
    "GenerationConflictError",
    "IdempotencyConflictError",
    "RowAlreadyExistsError",
    "RowNotFoundError",
    "RunRowNotFoundError",
    "SessionRowNotFoundError",
    "SessionStateStoreError",
    "StateStoreConfigurationError",
    "StateStoreUnavailableError",
    "TerminalStateConflictError",
]
