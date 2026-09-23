"""End-to-end smoke test for an async ``@workflow_tool`` on a real Functions host.

Boots the ``workflow-incident-triage`` sample under ``func start`` and runs a
workflow through Durable's built-in orchestration HTTP API. The sample's
``fetch_deploys`` handler is ``async def``; ``fetch_logs``, ``fetch_metrics``, and
``summarize_findings`` are synchronous. The test is model-free: the orchestration
input comes from the production ``start_workflow`` path, not from an agent.

A ``Completed`` status shows that the host discovered the async handler,
registered the workflow, scheduled the Durable Activity, awaited the handler, and
serialized its result for the downstream synchronous task.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.config.merge import compose
from azure_functions_agents.discovery.tools import discover_project_tools
from azure_functions_agents.registration.capabilities import build_capabilities
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog
from azure_functions_agents.workflows import integration
from azure_functions_agents.workflows import tools as workflow_tools
from azure_functions_agents.workflows.schema import WorkflowPlanPolicy
from tests.endtoend._func_host import running_host

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_APP = REPO_ROOT / "samples" / "workflow-incident-triage" / "src"
SERVICE = "orders-api"
_ORCHESTRATOR_NAME = "agents_workflow_orchestrator"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]


def _sample_workflow_policy() -> WorkflowPlanPolicy:
    """Build the sample policy through the production composition stages."""
    global_config = load_global_config(SAMPLE_APP)
    discovered = discover_project_tools(SAMPLE_APP)
    entries: dict[str, CatalogEntry] = {}
    for spec in load_agent_specs(SAMPLE_APP):
        resolved = compose(
            spec,
            global_config,
            discovered_mcp_names=[],
            discovered_skill_names=[],
        )
        entries[resolved.slug] = CatalogEntry(
            resolved,
            build_capabilities(
                resolved,
                discovered_user_tools=discovered.user_tools,
                discovered_workflow_tools=discovered.workflow_tools,
                discovered_mcp_tools={},
                discovered_skills={},
            ),
        )
    handler_catalog = integration.build_workflow_handler_catalog(discovered.workflow_tools)
    return integration.build_workflow_agent_policy_catalog(
        build_catalog(entries),
        handler_catalog,
    )["main"]


def _triage_submission() -> tuple[str, dict[str, Any]]:
    """Capture orchestration input produced by the production ``start_workflow`` path."""
    captured: dict[str, Any] = {}

    class _CapturingClient:
        async def get_status_all(self) -> list[Any]:
            return []

        async def schedule_new_orchestration(
            self,
            name: str,
            *,
            instance_id: str,
            input: Any,
            tags: dict[str, str],
        ) -> str:
            assert name == _ORCHESTRATOR_NAME
            captured.update(input)
            return instance_id

    params = workflow_tools.StartWorkflowParams.model_validate(
        {
            "tasks": [
                {"id": "logs", "type": "tool", "tool": "fetch_logs", "args": {"service": SERVICE}},
                {
                    "id": "metrics",
                    "type": "tool",
                    "tool": "fetch_metrics",
                    "args": {"service": SERVICE},
                },
                {
                    "id": "deploys",
                    "type": "tool",
                    "tool": "fetch_deploys",
                    "args": {"service": SERVICE},
                },
                {
                    "id": "summary",
                    "type": "tool",
                    "tool": "summarize_findings",
                    "args": {
                        "logs": "${logs.result}",
                        "metrics": "${metrics.result}",
                        "deploys": "${deploys.result}",
                    },
                    "depends_on": ["logs", "metrics", "deploys"],
                },
            ]
        }
    )
    response = json.loads(
        asyncio.run(
            workflow_tools.start_workflow(
                params,
                workflow_tools.WorkflowSessionContext(
                    workflow_agent_slug="main",
                    session_id="async-tool-e2e",
                    agent_name="main",
                    durable_client=_CapturingClient(),  # type: ignore[arg-type]
                ),
                policy=_sample_workflow_policy(),
            )
        )
    )
    return response["workflow_id"], captured


def _start_workflow(base_url: str, workflow_id: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        f"{base_url}/runtime/webhooks/durabletask/orchestrators"
        f"/{_ORCHESTRATOR_NAME}/{workflow_id}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status in {200, 202}


def _await_terminal(base_url: str, workflow_id: str, *, timeout: float = 180.0) -> dict[str, Any]:
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


def test_async_workflow_tool_completes_on_functions_host() -> None:
    workflow_id, payload = _triage_submission()

    with running_host(SAMPLE_APP) as host:
        _start_workflow(host.base_url, workflow_id, payload)
        status = _await_terminal(host.base_url, workflow_id)
        host_output = host.read_output()

    assert status["runtimeStatus"] == "Completed", f"{status}\n--- func output ---\n{host_output}"
    results = status["output"]["results"]
    deploys = results["deploys"]
    assert deploys["service"] == SERVICE
    assert deploys["lookback_hours"] == 24
    assert len(deploys["deploys"]) == 2
    assert results["logs"]["service"] == SERVICE
    assert results["summary"]["service"] == SERVICE
    assert any("deploy" in item for item in results["summary"]["evidence"])
