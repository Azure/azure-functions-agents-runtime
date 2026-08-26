"""SSE rendering for replayable ACA run journals."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable

from ..execution.backend import AgentExecutionBackend, EventCursorExpiredError, RunContext, RunEvent
from ..session_state import TERMINAL_RUN_STATUSES
from .http import status_payload

DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_LEASE_SECONDS = 210.0


def render_event(event: RunEvent) -> str:
    """Render a replayable data frame with the journal sequence as its SSE ID."""
    return (
        f"id: {event.sequence}\n"
        f"data: {json.dumps({'type': event.type, **event.data}, ensure_ascii=False)}\n\n"
    )


def render_snapshot_restart(error: EventCursorExpiredError) -> str:
    """Tell a caller how to reconcile an evicted cursor without skipping event E."""
    earliest = error.earliest_sequence
    payload = {
        "reason": "last-event-id-evicted",
        "earliest_available_event_id": earliest,
        "resume_last_event_id": None if earliest is None else earliest - 1,
        "guidance": "Read status/result, then reconnect at earliest_available_event_id - 1.",
    }
    return f"event: snapshot-restart\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def render_events(
    backend: AgentExecutionBackend,
    context: RunContext,
    *,
    after_sequence: int,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
) -> AsyncIterator[str]:
    """Render a bounded, heartbeat-bearing SSE lease without cancelling the run."""
    if heartbeat_seconds <= 0 or lease_seconds <= 0:
        raise ValueError("heartbeat_seconds and lease_seconds must be positive")
    if after_sequence < 0:
        raise ValueError("after_sequence must be non-negative")
    lease_deadline = clock() + lease_seconds if deadline is None else deadline
    iterator = backend.read_events(context, after_sequence).__aiter__()
    pending_event: asyncio.Future[RunEvent] | None = None
    try:
        while True:
            remaining = lease_deadline - clock()
            if remaining <= 0:
                return
            if pending_event is None:
                pending_event = asyncio.ensure_future(anext(iterator))
            try:
                completed, _ = await asyncio.wait(
                    {pending_event},
                    timeout=min(heartbeat_seconds, remaining),
                )
                if not completed:
                    yield ": heartbeat\n\n"
                    continue
                event = pending_event.result()
                pending_event = None
            except StopAsyncIteration:
                status = await backend.get_run(context)
                if status.state in TERMINAL_RUN_STATUSES and status.state != "succeeded":
                    yield (
                        "event: error\n"
                        f"data: {json.dumps(status_payload(status), ensure_ascii=False)}\n\n"
                    )
                return
            except EventCursorExpiredError as error:
                yield render_snapshot_restart(error)
                return
            yield render_event(event)
    finally:
        if pending_event is not None and not pending_event.done():
            pending_event.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending_event
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()
