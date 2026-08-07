from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from azure_functions_agents.controller.streaming import render_events
from azure_functions_agents.execution.backend import (
    EventCursorExpiredError,
    RunContext,
    RunEvent,
    RunStatus,
)


class StreamBackend:
    def __init__(self, events: list[RunEvent] | Exception) -> None:
        self._events = events
        self.cancel_calls = 0

    async def get_run(self, context: RunContext) -> RunStatus:
        return RunStatus(
            run_id=context.run_id,
            session_id=context.session_id,
            state="succeeded",
            last_sequence=1,
            result_available=False,
        )

    def read_events(self, context: RunContext, after_sequence: int) -> AsyncIterator[RunEvent]:
        del context, after_sequence

        async def stream() -> AsyncIterator[RunEvent]:
            if isinstance(self._events, Exception):
                raise self._events
            for event in self._events:
                yield event

        return stream()

    async def cancel_run(self, context: RunContext) -> RunStatus:
        del context
        self.cancel_calls += 1
        raise AssertionError("stream disconnect must not cancel")


@pytest.mark.asyncio
async def test_sse_frames_include_monotonic_ids() -> None:
    backend = StreamBackend(
        [
            RunEvent(
                sequence=7,
                type="delta",
                data={"content": "hello"},
                timestamp=datetime.now(UTC),
            )
        ]
    )

    frames = [
        frame
        async for frame in render_events(
            backend,  # type: ignore[arg-type]
            RunContext(run_id="run-1", session_id="session-1"),
            after_sequence=0,
        )
    ]

    assert frames == ['id: 7\ndata: {"type": "delta", "content": "hello"}\n\n']
    assert backend.cancel_calls == 0


@pytest.mark.asyncio
async def test_evicted_cursor_emits_snapshot_restart_guidance() -> None:
    backend = StreamBackend(
        EventCursorExpiredError(after_sequence=2, earliest_sequence=5)
    )

    frames = [
        frame
        async for frame in render_events(
            backend,  # type: ignore[arg-type]
            RunContext(run_id="run-1", session_id="session-1"),
            after_sequence=2,
        )
    ]

    assert frames[0].startswith("event: snapshot-restart\n")
    assert '"earliest_available_event_id": 5' in frames[0]
    assert '"resume_last_event_id": 4' in frames[0]


@pytest.mark.asyncio
async def test_lease_heartbeat_closes_without_canceling_run() -> None:
    class WaitingBackend(StreamBackend):
        def read_events(self, context: RunContext, after_sequence: int) -> AsyncIterator[RunEvent]:
            del context, after_sequence

            async def stream() -> AsyncIterator[RunEvent]:
                await asyncio.Event().wait()
                if False:
                    yield RunEvent(1, "done", {}, datetime.now(UTC))

            return stream()

    backend = WaitingBackend([])
    stream = render_events(
        backend,  # type: ignore[arg-type]
        RunContext(run_id="run-1", session_id="session-1"),
        after_sequence=0,
        heartbeat_seconds=0.001,
        lease_seconds=0.02,
    )
    frames = [frame async for frame in stream]

    assert frames.count(": heartbeat\n\n") >= 2
    assert backend.cancel_calls == 0
