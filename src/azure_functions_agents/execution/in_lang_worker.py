"""Language-worker implementation of the provider-neutral execution lifecycle."""

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

from .._logger import logger
from .backend import (
    TERMINAL_EVENT_TYPES,
    RunContext,
    RunError,
    RunEvent,
    RunHandle,
    RunResult,
    RunState,
    RunStatus,
    StartRunRequest,
    assert_event_cursor_available,
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
class _LanguageWorkerRun:
    request: StartRunRequest
    session_id: str
    status: RunStatus
    events: list[RunEvent]
    task: asyncio.Task[None] | None = None
    terminalizer_task: asyncio.Task[None] | None = None
    next_sequence: int = 1


class LanguageWorkerExecutionBackend:
    """Execute one bound agent in the language worker process through the lifecycle seam."""

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
        self._runs: dict[str, _LanguageWorkerRun] = {}
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
        run = _LanguageWorkerRun(request=request, session_id=session_id, status=status, events=[])
        async with self._condition:
            self._runs[run_id] = run
            task = asyncio.create_task(self._execute_run(run))
            task.add_done_callback(lambda completed: self._ensure_task_terminal(run, completed))
            run.task = task
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
                assert_event_cursor_available(
                    run.events[0].sequence if run.events else None,
                    after_sequence,
                )
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

    async def _execute_run(self, run: _LanguageWorkerRun) -> None:
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
                logger.exception("Agent run failed")
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
            logger.exception("Agent run failed")
            await self._record_failure(
                run,
                state="failed",
                code="run_failed",
                message=_exception_message(exc),
            )
        finally:
            await self._record_terminal_status(run, "abandoned")

    async def _execute_standard(self, run: _LanguageWorkerRun) -> None:
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

    async def _execute_stream(self, run: _LanguageWorkerRun) -> None:
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
        await self._record_success(run)

    async def _append_event(
        self,
        run: _LanguageWorkerRun,
        event_type: str,
        data: dict[str, object],
    ) -> None:
        async with self._condition:
            self._append_event_locked(run, event_type, data)
            self._condition.notify_all()

    def _append_event_locked(
        self,
        run: _LanguageWorkerRun,
        event_type: str,
        data: dict[str, object],
    ) -> None:
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

    async def _record_success(self, run: _LanguageWorkerRun, result: RunResult | None = None) -> None:
        async with self._condition:
            run.status = RunStatus(
                run_id=run.status.run_id,
                session_id=run.session_id,
                state="succeeded",
                last_sequence=run.next_sequence - 1,
                result_available=result is not None,
                result=result,
            )
            self._condition.notify_all()

    async def _record_failure(
        self,
        run: _LanguageWorkerRun,
        *,
        state: RunState,
        code: str,
        message: str,
        fault_domain: str | None = "runtime",
    ) -> None:
        async with self._condition:
            if run.status.state in _TERMINAL_STATES:
                return
            if self._stream_events and not _ends_with_terminal_event(run.events):
                self._append_event_locked(run, "error", {"content": message})
            run.status = RunStatus(
                run_id=run.status.run_id,
                session_id=run.session_id,
                state=state,
                last_sequence=run.next_sequence - 1,
                result_available=False,
                error=RunError(code=code, message=message, fault_domain=fault_domain),
            )
            self._condition.notify_all()

    def _ensure_task_terminal(self, run: _LanguageWorkerRun, task: asyncio.Future[None]) -> None:
        if task.cancelled():
            state: RunState = "canceled"
        else:
            failure = task.exception()
            if failure is None:
                return
            logger.error(
                "Agent execution task exited unexpectedly",
                exc_info=(type(failure), failure, failure.__traceback__),
            )
            state = "abandoned"
        run.terminalizer_task = asyncio.create_task(self._record_terminal_status(run, state))

    async def _record_terminal_status(self, run: _LanguageWorkerRun, state: RunState) -> None:
        async with self._condition:
            if run.status.state in _TERMINAL_STATES:
                return
            if (
                self._stream_events
                and state != "canceled"
                and not _ends_with_terminal_event(run.events)
            ):
                self._append_event_locked(
                    run,
                    "error",
                    {"content": f"agent run ended in state {state}"},
                )
            run.status = RunStatus(
                run_id=run.status.run_id,
                session_id=run.session_id,
                state=state,
                last_sequence=run.next_sequence - 1,
                result_available=False,
            )
            self._condition.notify_all()

    def _require_run(self, context: RunContext) -> _LanguageWorkerRun:
        run = self._runs.get(context.run_id)
        if run is None or run.session_id != context.session_id:
            raise ValueError("unknown run context")
        return run

    @staticmethod
    def _load_runner_module() -> _RunnerModule:
        return import_module("azure_functions_agents.runner")


def _ends_with_terminal_event(events: list[RunEvent]) -> bool:
    return bool(events) and events[-1].type in TERMINAL_EVENT_TYPES


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


def _exception_message(exc: Exception) -> str:
    return str(exc) or type(exc).__name__
