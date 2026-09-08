"""Deterministic release-readiness evidence for the Engineering Operations Hub."""

from __future__ import annotations

from typing import Any

from azure_functions_agents import workflow_tool


def _required(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _identity(args: dict[str, Any]) -> tuple[str, str]:
    return _required(args, "release_id"), _required(args, "service")


@workflow_tool(
    description=(
        "Return deterministic pull-request evidence. Args: "
        "{release_id: str, service: str}. Returns approvals and merge state."
    )
)
def get_release_pull_requests(args: dict[str, Any]) -> dict[str, Any]:
    release_id, service = _identity(args)
    return {
        "capability": "get_release_pull_requests",
        "release_id": release_id,
        "service": service,
        "pull_requests": [
            {"number": 1842, "title": "Checkout timeout policy", "approvals": 2, "merged": True},
            {"number": 1851, "title": "Payment retry telemetry", "approvals": 2, "merged": True},
        ],
        "unresolved_threads": 0,
    }


@workflow_tool(
    description=(
        "Return deterministic test evidence. Args: {release_id: str, service: str}. "
        "Returns suite totals, failures, and required-check status."
    )
)
def get_release_test_results(args: dict[str, Any]) -> dict[str, Any]:
    release_id, service = _identity(args)
    return {
        "capability": "get_release_test_results",
        "release_id": release_id,
        "service": service,
        "passed": 1284,
        "failed": 0,
        "skipped": 7,
        "required_checks": "passed",
        "performance_regression_percent": 0.8,
    }


@workflow_tool(
    description=(
        "Return deterministic vulnerability evidence. Args: "
        "{release_id: str, service: str}. Returns scanner findings and policy status."
    )
)
def get_release_vulnerabilities(args: dict[str, Any]) -> dict[str, Any]:
    release_id, service = _identity(args)
    return {
        "capability": "get_release_vulnerabilities",
        "release_id": release_id,
        "service": service,
        "findings": [
            {
                "id": "CVE-2026-41017",
                "severity": "critical",
                "component": "contoso-auth 3.7.1",
                "fix_version": "3.7.3",
                "exception": None,
            }
        ],
        "policy_status": "blocked",
    }


@workflow_tool(
    description=(
        "Return deterministic change-window evidence. Args: "
        "{release_id: str, service: str}. Returns window and staffing readiness."
    )
)
def get_release_change_window(args: dict[str, Any]) -> dict[str, Any]:
    release_id, service = _identity(args)
    return {
        "capability": "get_release_change_window",
        "release_id": release_id,
        "service": service,
        "window_start": "2026-08-11T02:00:00Z",
        "window_end": "2026-08-11T04:00:00Z",
        "within_freeze": False,
        "primary_oncall_confirmed": True,
        "rollback_owner_confirmed": True,
    }


@workflow_tool(
    description=(
        "Compile the terminal go/no-go dossier. Args: {release_id, service, "
        "pull_requests: <whole get_release_pull_requests result>, tests: <whole "
        "get_release_test_results result>, vulnerabilities: <whole "
        "get_release_vulnerabilities result>, change_window: <whole "
        "get_release_change_window result>, specialist_analysis: <whole "
        "release_risk_reviewer result>}. Returns RELEASE_DOSSIER_READY."
    )
)
def compile_release_dossier(args: dict[str, Any]) -> dict[str, Any]:
    release_id, service = _identity(args)
    expected = (
        (args.get("pull_requests"), "get_release_pull_requests"),
        (args.get("tests"), "get_release_test_results"),
        (args.get("vulnerabilities"), "get_release_vulnerabilities"),
        (args.get("change_window"), "get_release_change_window"),
    )
    for evidence, capability in expected:
        if not isinstance(evidence, dict) or evidence.get("capability") != capability:
            raise ValueError(f"{capability} must be supplied as a whole result")
        if (
            evidence.get("release_id") != release_id
            or evidence.get("service") != service
        ):
            raise ValueError(f"{capability} evidence identity does not match the dossier")
    specialist = args.get("specialist_analysis")
    if (
        not isinstance(specialist, dict)
        or specialist.get("agent") != "release_risk_reviewer"
        or not isinstance(specialist.get("text"), str)
    ):
        raise ValueError("specialist_analysis must be the release risk reviewer result")

    return {
        "marker": "RELEASE_DOSSIER_READY",
        "report_type": "release_readiness",
        "capability": "compile_release_dossier",
        "release_id": release_id,
        "service": service,
        "decision": "NO_GO",
        "blocking_findings": [
            "critical CVE-2026-41017 has no approved exception",
            "contoso-auth must be upgraded from 3.7.1 to 3.7.3",
        ],
        "passed_gates": [
            "all required pull requests merged with approvals",
            "all required tests passed",
            "change window and rollback staffing confirmed",
        ],
        "required_actions": [
            "upgrade contoso-auth to 3.7.3",
            "rerun vulnerability and regression suites",
            "request a new go/no-go review",
        ],
        "specialist": specialist,
    }


__all__ = [
    "compile_release_dossier",
    "get_release_change_window",
    "get_release_pull_requests",
    "get_release_test_results",
    "get_release_vulnerabilities",
]
