from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from azure_functions_agents.execution.backend import (
    RunContext,
    RunEvent,
    RunHandle,
    RunStatus,
    StartRunRequest,
)
from azure_functions_agents.execution.compat import (
    SynchronousRunTimeoutError,
    collect_terminal_run,
)


class _LiveBackend:
    def __init__(self) -> None:
        self.cancel_calls = 0
        self.reader_started = asyncio.Event()
        self.reader_closed = asyncio.Event()
        self.release_reader = asyncio.Event()

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        return RunHandle(
            run_id="run-1",
            session_id=request.session_id or "session-1",
            state="running",
            created_at=datetime.now(UTC),
        )

    async def get_run(self, context: RunContext) -> RunStatus:
        return RunStatus(
            run_id=context.run_id,
            session_id=context.session_id,
            state="running",
            last_sequence=0,
            result_available=False,
        )

    async def read_events(
        self,
        context: RunContext,
        after_sequence: int,
    ) -> AsyncIterator[RunEvent]:
        self.reader_started.set()
        try:
            await self.release_reader.wait()
            yield RunEvent(
                sequence=after_sequence + 1,
                type="delta",
                data={},
                timestamp=datetime.now(UTC),
            )
        finally:
            self.reader_closed.set()

    async def cancel_run(self, context: RunContext) -> RunStatus:
        self.cancel_calls += 1
        return await self.get_run(context)


@pytest.mark.asyncio
async def test_synchronous_wait_timeout_leaves_the_live_run_attached() -> None:
    backend = _LiveBackend()
    context = RunContext(run_id="run-1", session_id="session-1")
    collect = asyncio.create_task(
        collect_terminal_run(backend, context, wait_timeout_seconds=0.01)
    )
    await asyncio.wait_for(backend.reader_started.wait(), timeout=1.0)

    with pytest.raises(SynchronousRunTimeoutError):
        await collect

    assert backend.reader_closed.is_set()
    assert backend.cancel_calls == 0
    assert (await backend.get_run(context)).state == "running"
