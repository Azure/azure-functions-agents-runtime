from __future__ import annotations

import json
from typing import Any

import pytest
from durabletask.task import TaskFailedError

from azure_functions_agents.workflows.context import _workflow_task_idempotency_key
from azure_functions_agents.workflows.native_retry import (
    DurableRetryableActivityError,
    decode_durable_retry_failure,
)
from azure_functions_agents.workflows.policy import invoke_policy_handler
from azure_functions_agents.workflows.schema import WorkflowRetryableError


def _native_task(*, continue_on_error: bool = False) -> dict[str, Any]:
    workflow_id = "workflow-1"
    instance_id = "work[0]"
    return {
        "id": instance_id,
        "workflow_id": workflow_id,
        "task_id": "work",
        "node_instance_id": instance_id,
        "max_attempts": 3,
        "idempotency_key": _workflow_task_idempotency_key(workflow_id, instance_id),
        "execution": {
            "timeout_ms": 1_000,
            "max_attempts": 3,
            "retry_delays_ms": [100, 200],
            "continue_on_error": continue_on_error,
            "timeout_source": "task",
            "retry_source": "task",
            "durable_retry_policy": {
                "first_retry_interval_ms": 100,
                "max_number_of_attempts": 3,
                "backoff_coefficient": 2.0,
                "max_retry_interval_ms": 1_000,
                "retry_timeout_ms": 3_600_000,
            },
        },
    }


@pytest.mark.asyncio
async def test_native_retryable_outcome_raises_private_sanitized_exception() -> None:
    def handler(args: dict[str, Any]) -> None:
        raise WorkflowRetryableError("service_busy", "Try again.")

    with pytest.raises(DurableRetryableActivityError) as raised:
        await invoke_policy_handler(
            handler,
            {},
            task=_native_task(),
            target="run",
        )

    payload = json.loads(str(raised.value))
    assert payload == {
        "version": 1,
        "outcome": {
            "id": "work[0]",
            "ok": False,
            "failure": {
                "error_code": "service_busy",
                "error": "Try again.",
                "kind": "handler_transient",
                "retryable": True,
                "continuable": True,
            },
        },
    }


@pytest.mark.asyncio
async def test_native_terminal_outcome_remains_a_return_value() -> None:
    def handler(args: dict[str, Any]) -> None:
        raise RuntimeError("private detail")

    outcome = await invoke_policy_handler(
        handler,
        {},
        task=_native_task(),
        target="run",
    )

    assert outcome["ok"] is False
    assert outcome["failure"]["kind"] == "execution_unknown"


@pytest.mark.asyncio
async def test_native_exhaustion_round_trips_only_valid_private_failure() -> None:
    def handler(args: dict[str, Any]) -> None:
        raise WorkflowRetryableError("service_busy", "Try again.")

    with pytest.raises(DurableRetryableActivityError) as raised:
        await invoke_policy_handler(
            handler,
            {},
            task=_native_task(),
            target="run",
        )

    task_failure = TaskFailedError("Activity failed.", raised.value)
    assert decode_durable_retry_failure("work[0]", task_failure) == {
        "error_code": "service_busy",
        "error": "Try again.",
        "kind": "handler_transient",
        "retryable": True,
        "continuable": True,
    }
    assert decode_durable_retry_failure(
        "work[0]",
        TaskFailedError("Activity failed.", ValueError("private detail")),
    ) is None


@pytest.mark.asyncio
async def test_native_activity_context_has_no_synthetic_attempt() -> None:
    observed_attempt: int | None = 1

    def handler(args: dict[str, Any]) -> dict[str, bool]:
        nonlocal observed_attempt
        from azure_functions_agents.workflows.context import (
            current_workflow_task_context,
        )

        context = current_workflow_task_context()
        assert context is not None
        observed_attempt = context.attempt
        return {"ok": True}

    outcome = await invoke_policy_handler(
        handler,
        {},
        task=_native_task(),
        target="run",
    )

    assert outcome["ok"] is True
    assert observed_attempt is None
