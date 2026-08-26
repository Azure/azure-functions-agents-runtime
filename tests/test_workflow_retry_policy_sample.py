"""Contracts for the focused workflow retry-policy sample."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from azure_functions_agents.discovery.skills import discover_skills
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
    WorkflowRetryableError,
    resolve_workflow_task_execution,
    validate_plan,
)

_SAMPLE_ROOT = Path(__file__).resolve().parents[1] / "samples" / "workflow-retry-policy"
_SAMPLE_SRC = _SAMPLE_ROOT / "src"
_RESOURCE = (
    _SAMPLE_SRC
    / "skills"
    / "resilient-order-recovery"
    / "references"
    / "order-recovery-plan.json"
)
_SPEC = importlib.util.spec_from_file_location(
    "workflow_retry_policy_sample",
    _SAMPLE_SRC / "tools" / "order_tools.py",
)
assert _SPEC is not None and _SPEC.loader is not None
order_tools = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(order_tools)

_E2E_SPEC = importlib.util.spec_from_file_location(
    "workflow_retry_policy_e2e",
    _SAMPLE_ROOT / "scripts" / "run-e2e.py",
)
assert _E2E_SPEC is not None and _E2E_SPEC.loader is not None
e2e = importlib.util.module_from_spec(_E2E_SPEC)
_E2E_SPEC.loader.exec_module(e2e)


def test_sample_has_one_deployed_canonical_plan_resource() -> None:
    plan_text = _RESOURCE.read_text(encoding="utf-8").strip()
    plan = json.loads(plan_text)
    skill_text = (
        _SAMPLE_SRC / "skills" / "resilient-order-recovery" / "SKILL.md"
    ).read_text(encoding="utf-8")
    agent_text = (_SAMPLE_SRC / "main.agent.md").read_text(encoding="utf-8")

    assert "references/order-recovery-plan.json" in skill_text
    assert plan_text not in skill_text
    assert plan_text not in agent_text
    assert [task["id"] for task in plan["tasks"]] == [
        "open_inventory_incident",
        "load_order",
        "reserve_inventory",
        "confirm_order",
    ]


def test_sample_skill_and_workflow_tools_are_discoverable() -> None:
    skills = discover_skills(_SAMPLE_SRC)
    assert skills.failed_loads == []
    assert skills.skills == {
        "resilient-order-recovery": _SAMPLE_SRC / "skills" / "resilient-order-recovery"
    }

    clear_tool_discovery_cache()
    discovered = discover_project_tools(_SAMPLE_SRC)
    assert discovered.user_tools == []
    assert {tool.name for tool in discovered.workflow_tools} == {
        "open_inventory_incident",
        "load_order",
        "reserve_inventory",
        "confirm_order",
    }


def test_canonical_plan_demonstrates_decorator_precedence() -> None:
    raw = json.loads(_RESOURCE.read_text(encoding="utf-8"))
    plan = validate_plan(
        raw,
        allowed_tools={
            "open_inventory_incident",
            "load_order",
            "reserve_inventory",
            "confirm_order",
        },
    )
    reserve_task = next(task for task in plan.tasks if task.id == "reserve_inventory")

    clear_tool_discovery_cache()
    discovered = discover_project_tools(_SAMPLE_SRC)
    declaration = next(
        tool for tool in discovered.workflow_tools if tool.name == "reserve_inventory"
    )
    effective = resolve_workflow_task_execution(
        reserve_task,
        decorator_timeout=declaration.timeout,
        decorator_retry=declaration.retry,
    )

    assert effective is not None
    assert effective["timeout_ms"] == 5_000
    assert effective["max_attempts"] == 3
    assert effective["retry_delays_ms"] == [0, 0]
    assert effective["timeout_source"] == "decorator"
    assert effective["retry_source"] == "decorator"


def test_order_recovery_story_uses_blob_state_then_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: dict[str, tuple[bytes, str]] = {}

    class _Download:
        def __init__(self, value: bytes, etag: str) -> None:
            self._value = value
            self.properties = {"etag": etag}

        def readall(self) -> bytes:
            return self._value

    class _Blob:
        def __init__(self, name: str) -> None:
            self._name = name

        def upload_blob(self, value: str, **options: object) -> None:
            overwrite = options.pop("overwrite")
            expected_etag = options.pop("etag", None)
            options.pop("match_condition", None)
            assert options == {}
            current = stored.get(self._name)
            if overwrite is False and current is not None:
                raise order_tools.ResourceExistsError("blob already exists")
            assert overwrite in {True, False}
            if expected_etag is not None and (
                current is None or current[1] != expected_etag
            ):
                raise order_tools.ResourceModifiedError("entity tag changed")
            next_etag = str(int(current[1]) + 1) if current else "1"
            stored[self._name] = (value.encode(), next_etag)

        def download_blob(self) -> _Download:
            return _Download(*stored[self._name])

    class _Container:
        def create_container(self) -> None:
            return None

        def get_blob_client(self, name: str) -> _Blob:
            return _Blob(name)

    class _BlobService:
        @classmethod
        def from_connection_string(cls, value: str) -> _BlobService:
            assert value == "UseDevelopmentStorage=true"
            return cls()

        def get_container_client(self, name: str) -> _Container:
            assert name == "workflow-retry-policy"
            return _Container()

    monkeypatch.setattr(order_tools, "BlobServiceClient", _BlobService)
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")

    setup_context = WorkflowTaskContext(
        workflow_id="workflow-123",
        task_id="open_inventory_incident",
        node_instance_id="open_inventory_incident",
        attempt=1,
        max_attempts=1,
        idempotency_key="setup-key",
        deadline=datetime(2026, 8, 26, tzinfo=UTC),
    )
    token = _set_workflow_task_context(setup_context)
    try:
        incident = order_tools.open_inventory_incident({"order_id": "ORD-1001"})
    finally:
        _reset_workflow_task_context(token)
    assert incident == {
        "order_id": "ORD-1001",
        "incident_id": "5d29a9de372abe7a92de8f3c85f7cdf3",
        "status": "active",
        "failures_remaining": 2,
    }
    order = order_tools.load_order({"order_id": "ORD-1001", "incident": incident})
    reservation = None

    for call in (1, 2, 3):
        attempt_context = WorkflowTaskContext(
            workflow_id="workflow-123",
            task_id="reserve_inventory",
            node_instance_id="reserve_inventory",
            attempt=call,
            max_attempts=3,
            idempotency_key="reservation-key",
            deadline=datetime(2026, 8, 26, tzinfo=UTC),
        )
        token = _set_workflow_task_context(attempt_context)
        try:
            if call < 3:
                with pytest.raises(WorkflowRetryableError) as exc_info:
                    order_tools.reserve_inventory({"order": order})
                assert exc_info.value.error_code == "inventory_temporarily_unavailable"
                if call == 1:
                    with pytest.raises(WorkflowRetryableError):
                        order_tools.reserve_inventory({"order": order})
                    setup_token = _set_workflow_task_context(setup_context)
                    try:
                        duplicate = order_tools.open_inventory_incident({
                            "order_id": "ORD-1001"
                        })
                    finally:
                        _reset_workflow_task_context(setup_token)
                    assert duplicate["failures_remaining"] == 1
            else:
                reservation = order_tools.reserve_inventory({"order": order})
        finally:
            _reset_workflow_task_context(token)

    assert reservation is not None
    assert reservation["transient_failures_observed"] == 2
    assert order_tools.confirm_order({"reservation": reservation}) == {
        "order_id": "ORD-1001",
        "status": "confirmed",
        "transient_failures_observed": 2,
    }
    stored_incident = json.loads(
        stored["orders/ORD-1001/incidents/5d29a9de372abe7a92de8f3c85f7cdf3.json"][0]
    )
    assert stored_incident == {
        "order_id": "ORD-1001",
        "failures_remaining": 0,
        "failed_attempts": [1, 2],
        "status": "recovered",
    }


def test_sample_settings_template_uses_foundry_and_azurite() -> None:
    values = json.loads(
        (_SAMPLE_SRC / "local.settings.template.json").read_text(encoding="utf-8")
    )["Values"]
    assert values == {
        "FUNCTIONS_WORKER_RUNTIME": "python",
        "AzureWebJobsStorage": "UseDevelopmentStorage=true",
        "AZURE_FUNCTIONS_AGENTS_PROVIDER": "foundry",
        "FOUNDRY_PROJECT_ENDPOINT": "",
        "FOUNDRY_MODEL": "gpt-5.4-mini",
    }


def test_live_e2e_is_opt_in_with_unique_foundry_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(e2e.ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(e2e.MODEL_ENV, raising=False)
    assert e2e.read_opt_in() is None

    monkeypatch.setenv(e2e.ENDPOINT_ENV, "https://example.services.ai.azure.com")
    with pytest.raises(RuntimeError, match=e2e.MODEL_ENV):
        e2e.read_opt_in()

    monkeypatch.setenv(e2e.MODEL_ENV, "sample-model")
    assert e2e.read_opt_in() == (
        "https://example.services.ai.azure.com",
        "sample-model",
    )
