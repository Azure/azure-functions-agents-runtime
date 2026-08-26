"""Provider-neutral HTTP/LRO projections for the ACA session controller."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from .._logger import logger
from ..execution.backend import (
    SESSION_TOMBSTONED_ERROR_CODE,
    AgentExecutionBackend,
    DurableAdmissionOutcome,
    DurableAdmissionSetupTimeoutError,
    LinkedActiveRunConflictError,
    RunContext,
    RunError,
    RunHandle,
    RunPhase,
    RunResult,
    RunState,
    RunStatus,
    StartRunRequest,
)
from ..execution.compat import collect_terminal_run
from ..execution.setup_budget import SetupBudgetExpiredError, SetupTimeoutMetadata
from ..session_state import (
    TERMINAL_RUN_STATUSES,
    ActiveRunConflictError,
    IdempotencyConflictError,
    RunRowNotFoundError,
    SessionRowNotFoundError,
)
from ..transport.transport_models import (
    SANDBOX_GROUP_AUTHORIZATION_ERROR_CODE,
    SANDBOX_GROUP_AUTHORIZATION_MESSAGE,
)
from .budget import RequestBudget, RunDeadlineExceededError
from .idempotency import IdempotencyResultUnavailableError
from .readiness import (
    SessionActivationAuthorizationError,
    SessionActivationGoneError,
    SessionActivationNotFoundError,
    SessionActivationSetupTimeoutError,
)

_RESPOND_ASYNC_PREFERENCE = "respond-async"
_SETUP_TIMEOUT_ERROR_CODE = "setup_deadline_exceeded"
_RUN_TIMEOUT_ERROR_CODE = "run_deadline_exceeded"
_ADMISSION_OUTCOME_UNKNOWN_ERROR_CODE = "admission_outcome_unknown"
_ADMISSION_NOT_RESERVED = "not_reserved"
_ADMISSION_COMMITTED: DurableAdmissionOutcome = "committed"
_ADMISSION_POSSIBLY_COMMITTED: DurableAdmissionOutcome = "possibly_committed"
_PROVISIONING_PHASE: RunPhase = "provisioning"
_DEFAULT_ACCEPTED_RUN_PHASE: RunPhase = "executing"


@dataclass(frozen=True, slots=True)
class ControllerResponse:
    """An Azure-framework-neutral HTTP response projection."""

    status_code: int
    body: dict[str, object] | str
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_metadata: SetupTimeoutMetadata | None = field(
        default=None, repr=False, compare=False
    )


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
        token.strip().split(";", 1)[0].casefold() == _RESPOND_ASYNC_PREFERENCE
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
    defer_response: bool = False,
) -> ControllerResponse:
    """Start a run, return an LRO ticket, or preserve the synchronous contract."""
    started = await _start_run_or_response(
        backend,
        request,
        agent_slug=agent_slug,
        respond_async=respond_async,
    )
    if isinstance(started, ControllerResponse):
        return started
    context = RunContext(run_id=started.run_id, session_id=started.session_id)
    if respond_async or defer_response:
        return _accepted_response(agent_slug, started, context)
    try:
        status, _events = await budget.wait_for(collect_terminal_run(backend, context))
    except RunDeadlineExceededError:
        return await _synchronous_timeout_response(
            backend, context, budget, agent_slug=agent_slug
        )
    return _synchronous_status_response(agent_slug, status)


async def _start_run_or_response(
    backend: AgentExecutionBackend,
    request: StartRunRequest,
    *,
    agent_slug: str,
    respond_async: bool,
) -> RunHandle | ControllerResponse:
    try:
        return await backend.start_run(request)
    except DurableAdmissionSetupTimeoutError as exc:
        return _durable_admission_timeout_response(
            agent_slug,
            exc,
            respond_async=respond_async,
        )
    except (SetupBudgetExpiredError, SessionActivationSetupTimeoutError) as exc:
        return _setup_timeout_response(
            metadata=_with_request_metadata(
                exc.metadata,
                respond_async=respond_async,
                session_present=request.session_id is not None,
            )
        )
    except SessionActivationAuthorizationError:
        return _sandbox_group_authorization_response()
    except LinkedActiveRunConflictError as exc:
        return _linked_active_run_response(
            agent_slug,
            session_id=exc.session_id,
            run_id=exc.run_id,
            status=exc.status,
            phase=exc.phase,
        )
    except ActiveRunConflictError as exc:
        return _active_run_response(exc.active_run_id)
    except IdempotencyConflictError as exc:
        return _idempotency_conflict_response(exc.existing_run_id)
    except IdempotencyResultUnavailableError:
        return ControllerResponse(status_code=410, body={"error": "result_unavailable"})
    except SessionActivationGoneError:
        return ControllerResponse(status_code=410, body={"error": "session_gone"})
    except SessionActivationNotFoundError:
        return ControllerResponse(status_code=404, body={"error": "session_not_found"})


async def _synchronous_timeout_response(
    backend: AgentExecutionBackend,
    context: RunContext,
    budget: RequestBudget,
    *,
    agent_slug: str,
) -> ControllerResponse:
    try:
        status = await budget.wait_for_cleanup(backend.get_run(context))
        if status.state in TERMINAL_RUN_STATUSES:
            return _synchronous_status_response(agent_slug, status)
        await budget.wait_for_cleanup(backend.cancel_run(context))
    except Exception as exc:
        logger.warning(
            "Synchronous run cleanup deferred: session_id=%s run_id=%s error=%s",
            context.session_id,
            context.run_id,
            type(exc).__name__,
        )
    return _run_timeout_response(context)


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
    except SessionActivationNotFoundError:
        return ControllerResponse(status_code=404, body={"error": "run_not_found"})
    except SessionActivationGoneError:
        return ControllerResponse(status_code=410, body={"error": "session_gone"})
    except SessionActivationAuthorizationError:
        return _sandbox_group_authorization_response()


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
    except SessionActivationNotFoundError:
        return ControllerResponse(status_code=404, body={"error": "run_not_found"})
    except SessionActivationGoneError:
        return ControllerResponse(status_code=410, body={"error": "session_gone"})
    except SessionActivationAuthorizationError:
        return _sandbox_group_authorization_response()
    if (
        status.error is not None
        and status.error.code == SESSION_TOMBSTONED_ERROR_CODE
    ) or (status.state in TERMINAL_RUN_STATUSES and not status.result_available):
        return ControllerResponse(
            status_code=410,
            body={"error": "result_unavailable", "state": status.state},
        )
    if (
        status.state == "succeeded"
        and status.result_available
        and status.result is None
    ):
        return ControllerResponse(
            status_code=503,
            body={
                "error": "result_temporarily_unavailable",
                "state": status.state,
            },
            headers={"Retry-After": "2"},
        )
    return ControllerResponse(status_code=200, body=status_payload(status))


async def cancel_run(
    backend: AgentExecutionBackend,
    context: RunContext,
    *,
    touch: Callable[[], Awaitable[None]] | None = None,
) -> ControllerResponse:
    """Cancel a run, or return an accepted projection while launch settles."""
    try:
        if touch is not None:
            await touch()
        status = await backend.cancel_run(context)
        return ControllerResponse(
            status_code=200 if status.state in TERMINAL_RUN_STATUSES else 202,
            body=status_payload(status),
            headers={} if status.state in TERMINAL_RUN_STATUSES else {"Retry-After": "2"},
        )
    except (RunRowNotFoundError, SessionRowNotFoundError):
        return ControllerResponse(status_code=404, body={"error": "run_not_found"})
    except SessionActivationNotFoundError:
        return ControllerResponse(status_code=404, body={"error": "run_not_found"})
    except SessionActivationGoneError:
        return ControllerResponse(status_code=410, body={"error": "session_gone"})
    except SessionActivationAuthorizationError:
        return _sandbox_group_authorization_response()


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
    if status.phase is not None:
        payload["phase"] = status.phase
    return payload


def _accepted_response(
    agent_slug: str,
    handle: RunHandle,
    context: RunContext,
    *,
    admission: DurableAdmissionOutcome | None = None,
    phase: RunPhase | None = None,
    timeout_metadata: SetupTimeoutMetadata | None = None,
) -> ControllerResponse:
    urls = management_urls(agent_slug=agent_slug, context=context)
    return ControllerResponse(
        status_code=202,
        body=_run_ticket_payload(handle, urls, admission=admission, phase=phase),
        headers=_management_headers(context=context, urls=urls),
        timeout_metadata=timeout_metadata,
    )


def _synchronous_status_response(agent_slug: str, status: RunStatus) -> ControllerResponse:
    context = RunContext(session_id=status.session_id, run_id=status.run_id)
    urls = management_urls(agent_slug=agent_slug, context=context)
    headers = {
        "x-ms-session-id": status.session_id,
        "x-ms-run-id": status.run_id,
        "Location": urls["status_url"],
    }
    if status.state == "succeeded" and status.result is not None:
        return ControllerResponse(
            status_code=200,
            body=_result_payload(status.result),
            headers=headers,
        )
    if status.state in TERMINAL_RUN_STATUSES and status.state != "succeeded":
        return ControllerResponse(
            status_code=500,
            body=status_payload(status),
            headers=headers,
        )
    return ControllerResponse(
        status_code=500,
        body={"error": "run_did_not_reach_terminal_state", **status_payload(status)},
        headers=headers,
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


SETUP_TIMEOUT_RETRY_AFTER_SECONDS = 120


def _setup_timeout_response(*, metadata: SetupTimeoutMetadata) -> ControllerResponse:
    return ControllerResponse(
        status_code=504,
        body={
            "error": _SETUP_TIMEOUT_ERROR_CODE,
            "reason": _SETUP_TIMEOUT_ERROR_CODE,
            "retry_with": _RESPOND_ASYNC_PREFERENCE,
            "admission": _ADMISSION_NOT_RESERVED,
        },
        headers={
            "x-ms-retry-with": _RESPOND_ASYNC_PREFERENCE,
            "Retry-After": str(SETUP_TIMEOUT_RETRY_AFTER_SECONDS),
        },
        timeout_metadata=metadata,
    )


def _with_request_metadata(
    metadata: SetupTimeoutMetadata,
    *,
    respond_async: bool,
    session_present: bool,
) -> SetupTimeoutMetadata:
    return SetupTimeoutMetadata.create(
        phase=metadata.phase,
        reason=metadata.reason,
        exception_type=metadata.exception_type,
        configured_budget_seconds=metadata.configured_budget_seconds,
        elapsed_seconds=metadata.elapsed_seconds,
        remaining_seconds=metadata.remaining_seconds,
        request_mode="respond_async" if respond_async else "synchronous",
        session_present=session_present,
    )


def _sandbox_group_authorization_response() -> ControllerResponse:
    return ControllerResponse(
        status_code=503,
        body={
            "error": SANDBOX_GROUP_AUTHORIZATION_ERROR_CODE,
            "reason": SANDBOX_GROUP_AUTHORIZATION_ERROR_CODE,
            "message": SANDBOX_GROUP_AUTHORIZATION_MESSAGE,
        },
    )


def _durable_admission_timeout_response(
    agent_slug: str,
    error: DurableAdmissionSetupTimeoutError,
    *,
    respond_async: bool,
) -> ControllerResponse:
    context = RunContext(
        run_id=error.handle.run_id,
        session_id=error.handle.session_id,
    )
    metadata = _with_request_metadata(
        error.metadata,
        respond_async=respond_async,
        session_present=True,
    )
    phase = error.handle.phase or _PROVISIONING_PHASE
    if error.outcome == _ADMISSION_COMMITTED:
        if respond_async:
            return _accepted_response(
                agent_slug,
                error.handle,
                context,
                admission=_ADMISSION_COMMITTED,
                phase=phase,
                timeout_metadata=metadata,
            )
        return _linked_setup_timeout_response(
            agent_slug,
            error.handle,
            context,
            error_code=_SETUP_TIMEOUT_ERROR_CODE,
            admission=_ADMISSION_COMMITTED,
            phase=phase,
            timeout_metadata=metadata,
        )
    return _linked_setup_timeout_response(
        agent_slug,
        error.handle,
        context,
        error_code=_ADMISSION_OUTCOME_UNKNOWN_ERROR_CODE,
        admission=_ADMISSION_POSSIBLY_COMMITTED,
        phase=phase,
        timeout_metadata=metadata,
    )


def _linked_setup_timeout_response(
    agent_slug: str,
    handle: RunHandle,
    context: RunContext,
    *,
    error_code: str,
    admission: DurableAdmissionOutcome,
    phase: RunPhase,
    timeout_metadata: SetupTimeoutMetadata,
) -> ControllerResponse:
    urls = management_urls(agent_slug=agent_slug, context=context)
    body: dict[str, object] = {
        "error": error_code,
        "reason": error_code,
        "retry_with": _RESPOND_ASYNC_PREFERENCE,
        **_run_ticket_payload(handle, urls, admission=admission, phase=phase),
    }
    return ControllerResponse(
        status_code=504,
        body=body,
        headers=_management_headers(context=context, urls=urls, retry_with=True),
        timeout_metadata=timeout_metadata,
    )


def _run_timeout_response(context: RunContext) -> ControllerResponse:
    return ControllerResponse(
        status_code=504,
        body={
            "error": _RUN_TIMEOUT_ERROR_CODE,
            "reason": _RUN_TIMEOUT_ERROR_CODE,
            "retry_with": _RESPOND_ASYNC_PREFERENCE,
            "session_id": context.session_id,
            "run_id": context.run_id,
        },
        headers={
            "x-ms-retry-with": _RESPOND_ASYNC_PREFERENCE,
            "x-ms-session-id": context.session_id,
        },
    )


def _active_run_response(active_run_id: str | None) -> ControllerResponse:
    body: dict[str, object] = {"error": "active_run_exists"}
    if active_run_id is not None:
        body["run_id"] = active_run_id
    return ControllerResponse(status_code=409, body=body)


def _linked_active_run_response(
    agent_slug: str,
    *,
    session_id: str,
    run_id: str,
    status: RunState,
    phase: RunPhase | None,
) -> ControllerResponse:
    context = RunContext(run_id=run_id, session_id=session_id)
    urls = management_urls(agent_slug=agent_slug, context=context)
    body: dict[str, object] = {
        "error": "active_run_exists",
        "session_id": session_id,
        "run_id": run_id,
        "status": status,
        **urls,
    }
    if phase is not None:
        body["phase"] = phase
    return ControllerResponse(
        status_code=409,
        body=body,
        headers={
            "Location": urls["status_url"],
            "x-ms-session-id": session_id,
        },
    )


def _run_ticket_payload(
    handle: RunHandle,
    urls: Mapping[str, str],
    *,
    admission: DurableAdmissionOutcome | None = None,
    phase: RunPhase | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": handle.session_id,
        "run_id": handle.run_id,
        "status": handle.state,
    }
    if admission is not None:
        payload["admission"] = admission
    payload["phase"] = _ticket_phase(handle, phase)
    payload.update(urls)
    return payload


def _ticket_phase(handle: RunHandle, phase: RunPhase | None) -> RunPhase:
    if phase is not None:
        return phase
    if handle.phase is not None:
        return handle.phase
    if handle.state in TERMINAL_RUN_STATUSES:
        return "terminal"
    return _DEFAULT_ACCEPTED_RUN_PHASE


def _management_headers(
    *,
    context: RunContext,
    urls: Mapping[str, str],
    retry_with: bool = False,
) -> dict[str, str]:
    headers = {
        "Location": urls["status_url"],
        "Retry-After": "2",
        "x-ms-session-id": context.session_id,
    }
    if retry_with:
        headers["x-ms-retry-with"] = _RESPOND_ASYNC_PREFERENCE
    return headers


def _idempotency_conflict_response(existing_run_id: str | None) -> ControllerResponse:
    body: dict[str, object] = {"error": "idempotency_key_conflict"}
    if existing_run_id is not None:
        body["run_id"] = existing_run_id
    return ControllerResponse(status_code=422, body=body)
