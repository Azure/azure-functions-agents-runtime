"""End-to-end proof that Durable native Activity retry actually retries.

Boots the ``workflow-retry-policy`` sample under ``func start`` and drives its
workflow through Durable's built-in orchestration HTTP API. Going straight to
Durable keeps both cases deterministic and model-free: what is under test is the
runtime's retry behaviour, not an agent's ability to author a plan.

Two behaviours are asserted against a real host:

* a task whose tool reports transient failures is retried by Durable and the
  workflow still reaches ``Completed`` with the expected result;
* a task that keeps failing exhausts its attempt budget and the workflow reaches
  ``Failed`` carrying the application's own sanitized ``error_code`` rather than
  an opaque Durable ``TaskFailedError`` message.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from azure_functions_agents.workflows.schema import (
    WorkflowPlanPolicy,
    WorkflowRetryBackoff,
    WorkflowRetryPolicy,
    plan_to_activity_inputs,
    resolve_workflow_task_execution,
    validate_plan,
)
from tests.endtoend._func_host import running_host

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_APP = REPO_ROOT / "samples" / "workflow-retry-policy" / "src"
STORAGE_CONNECTION = "UseDevelopmentStorage=true"
CONTAINER = "workflow-retry-policy"
ORDER_ID = "ORD-1001"
MAX_ATTEMPTS = 3
_ORCHESTRATOR_NAME = "agents_workflow_orchestrator"
_EXPECTED_EXHAUSTION_FAILURE = (
    "task 'reserve_inventory': Inventory reservation is temporarily unavailable. "
    "(inventory_temporarily_unavailable)"
)
_PRIVATE_RETRY_MARKERS = (
    '"outcome"',
    '"version":1',
    '"version": 1',
    "Activity task #",
    "DurableRetryableActivityError",
)


def _failure_details_chain(failure_details: dict[str, Any]) -> list[dict[str, Any]]:
    chain = [failure_details]
    inner = failure_details.get("innerFailure")
    while inner is not None:
        if not isinstance(inner, dict):
            raise AssertionError(f"invalid nested failureDetails: {inner!r}")
        chain.append(inner)
        inner = inner.get("innerFailure")
    return chain


_STATUS_RUNTIME_ERROR_PREFIX = "builtins.RuntimeError: "

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]

# Mirrors the plan-authored policy in the sample agent instructions.
SAMPLE_RETRY = WorkflowRetryPolicy(
    max_attempts=MAX_ATTEMPTS,
    backoff=WorkflowRetryBackoff(initial="PT1S", multiplier=2.0, max="PT4S"),
)


def _order_recovery_payload() -> dict[str, Any]:
    """Build the orchestration input exactly as ``start_workflow`` would."""
    plan = validate_plan(
        {
            "tasks": [
                {
                    "id": "load_order",
                    "type": "tool",
                    "tool": "load_order",
                    "args": {"order_id": ORDER_ID},
                },
                {
                    "id": "reserve_inventory",
                    "type": "tool",
                    "tool": "reserve_inventory",
                    "args": {"order": "${load_order.result}"},
                    "depends_on": ["load_order"],
                    "execution": {"retry": SAMPLE_RETRY.model_dump()},
                },
                {
                    "id": "confirm_order",
                    "type": "tool",
                    "tool": "confirm_order",
                    "args": {"reservation": "${reserve_inventory.result}"},
                    "depends_on": ["reserve_inventory"],
                },
            ]
        },
        policy=WorkflowPlanPolicy(
            allowed_tools=frozenset({"load_order", "reserve_inventory", "confirm_order"})
        ),
    )
    effective = {
        task.id: policy
        for task in plan.tasks
        if task.id == "reserve_inventory"
        and (policy := resolve_workflow_task_execution(task)) is not None
    }
    assert "durable_retry_policy" in effective["reserve_inventory"]
    return {
        "tasks": plan_to_activity_inputs(plan, effective),
        "workflow_agent_slug": "main",
        "workflow_agent": {
            "workflow_agent_slug": "main",
            "session_id": "retry-e2e",
            "agent_name": "main",
        },
        "policy": {
            "allowed_tools": ["confirm_order", "load_order", "reserve_inventory"],
            "allowed_subagents": [],
        },
    }


def _incident_blob(workflow_id: str) -> Any:
    """Return the blob the sample uses to simulate a flaky inventory service."""
    from azure.storage.blob import BlobServiceClient

    incident_id = hashlib.sha256(workflow_id.encode()).hexdigest()[:32]
    container = BlobServiceClient.from_connection_string(
        STORAGE_CONNECTION
    ).get_container_client(CONTAINER)
    if not container.exists():
        container.create_container()
    return container.get_blob_client(f"orders/{ORDER_ID}/incidents/{incident_id}.json")


def _start_workflow(base_url: str, workflow_id: str) -> None:
    request = urllib.request.Request(
        f"{base_url}/runtime/webhooks/durabletask/orchestrators"
        f"/agents_workflow_orchestrator/{workflow_id}",
        data=json.dumps(_order_recovery_payload()).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status in {200, 202}


def _await_terminal(base_url: str, workflow_id: str, *, timeout: float = 240.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        with urllib.request.urlopen(
            f"{base_url}/runtime/webhooks/durabletask/instances/{workflow_id}", timeout=30
        ) as response:
            body = json.loads(response.read().decode())
        if body.get("runtimeStatus") in {"Completed", "Failed", "Terminated"}:
            return body
        time.sleep(2)
    raise AssertionError(f"workflow {workflow_id} never reached a terminal state: {body}")


def _final_orchestration_failure_from_log(host_output: str, workflow_id: str) -> str:
    """Extract the final orchestrator failure bounded by workflow-correlated records."""
    lines = host_output.splitlines()
    completed = (
        f"{workflow_id}: Orchestration {_ORCHESTRATOR_NAME} completed with status: FAILED"
    )
    completed_indexes = [index for index, line in enumerate(lines) if completed in line]
    if not completed_indexes:
        raise AssertionError(f"no final FAILED orchestration record for {workflow_id}")
    start = completed_indexes[-1]

    failed = (
        f"{workflow_id}: Function '{_ORCHESTRATOR_NAME} (Orchestrator)' "
        "failed with an error."
    )
    try:
        end = next(index for index in range(start + 1, len(lines)) if failed in lines[index])
    except StopIteration as exc:
        raise AssertionError(f"no correlated final failure record for {workflow_id}") from exc

    prefix = (
        "System.Private.CoreLib: Exception while executing function: "
        f"Functions.{_ORCHESTRATOR_NAME}. "
        "Microsoft.Azure.WebJobs.Extensions.DurableTask: "
    )
    messages = [
        line.partition(prefix)[2].removesuffix(".")
        for line in lines[start : end + 1]
        if prefix in line
    ]
    if len(messages) != 1:
        raise AssertionError(
            f"expected one final orchestration failure message for {workflow_id}, got {messages}"
        )
    return messages[0]


def _assert_decoded_exhaustion_failure(
    status: dict[str, Any],
    *,
    host_output: str,
    workflow_id: str,
) -> None:
    """Require the authoritative terminal failure to be the decoded application error."""
    failure_details = status.get("failureDetails")
    if failure_details is not None:
        if not isinstance(failure_details, dict) or not isinstance(
            failure_details.get("errorMessage"), str
        ):
            raise AssertionError(f"invalid terminal failureDetails: {failure_details!r}")
        failure_message = failure_details["errorMessage"]
        serialized_failure_details = json.dumps(
            _failure_details_chain(failure_details), sort_keys=True
        )
        for marker in _PRIVATE_RETRY_MARKERS:
            assert marker not in serialized_failure_details, (
                f"private retry marker {marker!r} leaked into terminal failureDetails: "
                f"{serialized_failure_details}"
            )
    else:
        output = status.get("output")
        if output is not None:
            if not isinstance(output, str):
                raise AssertionError(f"invalid terminal output: {output!r}")
            failure_message = output.removeprefix(_STATUS_RUNTIME_ERROR_PREFIX)
        else:
            failure_message = _final_orchestration_failure_from_log(host_output, workflow_id)

    assert failure_message == _EXPECTED_EXHAUSTION_FAILURE, (
        f"expected decoded final failure {_EXPECTED_EXHAUSTION_FAILURE!r}, "
        f"got {failure_message!r}"
    )
    assert "Activity task #" not in failure_message
    assert '"outcome"' not in failure_message


@pytest.fixture(scope="module")
def retry_sample_host() -> Any:
    """One host for both cases: a second host would contend for the task-hub lease."""
    with running_host(SAMPLE_APP) as host:
        yield host


def test_transient_tool_failures_are_retried_and_the_workflow_completes(
    retry_sample_host: Any,
) -> None:
    workflow_id = f"e2eretryok{int(time.time())}"
    _start_workflow(retry_sample_host.base_url, workflow_id)
    status = _await_terminal(retry_sample_host.base_url, workflow_id)

    assert status["runtimeStatus"] == "Completed", status
    results = status["output"]["results"]
    # The sample's inventory incident fails the first two deliveries; reaching a
    # reserved order at all means Durable re-delivered the Activity.
    assert results["reserve_inventory"]["reserved"] is True
    assert results["reserve_inventory"]["transient_failures_observed"] == 2
    assert results["confirm_order"]["status"] == "confirmed"


def test_exhausted_retry_fails_with_the_application_error_code(
    retry_sample_host: Any,
) -> None:
    workflow_id = f"e2eretryfail{int(time.time())}"
    blob = _incident_blob(workflow_id)
    # Never let the simulated dependency recover, so every attempt fails.
    blob.upload_blob(
        json.dumps(
            {
                "order_id": ORDER_ID,
                "failures_remaining": 99,
                "transient_failures_observed": 0,
                "status": "active",
            }
        ),
        overwrite=True,
    )

    log_start = len(retry_sample_host.read_output())
    _start_workflow(retry_sample_host.base_url, workflow_id)
    status = _await_terminal(retry_sample_host.base_url, workflow_id)

    assert status["runtimeStatus"] == "Failed", status

    incident = json.loads(blob.download_blob().readall())
    # Durable stopped at the declared attempt budget: no more, no fewer.
    assert incident["transient_failures_observed"] == MAX_ATTEMPTS
    assert incident["failures_remaining"] == 99 - MAX_ATTEMPTS

    # The authoritative terminal failure must be the decoded application error.
    # DTS currently reports `output: null`, so correlate the final orchestrator
    # failure records rather than accepting marker-bearing intermediate logs.
    _assert_decoded_exhaustion_failure(
        status,
        host_output=retry_sample_host.read_output()[log_start:],
        workflow_id=workflow_id,
    )
