"""In-process implementation of the provider-neutral execution lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, Protocol
from uuid import uuid4

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
from .binding import AgentBinding
from .result import AgentResult

_RUNNER_TIMEOUT_PREFIX = "Agent run timed out after "
_STREAM_TIMEOUT_PREFIX = "Timeout after "
_TERMINAL_STATES: frozenset[RunState] = frozenset(
    {"succeeded", "failed", "canceled", "timed_out", "abandoned"}
)


class _RunnerModule(Protocol):
    async def run_agent(self, prompt: str, **kwargs: Any) -> AgentResult: ...

    def run_agent_stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[str]: ...


@dataclass
class _LocalRun:
    request: StartRunRequest
    session_id: str
    status: RunStatus
    events: list[RunEvent]
    task: asyncio.Task[None] | None = None
    next_sequence: int = 1


class LocalExecutionBackend:
    """Execute one bound agent in-process through the lifecycle seam."""

    def __init__(
        self,
        binding: AgentBinding,
        *,
        stream_events: bool = False,
        event_retention: int | None = None,
    ) -> None:
        if event_retention is not None and event_retention < 1:
            raise ValueError("event_retention must be positive when specified")
        self._binding = binding
        self._stream_events = stream_events
        self._event_retention = event_retention
        self._runs: dict[str, _LocalRun] = {}
        self._condition = asyncio.Condition()

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        """Start the bound agent without importing the runner until execution."""
        run_id = uuid4().hex
        session_id = request.session_id or uuid4().hex
        status = RunStatus(
            run_id=run_id,
            session_id=session_id,
            state="accepted",
            last_sequence=0,
            result_available=False,
        )
        run = _LocalRun(request=request, session_id=session_id, status=status, events=[])
        async with self._condition:
            self._runs[run_id] = run
            run.task = asyncio.create_task(self._execute_run(run))
        return RunHandle(
            run_id=run_id,
            session_id=session_id,
            state="accepted",
            created_at=datetime.now(UTC),
        )

    async def get_run(self, context: RunContext) -> RunStatus:
        async with self._condition:
            return self._require_run(context).status

    async def cancel_run(self, context: RunContext) -> RunStatus:
        async with self._condition:
            run = self._require_run(context)
            task = run.task
            if run.status.state in _TERMINAL_STATES or task is None:
                return run.status
            task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await task

        await self._record_terminal_status(run, "canceled")
        async with self._condition:
            return self._require_run(context).status

    async def read_events(
        self,
        context: RunContext,
        after_sequence: int,
    ) -> AsyncIterator[RunEvent]:
        while True:
            async with self._condition:
                run = self._require_run(context)
                self._assert_cursor_available(run, after_sequence)
                pending = [event for event in run.events if event.sequence > after_sequence]
                terminal = run.status.state in _TERMINAL_STATES
                if not pending and not terminal:
                    await self._condition.wait()
                    continue

            for event in pending:
                after_sequence = event.sequence
                yield event
            if terminal:
                return

    async def _execute_run(self, run: _LocalRun) -> None:
        async with self._condition:
            run.status = replace(run.status, state="running")
            self._condition.notify_all()

        try:
            if self._stream_events:
                await self._execute_stream(run)
            else:
                await self._execute_standard(run)
        except asyncio.CancelledError:
            await self._record_terminal_status(run, "canceled")
            raise
        except TimeoutError as exc:
            await self._record_failure(
                run,
                state="timed_out",
                code="run_timed_out",
                message=_exception_message(exc),
            )
        except RuntimeError as exc:
            if str(exc).startswith(_RUNNER_TIMEOUT_PREFIX):
                await self._record_failure(
                    run,
                    state="timed_out",
                    code="run_timed_out",
                    message=_exception_message(exc),
                )
            else:
                await self._record_failure(
                    run,
                    state="failed",
                    code="run_failed",
                    message=_exception_message(exc),
                )
        except ValueError as exc:
            await self._record_failure(
                run,
                state="failed",
                code="invalid_argument",
                message=_exception_message(exc),
                fault_domain="app",
            )
        except Exception as exc:
            await self._record_failure(
                run,
                state="failed",
                code="run_failed",
                message=_exception_message(exc),
            )

    async def _execute_standard(self, run: _LocalRun) -> None:
        runner_module = self._load_runner_module()
        agent_result = await runner_module.run_agent(
            run.request.prompt,
            session_id=run.session_id,
            timeout=run.request.timeout,
            **self._binding.runner_kwargs(stream=False),
        )
        for event in agent_result.events:
            event_type = event.get("type")
            if not isinstance(event_type, str):
                raise RuntimeError("runner event missing string type")
            await self._append_event(
                run,
                event_type,
                {key: value for key, value in event.items() if key != "type"},
            )
        await self._record_success(
            run,
            RunResult(
                content=agent_result.content,
                content_intermediate=agent_result.content_intermediate,
                tool_calls=agent_result.tool_calls,
                reasoning=agent_result.reasoning,
                delegate_error_count=agent_result.delegate_error_count,
            ),
        )

    async def _execute_stream(self, run: _LocalRun) -> None:
        runner_module = self._load_runner_module()
        saw_terminal = False
        stream_error: RunError | None = None
        async for chunk in runner_module.run_agent_stream(
            run.request.prompt,
            session_id=run.session_id,
            timeout=run.request.timeout,
            **self._binding.runner_kwargs(stream=True),
        ):
            event_type, data = _parse_sse_chunk(chunk)
            await self._append_event(run, event_type, data)
            if event_type == "error":
                message = str(data.get("content", "Agent stream failed"))
                state: RunState = (
                    "timed_out" if message.startswith(_STREAM_TIMEOUT_PREFIX) else "failed"
                )
                stream_error = RunError(
                    code="run_timed_out" if state == "timed_out" else "run_failed",
                    message=message,
                )
                saw_terminal = True
            elif event_type == "done":
                saw_terminal = True

        if not saw_terminal:
            raise RuntimeError("agent stream ended without a terminal event")
        if stream_error is not None:
            await self._record_failure(
                run,
                state="timed_out" if stream_error.code == "run_timed_out" else "failed",
                code=stream_error.code,
                message=stream_error.message,
            )
            return
        await self._record_success(run, _stream_result(run.events))

    async def _append_event(
        self,
        run: _LocalRun,
        event_type: str,
        data: dict[str, object],
    ) -> None:
        async with self._condition:
            event = RunEvent(
                sequence=run.next_sequence,
                type=event_type,
                data=data,
                timestamp=datetime.now(UTC),
            )
            run.next_sequence += 1
            run.events.append(event)
            if self._event_retention is not None and len(run.events) > self._event_retention:
                del run.events[: len(run.events) - self._event_retention]
            run.status = replace(run.status, last_sequence=event.sequence)
            self._condition.notify_all()

    async def _record_success(self, run: _LocalRun, result: RunResult) -> None:
        async with self._condition:
            run.status = RunStatus(
                run_id=run.status.run_id,
                session_id=run.session_id,
                state="succeeded",
                last_sequence=run.next_sequence - 1,
                result_available=True,
                result=result,
            )
            self._condition.notify_all()

    async def _record_failure(
        self,
        run: _LocalRun,
        *,
        state: RunState,
        code: str,
        message: str,
        fault_domain: str | None = "runtime",
    ) -> None:
        async with self._condition:
            if run.status.state in _TERMINAL_STATES:
                return
            run.status = RunStatus(
                run_id=run.status.run_id,
                session_id=run.session_id,
                state=state,
                last_sequence=run.next_sequence - 1,
                result_available=False,
                error=RunError(code=code, message=message, fault_domain=fault_domain),
            )
            self._condition.notify_all()

    async def _record_terminal_status(self, run: _LocalRun, state: RunState) -> None:
        async with self._condition:
            if run.status.state in _TERMINAL_STATES:
                return
            run.status = RunStatus(
                run_id=run.status.run_id,
                session_id=run.session_id,
                state=state,
                last_sequence=run.next_sequence - 1,
                result_available=False,
            )
            self._condition.notify_all()

    def _require_run(self, context: RunContext) -> _LocalRun:
        run = self._runs.get(context.run_id)
        if run is None or run.session_id != context.session_id:
            raise ValueError("unknown run context")
        return run

    @staticmethod
    def _assert_cursor_available(run: _LocalRun, after_sequence: int) -> None:
        if not run.events or after_sequence == 0:
            return
        earliest = run.events[0].sequence
        if after_sequence < earliest - 1:
            raise EventCursorExpiredError(
                f"Event cursor {after_sequence} expired; earliest retained event is {earliest}"
            )

    @staticmethod
    def _load_runner_module() -> _RunnerModule:
        return import_module("azure_functions_agents.runner")


def _parse_sse_chunk(chunk: str) -> tuple[str, dict[str, object]]:
    if not chunk.startswith("data: ") or not chunk.endswith("\n\n"):
        raise RuntimeError("runner stream emitted an invalid SSE chunk")
    payload = json.loads(chunk.removeprefix("data: ").removesuffix("\n\n"))
    if not isinstance(payload, dict):
        raise RuntimeError("runner stream emitted a non-object SSE payload")
    event_type = payload.pop("type", None)
    if not isinstance(event_type, str):
        raise RuntimeError("runner stream emitted an SSE payload without a string type")
    return event_type, payload


def _stream_result(events: list[RunEvent]) -> RunResult:
    content_parts: list[str] = []
    intermediate: list[str] = []
    tool_calls: list[dict[str, object]] = []

    for event in events:
        if event.type in {"delta", "message"}:
            content = event.data.get("content")
            if isinstance(content, str):
                content_parts.append(content)
        elif event.type == "intermediate":
            content = event.data.get("content")
            if isinstance(content, str):
                intermediate.append(content)
        elif event.type == "tool_start":
            tool_calls.append({"type": event.type, **event.data})
        elif event.type == "tool_end":
            call_id = event.data.get("tool_call_id")
            for tool_call in reversed(tool_calls):
                if tool_call.get("tool_call_id") == call_id:
                    tool_call["result"] = event.data.get("result")
                    break

    return RunResult(
        content="".join(content_parts),
        content_intermediate=intermediate,
        tool_calls=tool_calls,
        reasoning=None,
        delegate_error_count=0,
    )


def _exception_message(exc: Exception) -> str:
    return str(exc) or type(exc).__name__
