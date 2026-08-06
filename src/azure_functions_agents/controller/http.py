"""Provider-neutral HTTP/LRO projections for the ACA session controller."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from ..execution.backend import (
    SESSION_TOMBSTONED_ERROR_CODE,
    AgentExecutionBackend,
    RunContext,
    RunError,
    RunHandle,
    RunResult,
    RunStatus,
    StartRunRequest,
)
from ..execution.compat import collect_terminal_run
from ..execution.setup_budget import SetupBudgetExpiredError
from ..session_state import (
    TERMINAL_RUN_STATUSES,
    ActiveRunConflictError,
    IdempotencyConflictError,
    RunRowNotFoundError,
    SessionRowNotFoundError,
)
from .budget import RequestBudget, RunDeadlineExceededError
from .idempotency import IdempotencyResultUnavailableError
from .readiness import SessionActivationGoneError, SessionActivationSetupTimeoutError


@dataclass(frozen=True, slots=True)
class ControllerResponse:
    """An Azure-framework-neutral HTTP response projection."""

    status_code: int
    body: dict[str, object] | str
    headers: Mapping[str, str] = field(default_factory=dict)


def prefers_respond_async(headers: Mapping[str, str] | None) -> bool:
    """Parse a case-insensitive RFC token list without treating other preferences as errors."""
    if headers is None:
        return False
    prefer = next(
        (
            value
            for key, value in headers.items()
            if key.casefold() == "prefer" and isinstance(value, str)
        ),
        "",
    )
    return any(
        token.strip().split(";", 1)[0].casefold() == "respond-async"
        for token in prefer.split(",")
    )


def parse_last_event_id(headers: Mapping[str, str] | None) -> int:
    """Return the exclusive replay cursor encoded by ``Last-Event-ID``."""
    if headers is None:
        return 0
    value = next(
        (
            item
            for key, item in headers.items()
            if key.casefold() == "last-event-id" and isinstance(item, str)
        ),
        None,
    )
    if value is None or not value.strip():
        return 0
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError("Last-Event-ID must be a non-negative integer.") from exc
    if parsed < 0:
        raise ValueError("Last-Event-ID must be a non-negative integer.")
    return parsed


def management_urls(*, agent_slug: str, context: RunContext) -> dict[str, str]:
    """Build one shared set of session-scoped management URLs."""
    base = f"/agents/{agent_slug}/sessions/{context.session_id}/runs/{context.run_id}"
    return {
        "status_url": base,
        "result_url": f"{base}/result",
        "events_url": f"{base}/events",
        "cancel_url": f"{base}/cancel",
    }


async def submit_run(
    backend: AgentExecutionBackend,
    request: StartRunRequest,
    *,
    agent_slug: str,
    respond_async: bool,
    budget: RequestBudget,
) -> ControllerResponse:
    """Start a run, return an LRO ticket, or preserve the synchronous contract."""
    try:
        handle = await backend.start_run(request)
    except (SetupBudgetExpiredError, SessionActivationSetupTimeoutError):
        return _setup_timeout_response()
    except ActiveRunConflictError as exc:
        return _active_run_response(exc.active_run_id)
    except IdempotencyConflictError as exc:
        return _idempotency_conflict_response(exc.existing_run_id)
    except IdempotencyResultUnavailableError:
        return ControllerResponse(status_code=410, body={"error": "result_unavailable"})
    except SessionActivationGoneError:
        return ControllerResponse(status_code=410, body={"error": "session_gone"})

    context = RunContext(run_id=handle.run_id, session_id=handle.session_id)
    if respond_async:
        return _accepted_response(agent_slug, handle, context)

    try:
        status, _events = await budget.wait_for(collect_terminal_run(backend, context))
    except RunDeadlineExceededError:
        status = await backend.get_run(context)
        if status.state in TERMINAL_RUN_STATUSES:
            return _synchronous_status_response(status)
        await backend.cancel_run(context)
        return _run_timeout_response(context)
    return _synchronous_status_response(status)


async def read_status(
    backend: AgentExecutionBackend,
    context: RunContext,
    *,
    touch: Callable[[], Awaitable[None]] | None = None,
) -> ControllerResponse:
    """Return durable run status; a readable terminal failure remains an HTTP 200."""
    try:
        if touch is not None:
            await touch()
        return ControllerResponse(status_code=200, body=status_payload(await backend.get_run(context)))
    except (RunRowNotFoundError, SessionRowNotFoundError):
        return ControllerResponse(status_code=404, body={"error": "run_not_found"})


async def read_result(
    backend: AgentExecutionBackend,
    context: RunContext,
    *,
    touch: Callable[[], Awaitable[None]] | None = None,
) -> ControllerResponse:
    """Return nonterminal state or ``410`` when a terminal result is unavailable."""
    try:
        if touch is not None:
            await touch()
        status = await backend.get_run(context)
    except (RunRowNotFoundError, SessionRowNotFoundError):
        return ControllerResponse(status_code=404, body={"error": "run_not_found"})
    if (
        status.error is not None
        and status.error.code == SESSION_TOMBSTONED_ERROR_CODE
    ) or (status.state in TERMINAL_RUN_STATUSES and not status.result_available):
        return ControllerResponse(
            status_code=410,
            body={"error": "result_unavailable", "state": status.state},
        )
    return ControllerResponse(status_code=200, body=status_payload(status))


async def cancel_run(
    backend: AgentExecutionBackend,
    context: RunContext,
    *,
    touch: Callable[[], Awaitable[None]] | None = None,
) -> ControllerResponse:
    """Cancel an attached run and return the terminal durable projection."""
    try:
        if touch is not None:
            await touch()
        return ControllerResponse(
            status_code=200,
            body=status_payload(await backend.cancel_run(context)),
        )
    except (RunRowNotFoundError, SessionRowNotFoundError):
        return ControllerResponse(status_code=404, body={"error": "run_not_found"})


def status_payload(status: RunStatus) -> dict[str, object]:
    """Render all durable status fields without exposing backend-specific objects."""
    payload: dict[str, object] = {
        "session_id": status.session_id,
        "run_id": status.run_id,
        "status": status.state,
        "state": status.state,
        "last_event_id": status.last_sequence,
        "result_available": status.result_available,
    }
    if status.result is not None:
        payload["result"] = _result_payload(status.result)
    if status.error is not None:
        payload["error"] = _error_payload(status.error)
    return payload


def _accepted_response(
    agent_slug: str,
    handle: RunHandle,
    context: RunContext,
) -> ControllerResponse:
    urls = management_urls(agent_slug=agent_slug, context=context)
    return ControllerResponse(
        status_code=202,
        body={
            "session_id": handle.session_id,
            "run_id": handle.run_id,
            "status": handle.state,
            **urls,
        },
        headers={
            "Location": urls["status_url"],
            "Retry-After": "2",
            "x-ms-session-id": handle.session_id,
        },
    )


def _synchronous_status_response(status: RunStatus) -> ControllerResponse:
    if status.state == "succeeded" and status.result is not None:
        return ControllerResponse(
            status_code=200,
            body=_result_payload(status.result),
            headers={"x-ms-session-id": status.session_id},
        )
    if status.state in TERMINAL_RUN_STATUSES and status.state != "succeeded":
        return ControllerResponse(
            status_code=500,
            body=status_payload(status),
            headers={"x-ms-session-id": status.session_id},
        )
    return ControllerResponse(
        status_code=500,
        body={"error": "run_did_not_reach_terminal_state", **status_payload(status)},
        headers={"x-ms-session-id": status.session_id},
    )


def _result_payload(result: RunResult) -> dict[str, object]:
    return {
        "response": result.content,
        "content": result.content,
        "content_intermediate": result.content_intermediate,
        "tool_calls": result.tool_calls,
        "reasoning": result.reasoning,
        "delegate_error_count": result.delegate_error_count,
    }


def _error_payload(error: RunError) -> dict[str, object]:
    payload: dict[str, object] = {"code": error.code, "message": error.message}
    if error.fault_domain is not None:
        payload["fault_domain"] = error.fault_domain
    return payload


def _setup_timeout_response() -> ControllerResponse:
    return ControllerResponse(
        status_code=504,
        body={
            "error": "setup_deadline_exceeded",
            "reason": "setup_deadline_exceeded",
            "retry_with": "respond-async",
        },
        headers={"x-ms-retry-with": "respond-async"},
    )


def _run_timeout_response(context: RunContext) -> ControllerResponse:
    return ControllerResponse(
        status_code=504,
        body={
            "error": "run_deadline_exceeded",
            "reason": "run_deadline_exceeded",
            "retry_with": "respond-async",
        },
        headers={
            "x-ms-retry-with": "respond-async",
            "x-ms-session-id": context.session_id,
        },
    )


def _active_run_response(active_run_id: str | None) -> ControllerResponse:
    body: dict[str, object] = {"error": "active_run_exists"}
    if active_run_id is not None:
        body["run_id"] = active_run_id
    return ControllerResponse(status_code=409, body=body)


def _idempotency_conflict_response(existing_run_id: str | None) -> ControllerResponse:
    body: dict[str, object] = {"error": "idempotency_key_conflict"}
    if existing_run_id is not None:
        body["run_id"] = existing_run_id
    return ControllerResponse(status_code=422, body=body)
