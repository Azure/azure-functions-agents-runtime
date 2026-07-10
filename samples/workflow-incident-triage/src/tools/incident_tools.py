"""Workflow-safe evidence tools for the incident-triage sample.

These are the tools the LLM gets to compose into a workflow when the
agent decides an incident warrants more than a single chat turn of
work. They are intentionally **not** used outside workflow plans:
``@workflow_tool`` opts them into Dynamic Workflow Activity execution,
and no plain public normal tool is exported from this module.

Design notes:

- Each handler is synchronous, takes a single ``args`` dict, and
  returns a JSON-serializable dict. This is the contract enforced by
  ``register_workflow_tool``; async handlers are rejected.
- Outputs are deterministic functions of their inputs so workflow
  replays produce stable results and so the demo narrative is
  reproducible. (Durable journals activity output, so this isn't a
  correctness requirement — it is a stakeholder-demo requirement.)
- Result shapes are deliberately *shallow* and *documented*: the
  summarize tool consumes whole upstream results via ``${id.result}``
  (not ``${id.result.path.to.deep.field}``) so the LLM doesn't have to
  guess nested keys when authoring its plan.

Result shapes (stable contract):

- ``fetch_logs`` →   ``{"service": str, "window_minutes": int,
                       "lines": [str], "errors": int, "warnings": int}``
- ``fetch_metrics`` →``{"service": str, "window_minutes": int,
                       "cpu_p99": float, "memory_p99": float,
                       "latency_p99_ms": float, "saturation": str}``
- ``fetch_deploys`` →``{"service": str, "lookback_hours": int,
                       "deploys": [{"id": str, "actor": str,
                                    "summary": str, "minutes_ago": int}]}``
- ``summarize_findings`` → ``{"service": str, "likely_cause": str,
                              "confidence": "low"|"medium"|"high",
                              "evidence": [str],
                              "recommended_action": str}``
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from azure_functions_agents import workflow_tool


def _seeded_int(seed: str, lo: int, hi: int) -> int:
    """Return a stable int in [lo, hi] derived from ``seed``.

    Used to make synthetic evidence interesting without being random
    across replays.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    span = hi - lo + 1
    return lo + int.from_bytes(digest[:4], "big") % span


def _seeded_float(seed: str, lo: float, hi: float) -> float:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    raw = int.from_bytes(digest[:6], "big") / float(1 << 48)
    return round(lo + raw * (hi - lo), 2)


def _require_service(args: Dict[str, Any], tool: str) -> str:
    service = args.get("service")
    if not isinstance(service, str) or not service:
        raise ValueError(f"{tool}: 'service' arg (string) is required")
    return service


@workflow_tool(
    description=(
        "Fetch recent log lines for a service. Args: "
        "{service: str, window_minutes?: int = 30}. "
        "Returns {service, window_minutes, lines: [str], errors: int, warnings: int}."
    )
)
def fetch_logs(args: Dict[str, Any]) -> Dict[str, Any]:
    service = _require_service(args, "fetch_logs")
    window_minutes = int(args.get("window_minutes") or 30)

    seed = f"{service}:{window_minutes}:logs"
    errors = _seeded_int(seed + ":errors", 2, 18)
    warnings = _seeded_int(seed + ":warnings", 5, 40)

    lines: List[str] = [
        f"[ERROR] {service}: upstream timeout calling payments-api",
        f"[ERROR] {service}: connection pool exhausted (size=32)",
        f"[WARN] {service}: latency above SLO on /orders/checkout",
        f"[INFO] {service}: deployed revision {seed[:8]}",
        f"[ERROR] {service}: 502 from inventory-service after 3 retries",
    ]
    return {
        "service": service,
        "window_minutes": window_minutes,
        "lines": lines,
        "errors": errors,
        "warnings": warnings,
    }


@workflow_tool(
    description=(
        "Fetch p99 CPU, memory, and latency metrics for a service. Args: "
        "{service: str, window_minutes?: int = 30}. "
        "Returns {service, window_minutes, cpu_p99, memory_p99, "
        "latency_p99_ms, saturation: 'moderate'|'high'}."
    )
)
def fetch_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    service = _require_service(args, "fetch_metrics")
    window_minutes = int(args.get("window_minutes") or 30)

    seed = f"{service}:{window_minutes}:metrics"
    cpu_p99 = _seeded_float(seed + ":cpu", 55.0, 96.0)
    memory_p99 = _seeded_float(seed + ":mem", 60.0, 92.0)
    latency_p99_ms = _seeded_float(seed + ":lat", 220.0, 1800.0)
    saturation = "high" if cpu_p99 > 85.0 or memory_p99 > 85.0 else "moderate"

    return {
        "service": service,
        "window_minutes": window_minutes,
        "cpu_p99": cpu_p99,
        "memory_p99": memory_p99,
        "latency_p99_ms": latency_p99_ms,
        "saturation": saturation,
    }


@workflow_tool(
    description=(
        "Fetch recent deploys for a service. Args: "
        "{service: str, lookback_hours?: int = 24}. "
        "Returns {service, lookback_hours, "
        "deploys: [{id, actor, summary, minutes_ago}]}."
    )
)
def fetch_deploys(args: Dict[str, Any]) -> Dict[str, Any]:
    service = _require_service(args, "fetch_deploys")
    lookback_hours = int(args.get("lookback_hours") or 24)

    seed = f"{service}:{lookback_hours}:deploys"
    base_age = _seeded_int(seed + ":age", 8, 90)
    deploys = [
        {
            "id": f"rev-{seed[:6]}",
            "actor": "build-bot",
            "summary": "bump payments-api client to 4.2.1; raise pool size to 32",
            "minutes_ago": base_age,
        },
        {
            "id": f"rev-{seed[6:12] or '000000'}",
            "actor": "release-bot",
            "summary": "config: enable retry-with-jitter for inventory-service",
            "minutes_ago": base_age + 240,
        },
    ]
    return {
        "service": service,
        "lookback_hours": lookback_hours,
        "deploys": deploys,
    }


@workflow_tool(
    description=(
        "Correlate prior fetch results into a structured incident summary. "
        "Args: {logs: <fetch_logs result>, metrics: <fetch_metrics result>, "
        "deploys: <fetch_deploys result>, service?: str}. Pass the whole "
        "upstream result via ${node.result} — do not pre-extract fields. "
        "Returns {service, likely_cause, confidence: 'low'|'medium'|'high', "
        "evidence: [str], recommended_action}."
    )
)
def summarize_findings(args: Dict[str, Any]) -> Dict[str, Any]:
    """Correlate the three fetch results into a structured incident summary.

    Designed to be called with whole upstream results:
    ``args["logs"] = "${fetch_logs.result}"``,
    ``args["metrics"] = "${fetch_metrics.result}"``,
    ``args["deploys"] = "${fetch_deploys.result}"``.

    The template substitutor in :mod:`azure_functions_agents.workflows.schema`
    only preserves native types for **full-string** ``${...}`` references —
    embedding a ref inside a larger string (e.g. ``"logs: ${fetch_logs.result}"``)
    JSON-stringifies the value. That would fall through ``dict.get(...)``
    and produce an empty, misleading summary, so we reject it loudly here.
    """
    for key in ("logs", "metrics", "deploys"):
        if key in args and not isinstance(args[key], dict):
            raise ValueError(
                f"summarize_findings: arg {key!r} must be the whole upstream "
                f"result (use \"${{node.result}}\" as the entire arg value, "
                "not embedded inside a larger string); got "
                f"{type(args[key]).__name__}"
            )

    logs = args.get("logs") or {}
    metrics = args.get("metrics") or {}
    deploys = args.get("deploys") or {}
    service = (
        args.get("service")
        or logs.get("service")
        or metrics.get("service")
        or deploys.get("service")
        or "unknown-service"
    )

    evidence: List[str] = []
    confidence = "low"
    likely_cause = "insufficient signal — gather more evidence"
    recommended_action = "expand the data window and re-run the workflow"

    errors = int(logs.get("errors") or 0)
    if errors:
        evidence.append(f"{errors} ERROR-level log lines in the last "
                        f"{logs.get('window_minutes', '?')} minutes")
    saturation = metrics.get("saturation")
    if saturation:
        evidence.append(
            f"resource saturation: {saturation} "
            f"(cpu_p99={metrics.get('cpu_p99')}%, "
            f"mem_p99={metrics.get('memory_p99')}%, "
            f"latency_p99_ms={metrics.get('latency_p99_ms')})"
        )
    deploy_list = deploys.get("deploys") or []
    recent = [d for d in deploy_list if int(d.get("minutes_ago", 9999)) <= 120]
    if recent:
        evidence.append(
            f"{len(recent)} deploy(s) in the last 2 hours; most recent: "
            f"{recent[0].get('summary')!r} ({recent[0].get('minutes_ago')} min ago)"
        )

    if recent and (errors >= 5 or saturation == "high"):
        likely_cause = (
            f"recent deploy ({recent[0].get('id')}) introduced regression "
            "correlating with elevated errors and resource pressure"
        )
        confidence = "high"
        recommended_action = (
            f"roll back {recent[0].get('id')} on {service} and re-evaluate"
        )
    elif saturation == "high" and errors >= 5:
        likely_cause = (
            "resource exhaustion under load — pool sizing or scale-out "
            "limits hit"
        )
        confidence = "medium"
        recommended_action = (
            f"scale {service} out and review pool/concurrency settings"
        )
    elif errors >= 5:
        likely_cause = "downstream dependency failures driving error rate"
        confidence = "medium"
        recommended_action = (
            "check health of upstreams (payments-api, inventory-service) "
            "before changing this service"
        )

    return {
        "service": service,
        "likely_cause": likely_cause,
        "confidence": confidence,
        "evidence": evidence,
        "recommended_action": recommended_action,
    }


__all__ = [
    "fetch_deploys",
    "fetch_logs",
    "fetch_metrics",
    "summarize_findings",
]
