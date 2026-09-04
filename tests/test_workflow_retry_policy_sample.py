"""Contracts for the customer-facing workflow retry-policy sample."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from azure_functions_agents.discovery.tools import (
    clear_tool_discovery_cache,
    discover_project_tools,
)
from azure_functions_agents.workflows.context import (
    WorkflowTaskContext,
    _reset_workflow_task_context,
    _set_workflow_task_context,
)
from azure_functions_agents.workflows.schema import (
    WorkflowPlanPolicy,
    WorkflowRetryableError,
    WorkflowRetryBackoff,
    WorkflowRetryPolicy,
    resolve_workflow_task_execution,
    validate_plan,
)
from tests.endtoend.test_workflow_native_retry_e2e import (
    _assert_decoded_exhaustion_failure,
)

_SAMPLE_SRC = (
    Path(__file__).resolve().parents[1] / "samples" / "workflow-retry-policy" / "src"
)
_SPEC = importlib.util.spec_from_file_location(
    "workflow_retry_policy_sample",
    _SAMPLE_SRC / "tools" / "order_tools.py",
)
assert _SPEC is not None and _SPEC.loader is not None
order_tools = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(order_tools)


def test_sample_agent_authors_the_retry_policy() -> None:
    agent_text = (_SAMPLE_SRC / "main.agent.md").read_text(encoding="utf-8")

    assert "start_workflow" in agent_text
    assert "execution.retry" in agent_text
    assert "max_attempts: 3" in agent_text


def _terminal_host_output(workflow_id: str, failure_message: str) -> str:
    return "\n".join(
        (
            f"{workflow_id}: Orchestration agents_workflow_orchestrator "
            "completed with status: FAILED",
            "Executed 'Functions.agents_workflow_orchestrator' (Failed, Id=test)",
            "System.Private.CoreLib: Exception while executing function: "
            "Functions.agents_workflow_orchestrator. "
            f"Microsoft.Azure.WebJobs.Extensions.DurableTask: {failure_message}.",
            f"{workflow_id}: Function 'agents_workflow_orchestrator (Orchestrator)' "
            "failed with an error.",
        )
    )


def test_retry_e2e_accepts_only_the_decoded_final_orchestration_failure() -> None:
    workflow_id = "workflow-1"
    decoded = (
        "task 'reserve_inventory': Inventory reservation is temporarily unavailable. "
        "(inventory_temporarily_unavailable)"
    )

    _assert_decoded_exhaustion_failure(
        {"output": None},
        host_output=_terminal_host_output(workflow_id, decoded),
        workflow_id=workflow_id,
    )


def test_retry_e2e_rejects_the_raw_activity_failure_wrapper() -> None:
    workflow_id = "workflow-1"
    raw_marker = (
        f"{workflow_id}: Activity task #2 failed: "
        '{"outcome":{"failure":{"error":"Inventory reservation is temporarily unavailable.",'
        '"error_code":"inventory_temporarily_unavailable","kind":"handler_transient",'
        '"retryable":true},"id":"reserve_inventory","ok":false},"version":1}'
    )

    with pytest.raises(AssertionError, match="expected decoded final failure"):
        _assert_decoded_exhaustion_failure(
            {"output": None},
            host_output=_terminal_host_output(workflow_id, raw_marker),
            workflow_id=workflow_id,
        )


def test_sample_tools_are_discoverable_without_retry_metadata() -> None:
    clear_tool_discovery_cache()
    discovered = discover_project_tools(_SAMPLE_SRC)

    by_name = {tool.name: tool for tool in discovered.workflow_tools}
    assert set(by_name) == {"load_order", "reserve_inventory", "confirm_order"}


def test_sample_plan_freezes_its_authored_retry() -> None:
    retry = WorkflowRetryPolicy(
        max_attempts=3,
        backoff=WorkflowRetryBackoff(initial="PT1S", multiplier=2.0, max="PT4S"),
    )
    plan = validate_plan(
        {
            "tasks": [
                {
                    "id": "reserve_inventory",
                    "type": "tool",
                    "tool": "reserve_inventory",
                    "args": {},
                    "execution": {"retry": retry.model_dump()},
                }
            ]
        },
        policy=WorkflowPlanPolicy(allowed_tools=frozenset({"reserve_inventory"})),
    )

    effective = resolve_workflow_task_execution(plan.tasks[0])

    assert effective == {
        "max_attempts": 3,
        "durable_retry_policy": {
            "first_retry_interval_ms": 1_000,
            "max_number_of_attempts": 3,
            "backoff_coefficient": 2.0,
            "max_retry_interval_ms": 4_000,
        },
    }


def test_sample_inventory_failure_is_classified_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incident = {
        "order_id": "ORD-1001",
        "failures_remaining": 1,
        "transient_failures_observed": 0,
        "status": "active",
    }

    def fake_load_or_create(incident_id: str) -> tuple[dict[str, object], str]:
        return dict(incident), "etag-1"

    def fake_write(incident_id: str, state: dict[str, object], **kwargs: object) -> None:
        incident.update(state)

    monkeypatch.setattr(order_tools, "_load_or_create_incident", fake_load_or_create)
    monkeypatch.setattr(order_tools, "_write_incident", fake_write)

    token = _set_workflow_task_context(
        WorkflowTaskContext(
            workflow_id="workflow-1",
            task_id="reserve_inventory",
            node_instance_id="reserve_inventory",
            max_attempts=3,
            idempotency_key="af-wf-task-v1:test",
        )
    )
    try:
        with pytest.raises(WorkflowRetryableError) as raised:
            order_tools.reserve_inventory({"order": {"order_id": "ORD-1001", "sku": "s"}})
    finally:
        _reset_workflow_task_context(token)

    assert raised.value.error_code == "inventory_temporarily_unavailable"
    assert incident["transient_failures_observed"] == 1
