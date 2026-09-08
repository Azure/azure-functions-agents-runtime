from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import azure.durable_functions as df
import pytest

from azure_functions_agents.app import create_function_app
from azure_functions_agents.config.loader import load_agent_specs
from azure_functions_agents.discovery.tools import (
    clear_tool_discovery_cache,
    discover_project_tools,
)

SAMPLE_ROOT = Path(__file__).resolve().parents[1] / "samples" / "per-agent-workflows"
SAMPLE_SRC = SAMPLE_ROOT / "src"

INCIDENT_ONLY = {
    "get_incident_logs",
    "get_incident_metrics",
    "get_incident_deployments",
    "compile_incident_report",
}
RELEASE_ONLY = {
    "get_release_pull_requests",
    "get_release_test_results",
    "get_release_vulnerabilities",
    "get_release_change_window",
    "compile_release_dossier",
}


def _workflow_tools() -> dict[str, Any]:
    clear_tool_discovery_cache()
    return {
        tool.name: tool.handler
        for tool in discover_project_tools(SAMPLE_SRC).workflow_tools
    }


def test_sample_has_two_non_main_workflow_agents_with_distinct_policies() -> None:
    specs = load_agent_specs(SAMPLE_SRC, strict=True)
    by_slug = {Path(spec.source_file).name.removesuffix(".agent.md"): spec for spec in specs}

    assert not any(spec.is_main for spec in specs)
    assert set(by_slug) == {
        "incident_commander",
        "release_manager",
        "incident_evidence_analyst",
        "release_risk_reviewer",
    }

    incident = by_slug["incident_commander"]
    release = by_slug["release_manager"]
    assert incident.builtin_endpoints is not None
    assert incident.builtin_endpoints.debug_chat_ui is True
    assert incident.builtin_endpoints.chat_api is True
    assert release.builtin_endpoints is not None
    assert release.builtin_endpoints.debug_chat_ui is True
    assert release.builtin_endpoints.chat_api is True

    assert incident.workflows is not None
    assert incident.workflows.enabled is True
    assert set(incident.workflows.exclude) == RELEASE_ONLY
    assert [ref.agent for ref in incident.workflows.subagents] == [
        "incident_evidence_analyst"
    ]

    assert release.workflows is not None
    assert release.workflows.enabled is True
    assert set(release.workflows.exclude) == INCIDENT_ONLY
    assert [ref.agent for ref in release.workflows.subagents] == [
        "release_risk_reviewer"
    ]


def test_sample_registers_each_owner_and_one_durable_engine() -> None:
    app = create_function_app(SAMPLE_SRC)
    names = [function.get_function_name() for function in app.get_functions()]

    assert isinstance(app, df.DFApp)
    assert names.count("agents_workflow_orchestrator") == 1
    assert names.count("agents_workflow_run_tool") == 1
    assert names.count("agents_workflow_run_sub_agent") == 1
    for owner in ("incident_commander", "release_manager"):
        assert f"agent_{owner}_builtin_chat" in names
        assert f"agent_{owner}_builtin_workflows" in names
        assert f"agent_{owner}_builtin_workflow_status" in names


def test_incident_fake_tools_are_deterministic_and_build_structured_report() -> None:
    tools = _workflow_tools()
    request = {"incident_id": "INC-4821", "service": "checkout-api"}

    logs = tools["get_incident_logs"](request)
    metrics = tools["get_incident_metrics"](request)
    deployments = tools["get_incident_deployments"](request)
    assert tools["get_incident_logs"](request) == logs
    assert tools["get_incident_metrics"](request) == metrics
    assert tools["get_incident_deployments"](request) == deployments

    report = tools["compile_incident_report"](
        {
            **request,
            "logs": logs,
            "metrics": metrics,
            "deployments": deployments,
            "specialist_analysis": {
                "agent": "incident_evidence_analyst",
                "text": "The deployment and saturation evidence correlate.",
            },
        }
    )
    assert report["marker"] == "INCIDENT_REPORT_READY"
    assert report["incident_id"] == "INC-4821"
    assert report["service"] == "checkout-api"
    assert report["severity"] == "SEV2"
    assert report["decision"] == "ROLLBACK"
    assert report["specialist"]["agent"] == "incident_evidence_analyst"

    for field, value in (
        ("incident_id", "INC-WRONG"),
        ("service", "inventory-api"),
    ):
        wrong_logs = {**logs, field: value}
        with pytest.raises(ValueError, match=r"get_incident_logs.*identity"):
            tools["compile_incident_report"](
                {
                    **request,
                    "logs": wrong_logs,
                    "metrics": metrics,
                    "deployments": deployments,
                    "specialist_analysis": {
                        "agent": "incident_evidence_analyst",
                        "text": "The deployment and saturation evidence correlate.",
                    },
                }
            )


def test_release_fake_tools_are_deterministic_and_build_go_no_go_dossier() -> None:
    tools = _workflow_tools()
    request = {"release_id": "REL-2026.08.11", "service": "checkout-api"}

    pull_requests = tools["get_release_pull_requests"](request)
    tests = tools["get_release_test_results"](request)
    vulnerabilities = tools["get_release_vulnerabilities"](request)
    change_window = tools["get_release_change_window"](request)
    assert tools["get_release_pull_requests"](request) == pull_requests
    assert tools["get_release_test_results"](request) == tests
    assert tools["get_release_vulnerabilities"](request) == vulnerabilities
    assert tools["get_release_change_window"](request) == change_window

    dossier = tools["compile_release_dossier"](
        {
            **request,
            "pull_requests": pull_requests,
            "tests": tests,
            "vulnerabilities": vulnerabilities,
            "change_window": change_window,
            "specialist_analysis": {
                "agent": "release_risk_reviewer",
                "text": "The open critical vulnerability is a release blocker.",
            },
        }
    )
    assert dossier["marker"] == "RELEASE_DOSSIER_READY"
    assert dossier["release_id"] == "REL-2026.08.11"
    assert dossier["service"] == "checkout-api"
    assert dossier["decision"] == "NO_GO"
    assert dossier["specialist"]["agent"] == "release_risk_reviewer"

    for field, value in (
        ("release_id", "REL-WRONG"),
        ("service", "inventory-api"),
    ):
        wrong_tests = {**tests, field: value}
        with pytest.raises(ValueError, match=r"get_release_test_results.*identity"):
            tools["compile_release_dossier"](
                {
                    **request,
                    "pull_requests": pull_requests,
                    "tests": wrong_tests,
                    "vulnerabilities": vulnerabilities,
                    "change_window": change_window,
                    "specialist_analysis": {
                        "agent": "release_risk_reviewer",
                        "text": "The open critical vulnerability is a release blocker.",
                    },
                }
            )


def test_sample_runtime_files_and_readme_are_complete() -> None:
    for name in (
        ".funcignore",
        "function_app.py",
        "host.json",
        "host.dts.json",
        "local.settings.template.json",
        "requirements.txt",
    ):
        assert (SAMPLE_SRC / name).is_file()
    assert not (SAMPLE_SRC / "main.agent.md").exists()
    readme = (SAMPLE_ROOT / "README.md").read_text(encoding="utf-8")
    for required in (
        "Engineering Operations Hub",
        "Architecture",
        "Incident workflow",
        "Release workflow",
        "INCIDENT_REPORT_READY",
        "RELEASE_DOSSIER_READY",
        "Troubleshooting",
    ):
        assert required in readme
    assert "Python 3.13+" in readme

    requirements = (SAMPLE_SRC / "requirements.txt").read_text(encoding="utf-8")
    assert requirements.strip() == "-e ../../..[monitor]"

    settings = json.loads(
        (SAMPLE_SRC / "local.settings.template.json").read_text(encoding="utf-8")
    )
    assert settings["Values"]["AzureWebJobsStorage"] == "UseDevelopmentStorage=true"
    assert settings["Values"]["TASKHUB_NAME"] == "engineeringopshub"
