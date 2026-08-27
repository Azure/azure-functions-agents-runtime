from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from durabletask import client, task, worker
from durabletask.task import TaskFailedError
from durabletask.testing import create_test_backend

from azure_functions_agents.workflows.native_retry import (
    create_durable_retry_policy,
    decode_durable_retry_failure,
    raise_for_durable_retry,
)
from azure_functions_agents.workflows.policy import ActivityFailureOutcome
from azure_functions_agents.workflows.schema import DurableRetryPolicyInput


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _runtime(
    activities: list[Callable[..., Any]],
    orchestrators: list[Callable[..., Any]],
) -> Iterator[client.TaskHubGrpcClient]:
    port = _free_port()
    backend = create_test_backend(port=port)
    runtime_worker = worker.TaskHubGrpcWorker(host_address=f"localhost:{port}")
    for orchestrator in orchestrators:
        runtime_worker.add_orchestrator(orchestrator)
    for activity in activities:
        runtime_worker.add_activity(activity)
    runtime_worker.start()
    runtime_client = client.TaskHubGrpcClient(host_address=f"localhost:{port}")
    try:
        yield runtime_client
    finally:
        runtime_worker.stop()
        backend.stop()
        backend.reset()


def _policy(*, attempts: int = 3) -> DurableRetryPolicyInput:
    return {
        "first_retry_interval_ms": 10,
        "max_number_of_attempts": attempts,
        "backoff_coefficient": 2.0,
        "max_retry_interval_ms": 40,
        "retry_timeout_ms": 3_600_000,
    }


def _retryable_outcome(instance_id: str) -> ActivityFailureOutcome:
    return {
        "id": instance_id,
        "ok": False,
        "failure": {
            "error_code": "inventory_busy",
            "error": "Inventory is temporarily unavailable.",
            "kind": "handler_transient",
            "retryable": True,
            "continuable": True,
        },
    }


def test_durable_backend_retries_direct_activities_independently() -> None:
    calls = {"inspect[0]": 0, "inspect[1]": 0, "inspect[2]": 0}
    failures_before_success = {"inspect[0]": 0, "inspect[1]": 1, "inspect[2]": 2}

    def inspect_activity(
        context: task.ActivityContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        instance_id = payload["id"]
        calls[instance_id] += 1
        assert not hasattr(context, "attempt")
        if calls[instance_id] <= failures_before_success[instance_id]:
            raise_for_durable_retry(_retryable_outcome(instance_id))
        return {"id": instance_id, "ok": True, "result": instance_id}

    def for_each_parent(
        context: task.OrchestrationContext,
        _: Any,
    ) -> Any:
        retry_policy = create_durable_retry_policy(_policy())
        activities = [
            context.call_activity(
                inspect_activity,
                input={"id": f"inspect[{index}]"},
                retry_policy=retry_policy,
            )
            for index in range(3)
        ]
        return (yield task.when_all(activities))

    with _runtime([inspect_activity], [for_each_parent]) as runtime_client:
        instance_id = runtime_client.schedule_new_orchestration(for_each_parent)
        state = runtime_client.wait_for_orchestration_completion(instance_id, timeout=10)

    assert state is not None
    assert state.runtime_status == client.OrchestrationStatus.COMPLETED
    assert [outcome["result"] for outcome in state.get_output()] == [
        "inspect[0]",
        "inspect[1]",
        "inspect[2]",
    ]
    assert calls == {"inspect[0]": 1, "inspect[1]": 2, "inspect[2]": 3}


def test_durable_backend_exhaustion_preserves_private_sanitized_failure() -> None:
    calls = 0

    def always_retry(
        _context: task.ActivityContext,
        payload: dict[str, Any],
    ) -> None:
        nonlocal calls
        calls += 1
        raise_for_durable_retry(_retryable_outcome(payload["id"]))

    def exhaustion_parent(
        context: task.OrchestrationContext,
        _: Any,
    ) -> Any:
        try:
            yield context.call_activity(
                always_retry,
                input={"id": "work"},
                retry_policy=create_durable_retry_policy(_policy()),
            )
        except TaskFailedError as exc:
            return decode_durable_retry_failure("work", exc)
        raise AssertionError("retrying Activity unexpectedly succeeded")

    with _runtime([always_retry], [exhaustion_parent]) as runtime_client:
        instance_id = runtime_client.schedule_new_orchestration(exhaustion_parent)
        state = runtime_client.wait_for_orchestration_completion(instance_id, timeout=10)

    assert state is not None
    assert state.runtime_status == client.OrchestrationStatus.COMPLETED
    assert state.get_output() == {
        "error_code": "inventory_busy",
        "error": "Inventory is temporarily unavailable.",
        "kind": "handler_transient",
        "retryable": True,
        "continuable": True,
    }
    assert calls == 3
