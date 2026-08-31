from __future__ import annotations

import hashlib
import json
from typing import Any


def _bounded_int(seed: str, low: int, high: int) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return low + int.from_bytes(digest[:4], "big") % (high - low + 1)


def build_service_evidence(
    trial_id: str,
    service: str,
    evidence_lines: int,
) -> dict[str, Any]:
    if not trial_id.strip():
        raise ValueError("trial_id must be a non-empty string")
    if not service.strip():
        raise ValueError("service must be a non-empty string")
    if not 1 <= evidence_lines <= 200:
        raise ValueError("evidence_lines must be between 1 and 200")

    errors = _bounded_int(f"{trial_id}:{service}:errors", 2, max(2, evidence_lines // 3))
    warnings = _bounded_int(f"{trial_id}:{service}:warnings", 1, max(1, evidence_lines // 2))
    latency_p95_ms = _bounded_int(f"{trial_id}:{service}:latency", 90, 1800)
    cpu_p95 = _bounded_int(f"{trial_id}:{service}:cpu", 35, 97)
    deploy_age_minutes = _bounded_int(f"{trial_id}:{service}:deploy", 5, 360)

    levels = ("INFO", "INFO", "WARN", "ERROR")
    operations = ("checkout", "inventory", "payment", "fulfillment")
    lines = []
    for index in range(evidence_lines):
        line_seed = f"{trial_id}:{service}:{index}"
        level = levels[_bounded_int(f"{line_seed}:level", 0, len(levels) - 1)]
        operation = operations[
            _bounded_int(f"{line_seed}:operation", 0, len(operations) - 1)
        ]
        duration = _bounded_int(f"{line_seed}:duration", 12, 2400)
        status = 502 if level == "ERROR" else 429 if level == "WARN" else 200
        request_id = hashlib.sha256(line_seed.encode("utf-8")).hexdigest()[:16]
        lines.append(
            f"2026-08-25T12:{index % 60:02d}:{(index * 7) % 60:02d}Z "
            f"level={level} service={service} operation={operation} "
            f"status={status} duration_ms={duration} request_id={request_id} "
            f"message=synthetic benchmark evidence for deterministic incident analysis"
        )

    return {
        "trial_id": trial_id,
        "service": service,
        "logs": lines,
        "log_summary": {
            "errors": errors,
            "warnings": warnings,
            "line_count": evidence_lines,
        },
        "metrics": {
            "latency_p95_ms": latency_p95_ms,
            "cpu_p95_percent": cpu_p95,
            "request_rate_per_minute": _bounded_int(
                f"{trial_id}:{service}:rate", 100, 5000
            ),
        },
        "deploy": {
            "revision": hashlib.sha256(
                f"{trial_id}:{service}:revision".encode()
            ).hexdigest()[:12],
            "age_minutes": deploy_age_minutes,
            "actor": "benchmark-release-bot",
        },
    }


def build_canonical_report(
    trial_id: str,
    service_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    if not trial_id.strip():
        raise ValueError("trial_id must be a non-empty string")
    if not service_reports:
        raise ValueError("service_reports must be a non-empty list")

    services: list[dict[str, Any]] = []
    total_errors = 0
    elevated_services = 0
    for evidence in service_reports:
        if evidence.get("trial_id") != trial_id:
            raise ValueError("every service report must match trial_id")
        service = evidence.get("service")
        log_summary = evidence.get("log_summary")
        metrics = evidence.get("metrics")
        deploy = evidence.get("deploy")
        if (
            not isinstance(service, str)
            or not isinstance(log_summary, dict)
            or not isinstance(metrics, dict)
            or not isinstance(deploy, dict)
        ):
            raise ValueError("service report has an invalid shape")

        errors = int(log_summary.get("errors") or 0)
        latency = int(metrics.get("latency_p95_ms") or 0)
        cpu = int(metrics.get("cpu_p95_percent") or 0)
        elevated = errors >= 5 or latency >= 900 or cpu >= 85
        total_errors += errors
        elevated_services += int(elevated)
        services.append(
            {
                "service": service,
                "status": "attention" if elevated else "healthy",
                "errors": errors,
                "warnings": int(log_summary.get("warnings") or 0),
                "evidence_lines": int(log_summary.get("line_count") or 0),
                "latency_p95_ms": latency,
                "cpu_p95_percent": cpu,
                "deploy_revision": str(deploy.get("revision") or ""),
                "deploy_age_minutes": int(deploy.get("age_minutes") or 0),
            }
        )

    return {
        "schema_version": 1,
        "trial_id": trial_id,
        "service_count": len(services),
        "elevated_service_count": elevated_services,
        "total_errors": total_errors,
        "services": services,
    }


def canonical_json_bytes(report: dict[str, Any]) -> bytes:
    return json.dumps(
        report,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
