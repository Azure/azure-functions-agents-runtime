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
    if configured_provider(APP_DIR) is None:
        pytest.skip("no LLM provider configured for retry-policy E2E")
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


def test_model_preserves_plan_and_decorator_retry_wins(
    retry_policy_host: HostHandle,
) -> None:
    session_id = f"retry-policy-e2e-{uuid.uuid4()}"
    reply = chat(
        retry_policy_host.base_url,
        "main",
        "Recover delayed order ORD-1001 and complete it safely.",
        session_id=session_id,
    )
    assert reply.status == 200, reply.body

    workflow = _wait_for_terminal_workflow(
        retry_policy_host.base_url,
        session_id,
        _workflow_id(reply.body.get("tool_calls")),
    )
    assert workflow["runtime_status"] == "Completed"
    custom_status = workflow.get("custom_status")
    assert isinstance(custom_status, dict)
    nodes = custom_status.get("nodes")
    assert isinstance(nodes, dict)
    reserve = nodes.get("reserve_inventory")
    confirm = nodes.get("confirm_order")
    assert isinstance(reserve, dict)
    assert reserve["state"] == "completed"
    assert reserve["attempt"] == 3
    assert reserve["max_attempts"] == 3
    assert isinstance(confirm, dict)
    assert confirm["state"] == "completed"
