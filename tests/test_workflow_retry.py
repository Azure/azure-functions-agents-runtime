from __future__ import annotations

from azure_functions_agents.workflows.context import _workflow_task_idempotency_key
from azure_functions_agents.workflows.retry import (
    ActivityFailure,
    PolicyActivityInputModel,
    RetryDisposition,
    decide_retry,
)
from azure_functions_agents.workflows.schema import EffectiveWorkflowTaskExecution


def _execution(*, attempts: int = 3, continue_on_error: bool = False):
    return EffectiveWorkflowTaskExecution(
        timeout_ms=1_000,
        max_attempts=attempts,
        retry_delays_ms=[100, 250][: attempts - 1],
        continue_on_error=continue_on_error,
        timeout_source="task",
        retry_source="task",
    )


def _failure(
    *,
    kind: str = "handler_transient",
    retryable: bool = True,
    continuable: bool = True,
) -> ActivityFailure:
    return {
        "error_code": "service_busy",
        "error": "Try again.",
        "kind": kind,
        "retryable": retryable,
        "continuable": continuable,
    }


def test_retry_decision_selects_attempt_delay() -> None:
    assert decide_retry(_execution(), attempt=1, failure=_failure()) == RetryDisposition(
        action="retry",
        delay_ms=100,
    )
    assert decide_retry(_execution(), attempt=2, failure=_failure()) == RetryDisposition(
        action="retry",
        delay_ms=250,
    )


def test_retry_decision_fails_after_last_attempt() -> None:
    assert decide_retry(_execution(), attempt=3, failure=_failure()) == RetryDisposition(
        action="fail",
    )


def test_retry_decision_continues_terminal_failure_when_enabled() -> None:
    assert decide_retry(
        _execution(continue_on_error=True),
        attempt=1,
        failure=_failure(
            kind="handler_terminal",
            retryable=False,
        ),
    ) == RetryDisposition(action="continue")


def test_retry_decision_never_continues_noncontinuable_failure() -> None:
    assert decide_retry(
        _execution(continue_on_error=True),
        attempt=1,
        failure=_failure(
            kind="authorization",
            retryable=False,
            continuable=False,
        ),
    ) == RetryDisposition(action="fail")


def test_retry_decision_retries_infrastructure_failure() -> None:
    assert decide_retry(
        _execution(),
        attempt=1,
        failure=_failure(kind="activity_infrastructure"),
    ) == RetryDisposition(action="retry", delay_ms=100)


def test_activity_validation_returns_plain_execution_wire_contract() -> None:
    workflow_id = "workflow-1"
    node_instance_id = "work[0]"
    validated = PolicyActivityInputModel.model_validate({
        "id": node_instance_id,
        "workflow_id": workflow_id,
        "execution": _execution(),
        "task_id": "work",
        "node_instance_id": node_instance_id,
        "attempt": 1,
        "max_attempts": 3,
        "idempotency_key": _workflow_task_idempotency_key(
            workflow_id,
            node_instance_id,
        ),
        "tool": "ignored-by-policy-validator",
    })

    execution = validated.execution.to_wire()

    assert type(execution) is dict
    assert execution == _execution()
