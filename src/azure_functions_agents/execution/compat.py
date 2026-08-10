"""Adapters between the runner-compatible registration boundary and the seam."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, cast

from .backend import AgentExecutionBackend, RunContext, RunEvent, RunStatus, StartRunRequest
from .binding import AgentBinding
from .result import AgentResult


class SynchronousRunTimeoutError(TimeoutError):
    """The controller stopped waiting while the sandbox run remains attachable."""


def split_runner_call(
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    *,
    stream: bool,
) -> tuple[StartRunRequest, AgentBinding]:
    """Separate per-turn data from the legacy runner-shaped registration call."""
    values = dict(kwargs)
    if len(args) > 1:
        raise TypeError("run_agent accepts at most one positional argument")
    if args and "prompt" in values:
        raise TypeError("prompt was provided both positionally and by keyword")
    if args:
        prompt = args[0]
    else:
        try:
            prompt = values.pop("prompt")
        except KeyError as exc:
            raise TypeError("run_agent missing required argument: 'prompt'") from exc

    request = StartRunRequest(
        prompt=cast(str, prompt),
        session_id=cast(str | None, values.pop("session_id", None)),
        idempotency_key=cast(str | None, values.pop("idempotency_key", None)),
        timeout=cast(float | None, values.pop("timeout", None)),
    )
    binding = AgentBinding(
        instructions=cast(str | None, values.pop("instructions", None)),
        tools=cast(list[Any] | None, values.pop("tools", None)),
        mcp_tools=cast(list[Any] | None, values.pop("mcp_tools", None)),
        skill_paths=cast(list[Any] | None, values.pop("skill_paths", None)),
        model=cast(str | None, values.pop("model", None)),
        sandbox_tools=cast(list[Any] | None, values.pop("sandbox_tools", None)),
        system_addendum=cast(str | None, values.pop("system_addendum", None)),
        workflow_enabled=cast(bool, values.pop("workflow_enabled", False)),
        workflow_durable_client=values.pop("workflow_durable_client", None),
        workflow_policy=values.pop("workflow_policy", None),
        agent_name=cast(str | None, values.pop("agent_name", None)),
        display_name=cast(str | None, values.pop("display_name", None)) if stream else None,
        web_request_tools=cast(list[Any] | None, values.pop("web_request_tools", None)),
        subagents=cast(list[Any] | None, values.pop("subagents", None)),
        catalog=values.pop("catalog", None),
        output_validator=values.pop("output_validator", None),
    )
    if values:
        unexpected = ", ".join(sorted(values))
        raise TypeError(f"unexpected runner keyword argument(s): {unexpected}")
    return request, binding


async def collect_terminal_run(
    backend: AgentExecutionBackend,
    context: RunContext,
    *,
    wait_timeout_seconds: float | None = None,
) -> tuple[RunStatus, list[RunEvent]]:
    """Read a run's complete journal, then return its terminal status."""
    if wait_timeout_seconds is None:
        return await _collect_terminal_run(backend, context)
    try:
        async with asyncio.timeout(wait_timeout_seconds):
            return await _collect_terminal_run(backend, context)
    except TimeoutError:
        raise SynchronousRunTimeoutError(
            "Controller synchronous wait expired while the run remains active."
        ) from None


async def _collect_terminal_run(
    backend: AgentExecutionBackend,
    context: RunContext,
) -> tuple[RunStatus, list[RunEvent]]:
    events = [event async for event in backend.read_events(context, after_sequence=0)]
    return await backend.get_run(context), events


async def run_to_agent_result(
    backend: AgentExecutionBackend,
    request: StartRunRequest,
    *,
    wait_timeout_seconds: float | None = None,
) -> AgentResult:
    """Run a request through the lifecycle and adapt its terminal result."""
    handle = await backend.start_run(request)
    status, events = await collect_terminal_run(
        backend,
        RunContext(run_id=handle.run_id, session_id=handle.session_id),
        wait_timeout_seconds=wait_timeout_seconds,
    )
    return status_to_agent_result(status, events)


def status_to_agent_result(status: RunStatus, events: list[RunEvent]) -> AgentResult:
    """Map a successful lifecycle result to the legacy direct-runner result."""
    if status.state != "succeeded" or status.result is None:
        if status.error is not None:
            if status.error.code == "invalid_argument":
                raise ValueError(status.error.message)
            raise RuntimeError(status.error.message)
        raise RuntimeError(f"agent run ended in state {status.state}")

    result = status.result
    return AgentResult(
        session_id=status.session_id,
        content=result.content,
        content_intermediate=result.content_intermediate,
        tool_calls=cast(list[dict[str, Any]], result.tool_calls),
        reasoning=result.reasoning,
        events=[{"type": event.type, **event.data} for event in events],
        delegate_error_count=result.delegate_error_count,
    )


def render_sse_event(event: RunEvent) -> str:
    """Render a journal event in the exact SSE form used by ``runner``."""
    return f"data: {json.dumps({'type': event.type, **event.data})}\n\n"
