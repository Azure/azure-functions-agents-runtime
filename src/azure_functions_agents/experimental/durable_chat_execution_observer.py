"""Best-effort execution-side observations for opted-in durable chat runs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from .._logger import logger
from .durable_chat_protocol import (
    DURABLE_CHAT_PUBLISHER_DRAIN_DEADLINE_SECONDS,
    MAX_DURABLE_CHAT_TEXT_DELTA_BYTES,
    DurableChatAssistantDraftReplacedObservationV1,
    DurableChatAssistantTextObservationV1,
    DurableChatDrainResultV1,
    DurableChatEnqueueDisposition,
    DurableChatEnqueueResultV1,
    DurableChatJournalPort,
    DurableChatModelAttemptObservationV1,
    DurableChatModelAttemptState,
    DurableChatModelProducerV1,
    DurableChatObservationDrainerPort,
    DurableChatObservationSink,
    DurableChatObservationV1,
    DurableChatProgressObservationV1,
    DurableChatProgressV1,
    DurableChatProtocolDocumentError,
    DurableChatRunStatusObservationV1,
    DurableChatSandboxObservationEventV1,
    DurableChatSandboxObservationV1,
    DurableChatSandboxState,
    DurableChatToolObservationV1,
    DurableChatToolProducerV1,
    DurableChatToolProgressV1,
    DurableChatToolState,
)
from .durable_loop_protocol import (
    DurableLoopRunStatus,
    SandboxExecutionProfile,
    ToolProvenance,
    ToolRequestV1,
    ToolResultStatus,
)

DURABLE_CHAT_FOREGROUND_MODEL_ATTEMPTS: Final[int] = 3
DURABLE_CHAT_OBSERVER_SETUP_DEADLINE_SECONDS: Final[float] = 10.0

_OBSERVATION_ERRORS = (
    DurableChatProtocolDocumentError,
    OSError,
    OverflowError,
    RuntimeError,
    TimeoutError,
    TypeError,
    ValueError,
)
_TOOL_STATES: Final[dict[ToolResultStatus, DurableChatToolState]] = {
    ToolResultStatus.SUCCEEDED: DurableChatToolState.SUCCEEDED,
    ToolResultStatus.FAILED: DurableChatToolState.FAILED,
    ToolResultStatus.TIMED_OUT: DurableChatToolState.TIMED_OUT,
    ToolResultStatus.CANCELLED: DurableChatToolState.CANCELLED,
    ToolResultStatus.AMBIGUOUS: DurableChatToolState.AMBIGUOUS,
}


@dataclass(frozen=True, slots=True)
class DurableChatExecutionObserver:
    """Safe sync capture facade over the non-blocking durable-chat sink."""

    sink: DurableChatObservationSink
    run_id: str
    session_id: str
    drainer: DurableChatObservationDrainerPort | None = None
    journal: DurableChatJournalPort | None = None
    owner_hash: str | None = None

    def model_attempt_started(self, producer: DurableChatModelProducerV1) -> None:
        """Record that a fresh externally-reserved model attempt started."""
        self._capture(
            lambda: DurableChatModelAttemptObservationV1(
                run_id=self.run_id,
                session_id=self.session_id,
                observed_at=_now(),
                producer=producer,
                state=DurableChatModelAttemptState.STARTED,
            )
        )

    def model_attempt_completed(self, producer: DurableChatModelProducerV1) -> None:
        """Record a terminal successful model-attempt boundary."""
        self._capture(
            lambda: DurableChatModelAttemptObservationV1(
                run_id=self.run_id,
                session_id=self.session_id,
                observed_at=_now(),
                producer=producer,
                state=DurableChatModelAttemptState.COMPLETED,
            )
        )

    def model_attempt_failed(self, producer: DurableChatModelProducerV1) -> None:
        """Record a terminal failed model-attempt boundary."""
        self._capture(
            lambda: DurableChatModelAttemptObservationV1(
                run_id=self.run_id,
                session_id=self.session_id,
                observed_at=_now(),
                producer=producer,
                state=DurableChatModelAttemptState.FAILED,
            )
        )

    def model_attempt_superseded(
        self,
        previous_producer: DurableChatModelProducerV1,
        producer: DurableChatModelProducerV1,
    ) -> None:
        """Fence an earlier streamed draft before retrying with a fresh epoch."""
        self._capture(
            lambda: DurableChatAssistantDraftReplacedObservationV1(
                run_id=self.run_id,
                session_id=self.session_id,
                observed_at=_now(),
                previous_producer=previous_producer,
                producer=producer,
            )
        )
        self._capture(
            lambda: DurableChatModelAttemptObservationV1(
                run_id=self.run_id,
                session_id=self.session_id,
                observed_at=_now(),
                producer=previous_producer,
                state=DurableChatModelAttemptState.SUPERSEDED,
            )
        )

    def assistant_text(
        self,
        producer: DurableChatModelProducerV1,
        delta: str,
    ) -> None:
        """Capture only assistant-visible text emitted by a foreground stream."""
        if not delta:
            return
        for text in _bounded_text_chunks(delta):
            def create_text_observation(
                text: str = text,
            ) -> DurableChatAssistantTextObservationV1:
                return DurableChatAssistantTextObservationV1(
                    run_id=self.run_id,
                    session_id=self.session_id,
                    observed_at=_now(),
                    producer=producer,
                    delta=text,
                )

            self._capture(create_text_observation)
        self._capture(
            lambda: DurableChatModelAttemptObservationV1(
                run_id=self.run_id,
                session_id=self.session_id,
                observed_at=_now(),
                producer=producer,
                state=DurableChatModelAttemptState.STREAMING,
            )
        )

    def progress(
        self,
        *,
        status: DurableLoopRunStatus,
        phase: str,
        model_steps: int,
        tool_calls: int,
        human_waits: int,
        step_index: int,
        result_available: bool = False,
    ) -> None:
        """Capture a content-free progress projection from an execution boundary."""
        self._capture(
            lambda: DurableChatProgressObservationV1(
                run_id=self.run_id,
                session_id=self.session_id,
                observed_at=_now(),
                progress=DurableChatProgressV1(
                    status=status,
                    phase=phase,
                    model_steps=model_steps,
                    tool_calls=tool_calls,
                    human_waits=human_waits,
                    step_index=step_index,
                    result_available=result_available,
                    updated_at=_now(),
                ),
            )
        )

    def run_status(
        self,
        *,
        status: DurableLoopRunStatus,
        phase: str,
        result_available: bool = False,
    ) -> None:
        """Capture one content-free run-status boundary."""
        self._capture(
            lambda: DurableChatRunStatusObservationV1(
                run_id=self.run_id,
                session_id=self.session_id,
                observed_at=_now(),
                status=status,
                phase=phase,
                result_available=result_available,
            )
        )

    def tool_started(self, request: ToolRequestV1) -> None:
        """Capture a real dispatch start without exposing arguments or results."""
        self._tool(request, DurableChatToolState.STARTED)

    def tool_finished(
        self,
        request: ToolRequestV1,
        status: ToolResultStatus,
    ) -> None:
        """Capture only the normalized terminal tool state."""
        self._tool(request, _TOOL_STATES[status])

    def remote_no_sandbox(self, request: ToolRequestV1) -> None:
        """Record that a remote MCP call has no local sandbox allocation."""
        if request.provenance is not ToolProvenance.REMOTE:
            return
        self._sandbox(
            request,
            state=DurableChatSandboxState.REMOTE_NO_SANDBOX,
        )

    def local_sandbox(
        self,
        request: ToolRequestV1,
        *,
        state: DurableChatSandboxState,
        sandbox_group_resource_id: str,
        sandbox_id: str | None = None,
        sandbox_generation: int | None = None,
        replaced_sandbox_id: str | None = None,
    ) -> None:
        """Capture one actual local sandbox lifecycle boundary for this call."""
        if request.provenance is not ToolProvenance.LOCAL:
            return
        self._sandbox(
            request,
            state=state,
            sandbox_group_resource_id=sandbox_group_resource_id,
            sandbox_id=sandbox_id,
            sandbox_generation=sandbox_generation,
            replaced_sandbox_id=replaced_sandbox_id,
        )

    def retained_sandbox(
        self,
        *,
        call_key: str,
        step_index: int,
        tool_name: str,
        state: DurableChatSandboxState,
        sandbox_group_resource_id: str,
        sandbox_id: str | None = None,
        sandbox_generation: int | None = None,
    ) -> None:
        """Capture retained cleanup using the last validated local call binding."""
        producer = DurableChatToolProducerV1(call_key=call_key)
        self._capture(
            lambda: DurableChatSandboxObservationEventV1(
                run_id=self.run_id,
                session_id=self.session_id,
                observed_at=_now(),
                observation=DurableChatSandboxObservationV1(
                    producer=producer,
                    step_index=step_index,
                    tool_name=tool_name,
                    provenance=ToolProvenance.LOCAL,
                    sandbox_profile=SandboxExecutionProfile.RETAINED_SESSION,
                    sandbox_group_resource_id=sandbox_group_resource_id,
                    sandbox_id=sandbox_id,
                    sandbox_generation=sandbox_generation,
                    state=state,
                    observed_at=_now(),
                ),
            )
        )

    async def drain(self, *, deadline: datetime) -> DurableChatDrainResultV1 | None:
        """Drain optional observations outside model-retry and tool-timeout scopes."""
        if self.drainer is None:
            return None
        try:
            return await self.drainer.drain(deadline=deadline)
        except _OBSERVATION_ERRORS as exc:
            _record_capture_failure(exc)
            return None

    async def reserve_model_producers(
        self,
        *,
        step_index: int,
        count: int,
        deadline: datetime,
    ) -> tuple[DurableChatModelProducerV1, ...]:
        """Reserve one bounded set of external epochs before a model retry loop."""
        if self.journal is None:
            return ()
        try:
            producers = await self.journal.reserve_model_producers(
                run_id=self.run_id,
                step_index=step_index,
                count=count,
                deadline=deadline,
            )
            if not isinstance(producers, tuple) or any(
                not isinstance(producer, DurableChatModelProducerV1)
                for producer in producers
            ):
                raise TypeError("durable-chat journal returned invalid producers")
            return producers
        except _OBSERVATION_ERRORS as exc:
            _record_capture_failure(exc)
            return ()

    def _tool(
        self,
        request: ToolRequestV1,
        state: DurableChatToolState,
    ) -> None:
        producer = DurableChatToolProducerV1(call_key=request.call_key)
        self._capture(
            lambda: DurableChatToolObservationV1(
                run_id=self.run_id,
                session_id=self.session_id,
                observed_at=_now(),
                progress=DurableChatToolProgressV1(
                    producer=producer,
                    step_index=request.step_index,
                    tool_name=request.tool_name,
                    provenance=request.provenance,
                    state=state,
                    updated_at=_now(),
                ),
            )
        )

    def _sandbox(
        self,
        request: ToolRequestV1,
        *,
        state: DurableChatSandboxState,
        sandbox_group_resource_id: str | None = None,
        sandbox_id: str | None = None,
        sandbox_generation: int | None = None,
        replaced_sandbox_id: str | None = None,
    ) -> None:
        producer = DurableChatToolProducerV1(call_key=request.call_key)
        self._capture(
            lambda: DurableChatSandboxObservationEventV1(
                run_id=self.run_id,
                session_id=self.session_id,
                observed_at=_now(),
                observation=DurableChatSandboxObservationV1(
                    producer=producer,
                    step_index=request.step_index,
                    tool_name=request.tool_name,
                    provenance=request.provenance,
                    sandbox_profile=request.sandbox_profile,
                    sandbox_group_resource_id=sandbox_group_resource_id,
                    sandbox_id=sandbox_id,
                    sandbox_generation=sandbox_generation,
                    state=state,
                    replaced_sandbox_id=replaced_sandbox_id,
                    observed_at=_now(),
                ),
            )
        )

    def _capture(
        self,
        create_observation: Callable[[], DurableChatObservationV1],
    ) -> None:
        try:
            result = self.sink.try_enqueue(observation=create_observation())
            if not isinstance(result, DurableChatEnqueueResultV1):
                raise TypeError("durable-chat sink returned an invalid enqueue result")
            if result.disposition is DurableChatEnqueueDisposition.DROPPED:
                logger.warning("Durable chat observation was dropped.")
        except _OBSERVATION_ERRORS as exc:
            _record_capture_failure(exc)


@dataclass(frozen=True, slots=True)
class DurableChatExecutionContext:
    """Per-activity context that never enters persisted provider inputs."""

    observer: DurableChatExecutionObserver
    model_producers: tuple[DurableChatModelProducerV1, ...] = ()

    def model_producer(
        self,
        attempt: int,
    ) -> DurableChatModelProducerV1 | None:
        """Return a pre-reserved model producer for a one-based retry attempt."""
        if attempt < 1 or attempt > len(self.model_producers):
            return None
        return self.model_producers[attempt - 1]


_execution_context: ContextVar[DurableChatExecutionContext | None] = ContextVar(
    "durable_chat_execution_context",
    default=None,
)


@contextmanager
def use_durable_chat_execution_context(
    context: DurableChatExecutionContext,
) -> Iterator[None]:
    """Make optional observation state available to local execution adapters."""
    token = _execution_context.set(context)
    try:
        yield
    finally:
        _execution_context.reset(token)


def current_durable_chat_execution_context() -> DurableChatExecutionContext | None:
    """Return this activity's optional durable-chat observation context."""
    return _execution_context.get()


def durable_chat_observer_setup_deadline() -> datetime:
    """Return the independent bounded deadline for journal initialization."""
    return _now() + timedelta(seconds=DURABLE_CHAT_OBSERVER_SETUP_DEADLINE_SECONDS)


def durable_chat_observer_drain_deadline() -> datetime:
    """Return the independent bounded deadline for observer publication."""
    return _now() + timedelta(
        seconds=DURABLE_CHAT_PUBLISHER_DRAIN_DEADLINE_SECONDS
    )


def _bounded_text_chunks(value: str) -> Iterator[str]:
    if not value:
        return
    chunk: list[str] = []
    byte_length = 0
    for character in value:
        character_bytes = len(character.encode("utf-8"))
        if chunk and byte_length + character_bytes > MAX_DURABLE_CHAT_TEXT_DELTA_BYTES:
            yield "".join(chunk)
            chunk = []
            byte_length = 0
        chunk.append(character)
        byte_length += character_bytes
    if chunk:
        yield "".join(chunk)


def _now() -> datetime:
    return datetime.now(UTC)


def _record_capture_failure(exc: BaseException) -> None:
    logger.warning(
        "Durable chat observation capture failed (error_type=%s).",
        type(exc).__name__,
    )
