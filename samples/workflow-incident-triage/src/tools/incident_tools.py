"""Workflow-safe evidence tools for the incident-triage sample.

These are the tools the LLM gets to compose into a workflow when the
agent decides an incident warrants more than a single chat turn of
work. They are intentionally **not** used outside workflow plans:
``@workflow_tool`` opts them into Dynamic Workflow Activity execution,
and no plain public normal tool is exported from this module.

Design notes:

- Each handler takes a single ``args`` dict and returns a JSON-serializable dict.
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

Collection (data-driven) tools — Issue #1276:

- ``discover_services`` → ``{"incident": str, "count": int,
                            "services": [{"name": str, "tier": str,
                                          "in_scope": bool}]}``
- ``inspect_service`` →   ``{"service": str, "index": int, "errors": int,
                            "saturation": str, "healthy": bool,
                            "headline": str}``
- ``summarize_scan`` →    ``{"incident": str, "scanned": int, "skipped": int,
                            "unhealthy": [str], "headline": str}``
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


# ---------------------------------------------------------------------------
# Data-driven (collection) workflow tools — Issue #1276.
#
# These three tools demonstrate the dynamic control-flow surface: a
# discovery tool returns a bounded JSON array, a per-item inspection tool
# is fanned out with ``for_each`` (skipping out-of-scope items with
# ``when``), and a downstream tool consumes the ordered ``{index, status,
# result}`` aggregate the logical ``for_each`` node exposes.
# ---------------------------------------------------------------------------

# A small, deterministic service catalog. ``marketing-site`` is always
# within the first three entries so every bounded slice includes at least
# one ``low`` tier service — the item a ``when`` predicate skips.
_SERVICE_CATALOG: List[Dict[str, Any]] = [
    {"name": "orders-api", "tier": "critical"},
    {"name": "marketing-site", "tier": "low"},
    {"name": "payments-api", "tier": "critical"},
    {"name": "inventory-service", "tier": "high"},
    {"name": "docs-site", "tier": "low"},
]

# Hard ceiling on the fan-out so a plan built from this discovery result
# always stays well under the workflow ``max_nodes`` budget.
_MAX_DISCOVERED_SERVICES = 5
_MIN_DISCOVERED_SERVICES = 3


@workflow_tool(
    description=(
        "Discover the services implicated by an incident. Args: "
        "{incident: str}. Returns {incident, count, services: "
        "[{name, tier: 'critical'|'high'|'low', in_scope: bool}]}. The array "
        "is bounded (3-5 items) and deterministic; low-tier services come back "
        "with in_scope=false so a for_each plan can skip them with a `when` "
        "predicate on ${item.in_scope}. Use its `services` array as the "
        "for_each source for a per-service inspection task."
    )
)
def discover_services(args: Dict[str, Any]) -> Dict[str, Any]:
    incident = args.get("incident")
    if not isinstance(incident, str) or not incident.strip():
        raise ValueError("discover_services: 'incident' arg (string) is required")

    # Deterministic bounded slice: between _MIN and _MAX entries, keyed off
    # the incident text so the same incident always yields the same fan-out.
    span = _MAX_DISCOVERED_SERVICES - _MIN_DISCOVERED_SERVICES
    take = _MIN_DISCOVERED_SERVICES + _seeded_int(incident + ":count", 0, span)
    take = min(take, len(_SERVICE_CATALOG))

    services: List[Dict[str, Any]] = [
        {
            "name": entry["name"],
            "tier": entry["tier"],
            # Low-tier services are intentionally out of scope: this is the
            # item the workflow's `when` predicate skips.
            "in_scope": entry["tier"] != "low",
        }
        for entry in _SERVICE_CATALOG[:take]
    ]
    return {"incident": incident, "count": len(services), "services": services}


@workflow_tool(
    description=(
        "Inspect a single service for incident signal. Args: "
        "{service: str, index?: int}. Intended to be fanned out with "
        "for_each over discover_services' `services` array, binding "
        "service=${item.name} (and optionally index=${index}). Returns "
        "{service, index, errors, saturation, healthy, headline}."
    )
)
def inspect_service(args: Dict[str, Any]) -> Dict[str, Any]:
    service = _require_service(args, "inspect_service")
    raw_index = args.get("index")
    index = int(raw_index) if isinstance(raw_index, (int, str)) and str(raw_index).lstrip("-").isdigit() else 0

    # Reuse the existing evidence tools so per-service inspection stays a
    # deterministic function of the service name.
    logs = fetch_logs({"service": service})
    metrics = fetch_metrics({"service": service})
    errors = int(logs.get("errors") or 0)
    saturation = str(metrics.get("saturation") or "moderate")
    healthy = errors < 8 and saturation != "high"
    headline = (
        f"{service}: healthy"
        if healthy
        else f"{service}: {errors} errors, {saturation} saturation"
    )
    return {
        "service": service,
        "index": index,
        "errors": errors,
        "saturation": saturation,
        "healthy": healthy,
        "headline": headline,
    }


@workflow_tool(
    description=(
        "Summarize a for_each service scan. Args: {incident?: str, findings: "
        "<inspect logical node result>}. Pass the whole ordered aggregate via "
        "${inspect_node.result} — a list of {index, status, result} envelopes "
        "in source order, where skipped items have result=null. Returns "
        "{incident, scanned, skipped, unhealthy: [str], headline}."
    )
)
def summarize_scan(args: Dict[str, Any]) -> Dict[str, Any]:
    """Consume the ordered aggregate a logical ``for_each`` node exposes.

    The aggregate is a source-ordered list of ``{index, status, result}``
    envelopes: ``status`` is ``"completed"`` or ``"skipped"`` and a skipped
    position carries ``result: null``. This tool must be passed the whole
    aggregate as a single ``${node.result}`` value, so it validates the
    shape loudly rather than silently degrading on an embedded template ref.
    """
    findings = args.get("findings")
    if not isinstance(findings, list):
        raise ValueError(
            "summarize_scan: 'findings' must be the whole for_each aggregate "
            "(use \"${inspect_node.result}\" as the entire arg value); got "
            f"{type(findings).__name__}"
        )

    incident = args.get("incident")
    scanned = 0
    skipped = 0
    unhealthy: List[str] = []
    # Envelopes arrive in source order; preserve it in the report.
    for envelope in findings:
        if not isinstance(envelope, dict):
            raise ValueError(
                "summarize_scan: each finding must be an {index, status, result} "
                f"envelope; got {type(envelope).__name__}"
            )
        status = envelope.get("status")
        if status == "skipped":
            skipped += 1
            continue
        scanned += 1
        result = envelope.get("result")
        if isinstance(result, dict) and not result.get("healthy", True):
            headline = result.get("headline")
            unhealthy.append(str(headline) if headline else str(result.get("service")))

    if not scanned:
        headline = "no in-scope services were inspected"
    elif unhealthy:
        headline = f"{len(unhealthy)} of {scanned} inspected service(s) unhealthy"
    else:
        headline = f"all {scanned} inspected service(s) healthy"

    return {
        "incident": str(incident) if isinstance(incident, str) else "unknown-incident",
        "scanned": scanned,
        "skipped": skipped,
        "unhealthy": unhealthy,
        "headline": headline,
    }


__all__ = [
    "discover_services",
    "fetch_deploys",
    "fetch_logs",
    "fetch_metrics",
    "inspect_service",
    "summarize_findings",
    "summarize_scan",
]
