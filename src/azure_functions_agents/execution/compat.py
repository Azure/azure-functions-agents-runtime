"""Adapters between the runner-compatible registration boundary and the seam."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from .backend import AgentExecutionBackend, RunContext, RunEvent, RunStatus, StartRunRequest
from .binding import AgentBinding
from .result import AgentResult


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
        agent_name=cast(str | None, values.pop("agent_name", None)),
        display_name=cast(str | None, values.pop("display_name", None)) if stream else None,
        web_request_tools=cast(list[Any] | None, values.pop("web_request_tools", None)),
        subagents=cast(list[Any] | None, values.pop("subagents", None)),
        catalog=values.pop("catalog", None),
    )
    if values:
        unexpected = ", ".join(sorted(values))
        raise TypeError(f"unexpected runner keyword argument(s): {unexpected}")
    return request, binding


async def collect_terminal_run(
    backend: AgentExecutionBackend,
    context: RunContext,
) -> tuple[RunStatus, list[RunEvent]]:
    """Read a run's complete journal, then return its terminal status."""
    events = [event async for event in backend.read_events(context, after_sequence=0)]
    return await backend.get_run(context), events


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
