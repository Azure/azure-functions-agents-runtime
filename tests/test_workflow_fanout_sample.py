"""Contracts for the release evidence review scenarios."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from azure_functions_agents.discovery.tools import (
    clear_tool_discovery_cache,
    discover_project_tools,
)

SAMPLE_ROOT = Path(__file__).resolve().parents[1] / "samples" / "workflow-fanout-review"
_SPEC = importlib.util.spec_from_file_location(
    "workflow_fanout_sample_verify",
    SAMPLE_ROOT / "scripts" / "verify.py",
)
assert _SPEC is not None and _SPEC.loader is not None
verifier = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verifier)


@pytest.mark.parametrize("scenario", verifier.SCENARIOS)
def test_scenario_payloads_do_not_select_a_wait_mode(scenario: str) -> None:
    payload = verifier.workflow_payload(scenario)
    assert "wave_wait_mode" not in payload
    assert payload["workflow_agent_slug"] == "main"


def test_cancellation_plan_keeps_a_slow_task_and_timer_without_a_failure() -> None:
    tasks = verifier.scenario_plan("cancel")["tasks"]
    assert not any(task.get("args", {}).get("fail") for task in tasks)
    assert next(task for task in tasks if task["id"] == "tests")["args"]["delay_seconds"] == 20
    assert next(task for task in tasks if task["id"] == "pause")["duration"] == "PT30S"


def test_sample_discovers_only_the_scenario_tools() -> None:
    clear_tool_discovery_cache()
    discovered = discover_project_tools(SAMPLE_ROOT / "src")
    assert {tool.name for tool in discovered.workflow_tools} == verifier.TOOLS


def test_unknown_scenario_fails_explicitly() -> None:
    with pytest.raises(ValueError, match="Unknown scenario"):
        verifier.scenario_plan("unknown")
