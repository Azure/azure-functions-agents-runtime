from __future__ import annotations

from typing import Any

from _benchmark_core import build_service_evidence

from azure_functions_agents import tool, workflow_tool


@tool
@workflow_tool(
    description=(
        "Return deterministic logs, metrics, and deployment evidence for one service. "
        "Args: {trial_id: str, service: str, evidence_lines: int}. The result must "
        "be passed whole and unchanged to publish_benchmark_report."
    )
)
def inspect_service_evidence(args: dict[str, Any]) -> dict[str, Any]:
    trial_id = args.get("trial_id")
    service = args.get("service")
    evidence_lines = args.get("evidence_lines")
    if not isinstance(trial_id, str) or not isinstance(service, str):
        raise ValueError("trial_id and service must be strings")
    if not isinstance(evidence_lines, int) or isinstance(evidence_lines, bool):
        raise ValueError("evidence_lines must be an integer")
    return build_service_evidence(trial_id, service, evidence_lines)
