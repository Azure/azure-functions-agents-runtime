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

    _start_workflow(retry_sample_host.base_url, workflow_id)
    status = _await_terminal(retry_sample_host.base_url, workflow_id)

    assert status["runtimeStatus"] == "Failed", status

    incident = json.loads(blob.download_blob().readall())
    # Durable stopped at the declared attempt budget: no more, no fewer.
    assert incident["transient_failures_observed"] == MAX_ATTEMPTS
    assert incident["failures_remaining"] == 99 - MAX_ATTEMPTS

    # The application's own sanitized failure survives, rather than degrading to
    # an opaque Durable TaskFailedError message. Only the Azure Storage backend
    # surfaces the orchestration failure through the status API's `output`; the
    # Durable Task Scheduler backend reports `output: null` for a failed
    # orchestration, so fall back to the host log there rather than asserting
    # nothing.
    output = status.get("output")
    failure_text = str(output) if output is not None else retry_sample_host.read_output()
    assert "inventory_temporarily_unavailable" in failure_text
    assert "Inventory reservation is temporarily unavailable." in failure_text
    assert "reserve_inventory" in failure_text
