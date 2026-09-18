"""Contracts for the customer-facing workflow retry-policy sample."""

from __future__ import annotations

import importlib.util
import json
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
    WorkflowTerminalError,
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


def test_sample_provides_storage_and_dts_host_configurations() -> None:
    host_config = json.loads((_SAMPLE_SRC / "host.json").read_text(encoding="utf-8"))
    assert "durableTask" not in host_config["extensions"]

    dts_config = json.loads(
        (_SAMPLE_SRC / "host.dts.json").read_text(encoding="utf-8")
    )
    assert dts_config["extensions"]["durableTask"] == {
        "hubName": "%TASKHUB_NAME%",
        "tracing": {
            "distributedTracingEnabled": True,
            "version": "V2",
        },
        "storageProvider": {
            "type": "azureManaged",
            "connectionStringName": "DURABLE_TASK_SCHEDULER_CONNECTION_STRING",
        },
    }

    settings = json.loads(
        (_SAMPLE_SRC / "local.settings.template.json").read_text(encoding="utf-8")
    )
    assert settings["Values"]["DURABLE_TASK_SCHEDULER_CONNECTION_STRING"] == (
        "Endpoint=http://localhost:8080;Authentication=None"
    )
    assert settings["Values"]["TASKHUB_NAME"] == "workflowretrypolicy"


def test_sample_agent_relies_on_the_tool_retry_policy() -> None:
    agent_text = (_SAMPLE_SRC / "main.agent.md").read_text(encoding="utf-8")

    assert "start_workflow" in agent_text
    assert "execution.retry" in agent_text
    assert "execution.timeout" in agent_text
    assert "execution.continue_on_error" in agent_text
    assert "tool owns its retry policy" in agent_text


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
        {"failureDetails": {"errorMessage": decoded}},
        host_output="",
        workflow_id=workflow_id,
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


def test_retry_e2e_rejects_private_marker_nested_under_clean_failure_details() -> None:
    workflow_id = "workflow-1"
    decoded = (
        "task 'reserve_inventory': Inventory reservation is temporarily unavailable. "
        "(inventory_temporarily_unavailable)"
    )
    raw_marker = (
        'private retry metadata: {"outcome":{"ok":false},"version":1}'
    )

    with pytest.raises(AssertionError, match="private retry marker"):
        _assert_decoded_exhaustion_failure(
            {
                "failureDetails": {
                    "errorMessage": decoded,
                    "innerFailure": {
                        "errorType": "RuntimeError",
                        "errorMessage": raw_marker,
                    },
                }
            },
            host_output="",
            workflow_id=workflow_id,
        )


def test_sample_tools_declare_retry_only_on_inventory_reservation() -> None:
    clear_tool_discovery_cache()
    discovered = discover_project_tools(_SAMPLE_SRC)

    by_name = {tool.name: tool for tool in discovered.workflow_tools}
    assert set(by_name) == {
        "confirm_order",
        "load_order",
        "notify_customer",
        "reserve_inventory",
        "verify_carrier",
    }
    assert by_name["load_order"].retry is None
    assert by_name["confirm_order"].retry is None
    assert by_name["notify_customer"].retry is None
    assert by_name["verify_carrier"].retry is None
    assert by_name["verify_carrier"].timeout == "PT1S"
    retry = by_name["reserve_inventory"].retry
    assert retry is not None
    assert retry.max_attempts == 3


def test_sample_plan_freezes_its_tool_declared_retry() -> None:
    clear_tool_discovery_cache()
    discovered = discover_project_tools(_SAMPLE_SRC)
    retry = {
        tool.name: tool.retry for tool in discovered.workflow_tools
    }["reserve_inventory"]
    assert retry is not None
    plan = validate_plan(
        {
            "tasks": [
                {
                    "id": "reserve_inventory",
                    "type": "tool",
                    "tool": "reserve_inventory",
                    "args": {},
                }
            ]
        },
        policy=WorkflowPlanPolicy(allowed_tools=frozenset({"reserve_inventory"})),
    )

    effective = resolve_workflow_task_execution(
        plan.tasks[0],
        decorator_retry=retry,
    )

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


def test_sample_notification_failure_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incident = {
        "order_id": "ORD-1001",
        "failures_remaining": 0,
        "transient_failures_observed": 2,
        "carrier_attempts": 2,
        "notice_attempts": 0,
        "status": "recovered",
    }

    def fake_read(incident_id: str) -> tuple[dict[str, object], str]:
        return dict(incident), "etag-1"

    def fake_write(incident_id: str, state: dict[str, object], **kwargs: object) -> None:
        incident.update(state)

    monkeypatch.setattr(order_tools, "_read_incident", fake_read)
    monkeypatch.setattr(order_tools, "_write_incident", fake_write)
    token = _set_workflow_task_context(
        WorkflowTaskContext(
            workflow_id="workflow-1",
            task_id="notify_customer",
            node_instance_id="notify_customer",
            max_attempts=1,
            idempotency_key="af-wf-task-v1:test",
        )
    )
    try:
        with pytest.raises(WorkflowTerminalError) as raised:
            order_tools.notify_customer(
                {"reservation": {"order_id": "ORD-1001", "reserved": True}}
            )
    finally:
        _reset_workflow_task_context(token)

    assert raised.value.error_code == "customer_notice_unavailable"
    assert incident["notice_attempts"] == 1


def test_sample_confirmation_accepts_continued_failures() -> None:
    result = order_tools.confirm_order({
        "carrier": {
            "failed": True,
            "error_code": "workflow_task_timeout",
            "error": "Task attempt timed out.",
            "kind": "handler_transient",
        },
        "notice": {
            "failed": True,
            "error_code": "customer_notice_unavailable",
            "error": "Customer notification is unavailable.",
            "kind": "handler_terminal",
        },
    })

    assert result == {
        "order_id": "ORD-1001",
        "status": "confirmed",
        "carrier": "manual verification required",
        "timeout_attempts_observed": None,
        "carrier_verification_failed": True,
        "customer_notice_failed": True,
    }
