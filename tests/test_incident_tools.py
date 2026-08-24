"""Unit tests for the incident-triage sample's workflow-safe tools.

Lightweight contract checks — the heavier validation is the workflow
schema/registry test suite. These guard the result-shape contract
documented in the sample README and the embedded-template-ref guard
in ``summarize_findings``.
"""

from __future__ import annotations

import importlib.util
import json
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


@pytest.mark.parametrize(
    "template_name",
    ["local.settings.template.json", "local.settings.dts.json.template"],
)
def test_sample_settings_templates_use_current_foundry_contract(
    template_name: str,
) -> None:
    values = json.loads((_SAMPLE_SRC / template_name).read_text(encoding="utf-8"))[
        "Values"
    ]

    assert values["AZURE_FUNCTIONS_AGENTS_PROVIDER"] == "foundry"
    assert values["FOUNDRY_PROJECT_ENDPOINT"] == ""
    assert values["FOUNDRY_MODEL"]
    assert values["APPLICATIONINSIGHTS_CONNECTION_STRING"] == ""
    assert "GITHUB_TOKEN" not in values


def test_sample_installs_runtime_monitor_extra() -> None:
    requirements = (_SAMPLE_SRC / "requirements.txt").read_text(encoding="utf-8").splitlines()

    assert requirements == ["-e ../../..[monitor]"]


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
        "discover_services",
        "inspect_service",
        "summarize_scan",
    }


def test_discover_services_is_deterministic_and_bounded():
    first = incident_tools.discover_services({"incident": "latency on orders-api"})
    second = incident_tools.discover_services({"incident": "latency on orders-api"})
    assert first == second  # deterministic function of the incident text
    services = first["services"]
    assert first["count"] == len(services)
    # Bounded fan-out stays well under the workflow max_nodes budget.
    assert 3 <= len(services) <= 5
    names = [s["name"] for s in services]
    assert len(names) == len(set(names))  # no duplicate instances
    assert all({"name", "tier", "in_scope"} <= set(s) for s in services)


def test_discover_services_always_marks_at_least_one_item_for_skip():
    # Across a range of incidents the discovery result must always include a
    # skip candidate (in_scope=false) so the for_each `when` demo has an item
    # to skip regardless of the bounded slice size.
    for incident in ("orders-api p99 spike", "checkout 502s", "queue backlog", "x"):
        services = incident_tools.discover_services({"incident": incident})["services"]
        skipped = [s for s in services if not s["in_scope"]]
        assert skipped, incident
        assert all(s["tier"] == "low" for s in skipped)


def test_discover_services_requires_incident():
    with pytest.raises(ValueError, match="incident"):
        incident_tools.discover_services({})


def test_inspect_service_shape_and_determinism():
    first = incident_tools.inspect_service({"service": "orders-api", "index": 2})
    second = incident_tools.inspect_service({"service": "orders-api", "index": 2})
    assert first == second
    assert first["service"] == "orders-api"
    assert first["index"] == 2
    assert isinstance(first["healthy"], bool)
    assert first["saturation"] in ("moderate", "high")
    assert first["service"] in first["headline"]


def test_summarize_scan_consumes_ordered_aggregate_with_skips():
    # Shape mirrors the ordered {index, status, result} aggregate a logical
    # for_each node exposes: skipped positions carry result=null.
    aggregate = [
        {
            "index": 0,
            "status": "completed",
            "result": incident_tools.inspect_service({"service": "orders-api"}),
        },
        {"index": 1, "status": "skipped", "result": None},
        {
            "index": 2,
            "status": "completed",
            "result": {"service": "payments-api", "healthy": False, "headline": "payments-api: down"},
        },
    ]
    out = incident_tools.summarize_scan(
        {"incident": "orders latency", "findings": aggregate}
    )
    assert out["scanned"] == 2
    assert out["skipped"] == 1
    assert "payments-api: down" in out["unhealthy"]
    assert out["incident"] == "orders latency"


def test_summarize_scan_rejects_non_aggregate_findings():
    # If the LLM passes an embedded template ref, the substitutor stringifies
    # the array; the handler must reject that loudly, not silently degrade.
    with pytest.raises(ValueError, match="whole for_each aggregate"):
        incident_tools.summarize_scan({"findings": '[{"index": 0}]'})


def test_summarize_scan_rejects_malformed_envelope():
    with pytest.raises(ValueError, match="envelope"):
        incident_tools.summarize_scan({"findings": ["not-a-dict"]})
