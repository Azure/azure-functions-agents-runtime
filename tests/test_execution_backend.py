from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import get_args

import pytest

from azure_functions_agents.execution.backend import (
    AgentExecutionBackend,
    EventCursorExpiredError,
    RunContext,
    RunError,
    RunEvent,
    RunHandle,
    RunResult,
    RunState,
    RunStatus,
    StartRunRequest,
)


class InMemoryExecutionBackend:
    """Reusable conformance fake with a retained event window."""

    def __init__(self, events: list[RunEvent], earliest_sequence: int) -> None:
        self._events = events
        self._earliest_sequence = earliest_sequence
        self._context: RunContext | None = None
        self._status: RunStatus | None = None

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        session_id = request.session_id or "ephemeral-session"
        self._context = RunContext(run_id="run-1", session_id=session_id)
        self._status = RunStatus(
            run_id=self._context.run_id,
            session_id=session_id,
            state="accepted",
            last_sequence=max((event.sequence for event in self._events), default=0),
            result_available=False,
        )
        return RunHandle(
            run_id=self._context.run_id,
            session_id=session_id,
            state="accepted",
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
        )

    async def get_run(self, context: RunContext) -> RunStatus:
        self._validate_context(context)
        assert self._status is not None
        return self._status

    async def read_events(
        self, context: RunContext, after_sequence: int
    ) -> AsyncIterator[RunEvent]:
        self._validate_context(context)
        if after_sequence != 0 and after_sequence < self._earliest_sequence - 1:
            raise EventCursorExpiredError(f"events before {self._earliest_sequence} have expired")
        for event in self._events:
            if event.sequence > after_sequence:
                yield event

    async def cancel_run(self, context: RunContext) -> RunStatus:
        self._validate_context(context)
        assert self._status is not None
        self._status = RunStatus(
            run_id=self._status.run_id,
            session_id=self._status.session_id,
            state="canceled",
            last_sequence=self._status.last_sequence,
            result_available=False,
        )
        return self._status

    def _validate_context(self, context: RunContext) -> None:
        if context != self._context:
            raise ValueError("unknown run context")


def _events() -> list[RunEvent]:
    timestamp = datetime(2026, 7, 28, tzinfo=UTC)
    return [
        RunEvent(sequence=3, type="delta", data={"content": "a"}, timestamp=timestamp),
        RunEvent(sequence=4, type="delta", data={"content": "b"}, timestamp=timestamp),
        RunEvent(sequence=5, type="done", data={}, timestamp=timestamp),
    ]


def _started_backend() -> tuple[InMemoryExecutionBackend, RunContext]:
    backend = InMemoryExecutionBackend(_events(), earliest_sequence=3)
    handle = asyncio.run(
        backend.start_run(
            StartRunRequest(
                prompt="hello",
                session_id="session-1",
                idempotency_key="request-1",
                timeout=60.0,
            )
        )
    )
    return backend, RunContext(run_id=handle.run_id, session_id=handle.session_id)


async def collect_run_events(
    backend: AgentExecutionBackend, context: RunContext, after_sequence: int
) -> list[RunEvent]:
    return [
        event async for event in backend.read_events(context=context, after_sequence=after_sequence)
    ]


async def assert_event_cursor_conformance(
    backend: AgentExecutionBackend,
    context: RunContext,
    *,
    retained_sequences: tuple[int, ...],
    earliest_available_sequence: int,
    too_old_cursor: int,
) -> None:
    """Assert the shared exclusive-cursor guarantees for an event backend."""

    assert retained_sequences
    assert retained_sequences[0] == earliest_available_sequence
    assert too_old_cursor < earliest_available_sequence - 1

    assert [
        event.sequence
        for event in await collect_run_events(backend, context, after_sequence=0)
    ] == list(retained_sequences)
    assert [
        event.sequence
        for event in await collect_run_events(
            backend, context, after_sequence=earliest_available_sequence - 1
        )
    ] == list(retained_sequences)
    assert [
        event.sequence
        for event in await collect_run_events(
            backend, context, after_sequence=earliest_available_sequence
        )
    ] == list(retained_sequences[1:])
    with pytest.raises(EventCursorExpiredError):
        await collect_run_events(backend, context, after_sequence=too_old_cursor)


def test_contract_dataclasses_and_run_state_literals() -> None:
    timestamp = datetime(2026, 7, 28, tzinfo=UTC)
    request = StartRunRequest(
        prompt="hello",
        session_id="session-1",
        idempotency_key="request-1",
        timeout=30.0,
    )
    handle = RunHandle("run-1", "session-1", "accepted", timestamp)
    context = RunContext("run-1", "session-1")
    result = RunResult("answer", ["partial"], [{"name": "tool"}], None, 0)
    error = RunError("sandbox_unavailable", "sandbox is unavailable", "sandbox-provision")
    status = RunStatus("run-1", "session-1", "succeeded", 4, True, result, None)
    failed_status = RunStatus("run-1", "session-1", "failed", 5, False, None, error)
    event = RunEvent(1, "message", {"content": "answer"}, timestamp)

    assert request.session_id == handle.session_id == context.session_id
    assert status.result == result
    assert failed_status.error == error
    assert event.sequence == 1
    assert get_args(RunState.__value__) == (
        "accepted",
        "running",
        "succeeded",
        "failed",
        "canceled",
        "timed_out",
        "abandoned",
    )
    assert {"succeeded", "failed", "canceled", "timed_out", "abandoned"} == {
        state
        for state in get_args(RunState.__value__)
        if state not in {"accepted", "running"}
    }
    assert len({"canceled", "timed_out", "abandoned"}) == 3


def test_cursor_expiry_is_not_a_run_error() -> None:
    cursor_error = EventCursorExpiredError("expired")
    run_error = RunError("run_failed", "failure")

    assert EventCursorExpiredError.__bases__ == (Exception,)
    assert isinstance(cursor_error, Exception)
    assert isinstance(run_error, RunError)
    assert not isinstance(cursor_error, RunError)
    assert not issubclass(EventCursorExpiredError, RunError)


def test_in_memory_backend_satisfies_protocol_and_lifecycle() -> None:
    backend, context = _started_backend()

    assert isinstance(backend, AgentExecutionBackend)
    assert asyncio.run(backend.get_run(context)).state == "accepted"
    assert asyncio.run(backend.cancel_run(context)).state == "canceled"


def test_in_memory_backend_cursor_conformance() -> None:
    backend, context = _started_backend()

    asyncio.run(
        assert_event_cursor_conformance(
            backend,
            context,
            retained_sequences=(3, 4, 5),
            earliest_available_sequence=3,
            too_old_cursor=1,
        )
    )
    assert asyncio.run(collect_run_events(backend, context, after_sequence=5)) == []
