"""Deterministic quality scoring and ATIF export for benchmark results."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SAMPLE_ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = SAMPLE_ROOT / "src" / "tools" / "_benchmark_core.py"


@dataclass(frozen=True)
class QualityScore:
    score: float
    exact: bool
    matching_fields: int
    compared_fields: int


def _load_core() -> Any:
    module_name = "workflow_token_benchmark_evaluation_core"
    spec = importlib.util.spec_from_file_location(module_name, CORE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load benchmark core from {CORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def build_expected_report(
    *,
    trial_id: str,
    services: Sequence[str],
    evidence_lines: int,
) -> dict[str, Any]:
    """Build the oracle report from complete source evidence, outside either agent path."""
    core = _load_core()
    expected_services: list[dict[str, Any]] = []
    total_errors = 0
    elevated_service_count = 0
    for service in services:
        evidence = core.build_service_evidence(trial_id, service, evidence_lines)
        log_summary = evidence["log_summary"]
        metrics = evidence["metrics"]
        deploy = evidence["deploy"]
        errors = int(log_summary["errors"])
        latency = int(metrics["latency_p95_ms"])
        cpu = int(metrics["cpu_p95_percent"])
        elevated = errors >= 5 or latency >= 900 or cpu >= 85
        total_errors += errors
        elevated_service_count += int(elevated)
        expected_services.append(
            {
                "service": service,
                "status": "attention" if elevated else "healthy",
                "errors": errors,
                "warnings": int(log_summary["warnings"]),
                "evidence_lines": int(log_summary["line_count"]),
                "latency_p95_ms": latency,
                "cpu_p95_percent": cpu,
                "deploy_revision": str(deploy["revision"]),
                "deploy_age_minutes": int(deploy["age_minutes"]),
            }
        )
    return {
        "schema_version": 1,
        "trial_id": trial_id,
        "service_count": len(expected_services),
        "elevated_service_count": elevated_service_count,
        "total_errors": total_errors,
        "services": expected_services,
    }


def _flatten(value: Any, path: str = "$") -> dict[str, Any]:
    if isinstance(value, Mapping):
        flattened: dict[str, Any] = {}
        if not value:
            flattened[path] = {}
        for key, child in value.items():
            flattened.update(_flatten(child, f"{path}.{key}"))
        return flattened
    if isinstance(value, list):
        flattened = {}
        if not value:
            flattened[path] = []
        for index, child in enumerate(value):
            flattened.update(_flatten(child, f"{path}[{index}]"))
        return flattened
    return {path: value}


def score_report(candidate: Mapping[str, Any], expected: Mapping[str, Any]) -> QualityScore:
    """Score matching leaf fields over the union, penalizing missing and extra fields."""
    candidate_fields = _flatten(candidate)
    expected_fields = _flatten(expected)
    compared_paths = candidate_fields.keys() | expected_fields.keys()
    matching_fields = sum(
        path in candidate_fields
        and path in expected_fields
        and candidate_fields[path] == expected_fields[path]
        for path in compared_paths
    )
    compared_fields = len(compared_paths)
    score = matching_fields / compared_fields if compared_fields else 1.0
    return QualityScore(
        score=score,
        exact=candidate == expected,
        matching_fields=matching_fields,
        compared_fields=compared_fields,
    )


def build_atif_trajectory(
    *,
    trial_id: str,
    request: Mapping[str, Any],
    report: Mapping[str, Any],
    expected_report: Mapping[str, Any],
    quality: QualityScore,
    report_latency_ms: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    model: str | None,
) -> dict[str, Any]:
    """Represent a completed benchmark mode as an ATIF trajectory for offline grading."""
    task = {
        "instruction": (
            "Inspect every requested service exactly once, preserve request order, "
            "and publish a complete incident report matching the reference facts."
        ),
        "request": dict(request),
        "reference_report": dict(expected_report),
    }
    final_metrics: dict[str, Any] = {
        "total_steps": 2,
        "extra": {
            "report_latency_ms": report_latency_ms,
            "deterministic_quality_score": quality.score,
            "deterministic_quality_exact": quality.exact,
        },
    }
    if input_tokens is not None:
        final_metrics["total_prompt_tokens"] = input_tokens
    if output_tokens is not None:
        final_metrics["total_completion_tokens"] = output_tokens
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": f"{trial_id}-candidate",
        "agent": {
            "name": "workflow-token-benchmark",
            "version": "1",
            "model_name": model,
        },
        "steps": [
            {
                "step_id": 1,
                "source": "user",
                "message": json.dumps(task, ensure_ascii=True, sort_keys=True),
            },
            {
                "step_id": 2,
                "source": "agent",
                "model_name": model,
                "message": json.dumps(report, ensure_ascii=True, sort_keys=True),
            },
        ],
        "final_metrics": final_metrics,
        "extra": {
            "trial_id": trial_id,
        },
    }


def write_atif_trajectory(path: Path, trajectory: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trajectory, indent=2) + "\n", encoding="utf-8")
