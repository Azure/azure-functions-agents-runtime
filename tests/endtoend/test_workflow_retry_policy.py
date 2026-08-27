"""Model-backed E2E coverage for Dynamic Workflow retry policy precedence."""

from __future__ import annotations

import json
import shutil
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.endtoend._agent_probe import chat, wait_until_responsive
from tests.endtoend._func_host import (
    HostHandle,
    configured_provider,
    overlay_provider_settings,
    running_host,
)

APP_DIR = Path(__file__).resolve().parent / "apps" / "workflow-retry-policy"
TERMINAL_STATES = {"Completed", "Failed", "Canceled", "Terminated"}

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        shutil.which("func") is None,
        reason="Azure Functions Core Tools (`func`) not found on PATH",
    ),
]


@pytest.fixture(scope="module")
def retry_policy_host() -> Iterator[HostHandle]:
    overlay_provider_settings(APP_DIR)
    with running_host(APP_DIR) as handle:
        wait_until_responsive(handle.base_url)
        yield handle


def _workflow_id(tool_calls: Any) -> str:
    if not isinstance(tool_calls, list):
        raise AssertionError("chat response omitted tool calls")
    names = {
        call.get("tool_name")
        for call in tool_calls
        if isinstance(call, dict) and call.get("type") == "tool_start"
    }
    assert {"load_skill", "read_skill_resource", "start_workflow"} <= names
    start = next(
        call
        for call in tool_calls
        if isinstance(call, dict)
        and call.get("type") == "tool_start"
        and call.get("tool_name") == "start_workflow"
    )
    result = start.get("result")
    parsed = json.loads(result) if isinstance(result, str) else result
    assert isinstance(parsed, dict)
    workflow_id = parsed.get("workflow_id")
    assert isinstance(workflow_id, str) and workflow_id
    return workflow_id


def _request_workflows(base_url: str, session_id: str) -> list[Any]:
    request = urllib.request.Request(
        f"{base_url}/agents/main/workflows",
        headers={"x-ms-session-id": session_id},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert isinstance(payload, dict)
    workflows = payload.get("workflows")
    assert isinstance(workflows, list)
    return workflows


def _start_model_workflow(base_url: str, session_id: str) -> str:
    tool_calls: list[Any] = []
    prompts = [
        "Recover delayed order ORD-1001 and complete it safely.",
        (
            "Continue the retry-policy-e2e skill now. Read its resource and call "
            "start_workflow; do not stop until start_workflow returns."
        ),
        (
            "Finish the retry-policy-e2e skill now by calling any remaining required "
            "tool. Return only after start_workflow returns."
        ),
    ]
    last_error: AssertionError | None = None
    for prompt in prompts:
        reply = chat(base_url, "main", prompt, session_id=session_id)
        assert reply.status == 200, reply.body
        calls = reply.body.get("tool_calls")
        if isinstance(calls, list):
            tool_calls.extend(calls)
        try:
            return _workflow_id(tool_calls)
        except AssertionError as exc:
            last_error = exc

    assert last_error is not None
    raise AssertionError(
        f"model did not complete retry-policy-e2e after {len(prompts)} turns"
    ) from last_error


def _wait_for_terminal_workflow(
    base_url: str,
    session_id: str,
    workflow_id: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        workflow = next(
            (
                item
                for item in _request_workflows(base_url, session_id)
                if isinstance(item, dict) and item.get("workflow_id") == workflow_id
            ),
            None,
        )
        if workflow is not None and workflow.get("runtime_status") in TERMINAL_STATES:
            return workflow
        time.sleep(1)
    raise AssertionError(f"workflow {workflow_id!r} did not finish within 120 seconds")


def _start_durable_workflow(base_url: str, payload: dict[str, Any]) -> str:
    request = urllib.request.Request(
        f"{base_url}/runtime/webhooks/durabletask/orchestrators/"
        "agents_workflow_orchestrator",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 202
        started = json.loads(response.read().decode())
    status_uri = started.get("statusQueryGetUri")
    assert isinstance(status_uri, str) and status_uri
    return status_uri


def _wait_for_durable_status(status_uri: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        with urllib.request.urlopen(status_uri, timeout=10) as response:
            status = json.loads(response.read().decode())
        if status.get("runtimeStatus") in TERMINAL_STATES:
            return status
        time.sleep(1)
    raise AssertionError("Durable workflow did not finish within 120 seconds")


def _native_execution() -> dict[str, Any]:
    return {
        "timeout_ms": 5_000,
        "max_attempts": 3,
        "retry_delays_ms": [100, 100],
        "continue_on_error": False,
        "timeout_source": "decorator",
        "retry_source": "decorator",
        "durable_retry_policy": {
            "first_retry_interval_ms": 100,
            "max_number_of_attempts": 3,
            "backoff_coefficient": 1.0,
            "max_retry_interval_ms": 100,
            "retry_timeout_ms": 3_600_000,
        },
    }


def test_durable_native_retry_reaches_success(retry_policy_host: HostHandle) -> None:
    plan = json.loads(
        (
            APP_DIR
            / "skills"
            / "retry-policy-e2e"
            / "references"
            / "order-recovery-plan.json"
        ).read_text(encoding="utf-8")
    )
    reserve = next(task for task in plan["tasks"] if task["id"] == "reserve_inventory")
    reserve["execution"] = _native_execution()
    status = _wait_for_durable_status(
        _start_durable_workflow(
            retry_policy_host.base_url,
            {
                "workflow_agent_slug": "main",
                "tasks": plan["tasks"],
                "policy": {
                    "allowed_tools": [
                        "confirm_order",
                        "load_order",
                        "reserve_inventory",
                    ],
                    "allowed_subagents": [],
                },
            },
        )
    )

    assert status["runtimeStatus"] == "Completed"
    assert status["output"]["results"]["reserve_inventory"][
        "transient_failures_observed"
    ] == 2
    custom_status = status["customStatus"]
    assert custom_status["schema_version"] == 4
    assert custom_status["retry_driver"] == "durable"
    assert custom_status["nodes"]["reserve_inventory"] == {
        "state": "completed",
        "max_attempts": 3,
    }


def test_durable_native_retry_exhaustion_restores_failure(
    retry_policy_host: HostHandle,
) -> None:
    status = _wait_for_durable_status(
        _start_durable_workflow(
            retry_policy_host.base_url,
            {
                "workflow_agent_slug": "main",
                "tasks": [{
                    "id": "always_fail",
                    "type": "tool",
                    "tool": "always_fail_inventory",
                    "args": {},
                    "depends_on": [],
                    "execution": _native_execution(),
                }],
                "policy": {
                    "allowed_tools": ["always_fail_inventory"],
                    "allowed_subagents": [],
                },
            },
        )
    )

    assert status["runtimeStatus"] == "Completed"
    assert status["output"] == {
        "failed": True,
        "error": "Inventory remains temporarily unavailable.",
        "error_code": "inventory_retry_exhausted",
        "node_id": "always_fail",
        "path": None,
        "results": {},
        "attempts": 3,
        "kind": "handler_transient",
    }
    assert status["customStatus"]["nodes"]["always_fail"] == {
        "state": "failed",
        "max_attempts": 3,
    }


def test_model_preserves_plan_and_decorator_retry_wins(
    retry_policy_host: HostHandle,
) -> None:
    if configured_provider(APP_DIR) is None:
        pytest.skip("no LLM provider configured for retry-policy E2E")
    session_id = f"retry-policy-e2e-{uuid.uuid4()}"
    workflow = _wait_for_terminal_workflow(
        retry_policy_host.base_url,
        session_id,
        _start_model_workflow(retry_policy_host.base_url, session_id),
    )
    assert workflow["runtime_status"] == "Completed"
    output = workflow.get("output")
    assert isinstance(output, dict)
    results = output.get("results")
    assert isinstance(results, dict)
    assert results["reserve_inventory"]["transient_failures_observed"] == 2
    assert results["confirm_order"]["transient_failures_observed"] == 2
    custom_status = workflow.get("custom_status")
    assert isinstance(custom_status, dict)
    nodes = custom_status.get("nodes")
    assert isinstance(nodes, dict)
    reserve = nodes.get("reserve_inventory")
    confirm = nodes.get("confirm_order")
    assert isinstance(reserve, dict)
    assert reserve["state"] == "completed"
    assert reserve["max_attempts"] == 3
    assert "attempt" not in reserve
    assert custom_status["schema_version"] == 4
    assert custom_status["retry_driver"] == "durable"
    assert isinstance(confirm, dict)
    assert confirm["state"] == "completed"
