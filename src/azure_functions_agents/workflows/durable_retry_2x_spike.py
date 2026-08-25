"""Isolated Durable 2.x retry-driver prototype.

This module is registered as a dormant sub-orchestrator but is deliberately not
invoked by the production workflow scheduler. It preserves enough policy data
to exercise the native ``durabletask`` retry engine and make the eventual
Option A deletion seam concrete.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from datetime import timedelta
from typing import Any, TypedDict

from durabletask import task as durable_task

from .schema import DurableRetryPolicyInput

RETRY_NODE_ORCHESTRATOR_NAME = "agents_workflow_retry_node_2x_spike"


class DurableRetryNodeInput(TypedDict):
    """Persisted input for one isolated retry-driving sub-orchestration."""

    activity_name: str
    activity_input: dict[str, Any]
    node_instance_id: str
    retry_policy: DurableRetryPolicyInput


class DurableRetryableActivityError(RuntimeError):
    """Internal signal that asks Durable 2.x to retry an Activity."""


class DurableRetryableTimeoutError(DurableRetryableActivityError):
    """Internal timeout signal kept distinct in Durable failure details."""


def create_durable_retry_policy(
    policy: DurableRetryPolicyInput,
) -> durable_task.RetryPolicy:
    """Translate the persisted authoring policy to Durable 2.x."""
    return durable_task.RetryPolicy(
        first_retry_interval=timedelta(
            milliseconds=policy["first_retry_interval_ms"]
        ),
        max_number_of_attempts=policy["max_number_of_attempts"],
        backoff_coefficient=policy["backoff_coefficient"],
        max_retry_interval=timedelta(
            milliseconds=policy["max_retry_interval_ms"]
        ),
    )


def bridge_activity_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Return success/terminal outcomes and raise only retryable outcomes."""
    copied = dict(outcome)
    if copied.get("ok") is True:
        return copied
    failure = copied.get("failure")
    if not isinstance(failure, Mapping) or failure.get("retryable") is not True:
        return copied
    if failure.get("kind") == "timeout":
        raise DurableRetryableTimeoutError("Workflow task attempt timed out.")
    raise DurableRetryableActivityError("Workflow task requested a retry.")


def durable_retry_node_orchestrator(
    context: durable_task.OrchestrationContext,
    payload: DurableRetryNodeInput,
) -> Generator[durable_task.Task[Any], Any, dict[str, Any]]:
    """Drive one Activity through Durable 2.x and normalize exhaustion."""
    try:
        outcome = yield context.call_activity(
            payload["activity_name"],
            input=payload["activity_input"],
            retry_policy=create_durable_retry_policy(payload["retry_policy"]),
        )
    except durable_task.TaskFailedError as exc:
        kind = (
            "timeout"
            if exc.details.is_caused_by(DurableRetryableTimeoutError)
            else "handler_transient"
        )
        return {
            "id": payload["node_instance_id"],
            "ok": False,
            "failure": {
                "error_code": "workflow_task_retry_exhausted",
                "error": "Task retries were exhausted.",
                "kind": kind,
                "retryable": True,
                "continuable": True,
            },
        }
    if not isinstance(outcome, Mapping):
        return {
            "id": payload["node_instance_id"],
            "ok": False,
            "failure": {
                "error_code": "workflow_task_handler_contract",
                "error": "Task Activity returned an invalid outcome.",
                "kind": "handler_contract",
                "retryable": False,
                "continuable": False,
            },
        }
    return dict(outcome)


durable_retry_node_orchestrator.__name__ = RETRY_NODE_ORCHESTRATOR_NAME


__all__ = [
    "RETRY_NODE_ORCHESTRATOR_NAME",
    "DurableRetryNodeInput",
    "DurableRetryableActivityError",
    "DurableRetryableTimeoutError",
    "bridge_activity_outcome",
    "create_durable_retry_policy",
    "durable_retry_node_orchestrator",
]
