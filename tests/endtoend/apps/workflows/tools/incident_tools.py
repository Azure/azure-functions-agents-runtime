"""Deterministic workflow-safe tools for the workflows E2E app.

Each handler is synchronous, takes a single ``args`` dict, and returns a
JSON-serializable dict. ``@workflow_tool`` opts them into Dynamic Workflow
activity execution. Outputs are deterministic so Durable replays are stable.
"""

from typing import Any

from azure_functions_agents import workflow_tool


@workflow_tool(
    description=(
        "Fetch recent log evidence for a service. "
        "Args: {service: str}. Returns {service, lines: [str], errors: int}."
    )
)
def fetch_logs(args: dict[str, Any]) -> dict[str, Any]:
    """Return synthetic log evidence for a service."""
    service = str(args.get("service", "unknown"))
    return {"service": service, "lines": [f"{service}: request handled"], "errors": 0}


@workflow_tool(
    description=(
        "Fetch recent metrics for a service. "
        "Args: {service: str}. Returns {service, cpu_p99: float, latency_p99_ms: float}."
    )
)
def fetch_metrics(args: dict[str, Any]) -> dict[str, Any]:
    """Return synthetic metric evidence for a service."""
    service = str(args.get("service", "unknown"))
    return {"service": service, "cpu_p99": 42.0, "latency_p99_ms": 120.0}


@workflow_tool(
    description=(
        "Summarize findings from upstream evidence. "
        "Args: {logs: dict, metrics: dict}. Returns {likely_cause: str, confidence: str}."
    )
)
def summarize_findings(args: dict[str, Any]) -> dict[str, Any]:
    """Combine upstream log and metric evidence into a short conclusion."""
    logs = args.get("logs", {})
    errors = int(logs.get("errors", 0)) if isinstance(logs, dict) else 0
    likely_cause = "elevated error rate" if errors else "no clear signal"
    return {"likely_cause": likely_cause, "confidence": "low"}
