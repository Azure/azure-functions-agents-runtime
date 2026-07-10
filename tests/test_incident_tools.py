"""Unit tests for the incident-triage sample's workflow-safe tools.

Lightweight contract checks — the heavier validation is the workflow
schema/registry test suite. These guard the result-shape contract
documented in the sample README and the embedded-template-ref guard
in ``summarize_findings``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from azure_functions_agents.discovery.tools import (
    clear_tool_discovery_cache,
    discover_project_tools,
)

_SAMPLE_SRC = Path(__file__).resolve().parents[1] / "samples" / "workflow-incident-triage" / "src"
_SPEC = importlib.util.spec_from_file_location(
    "incident_tools_sample",
    _SAMPLE_SRC / "tools" / "incident_tools.py",
)
assert _SPEC is not None and _SPEC.loader is not None

incident_tools = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(incident_tools)


def test_fetch_logs_shape():
    out = incident_tools.fetch_logs({"service": "orders-api"})
    assert out["service"] == "orders-api"
    assert isinstance(out["lines"], list) and out["lines"]
    assert isinstance(out["errors"], int)
    assert isinstance(out["warnings"], int)


def test_sample_import_does_not_leave_source_folder_on_sys_path():
    assert str(_SAMPLE_SRC) not in sys.path


def test_fetch_metrics_shape():
    out = incident_tools.fetch_metrics({"service": "orders-api", "window_minutes": 15})
    assert out["window_minutes"] == 15
    assert out["saturation"] in ("moderate", "high")
    assert isinstance(out["cpu_p99"], float)


def test_fetch_deploys_shape():
    out = incident_tools.fetch_deploys({"service": "orders-api"})
    assert out["service"] == "orders-api"
    assert len(out["deploys"]) >= 1
    assert {"id", "actor", "summary", "minutes_ago"} <= set(out["deploys"][0])


def test_fetch_logs_requires_service():
    with pytest.raises(ValueError, match="service"):
        incident_tools.fetch_logs({})


def test_summarize_findings_with_full_results():
    logs = incident_tools.fetch_logs({"service": "orders-api"})
    metrics = incident_tools.fetch_metrics({"service": "orders-api"})
    deploys = incident_tools.fetch_deploys({"service": "orders-api"})
    out = incident_tools.summarize_findings(
        {"logs": logs, "metrics": metrics, "deploys": deploys}
    )
    assert out["service"] == "orders-api"
    assert out["confidence"] in ("low", "medium", "high")
    assert isinstance(out["evidence"], list)


def test_summarize_findings_rejects_embedded_template_ref():
    """If the LLM emits ``"foo: ${fetch_logs.result}"`` the substitutor
    will JSON-stringify the dict; the handler must reject this loudly
    rather than silently returning empty evidence.
    """
    with pytest.raises(ValueError, match="whole upstream result"):
        incident_tools.summarize_findings(
            {
                "logs": '{"service": "orders-api"}',  # str, not dict
                "metrics": {},
                "deploys": {},
            }
        )


def test_sample_workflow_tools_are_auto_discovered():
    clear_tool_discovery_cache()
    discovered = discover_project_tools(_SAMPLE_SRC)
    assert discovered.user_tools == []
    assert {tool.name for tool in discovered.workflow_tools} == {
        "fetch_logs",
        "fetch_metrics",
        "fetch_deploys",
        "summarize_findings",
    }
