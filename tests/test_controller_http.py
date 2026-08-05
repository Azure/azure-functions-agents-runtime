from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from azure_functions_agents.controller.budget import RequestBudget
from azure_functions_agents.controller.http import (
    read_result,
    read_status,
    submit_run,
)
from azure_functions_agents.controller.idempotency import IdempotencyResultUnavailableError
from azure_functions_agents.execution.backend import (
    SESSION_TOMBSTONED_ERROR_CODE,
    RunContext,
    RunError,
    RunEvent,
    RunHandle,
    RunResult,
    RunStatus,
    StartRunRequest,
)
from azure_functions_agents.execution.setup_budget import SetupBudget, SetupBudgetExpiredError
from azure_functions_agents.session_state import RunRowNotFoundError


class FakeBackend:
    def __init__(self, status: RunStatus) -> None:
        self.status = status
        self.cancelled = False
        self.started = False
        self.raise_on_start: Exception | None = None
        self.raise_on_get: Exception | None = None

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        del request
        if self.raise_on_start is not None:
            raise self.raise_on_start
        self.started = True
        return RunHandle(
            run_id=self.status.run_id,
            session_id=self.status.session_id,
            state=self.status.state,
            created_at=datetime.now(UTC),
        )

    async def get_run(self, context: RunContext) -> RunStatus:
        assert context.run_id == self.status.run_id
        if self.raise_on_get is not None:
            raise self.raise_on_get
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
) -> RunStatus:
    return RunStatus(
        run_id="run-1",
        session_id="session-1",
        state=state,  # type: ignore[arg-type]
        last_sequence=0,
        result_available=result_available,
        result=result,
        error=error,
    )


def _expired_budget() -> RequestBudget:
    return RequestBudget(
        wall_deadline=0.0,
        setup=SetupBudget.create(deadline=1.0, clock=lambda: 0.0),
        _clock=lambda: 1.0,
    )


@pytest.mark.asyncio
async def test_async_submission_returns_shared_management_urls() -> None:
    backend = FakeBackend(_status())

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
    }


@pytest.mark.asyncio
async def test_setup_expiry_returns_retry_hint_before_run_starts() -> None:
    backend = FakeBackend(_status())
    backend.raise_on_start = SetupBudgetExpiredError("expired")

    response = await submit_run(
        backend,  # type: ignore[arg-type]
        StartRunRequest(prompt="hello"),
        agent_slug="main",
        respond_async=True,
        budget=_expired_budget(),
    )

    assert not backend.started
    assert response.status_code == 504
    assert response.headers["x-ms-retry-with"] == "respond-async"


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
