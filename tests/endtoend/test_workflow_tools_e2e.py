"""End-to-end test for sync and async ``@workflow_tool`` handlers driven by a live agent.

Boots the ``workflow-incident-triage`` sample under ``func start`` with a live
model provider. A chat prompt asks the agent to investigate an incident; the agent
authors a workflow plan and calls ``start_workflow``. The test then follows that
session's workflow through the runtime's ``/agents/main/workflows`` endpoint.

The sample's ``fetch_deploys`` handler is ``async def``; ``fetch_logs``,
``fetch_metrics``, and ``summarize_findings`` are synchronous. A ``Completed``
workflow that contains the results of both kinds of handler shows that the host
discovered them, exposed them to the agent, scheduled the Durable Activities,
ran the sync handlers, awaited the async handler, and serialized the results.

Provider configuration matches ``test_samples_agentic.py``: CI pipeline variables
are copied into the sample's gitignored ``local.settings.json``. The module skips
when no provider is configured.
"""

from __future__ import annotations

import json
import shutil
import time
import urllib.error
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

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_APP = REPO_ROOT / "samples" / "workflow-incident-triage" / "src"
AGENT_SLUG = "main"
SERVICE = "orders-api"
_TERMINAL_STATUSES = {"Completed", "Failed", "Canceled", "Terminated"}
_PROMPT = (
    f"We're seeing latency spikes and intermittent 502s on the `{SERVICE}` service "
    "for the last 20 minutes. Start a workflow now that pulls recent logs, metrics, "
    "and the deploy history in parallel, then summarizes what you find. "
    "Do not add a wait task."
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]


@pytest.fixture(scope="module")
def triage_host() -> Iterator[HostHandle]:
    """Boot the sample with the resolved provider on a dedicated task hub."""
    overlay_provider_settings(SAMPLE_APP)
    if configured_provider(SAMPLE_APP) is None:
        pytest.skip(
            "no LLM provider configured — set FOUNDRY_PROJECT_ENDPOINT / FOUNDRY_MODEL "
            "(or another provider) to run the workflow agentic E2E test"
        )
    # A dedicated task hub keeps other E2E hosts' instances and leases out of this run.
    env = {"AzureFunctionsJobHost__extensions__durableTask__hubName": "WorkflowToolsE2E"}
    with running_host(SAMPLE_APP, env=env) as handle:
        wait_until_responsive(handle.base_url)
        yield handle


def _session_workflows(base_url: str, session_id: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        f"{base_url}/agents/{AGENT_SLUG}/workflows",
        headers={"x-ms-session-id": session_id},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        workflows = json.loads(response.read().decode())["workflows"]
    assert isinstance(workflows, list)
    return workflows


def _await_terminal_workflow(
    host: HostHandle, session_id: str, *, timeout: float = 240.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    workflows: list[dict[str, Any]] = []
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            workflows = _session_workflows(host.base_url, session_id)
        except (TimeoutError, urllib.error.URLError) as exc:
            # One slow or dropped status request must not fail the run.
            last_error = exc
        else:
            if workflows and all(w.get("runtime_status") in _TERMINAL_STATUSES for w in workflows):
                return workflows[0]
        time.sleep(2)
    raise AssertionError(
        f"session workflows never reached a terminal state: {workflows}; "
        f"last request error: {last_error!r}\n--- func output ---\n{host.read_output()}"
    )


def _results_with(results: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    """Return task results that have all ``keys``; the agent chooses the task ids."""
    return [
        value
        for value in results.values()
        if isinstance(value, dict) and all(key in value for key in keys)
    ]


def test_agent_workflow_runs_sync_and_async_tools(triage_host: HostHandle) -> None:
    session_id = f"e2e-{uuid.uuid4().hex}"

    reply = chat(triage_host.base_url, AGENT_SLUG, _PROMPT, session_id=session_id, timeout=180)
    assert reply.status == 200, f"chat request failed: {reply.status} {reply.body}"

    workflow = _await_terminal_workflow(triage_host, session_id)
    host_output = triage_host.read_output()
    assert workflow["runtime_status"] == "Completed", (
        f"{workflow}\n--- agent reply ---\n{reply.response_text}"
        f"\n--- func output ---\n{host_output}"
    )

    results = workflow["output"]["results"]
    deploys = _results_with(results, "deploys", "lookback_hours")
    assert deploys, f"the workflow did not run the async fetch_deploys tool: {results}"
    assert deploys[0]["service"] == SERVICE
    assert len(deploys[0]["deploys"]) == 2

    logs = _results_with(results, "lines", "errors", "warnings")
    assert logs, f"the workflow did not run the sync fetch_logs tool: {results}"
    assert logs[0]["service"] == SERVICE

    metrics = _results_with(results, "cpu_p99", "latency_p99_ms")
    assert metrics, f"the workflow did not run the sync fetch_metrics tool: {results}"
    assert metrics[0]["service"] == SERVICE
