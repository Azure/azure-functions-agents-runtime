"""Run deterministic release-review scenarios against a local Functions host."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from typing import Any
from uuid import uuid4

from azure_functions_agents.workflows.schema import (
    plan_to_activity_inputs,
    resolve_workflow_task_execution,
    validate_plan,
)

SCENARIOS = ("parallel", "collection", "failure", "cancel", "continuation")
TOOLS = frozenset({"list_release_checks", "check_release", "summarize_release"})


def scenario_plan(scenario: str) -> dict[str, Any]:
    """Build the release review plan for one scenario."""
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {scenario}")
    if scenario == "collection":
        return {
            "tasks": [
                {"id": "source", "tool": "list_release_checks", "args": {}},
                {
                    "id": "inspect",
                    "tool": "check_release",
                    "depends_on": ["source"],
                    "for_each": "${source.result.checks}",
                    "args": {"check": "${item.name}", "delay_seconds": "${item.delay_seconds}"},
                },
                {
                    "id": "report",
                    "tool": "summarize_release",
                    "depends_on": ["inspect"],
                    "args": {"evidence": "${inspect.result}"},
                },
            ]
        }
    tasks = [
        {
            "id": check,
            "tool": "check_release",
            "args": {"check": check, "delay_seconds": delay},
        }
        for check, delay in (("tests", 6), ("security", 2), ("deployment", 4))
    ]
    if scenario in {"failure", "continuation"}:
        tasks[1]["args"]["fail"] = True
    if scenario == "continuation":
        tasks[1]["execution"] = {"continue_on_error": True}
    tasks.append(
        {
            "id": "report",
            "tool": "summarize_release",
            "depends_on": ["tests", "security", "deployment"],
            "args": {
                "evidence": [
                    "${tests.result}",
                    "${security.result}",
                    "${deployment.result}",
                ]
            },
        }
    )
    if scenario == "cancel":
        tasks[0]["args"]["delay_seconds"] = 20
        tasks.append({"id": "pause", "type": "wait", "duration": "PT30S"})
    return {"tasks": tasks}


def workflow_payload(scenario: str) -> dict[str, Any]:
    """Use production plan validation and execution-policy translation."""
    plan = validate_plan(scenario_plan(scenario), allowed_tools=TOOLS)
    effective = {}
    for task in plan.tasks:
        execution = resolve_workflow_task_execution(task)
        if execution is not None:
            effective[task.id] = execution
    return {
        "tasks": plan_to_activity_inputs(plan, effective),
        "workflow_agent_slug": "main",
        "policy": {"allowed_tools": sorted(TOOLS), "allowed_subagents": []},
    }


def request_json(url: str, payload: Any = None, *, method: str = "GET") -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if method == "POST" else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
        return json.loads(data) if data else None


def run_scenario(base_url: str, scenario: str) -> dict[str, Any]:
    """Submit one plan and assert its terminal result."""
    workflow_id = f"fanout-{scenario}-{uuid4().hex}"
    root = f"{base_url.rstrip('/')}/runtime/webhooks/durabletask"
    instance = f"{root}/instances/{workflow_id}"
    request_json(
        f"{root}/orchestrators/agents_workflow_orchestrator/{workflow_id}",
        workflow_payload(scenario),
        method="POST",
    )
    print(f"{scenario}: {workflow_id}", flush=True)
    deadline = time.monotonic() + 120
    started = time.monotonic()
    canceled = False
    while time.monotonic() < deadline:
        status = request_json(instance)
        if scenario == "cancel" and not canceled and time.monotonic() - started >= 8:
            request_json(f"{instance}/raiseEvent/cancel", "operator stopped review", method="POST")
            canceled = True
        if status["runtimeStatus"] in {"Completed", "Failed", "Terminated"}:
            break
        time.sleep(0.5)
    else:
        raise AssertionError(f"{workflow_id} did not finish: {status}")
    expected = "Failed" if scenario == "failure" else "Completed"
    assert status["runtimeStatus"] == expected, status
    output = status.get("output")
    assert output is not None or expected == "Failed", (
        "The provider did not return workflow output. Inspect the host and DTS history.",
        status,
    )
    if expected == "Completed":
        if scenario == "cancel":
            assert output["canceled"] is True, output
            assert output["results"] == {}, output
        else:
            report = output["results"]["report"]
            assert report["marker"] == "RELEASE_REVIEW_READY", report
            evidence = report["evidence"]
            if scenario == "collection":
                assert [item["index"] for item in evidence] == [0, 1, 2], evidence
                assert all(item["status"] == "completed" for item in evidence), evidence
                evidence = [item["result"] for item in evidence]
            if scenario == "continuation":
                assert evidence[1]["error_code"] == "release_check_failed", evidence
            else:
                assert [item["check"] for item in evidence] == ["tests", "security", "deployment"]
    return {"scenario": scenario, "workflow_id": workflow_id, "status": status}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:7071")
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    args = parser.parse_args()
    for scenario in SCENARIOS if args.scenario == "all" else (args.scenario,):
        result = run_scenario(args.base_url, scenario)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
