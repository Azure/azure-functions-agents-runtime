"""Unit tests for policy-aware workflow Activity execution."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from azure_functions_agents.workflows import activity
from azure_functions_agents.workflows.native_retry import DurableRetryableActivityError


def _execution(*, timeout_ms: int | None = None) -> dict[str, Any]:
    execution: dict[str, Any] = {
        "max_attempts": 1,
        "durable_retry_policy": {
            "first_retry_interval_ms": 0,
            "max_number_of_attempts": 1,
            "backoff_coefficient": 1.0,
            "max_retry_interval_ms": 0,
        },
    }
    if timeout_ms is not None:
        execution["timeout_ms"] = timeout_ms
    return execution


def _task(*, timeout_ms: int | None = None) -> dict[str, Any]:
    return {
        "id": "probe",
        "task_id": "probe",
        "workflow_id": "workflow-1",
        "execution": _execution(timeout_ms=timeout_ms),
    }


@pytest.mark.asyncio
async def test_expired_attempt_deadline_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_timeout = asyncio.timeout
    monkeypatch.setattr(activity.asyncio, "timeout", lambda _: real_timeout(0.01))

    async def slow(_: dict[str, Any]) -> None:
        await asyncio.sleep(1)

    with pytest.raises(DurableRetryableActivityError, match="workflow_task_timeout"):
        await activity.invoke_policy_handler(
            slow,
            {},
            task=_task(timeout_ms=1_000),
            target="probe",
        )


@pytest.mark.asyncio
async def test_handler_timeout_error_is_not_an_attempt_deadline() -> None:
    async def raises_timeout(_: dict[str, Any]) -> None:
        raise TimeoutError("handler-owned timeout")

    outcome = await activity.invoke_policy_handler(
        raises_timeout,
        {},
        task=_task(timeout_ms=1_000),
        target="probe",
    )

    assert outcome["ok"] is False
    assert outcome["failure"] == {
        "error_code": "workflow_task_execution_unknown",
        "error": "Task execution failed.",
        "kind": "execution_unknown",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_sync_handler_can_finish_after_attempt_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_timeout = asyncio.timeout
    monkeypatch.setattr(activity.asyncio, "timeout", lambda _: real_timeout(0.01))
    finished = threading.Event()

    def slow(_: dict[str, Any]) -> None:
        time.sleep(0.05)
        finished.set()

    with pytest.raises(DurableRetryableActivityError, match="workflow_task_timeout"):
        await activity.invoke_policy_handler(
            slow,
            {},
            task=_task(timeout_ms=1_000),
            target="probe",
        )

    assert finished.wait(timeout=1)


@pytest.mark.asyncio
async def test_external_cancellation_is_not_reported_as_timeout() -> None:
    started = asyncio.Event()

    async def wait_forever(_: dict[str, Any]) -> None:
        started.set()
        await asyncio.Event().wait()

    invocation = asyncio.create_task(
        activity.invoke_policy_handler(
            wait_forever,
            {},
            task=_task(timeout_ms=10_000),
            target="probe",
        )
    )
    await started.wait()
    invocation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await invocation


def test_invalid_persisted_timeout_is_a_contract_failure() -> None:
    invalid = activity.validate_policy_activity_input(
        _task(timeout_ms=999),
        target_type="tool",
    )

    assert invalid is not None
    assert invalid["failure"]["kind"] == "handler_contract"


def test_invalid_persisted_continuation_is_a_contract_failure() -> None:
    task = _task()
    task["execution"]["continue_on_error"] = "yes"

    invalid = activity.validate_policy_activity_input(task, target_type="tool")

    assert invalid is not None
    assert invalid["failure"]["kind"] == "handler_contract"
