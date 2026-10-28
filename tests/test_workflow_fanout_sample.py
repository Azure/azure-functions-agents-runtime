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
def test_scenario_payloads_select_new_mode_without_changing_legacy_tasks(scenario: str) -> None:
    payload = verifier.workflow_payload(scenario)
    legacy = verifier.workflow_payload(scenario, legacy=True)
    assert payload.pop("wave_wait_mode") == "all"
    assert payload == legacy
    assert payload["workflow_agent_slug"] == "main"


def test_sample_discovers_only_the_scenario_tools() -> None:
    clear_tool_discovery_cache()
    discovered = discover_project_tools(SAMPLE_ROOT / "src")
    assert {tool.name for tool in discovered.workflow_tools} == verifier.TOOLS


def test_unknown_scenario_fails_explicitly() -> None:
    with pytest.raises(ValueError, match="Unknown scenario"):
        verifier.scenario_plan("unknown")
