"""Contracts for the customer-facing workflow retry-policy sample."""

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
_SPEC = importlib.util.spec_from_file_location(
    "workflow_retry_policy_sample",
    _SAMPLE_SRC / "tools" / "order_tools.py",
)
assert _SPEC is not None and _SPEC.loader is not None
order_tools = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(order_tools)


def test_sample_agent_uses_normal_workflow_authoring() -> None:
    agent_text = (_SAMPLE_SRC / "main.agent.md").read_text(encoding="utf-8")
    assert "start_workflow" in agent_text
    assert "load_order" in agent_text
    assert "reserve_inventory" in agent_text
    assert "confirm_order" in agent_text
    assert "read_skill_resource" not in agent_text
    assert "order-recovery-plan.json" not in agent_text

    skills = discover_skills(_SAMPLE_SRC)
    assert skills.failed_loads == []
    assert skills.skills == {}


def test_sample_workflow_tools_are_discoverable() -> None:
    clear_tool_discovery_cache()
    discovered = discover_project_tools(_SAMPLE_SRC)
    assert discovered.user_tools == []
    assert {tool.name for tool in discovered.workflow_tools} == {
        "load_order",
        "reserve_inventory",
        "confirm_order",
    }


def test_reserve_inventory_decorator_supplies_retry_policy() -> None:
    plan = validate_plan(
        {
            "version": 1,
            "tasks": [{
                "id": "reserve_inventory",
                "type": "tool",
                "tool": "reserve_inventory",
                "args": {},
                "depends_on": [],
            }],
        },
        allowed_tools={"reserve_inventory"},
    )
    clear_tool_discovery_cache()
    declaration = next(
        tool
        for tool in discover_project_tools(_SAMPLE_SRC).workflow_tools
        if tool.name == "reserve_inventory"
    )
    effective = resolve_workflow_task_execution(
        plan.tasks[0],
        decorator_timeout=declaration.timeout,
        decorator_retry=declaration.retry,
    )

    assert effective is not None
    assert effective["timeout_ms"] == 5_000
    assert effective["max_attempts"] == 3
    assert effective["retry_delays_ms"] == [1_000, 2_000]
    assert effective["timeout_source"] == "decorator"
    assert effective["retry_source"] == "decorator"


def test_order_recovery_story_uses_internal_blob_state(
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

    order = order_tools.load_order({"order_id": "ORD-1001"})
    reservation = None
    for attempt in (1, 2, 3):
        context = WorkflowTaskContext(
            workflow_id="workflow-123",
            task_id="reserve_inventory",
            node_instance_id="reserve_inventory",
            attempt=attempt,
            max_attempts=3,
            idempotency_key="reservation-key",
            deadline=datetime(2026, 8, 26, tzinfo=UTC),
        )
        token = _set_workflow_task_context(context)
        try:
            if attempt < 3:
                with pytest.raises(WorkflowRetryableError) as exc_info:
                    order_tools.reserve_inventory({"order": order})
                assert exc_info.value.error_code == "inventory_temporarily_unavailable"
                if attempt == 1:
                    with pytest.raises(WorkflowRetryableError):
                        order_tools.reserve_inventory({"order": order})
            else:
                reservation = order_tools.reserve_inventory({"order": order})
        finally:
            _reset_workflow_task_context(token)

    assert reservation is not None
    assert order_tools.confirm_order({"reservation": reservation}) == {
        "order_id": "ORD-1001",
        "status": "confirmed",
        "transient_failures_observed": 2,
    }
    state = json.loads(
        stored["orders/ORD-1001/incidents/5d29a9de372abe7a92de8f3c85f7cdf3.json"][0]
    )
    assert state == {
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
