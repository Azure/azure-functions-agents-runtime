from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import azure_functions_agents.transport.aca_sdk as aca_sdk
from azure_functions_agents.controller.budget import RequestBudget
from azure_functions_agents.controller.http import (
    cancel_run,
    prefers_respond_async,
    read_result,
    read_status,
    submit_run,
)
from azure_functions_agents.controller.idempotency import IdempotencyResultUnavailableError
from azure_functions_agents.controller.readiness import (
    SessionActivationAuthorizationError,
    SessionActivationConflictError,
    SessionActivationNotFoundError,
    SessionActivationSetupTimeoutError,
    SessionRuntimeBinding,
    StateStoreBinding,
)
from azure_functions_agents.execution.backend import (
    SESSION_TOMBSTONED_ERROR_CODE,
    DurableAdmissionOutcome,
    DurableAdmissionSetupTimeoutError,
    LinkedActiveRunConflictError,
    RunContext,
    RunError,
    RunEvent,
    RunHandle,
    RunPhase,
    RunResult,
    RunStatus,
    StartRunRequest,
)
from azure_functions_agents.execution.setup_budget import (
    SetupBudget,
    SetupBudgetExpiredError,
    SetupPhase,
    SetupTimeoutExceptionType,
    SetupTimeoutMetadata,
    SetupTimeoutReason,
)
from azure_functions_agents.session_state import (
    ActiveRunConflictError,
    AppIdentity,
    IdempotencyConflictError,
    RunRowNotFoundError,
)
from azure_functions_agents.transport.ports import SandboxSessionProvider
from azure_functions_agents.transport.transport_models import SandboxGroupBindingError


class FakeBackend:
    def __init__(self, status: RunStatus) -> None:
        self.status = status
        self.cancelled = False
        self.started = False
        self.requests: list[StartRunRequest] = []
        self.raise_on_start: Exception | None = None
        self.raise_on_get: Exception | None = None
        self.raise_on_cancel: Exception | None = None
        self.hang_on_get = False
        self.hang_on_cancel = False

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        self.requests.append(request)
        if self.raise_on_start is not None:
            raise self.raise_on_start
        self.started = True
        return RunHandle(
            run_id=self.status.run_id,
            session_id=self.status.session_id,
            state=self.status.state,
            created_at=datetime.now(UTC),
            phase=self.status.phase,
        )

    async def get_run(self, context: RunContext) -> RunStatus:
        assert context.run_id == self.status.run_id
        if self.raise_on_get is not None:
            raise self.raise_on_get
        if self.hang_on_get:
            await asyncio.Event().wait()
        return self.status

    def read_events(self, context: RunContext, after_sequence: int) -> AsyncIterator[RunEvent]:
        del context, after_sequence

        async def stream() -> AsyncIterator[RunEvent]:
            if self.status.state not in {"succeeded", "failed", "canceled", "timed_out", "abandoned"}:
                await asyncio.Event().wait()
            if False:
                yield RunEvent(0, "done", {}, datetime.now(UTC))

        return stream()

    async def cancel_run(self, context: RunContext) -> RunStatus:
        assert context.run_id == self.status.run_id
        if self.raise_on_cancel is not None:
            raise self.raise_on_cancel
        if self.hang_on_cancel:
            await asyncio.Event().wait()
        self.cancelled = True
        self.status = RunStatus(
            run_id=self.status.run_id,
            session_id=self.status.session_id,
            state="canceled",
            last_sequence=self.status.last_sequence,
            result_available=False,
            error=RunError(code="run_canceled", message="Canceled"),
        )
        return self.status


def _status(
    *,
    state: str = "accepted",
    result_available: bool = False,
    result: RunResult | None = None,
    error: RunError | None = None,
    phase: RunPhase | None = None,
) -> RunStatus:
    return RunStatus(
        run_id="run-1",
        session_id="session-1",
        state=state,  # type: ignore[arg-type]
        last_sequence=0,
        result_available=result_available,
        result=result,
        error=error,
        phase=phase,
    )


def _expired_budget() -> RequestBudget:
    return RequestBudget(
        wall_deadline=0.0,
        setup=SetupBudget.create(deadline=1.0, clock=lambda: 0.0),
        _clock=lambda: 1.0,
    )


def _durable_admission_timeout(
    *,
    outcome: DurableAdmissionOutcome,
    handle: RunHandle,
) -> DurableAdmissionSetupTimeoutError:
    return DurableAdmissionSetupTimeoutError(
        outcome=outcome,
        handle=handle,
        metadata=SetupTimeoutMetadata.create(
            phase=SetupPhase.PROVISION_CREATE,
            reason=SetupTimeoutReason.DEADLINE_ELAPSED,
            exception_type=SetupTimeoutExceptionType.SETUP_BUDGET_EXPIRED,
            configured_budget_seconds=90.0,
            elapsed_seconds=90.0,
            remaining_seconds=0.0,
        ),
    )


def _management_urls(*, session_id: str, run_id: str) -> dict[str, str]:
    base = f"/agents/main/sessions/{session_id}/runs/{run_id}"
    return {
        "status_url": base,
        "result_url": f"{base}/result",
        "events_url": f"{base}/events",
        "cancel_url": f"{base}/cancel",
    }


def test_durable_admission_timeout_is_a_provider_neutral_public_contract() -> None:
    handle = RunHandle(
        run_id="run-1",
        session_id="session-1",
        state="accepted",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="outcome"):
        DurableAdmissionSetupTimeoutError(  # type: ignore[arg-type]
            outcome="not_reserved",
            handle=handle,
            metadata=SetupTimeoutMetadata.create(
                phase=SetupPhase.PROVISION_CREATE,
                reason=SetupTimeoutReason.DEADLINE_ELAPSED,
                exception_type=SetupTimeoutExceptionType.SETUP_BUDGET_EXPIRED,
                configured_budget_seconds=90.0,
                elapsed_seconds=90.0,
                remaining_seconds=0.0,
            ),
        )


@pytest.mark.asyncio
async def test_async_submission_returns_shared_management_urls() -> None:
    backend = FakeBackend(_status(phase="provisioning"))

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 202
    assert response.headers["Location"].endswith("/sessions/session-1/runs/run-1")
    assert isinstance(response.body, dict)
    assert response.body["events_url"].endswith("/events")
    assert response.body["phase"] == "provisioning"


@pytest.mark.asyncio
async def test_async_submission_derives_phase_when_the_handle_has_none() -> None:
    backend = FakeBackend(_status())

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 202
    assert response.body["phase"] == "executing"


@pytest.mark.asyncio
async def test_status_payload_includes_provider_neutral_phase_when_available() -> None:
    backend = FakeBackend(_status(phase="settling"))

    response = await read_status(
        backend,  # type: ignore[arg-type]
        RunContext(run_id="run-1", session_id="session-1"),
    )

    assert response.status_code == 200
    assert response.body["phase"] == "settling"


@pytest.mark.asyncio
async def test_sync_success_keeps_session_identity_in_the_response_header() -> None:
    backend = FakeBackend(
        _status(
            state="succeeded",
            result_available=True,
            result=RunResult(
                content="answer",
                content_intermediate=[],
                tool_calls=[],
                reasoning=None,
                delegate_error_count=0,
            ),
        )
    )

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=False,
        budget=RequestBudget.start(authored_timeout=1),
    )

    assert response.status_code == 200
    assert response.body == {
        "response": "answer",
        "content": "answer",
        "content_intermediate": [],
        "tool_calls": [],
        "reasoning": None,
        "delegate_error_count": 0,
    }
    assert response.headers["x-ms-session-id"] == "session-1"


@pytest.mark.asyncio
async def test_prefer_only_changes_submission_projection() -> None:
    request = StartRunRequest(
        prompt="hello",
        session_id="session-1",
        idempotency_key="caller-key",
        timeout=30.0,
    )
    completed = _status(
        state="succeeded",
        result_available=True,
        result=RunResult(
            content="answer",
            content_intermediate=[],
            tool_calls=[],
            reasoning=None,
            delegate_error_count=0,
        ),
    )
    async_backend = FakeBackend(completed)
    sync_backend = FakeBackend(completed)

    async_response = await submit_run(
        async_backend,  # type: ignore[arg-type]
        request,
        agent_slug="main",
        respond_async=prefers_respond_async({"Prefer": "wait=5, respond-async"}),
        budget=RequestBudget.start(authored_timeout=30.0),
    )
    sync_response = await submit_run(
        sync_backend,  # type: ignore[arg-type]
        request,
        agent_slug="main",
        respond_async=prefers_respond_async({"Prefer": "wait=5"}),
        budget=RequestBudget.start(authored_timeout=30.0),
    )

    assert async_backend.requests == sync_backend.requests == [request]
    assert async_response.status_code == 202
    assert sync_response.status_code == 200
    assert async_response.headers["x-ms-session-id"] == sync_response.headers["x-ms-session-id"]


@pytest.mark.asyncio
async def test_sync_wall_expiry_cancels_before_returning_typed_timeout() -> None:
    backend = FakeBackend(_status(state="running"))

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=False,
        budget=_expired_budget(),
    )

    assert backend.cancelled
    assert response.status_code == 504
    assert response.body == {
        "error": "run_deadline_exceeded",
        "reason": "run_deadline_exceeded",
        "retry_with": "respond-async",
        "session_id": "session-1",
        "run_id": "run-1",
    }
    assert response.headers["x-ms-session-id"] == "session-1"


def _cleanup_budget() -> RequestBudget:
    return RequestBudget(
        wall_deadline=0.0,
        setup=SetupBudget.create(deadline=1.0, clock=lambda: 0.0),
        _clock=lambda: 0.0,
    )


@pytest.mark.asyncio
async def test_sync_deadline_bounds_a_hanging_status_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "azure_functions_agents.controller.budget.CLEANUP_HEADROOM_SECONDS",
        0.01,
    )
    backend = FakeBackend(_status(state="running"))
    backend.hang_on_get = True

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=False,
        budget=_cleanup_budget(),
    )

    assert response.status_code == 504
    assert isinstance(response.body, dict)
    assert response.body["run_id"] == "run-1"
    assert response.body["session_id"] == "session-1"
    assert not backend.cancelled


@pytest.mark.asyncio
async def test_sync_deadline_bounds_a_hanging_cancel_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "azure_functions_agents.controller.budget.CLEANUP_HEADROOM_SECONDS",
        0.01,
    )
    backend = FakeBackend(_status(state="running"))
    backend.hang_on_cancel = True

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=False,
        budget=_cleanup_budget(),
    )

    assert response.status_code == 504
    assert isinstance(response.body, dict)
    assert response.body["run_id"] == "run-1"
    assert response.body["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_sync_deadline_provider_cleanup_error_returns_typed_timeout() -> None:
    backend = FakeBackend(_status(state="running"))
    backend.raise_on_get = RuntimeError("provider unavailable")

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=False,
        budget=_cleanup_budget(),
    )

    assert response.status_code == 504
    assert isinstance(response.body, dict)
    assert response.body["run_id"] == "run-1"
    assert response.body["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_not_reserved_setup_timeout_is_context_free() -> None:
    backend = FakeBackend(_status())
    backend.raise_on_start = SetupBudgetExpiredError(
        SetupTimeoutMetadata.create(
            phase=SetupPhase.PROVISION_CREATE,
            reason=SetupTimeoutReason.DEADLINE_ELAPSED,
            exception_type=SetupTimeoutExceptionType.SETUP_BUDGET_EXPIRED,
            configured_budget_seconds=90.0,
            elapsed_seconds=90.0,
            remaining_seconds=0.0,
        )
    )

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert not backend.started
    assert response.status_code == 504
    assert response.body == {
        "error": "setup_deadline_exceeded",
        "reason": "setup_deadline_exceeded",
        "retry_with": "respond-async",
        "admission": "not_reserved",
    }
    assert response.headers == {
        "x-ms-retry-with": "respond-async",
        "Retry-After": "120",
    }


@pytest.mark.asyncio
async def test_confirmed_setup_timeout_projects_the_reserved_handle_by_preference() -> None:
    handle = RunHandle(
        run_id="run-1",
        session_id="session-1",
        state="accepted",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    urls = _management_urls(session_id=handle.session_id, run_id=handle.run_id)
    request = StartRunRequest(prompt="hello", idempotency_key="caller-key", timeout=30.0)

    async_backend = FakeBackend(_status())
    async_backend.raise_on_start = _durable_admission_timeout(
        outcome="committed",
        handle=handle,
    )
    async_response = await submit_run(
        async_backend,  # type: ignore[arg-type]
        request,
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    sync_backend = FakeBackend(_status())
    sync_backend.raise_on_start = _durable_admission_timeout(
        outcome="committed",
        handle=handle,
    )
    sync_response = await submit_run(
        sync_backend,  # type: ignore[arg-type]
        request,
        agent_slug="main",
        respond_async=False,
        budget=_expired_budget(),
    )

    assert async_response.status_code == 202
    assert async_response.body == {
        "session_id": "session-1",
        "run_id": "run-1",
        "status": "accepted",
        "admission": "committed",
        "phase": "provisioning",
        **urls,
    }
    assert async_response.headers == {
        "Location": urls["status_url"],
        "Retry-After": "2",
        "x-ms-session-id": "session-1",
    }
    assert sync_response.status_code == 504
    assert sync_response.body == {
        "error": "setup_deadline_exceeded",
        "reason": "setup_deadline_exceeded",
        "retry_with": "respond-async",
        "admission": "committed",
        "session_id": "session-1",
        "run_id": "run-1",
        "status": "accepted",
        "phase": "provisioning",
        **urls,
    }
    assert sync_response.headers == {
        "Location": urls["status_url"],
        "Retry-After": "2",
        "x-ms-session-id": "session-1",
        "x-ms-retry-with": "respond-async",
    }


@pytest.mark.asyncio
async def test_ambiguous_setup_timeout_returns_a_linked_confirmation_ticket() -> None:
    handle = RunHandle(
        run_id="run-1",
        session_id="session-1",
        state="accepted",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    urls = _management_urls(session_id=handle.session_id, run_id=handle.run_id)
    backend = FakeBackend(_status())
    backend.raise_on_start = _durable_admission_timeout(
        outcome="possibly_committed",
        handle=handle,
    )

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello", idempotency_key="caller-key", timeout=30.0),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 504
    assert response.body == {
        "error": "admission_outcome_unknown",
        "reason": "admission_outcome_unknown",
        "retry_with": "respond-async",
        "admission": "possibly_committed",
        "session_id": "session-1",
        "run_id": "run-1",
        "status": "accepted",
        "phase": "provisioning",
        **urls,
    }
    assert response.headers == {
        "Location": urls["status_url"],
        "Retry-After": "2",
        "x-ms-session-id": "session-1",
        "x-ms-retry-with": "respond-async",
    }


@pytest.mark.asyncio
async def test_linked_active_run_conflict_has_management_context() -> None:
    conflict = LinkedActiveRunConflictError(
        "session already has an active run",
        session_id="session-1",
        run_id="run-active",
        status="accepted",
        phase="provisioning",
    )
    urls = _management_urls(session_id="session-1", run_id="run-active")
    backend = FakeBackend(_status())
    backend.raise_on_start = conflict

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello", session_id="session-1", idempotency_key="next-key"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 409
    assert response.body == {
        "error": "active_run_exists",
        "session_id": "session-1",
        "run_id": "run-active",
        "status": "accepted",
        "phase": "provisioning",
        **urls,
    }
    assert response.headers == {
        "Location": urls["status_url"],
        "x-ms-session-id": "session-1",
    }


@pytest.mark.asyncio
async def test_unlinked_active_run_conflict_keeps_legacy_projection() -> None:
    backend = FakeBackend(_status())
    backend.raise_on_start = ActiveRunConflictError(
        "session already has an active run",
        active_run_id="run-active",
    )

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello", session_id="session-1"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 409
    assert response.body == {"error": "active_run_exists", "run_id": "run-active"}
    assert response.headers == {}


@pytest.mark.asyncio
async def test_live_provision_lease_returns_the_same_setup_retry_hint() -> None:
    backend = FakeBackend(_status())
    backend.raise_on_start = SessionActivationSetupTimeoutError(
        SetupTimeoutMetadata.create(
            phase=SetupPhase.PROVISION_RECONCILE,
            reason=SetupTimeoutReason.PROVISION_LEASE_LIVE,
            exception_type=SetupTimeoutExceptionType.SESSION_ACTIVATION_SETUP_TIMEOUT,
            configured_budget_seconds=90.0,
            elapsed_seconds=90.0,
            remaining_seconds=0.0,
        )
    )

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 504
    assert response.body == {
        "error": "setup_deadline_exceeded",
        "reason": "setup_deadline_exceeded",
        "retry_with": "respond-async",
        "admission": "not_reserved",
    }
    assert response.headers == {
        "x-ms-retry-with": "respond-async",
        "Retry-After": "120",
    }


@pytest.mark.asyncio
async def test_sandbox_group_authorization_failure_returns_actionable_nonretryable_response() -> None:
    backend = FakeBackend(_status())
    backend.raise_on_start = SessionActivationAuthorizationError(
        "Sandbox Group data-plane authorization failed."
    )

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 503
    assert response.body == {
        "error": "sandbox_group_authorization_failed",
        "reason": "sandbox_group_authorization_failed",
        "message": (
            "Sandbox Group data-plane authorization failed. Grant the controller "
            "identity 'Container Apps SandboxGroup Data Owner' on the configured "
            "Sandbox Group."
        ),
    }
    assert response.headers == {}


@pytest.mark.asyncio
async def test_cancel_sandbox_group_authorization_failure_returns_actionable_nonretryable_response() -> (
    None
):
    backend = FakeBackend(_status())
    backend.raise_on_cancel = SessionActivationAuthorizationError(
        "Sandbox Group data-plane authorization failed."
    )

    response = await cancel_run(
        backend,  # type: ignore[arg-type]
        RunContext(run_id="run-1", session_id="session-1"),
    )

    assert response.status_code == 503
    assert response.body == {
        "error": "sandbox_group_authorization_failed",
        "reason": "sandbox_group_authorization_failed",
        "message": (
            "Sandbox Group data-plane authorization failed. Grant the controller "
            "identity 'Container Apps SandboxGroup Data Owner' on the configured "
            "Sandbox Group."
        ),
    }
    assert response.headers == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", [read_status, read_result])
async def test_status_and_result_authorization_failures_are_redacted(
    reader,
) -> None:  # type: ignore[no-untyped-def]
    backend = FakeBackend(_status())
    backend.raise_on_get = SessionActivationAuthorizationError("provider body: secret")

    response = await reader(
        backend,  # type: ignore[arg-type]
        RunContext(run_id="run-1", session_id="session-1"),
    )

    assert response.status_code == 503
    assert response.body == {
        "error": "sandbox_group_authorization_failed",
        "reason": "sandbox_group_authorization_failed",
        "message": (
            "Sandbox Group data-plane authorization failed. Grant the controller "
            "identity 'Container Apps SandboxGroup Data Owner' on the configured "
            "Sandbox Group."
        ),
    }
    assert "secret" not in str(response.body)


@pytest.mark.asyncio
async def test_evicted_idempotent_result_returns_gone() -> None:
    backend = FakeBackend(_status())
    backend.raise_on_start = IdempotencyResultUnavailableError("evicted")

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 410


@pytest.mark.asyncio
async def test_idempotency_payload_mutation_returns_unprocessable_content() -> None:
    backend = FakeBackend(_status())
    backend.raise_on_start = IdempotencyConflictError(
        "idempotency key already used with a different payload",
        existing_run_id="run-1",
    )

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="changed", idempotency_key="caller-key", timeout=31.0),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 422
    assert response.body == {
        "error": "idempotency_key_conflict",
        "run_id": "run-1",
    }


@pytest.mark.asyncio
async def test_unknown_session_submission_returns_sanitized_not_found() -> None:
    backend = FakeBackend(_status())
    backend.raise_on_start = SessionActivationNotFoundError(
        "Session was not found for this owner."
    )

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello", session_id="unknown-session"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 404
    assert response.body == {"error": "session_not_found"}


@pytest.mark.asyncio
async def test_failed_status_remains_readable_when_no_result_is_available() -> None:
    backend = FakeBackend(
        _status(
            state="failed",
            error=RunError(code="run_failed", message="failed"),
        )
    )
    context = RunContext(run_id="run-1", session_id="session-1")

    status_response = await read_status(backend, context)  # type: ignore[arg-type]
    result_response = await read_result(backend, context)  # type: ignore[arg-type]

    assert status_response.status_code == 200
    assert isinstance(status_response.body, dict)
    assert status_response.body["error"] == {"code": "run_failed", "message": "failed"}
    assert result_response.status_code == 410


@pytest.mark.asyncio
async def test_succeeded_unmaterialized_result_is_temporarily_unavailable() -> None:
    backend = FakeBackend(_status(state="succeeded", result_available=True))
    context = RunContext(run_id="run-1", session_id="session-1")

    response = await read_result(backend, context)  # type: ignore[arg-type]

    assert response.status_code == 503
    assert response.body == {
        "error": "result_temporarily_unavailable",
        "state": "succeeded",
    }
    assert response.headers == {"Retry-After": "2"}


@pytest.mark.asyncio
async def test_result_endpoint_recovers_when_a_success_result_materializes() -> None:
    backend = FakeBackend(_status(state="succeeded", result_available=True))
    context = RunContext(run_id="run-1", session_id="session-1")

    first = await read_result(backend, context)  # type: ignore[arg-type]
    backend.status = _status(
        state="succeeded",
        result_available=True,
        result=RunResult(
            content="answer",
            content_intermediate=[],
            tool_calls=[],
            reasoning=None,
            delegate_error_count=0,
        ),
    )
    second = await read_result(backend, context)  # type: ignore[arg-type]

    assert first.status_code == 503
    assert second.status_code == 200
    assert isinstance(second.body, dict)
    assert second.body["result"]["response"] == "answer"


@pytest.mark.asyncio
async def test_unknown_run_is_not_reported_as_a_server_fault() -> None:
    backend = FakeBackend(_status())
    backend.raise_on_get = RunRowNotFoundError("missing")

    response = await read_status(
        backend,  # type: ignore[arg-type]
        RunContext(run_id="run-1", session_id="session-1"),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_tombstoned_abandoned_run_keeps_status_but_result_is_gone() -> None:
    backend = FakeBackend(
        _status(
            state="abandoned",
            error=RunError(
                code=SESSION_TOMBSTONED_ERROR_CODE,
                message="Session backing is no longer available.",
                fault_domain="sandbox",
            ),
        )
    )
    context = RunContext(run_id="run-1", session_id="session-1")

    status_response = await read_status(backend, context)  # type: ignore[arg-type]
    result_response = await read_result(backend, context)  # type: ignore[arg-type]

    assert status_response.status_code == 200
    assert result_response.status_code == 410


# ---------------------------------------------------------------------------
# Cross-layer regression: an ARM status must reach the caller as an HTTP status.
#
# The transport tests prove classification and the handler tests prove response
# shape, but the defect this guards against lived in the seam between them: the
# transport raised a typed error that nothing on the request path caught, so it
# escaped as an untyped 500. Testing either side alone would have stayed green.
# ---------------------------------------------------------------------------


def _arm_status_session(status: int) -> object:
    """Fake an aiohttp session whose ARM GET answers with ``status``."""

    class _Response:
        def __init__(self) -> None:
            self.status = status

        async def __aenter__(self) -> _Response:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def json(self, *, content_type: object) -> dict[str, str]:
            del content_type
            return {"error": {"code": "TooManyRequests"}}

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        def get(self, *args: object, **kwargs: object) -> _Response:
            del args, kwargs
            return _Response()

    return _Session()


class _ArmBoundBackend:
    """A backend whose start_run resolves the provider, as the real one does."""

    def __init__(self, runtime: SessionRuntimeBinding) -> None:
        self._runtime = runtime

    async def start_run(self, *args: object, **kwargs: object) -> RunHandle:
        del args, kwargs
        await self._runtime.get_provider()
        raise AssertionError("provider resolution should not have succeeded")


def _arm_bound_runtime(tmp_path: Path) -> SessionRuntimeBinding:
    async def provider_factory() -> SandboxSessionProvider:
        await aca_sdk._read_arm_group(
            _ArmCredential(),
            "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/sandboxGroups/g",
        )
        raise AssertionError("the ARM read should have raised")

    async def state_store_factory() -> StateStoreBinding:
        raise AssertionError("the state store must not be resolved for this path")

    return SessionRuntimeBinding.create(
        app_identity=AppIdentity.create(
            subscription_id="11111111-2222-3333-4444-555555555555",
            site_name="agent-app",
        ),
        sandbox_group_resource_id=(
            "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/sandboxGroups/g"
        ),
        script_root=tmp_path,
        provider_factory=provider_factory,
        state_store_factory=state_store_factory,
    )


class _ArmCredential:
    async def get_token(self, *scopes: str) -> object:
        del scopes

        class _Token:
            token = "redacted"

        return _Token()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_transient_arm_status_reaches_the_caller_as_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A retryable ARM status must become 503 with Retry-After, not an untyped 500."""
    monkeypatch.setattr(aca_sdk.aiohttp, "ClientSession", lambda **_: _arm_status_session(status))
    backend = _ArmBoundBackend(_arm_bound_runtime(tmp_path))

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "2"
    assert response.body == {
        "error": "sandbox_group_transient",
        "reason": "sandbox_group_transient",
    }
    assert str(status) not in str(response.body)


@pytest.mark.asyncio
async def test_permanent_arm_status_is_not_reported_as_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing group is a permanent fault and must not be advertised as retryable."""
    monkeypatch.setattr(aca_sdk.aiohttp, "ClientSession", lambda **_: _arm_status_session(404))
    backend = _ArmBoundBackend(_arm_bound_runtime(tmp_path))

    with pytest.raises(SandboxGroupBindingError):
        await submit_run(
            backend,  # type: ignore[arg-type]
            StartRunRequest(prompt="hello"),
            agent_slug="main",
            respond_async=True,
            budget=_expired_budget(),
        )


@pytest.mark.asyncio
async def test_invalid_state_resume_409_produces_structured_response_not_untyped_500() -> None:
    """A sandbox invalid-state error must become a structured 409, not an unparseable 500."""
    backend = FakeBackend(_status())
    backend.raise_on_start = SessionActivationConflictError(
        "sandbox_invalid_state"
    )

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert response.status_code == 409
    assert response.body == {
        "error": "sandbox_invalid_state",
        "reason": "sandbox_invalid_state",
    }
    # No provider payload leaks into the structured response.
    body_text = str(response.body)
    assert "traceId" not in body_text
    assert "requestId" not in body_text
    assert "sandboxGroups" not in body_text


@pytest.mark.asyncio
async def test_invalid_state_on_get_run_produces_structured_409() -> None:
    """The get_run path also catches conflict errors as structured 409."""
    backend = FakeBackend(_status())
    backend.raise_on_get = SessionActivationConflictError(
        "sandbox_invalid_state"
    )

    response = await read_result(backend, RunContext(run_id="run-1", session_id="session-1"))  # type: ignore[arg-type]

    assert response.status_code == 409
    assert response.body == {
        "error": "sandbox_invalid_state",
        "reason": "sandbox_invalid_state",
    }
