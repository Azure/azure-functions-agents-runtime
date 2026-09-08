"""Deterministic incident evidence for the Engineering Operations Hub."""

from __future__ import annotations

from typing import Any

from azure_functions_agents import workflow_tool


def _required(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@workflow_tool(
    description=(
        "Return deterministic production log evidence. Args: "
        "{incident_id: str, service: str}. Returns an incident-scoped evidence record."
    )
)
def get_incident_logs(args: dict[str, Any]) -> dict[str, Any]:
    incident_id = _required(args, "incident_id")
    service = _required(args, "service")
    return {
        "capability": "get_incident_logs",
        "incident_id": incident_id,
        "service": service,
        "window": "2026-08-10T18:35:00Z/2026-08-10T19:05:00Z",
        "error_count": 184,
        "signature": "upstream checkout timeout after connection pool exhaustion",
        "sample": [
            "18:42:11Z ERROR checkout request timed out after 3000ms",
            "18:42:11Z WARN sql connection pool at 100% (64/64)",
            "18:42:12Z ERROR retry budget exhausted for payment authorization",
        ],
    }


@workflow_tool(
    description=(
        "Return deterministic service metrics. Args: {incident_id: str, service: str}. "
        "Returns latency, errors, saturation, and the comparison baseline."
    )
)
def get_incident_metrics(args: dict[str, Any]) -> dict[str, Any]:
    incident_id = _required(args, "incident_id")
    service = _required(args, "service")
    return {
        "capability": "get_incident_metrics",
        "incident_id": incident_id,
        "service": service,
        "latency_p99_ms": 3280,
        "error_rate_percent": 12.7,
        "connection_pool_percent": 100,
        "baseline_latency_p99_ms": 410,
        "slo_breached": True,
    }


@workflow_tool(
    description=(
        "Return deterministic deployment evidence. Args: "
        "{incident_id: str, service: str}. Returns recent revisions and timing."
    )
)
def get_incident_deployments(args: dict[str, Any]) -> dict[str, Any]:
    incident_id = _required(args, "incident_id")
    service = _required(args, "service")
    return {
        "capability": "get_incident_deployments",
        "incident_id": incident_id,
        "service": service,
        "deployments": [
            {
                "revision": "checkout-api-2026.08.10.4",
                "deployed_at": "2026-08-10T18:37:00Z",
                "change": "lower SQL command timeout and raise checkout concurrency",
                "actor": "release-pipeline",
            },
            {
                "revision": "checkout-api-2026.08.09.2",
                "deployed_at": "2026-08-09T16:10:00Z",
                "change": "payment retry jitter",
                "actor": "release-pipeline",
            },
        ],
    }


@workflow_tool(
    description=(
        "Compile the terminal structured incident report. Args: {incident_id, service, "
        "logs: <whole get_incident_logs result>, metrics: <whole get_incident_metrics "
        "result>, deployments: <whole get_incident_deployments result>, "
        "specialist_analysis: <whole incident_evidence_analyst result>}. "
        "Returns the INCIDENT_REPORT_READY report."
    )
)
def compile_incident_report(args: dict[str, Any]) -> dict[str, Any]:
    incident_id = _required(args, "incident_id")
    service = _required(args, "service")
    logs = args.get("logs")
    metrics = args.get("metrics")
    deployments = args.get("deployments")
    specialist = args.get("specialist_analysis")
    expected = (
        (logs, "get_incident_logs"),
        (metrics, "get_incident_metrics"),
        (deployments, "get_incident_deployments"),
    )
    for evidence, capability in expected:
        if not isinstance(evidence, dict) or evidence.get("capability") != capability:
            raise ValueError(f"{capability} must be supplied as a whole result")
        if (
            evidence.get("incident_id") != incident_id
            or evidence.get("service") != service
        ):
            raise ValueError(f"{capability} evidence identity does not match the report")
    if (
        not isinstance(specialist, dict)
        or specialist.get("agent") != "incident_evidence_analyst"
        or not isinstance(specialist.get("text"), str)
    ):
        raise ValueError("specialist_analysis must be the incident evidence analyst result")

    return {
        "marker": "INCIDENT_REPORT_READY",
        "report_type": "incident",
        "capability": "compile_incident_report",
        "incident_id": incident_id,
        "service": service,
        "severity": "SEV2",
        "decision": "ROLLBACK",
        "likely_cause": (
            "revision checkout-api-2026.08.10.4 increased concurrency while lowering "
            "timeouts, exhausting the SQL connection pool"
        ),
        "evidence": [
            f"{logs['error_count']} matching errors in the incident window",
            (
                f"p99 latency {metrics['latency_p99_ms']}ms versus "
                f"{metrics['baseline_latency_p99_ms']}ms baseline"
            ),
            "symptoms began five minutes after checkout-api-2026.08.10.4",
        ],
        "recommended_actions": [
            "roll back checkout-api-2026.08.10.4",
            "cap checkout concurrency at the previous value",
            "verify p99 latency and error rate for 15 minutes",
        ],
        "specialist": specialist,
    }


__all__ = [
    "compile_incident_report",
    "get_incident_deployments",
    "get_incident_logs",
    "get_incident_metrics",
]
