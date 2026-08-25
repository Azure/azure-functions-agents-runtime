"""Executable evidence for the isolated Durable 2.x retry spike."""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from durabletask import client, task, worker
from durabletask.testing import create_test_backend

from azure_functions_agents.workflows import engine
from azure_functions_agents.workflows.durable_retry_2x_spike import (
    RETRY_NODE_ORCHESTRATOR_NAME,
    bridge_activity_outcome,
    create_durable_retry_policy,
    durable_retry_node_orchestrator,
)
from azure_functions_agents.workflows.schema import (
    WorkflowRetryBackoff,
    WorkflowRetryPolicy,
    durable_retry_policy_input,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _runtime(
    activities: list[Callable[..., Any]],
    orchestrators: list[Callable[..., Any]] | None = None,
) -> Iterator[tuple[client.TaskHubGrpcClient, worker.TaskHubGrpcWorker]]:
    port = _free_port()
    backend = create_test_backend(port=port)
    task_worker = worker.TaskHubGrpcWorker(host_address=f"localhost:{port}")
    task_worker.add_orchestrator(durable_retry_node_orchestrator)
    for orchestrator in orchestrators or []:
        task_worker.add_orchestrator(orchestrator)
    for activity in activities:
        task_worker.add_activity(activity)
    task_worker.start()
    task_client = client.TaskHubGrpcClient(host_address=f"localhost:{port}")
    try:
        yield task_client, task_worker
    finally:
        task_worker.stop()
        backend.stop()
        backend.reset()


def _policy(
    *,
    attempts: int = 3,
    initial: str = "PT0.01S",
    multiplier: float = 2.0,
    maximum: str = "PT0.04S",
) -> dict[str, Any]:
    return durable_retry_policy_input(
        WorkflowRetryPolicy(
            max_attempts=attempts,
            backoff=WorkflowRetryBackoff(
                initial=initial,
                multiplier=multiplier,
                max=maximum,
            ),
        )
    )


def _node_input(
    activity_name: str,
    node_id: str = "work",
    *,
    attempts: int = 3,
    initial: str = "PT0.01S",
    maximum: str = "PT0.04S",
) -> dict[str, Any]:
    return {
        "activity_name": activity_name,
        "activity_input": {"id": node_id},
        "node_instance_id": node_id,
        "retry_policy": _policy(
            attempts=attempts,
            initial=initial,
            maximum=maximum,
        ),
    }


def test_maps_existing_exponential_policy_to_durable_retry_policy() -> None:
    spec = _policy(
        attempts=5,
        initial="PT1.25S",
        multiplier=2.5,
        maximum="PT9S",
    )
    policy = create_durable_retry_policy(spec)

    assert policy.first_retry_interval.total_seconds() == 1.25
    assert policy.max_number_of_attempts == 5
    assert policy.backoff_coefficient == 2.5
    assert policy.max_retry_interval is not None
    assert policy.max_retry_interval.total_seconds() == 9
    assert policy.retry_timeout is None


def test_retryable_outcome_retries_to_success_without_attempt_input() -> None:
    calls = 0
    task_ids: list[int] = []
    has_attempt_metadata: list[bool] = []

    def retryable_activity(
        context: task.ActivityContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        task_ids.append(context.task_id)
        has_attempt_metadata.append(hasattr(context, "attempt"))
        if calls < 3:
            return bridge_activity_outcome({
                "id": payload["id"],
                "ok": False,
                "failure": {
                    "error_code": "inventory_busy",
                    "error": "Inventory is temporarily unavailable.",
                    "kind": "handler_transient",
                    "retryable": True,
                    "continuable": True,
                },
            })
        return bridge_activity_outcome({
            "id": payload["id"],
            "ok": True,
            "result": {"recovered": True},
        })

    with _runtime([retryable_activity]) as (task_client, _):
        instance_id = task_client.schedule_new_orchestration(
            RETRY_NODE_ORCHESTRATOR_NAME,
            input=_node_input(retryable_activity.__name__),
        )
        state = task_client.wait_for_orchestration_completion(instance_id, timeout=10)
        history = task_client.get_orchestration_history(instance_id)

    assert state is not None
    assert state.runtime_status == client.OrchestrationStatus.COMPLETED
    assert state.get_output() == {
        "id": "work",
        "ok": True,
        "result": {"recovered": True},
    }
    assert calls == 3
    assert len(set(task_ids)) == 1
    assert has_attempt_metadata == [False, False, False]
    assert sum(type(event).__name__ == "TaskScheduledEvent" for event in history) == 3


def test_terminal_and_unknown_outcomes_are_not_retried() -> None:
    calls: dict[str, int] = {"terminal": 0, "unknown": 0}

    def terminal_activity(
        _context: task.ActivityContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        calls[payload["id"]] += 1
        kind = "handler_terminal" if payload["id"] == "terminal" else "execution_unknown"
        return bridge_activity_outcome({
            "id": payload["id"],
            "ok": False,
            "failure": {
                "error_code": f"{payload['id']}_failure",
                "error": "Safe failure.",
                "kind": kind,
                "retryable": False,
                "continuable": True,
            },
        })

    with _runtime([terminal_activity]) as (task_client, _):
        outputs = []
        for node_id in ("terminal", "unknown"):
            instance_id = task_client.schedule_new_orchestration(
                RETRY_NODE_ORCHESTRATOR_NAME,
                input=_node_input(terminal_activity.__name__, node_id),
            )
            state = task_client.wait_for_orchestration_completion(instance_id, timeout=10)
            assert state is not None
            outputs.append(state.get_output())

    assert calls == {"terminal": 1, "unknown": 1}
    assert [outcome["failure"]["kind"] for outcome in outputs] == [
        "handler_terminal",
        "execution_unknown",
    ]


def test_retry_exhaustion_returns_stable_but_lossy_failure() -> None:
    calls = 0

    def always_retry(
        _context: task.ActivityContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return bridge_activity_outcome({
            "id": payload["id"],
            "ok": False,
            "failure": {
                "error_code": "inventory_busy",
                "error": "Inventory is temporarily unavailable.",
                "kind": "handler_transient",
                "retryable": True,
                "continuable": True,
            },
        })

    with _runtime([always_retry]) as (task_client, _):
        instance_id = task_client.schedule_new_orchestration(
            RETRY_NODE_ORCHESTRATOR_NAME,
            input=_node_input(always_retry.__name__),
        )
        state = task_client.wait_for_orchestration_completion(instance_id, timeout=10)

    assert state is not None
    assert state.runtime_status == client.OrchestrationStatus.COMPLETED
    assert state.get_output() == {
        "id": "work",
        "ok": False,
        "failure": {
            "error_code": "workflow_task_retry_exhausted",
            "error": "Task retries were exhausted.",
            "kind": "handler_transient",
            "retryable": True,
            "continuable": True,
        },
    }
    assert calls == 3
    succeeded, failure = engine._validated_policy_activity_result(
        {"instance_id": "work"},  # type: ignore[arg-type]
        state.get_output(),
    )
    assert succeeded is False
    assert failure["error_code"] == "workflow_task_retry_exhausted"


def test_timeout_exhaustion_preserves_contract_classification() -> None:
    def always_timeout(
        _context: task.ActivityContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return bridge_activity_outcome({
            "id": payload["id"],
            "ok": False,
            "failure": {
                "error_code": "workflow_task_timeout",
                "error": "Task attempt timed out.",
                "kind": "timeout",
                "retryable": True,
                "continuable": True,
            },
        })

    with _runtime([always_timeout]) as (task_client, _):
        instance_id = task_client.schedule_new_orchestration(
            RETRY_NODE_ORCHESTRATOR_NAME,
            input=_node_input(always_timeout.__name__, attempts=2),
        )
        state = task_client.wait_for_orchestration_completion(instance_id, timeout=10)

    assert state is not None
    outcome = state.get_output()
    assert outcome["failure"]["kind"] == "timeout"
    succeeded, failure = engine._validated_policy_activity_result(
        {"instance_id": "work"},  # type: ignore[arg-type]
        outcome,
    )
    assert succeeded is False
    assert failure["error_code"] == "workflow_task_retry_exhausted"


def test_native_retry_replays_orchestrator_deterministically() -> None:
    calls = 0
    replay_flags: list[bool] = []

    def replay_activity(
        _context: task.ActivityContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return bridge_activity_outcome({
                "id": payload["id"],
                "ok": False,
                "failure": {
                    "error_code": "busy",
                    "error": "Safe failure.",
                    "kind": "handler_transient",
                    "retryable": True,
                    "continuable": True,
                },
            })
        return {"id": payload["id"], "ok": True, "result": "done"}

    def replay_probe(
        context: task.OrchestrationContext,
        payload: dict[str, Any],
    ) -> Any:
        replay_flags.append(context.is_replaying)
        return (
            yield context.call_activity(
                replay_activity,
                input=payload,
                retry_policy=create_durable_retry_policy(_policy(attempts=2)),
            )
        )

    with _runtime([replay_activity], [replay_probe]) as (task_client, _):
        instance_id = task_client.schedule_new_orchestration(
            replay_probe,
            input={"id": "work"},
        )
        state = task_client.wait_for_orchestration_completion(instance_id, timeout=10)

    assert state is not None
    assert state.get_output()["result"] == "done"
    assert calls == 2
    assert replay_flags[0] is False
    assert True in replay_flags[1:]


def test_for_each_shaped_children_retry_independently() -> None:
    calls: dict[str, int] = {"inspect[0]": 0, "inspect[1]": 0}

    def inspect_activity(
        _context: task.ActivityContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        node_id = payload["id"]
        calls[node_id] += 1
        if calls[node_id] <= int(node_id == "inspect[0]"):
            return bridge_activity_outcome({
                "id": node_id,
                "ok": False,
                "failure": {
                    "error_code": "busy",
                    "error": "Safe failure.",
                    "kind": "handler_transient",
                    "retryable": True,
                    "continuable": True,
                },
            })
        return {"id": node_id, "ok": True, "result": node_id}

    def for_each_parent(
        context: task.OrchestrationContext,
        _: Any,
    ) -> Any:
        children = [
            context.call_sub_orchestrator(
                RETRY_NODE_ORCHESTRATOR_NAME,
                input=_node_input(inspect_activity.__name__, f"inspect[{index}]"),
                instance_id=f"{context.instance_id}:inspect-{index}",
            )
            for index in range(2)
        ]
        return (yield task.when_all(children))

    with _runtime([inspect_activity], [for_each_parent]) as (task_client, _):
        instance_id = task_client.schedule_new_orchestration(for_each_parent)
        state = task_client.wait_for_orchestration_completion(instance_id, timeout=10)

    assert state is not None
    assert [outcome["result"] for outcome in state.get_output()] == [
        "inspect[0]",
        "inspect[1]",
    ]
    assert calls == {"inspect[0]": 2, "inspect[1]": 1}


def test_cooperative_cancel_does_not_cancel_retrying_child() -> None:
    first_attempt = threading.Event()
    calls = 0

    def slow_retry(
        _context: task.ActivityContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        first_attempt.set()
        return bridge_activity_outcome({
            "id": payload["id"],
            "ok": False,
            "failure": {
                "error_code": "busy",
                "error": "Safe failure.",
                "kind": "handler_transient",
                "retryable": True,
                "continuable": True,
            },
        })

    def cancel_parent(
        context: task.OrchestrationContext,
        _: Any,
    ) -> Any:
        child = context.call_sub_orchestrator(
            RETRY_NODE_ORCHESTRATOR_NAME,
            input=_node_input(
                slow_retry.__name__,
                attempts=2,
                initial="PT0.5S",
                maximum="PT0.5S",
            ),
            instance_id=f"{context.instance_id}:node",
        )
        cancel = context.wait_for_external_event("cancel")
        winner = yield task.when_any([child, cancel])
        if winner is cancel:
            return {"canceled": True}
        return child.result

    with _runtime([slow_retry], [cancel_parent]) as (task_client, _):
        parent_id = task_client.schedule_new_orchestration(
            cancel_parent,
            instance_id="cooperative-parent",
        )
        assert first_attempt.wait(timeout=5), task_client.get_orchestration_state(parent_id)
        task_client.raise_orchestration_event(parent_id, "cancel", data="stop")
        parent = task_client.wait_for_orchestration_completion(parent_id, timeout=10)
        child = task_client.wait_for_orchestration_completion(
            "cooperative-parent:node",
            timeout=10,
        )

    assert parent is not None
    assert parent.get_output() == {"canceled": True}
    assert child is not None
    assert child.runtime_status == client.OrchestrationStatus.COMPLETED
    assert calls == 2


def test_in_memory_backend_cannot_verify_recursive_child_termination() -> None:
    first_attempt = threading.Event()
    calls = 0

    def terminating_retry(
        _context: task.ActivityContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        first_attempt.set()
        return bridge_activity_outcome({
            "id": payload["id"],
            "ok": False,
            "failure": {
                "error_code": "busy",
                "error": "Safe failure.",
                "kind": "handler_transient",
                "retryable": True,
                "continuable": True,
            },
        })

    def terminate_parent(
        context: task.OrchestrationContext,
        _: Any,
    ) -> Any:
        return (
            yield context.call_sub_orchestrator(
                RETRY_NODE_ORCHESTRATOR_NAME,
                input=_node_input(
                    terminating_retry.__name__,
                    attempts=3,
                    initial="PT5S",
                    maximum="PT5S",
                ),
                instance_id=f"{context.instance_id}:node",
            )
        )

    with _runtime([terminating_retry], [terminate_parent]) as (task_client, _):
        parent_id = task_client.schedule_new_orchestration(
            terminate_parent,
            instance_id="terminate-parent",
        )
        assert first_attempt.wait(timeout=5), task_client.get_orchestration_state(parent_id)
        task_client.terminate_orchestration(parent_id, output="stop", recursive=True)
        parent = task_client.wait_for_orchestration_completion(parent_id, timeout=10)
        child = task_client.get_orchestration_state("terminate-parent:node")

    assert parent is not None
    assert parent.runtime_status == client.OrchestrationStatus.TERMINATED
    assert child is not None
    assert child.runtime_status == client.OrchestrationStatus.RUNNING
    assert calls == 1
