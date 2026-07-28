"""In-process implementation of the execution backend seam."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol, cast

from .backend import (
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

if TYPE_CHECKING:
    from ..runner import AgentResult


class _RunnerModule(Protocol):
    async def run_agent(self, *args: Any, **kwargs: Any) -> AgentResult:
        """Run a non-streaming agent call."""

    def run_agent_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        """Create a streaming agent call."""

_TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled", "timed_out", "abandoned"})
_RUNNER_TIMEOUT_PREFIX = "Agent run timed out after "


def _load_runner_module() -> _RunnerModule:
    return cast(_RunnerModule, import_module("azure_functions_agents.runner"))


@dataclass
class _LocalRun:
    """Mutable in-memory state for one locally executed run."""

    handle: RunHandle
    status: RunStatus
    events: list[RunEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task[None] | None = None
    next_sequence: int = 1


class LocalExecutionBackend:
    """Run agents in process while preserving the existing runner behavior."""

    def __init__(
        self,
        *,
        event_retention: int = 16,
    ) -> None:
        if event_retention < 1:
            raise ValueError("event_retention must be positive")

        self._event_retention = event_retention
        self._runs: dict[tuple[str, str], _LocalRun] = {}

    async def run_agent(self, *args: Any, **kwargs: Any) -> AgentResult:
        """Delegate a non-streaming call without changing its inputs or result."""
        return await _load_runner_module().run_agent(*args, **kwargs)

    def run_agent_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        """Delegate a streaming call without changing its inputs or events."""
        return _load_runner_module().run_agent_stream(*args, **kwargs)

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        """Create and begin an in-process run."""
        session_id = request.session_id or uuid.uuid4().hex
        run_id = uuid.uuid4().hex
        handle = RunHandle(
            run_id=run_id,
            session_id=session_id,
            state="accepted",
            created_at=datetime.now(UTC),
        )
        run = _LocalRun(
            handle=handle,
            status=RunStatus(
                run_id=run_id,
                session_id=session_id,
                state="accepted",
                last_sequence=0,
                result_available=False,
            ),
        )
        self._runs[(run_id, session_id)] = run
        run.task = asyncio.create_task(self._execute_run(run, request, session_id))
        return handle

    async def get_run(self, context: RunContext) -> RunStatus:
        """Return the current local status for a run."""
        return self._resolve_run(context).status

    async def read_events(
        self, context: RunContext, after_sequence: int
    ) -> AsyncIterator[RunEvent]:
        """Tail the retained local journal after an exclusive cursor."""
        run = self._resolve_run(context)
        cursor = after_sequence

        while True:
            async with run.condition:
                earliest_sequence = run.events[0].sequence if run.events else None
                if (
                    cursor != 0
                    and earliest_sequence is not None
                    and cursor < earliest_sequence - 1
                ):
                    raise EventCursorExpiredError(
                        f"events before {earliest_sequence} have expired"
                    )

                events = [event for event in run.events if event.sequence > cursor]
                if not events:
                    if run.status.state in _TERMINAL_STATES:
                        return
                    await run.condition.wait()
                    continue

            for event in events:
                cursor = event.sequence
                yield event

    async def cancel_run(self, context: RunContext) -> RunStatus:
        """Cancel an active local run without affecting completed runs."""
        run = self._resolve_run(context)
        async with run.condition:
            if run.status.state in _TERMINAL_STATES:
                return run.status
            task = run.task

        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        async with run.condition:
            if run.status.state not in _TERMINAL_STATES:
                run.status = RunStatus(
                    run_id=run.handle.run_id,
                    session_id=run.handle.session_id,
                    state="canceled",
                    last_sequence=run.next_sequence - 1,
                    result_available=False,
                )
                run.condition.notify_all()
            return run.status

    async def _execute_run(
        self, run: _LocalRun, request: StartRunRequest, session_id: str
    ) -> None:
        async with run.condition:
            run.status = RunStatus(
                run_id=run.handle.run_id,
                session_id=session_id,
                state="running",
                last_sequence=run.next_sequence - 1,
                result_available=False,
            )
            run.condition.notify_all()

        try:
            agent_result = await self.run_agent(
                request.prompt,
                session_id=session_id,
                timeout=request.timeout,
            )
        except asyncio.CancelledError:
            await self._record_terminal_status(run, "canceled")
            raise
        except TimeoutError as exc:
            await self._record_failure(run, "timed_out", exc)
        except RuntimeError as exc:
            if str(exc).startswith(_RUNNER_TIMEOUT_PREFIX):
                await self._record_failure(run, "timed_out", exc)
            else:
                await self._record_failure(run, "failed", exc)
        except Exception as exc:
            await self._record_failure(run, "failed", exc)
        else:
            try:
                await self._record_success(run, agent_result)
            except Exception as exc:
                await self._record_failure(run, "failed", exc)

    async def _record_success(self, run: _LocalRun, agent_result: AgentResult) -> None:
        events = self._to_run_events(agent_result.events, run.next_sequence)
        result = RunResult(
            content=agent_result.content,
            content_intermediate=agent_result.content_intermediate,
            tool_calls=agent_result.tool_calls,
            reasoning=agent_result.reasoning,
            delegate_error_count=agent_result.delegate_error_count,
        )

        async with run.condition:
            run.events.extend(events)
            if len(run.events) > self._event_retention:
                del run.events[: len(run.events) - self._event_retention]
            run.next_sequence += len(events)
            run.status = RunStatus(
                run_id=run.handle.run_id,
                session_id=run.handle.session_id,
                state="succeeded",
                last_sequence=run.next_sequence - 1,
                result_available=True,
                result=result,
            )
            run.condition.notify_all()

    async def _record_failure(self, run: _LocalRun, state: RunState, exc: Exception) -> None:
        message = str(exc) or type(exc).__name__
        error = RunError(
            code="run_timed_out" if state == "timed_out" else "run_failed",
            message=message,
            fault_domain="runtime",
        )
        async with run.condition:
            run.status = RunStatus(
                run_id=run.handle.run_id,
                session_id=run.handle.session_id,
                state=state,
                last_sequence=run.next_sequence - 1,
                result_available=False,
                error=error,
            )
            run.condition.notify_all()

    async def _record_terminal_status(self, run: _LocalRun, state: RunState) -> None:
        async with run.condition:
            run.status = RunStatus(
                run_id=run.handle.run_id,
                session_id=run.handle.session_id,
                state=state,
                last_sequence=run.next_sequence - 1,
                result_available=False,
            )
            run.condition.notify_all()

    @staticmethod
    def _to_run_events(
        events: list[dict[str, Any]], first_sequence: int
    ) -> list[RunEvent]:
        timestamp = datetime.now(UTC)
        run_events: list[RunEvent] = []
        for offset, event in enumerate(events):
            event_type = event.get("type")
            if not isinstance(event_type, str):
                raise ValueError("runner event is missing a string type")
            run_events.append(
                RunEvent(
                    sequence=first_sequence + offset,
                    type=event_type,
                    data={key: value for key, value in event.items() if key != "type"},
                    timestamp=timestamp,
                )
            )
        return run_events

    def _resolve_run(self, context: RunContext) -> _LocalRun:
        try:
            return self._runs[(context.run_id, context.session_id)]
        except KeyError:
            raise ValueError("unknown run context") from None
