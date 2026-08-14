"""Provider-neutral contracts for executing an agent run."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from .setup_budget import SetupBudgetExpiredError, SetupTimeoutMetadata

type RunState = Literal[
    "accepted",
    "running",
    "succeeded",
    "failed",
    "canceled",
    "timed_out",
    "abandoned",
]
type RunPhase = Literal["provisioning", "executing", "settling", "terminal"]
type DurableAdmissionOutcome = Literal["committed", "possibly_committed"]

TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"done", "error"})
SESSION_TOMBSTONED_ERROR_CODE = "session_tombstoned"


@dataclass
class StartRunRequest:
    """Serializable per-turn input for a run."""

    prompt: str
    session_id: str | None = None
    idempotency_key: str | None = None
    timeout: float | None = None


@dataclass
class RunHandle:
    """Ticket returned when a backend creates a run."""

    run_id: str
    session_id: str
    state: RunState
    created_at: datetime
    phase: RunPhase | None = None


@dataclass
class RunContext:
    """Address of an existing run."""

    run_id: str
    session_id: str


class DurableAdmissionSetupTimeoutError(SetupBudgetExpiredError):
    """Setup timed out after a durable admission may have been recorded."""

    def __init__(
        self,
        *,
        outcome: DurableAdmissionOutcome,
        handle: RunHandle,
        metadata: SetupTimeoutMetadata,
    ) -> None:
        if outcome not in {"committed", "possibly_committed"}:
            raise ValueError("outcome must be 'committed' or 'possibly_committed'")
        super().__init__(metadata)
        self.outcome = outcome
        self.handle = handle


class LinkedActiveRunConflictError(Exception):
    """An active-run conflict with durable management context."""

    def __init__(
        self,
        message: str,
        *,
        session_id: str,
        run_id: str,
        status: RunState,
        phase: RunPhase | None = None,
    ) -> None:
        super().__init__(message)
        self.session_id = session_id
        self.run_id = run_id
        self.status = status
        self.phase = phase


@dataclass
class RunResult:
    """Successful run payload, independent of the execution provider."""

    content: str
    content_intermediate: list[str]
    tool_calls: list[dict[str, object]]
    reasoning: str | None
    delegate_error_count: int


@dataclass
class RunError:
    """Structured, already-sanitized terminal failure."""

    code: str
    message: str
    fault_domain: str | None = None


@dataclass
class RunStatus:
    """Current run state, including a terminal result or error when available."""

    run_id: str
    session_id: str
    state: RunState
    last_sequence: int
    result_available: bool
    result: RunResult | None = None
    error: RunError | None = None
    phase: RunPhase | None = None


@dataclass
class RunEvent:
    """One monotonically sequenced event in a run's journal."""

    sequence: int
    type: str
    data: dict[str, object]
    timestamp: datetime


class EventCursorExpiredError(Exception):
    """Raised when an event cursor is older than the backend's retained journal."""

    def __init__(
        self,
        message: str | None = None,
        *,
        after_sequence: int | None = None,
        earliest_sequence: int | None = None,
    ) -> None:
        self.after_sequence = after_sequence
        self.earliest_sequence = earliest_sequence
        super().__init__(
            message
            or (
                f"Event cursor {after_sequence} expired; "
                f"earliest retained event is {earliest_sequence}"
            )
        )


def assert_event_cursor_available(earliest_sequence: int | None, after_sequence: int) -> None:
    """Raise when an exclusive event cursor precedes retained history."""
    if earliest_sequence is None or after_sequence == 0:
        return
    if after_sequence < earliest_sequence - 1:
        raise EventCursorExpiredError(
            after_sequence=after_sequence,
            earliest_sequence=earliest_sequence,
        )


@runtime_checkable
class AgentExecutionBackend(Protocol):
    """Provider-neutral run lifecycle seam.

    The backend watchdog owns the agent-run deadline from
    ``StartRunRequest.timeout`` (the resolved agent's authored timeout) and
    records watchdog expiry as ``timed_out``. The controller/HTTP layer owns its
    separate synchronous wait boundary of ``min(timeout, 180 seconds)``; it is
    not a seam field. ``in_lang_worker`` execution has no 180-second cap.

    ``cancel_run`` is used only for explicit cancellation or the synchronous
    wait cap. A client disconnect never cancels a run.
    """

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        """Create a run resource and return its handle."""

    async def get_run(self, context: RunContext) -> RunStatus:
        """Return the current run status and terminal result or error, if any."""

    def read_events(
        self, context: RunContext, after_sequence: int
    ) -> AsyncIterator[RunEvent]:
        """Tail events strictly after an exclusive cursor.

        ``after_sequence=0`` reads all retained events and never expires solely
        because the oldest retained event is later than one. When the earliest
        available event is ``E``, resume with ``after_sequence=E - 1``. A cursor
        that has rotated out must raise :class:`EventCursorExpiredError` rather
        than silently skipping the missing events.
        """

    async def cancel_run(self, context: RunContext) -> RunStatus:
        """Explicitly cancel a run and return its resulting status."""


if TYPE_CHECKING:

    class _AsyncGeneratorBackend:
        """Illustrates the async-generator implementation required by the seam."""

        async def start_run(self, request: StartRunRequest) -> RunHandle:
            raise NotImplementedError

        async def get_run(self, context: RunContext) -> RunStatus:
            raise NotImplementedError

        async def read_events(
            self, context: RunContext, after_sequence: int
        ) -> AsyncIterator[RunEvent]:
            if after_sequence < 0:
                yield RunEvent(
                    sequence=0,
                    type="",
                    data={},
                    timestamp=datetime.min,
                )

        async def cancel_run(self, context: RunContext) -> RunStatus:
            raise NotImplementedError

    _async_generator_backend: AgentExecutionBackend = _AsyncGeneratorBackend()
