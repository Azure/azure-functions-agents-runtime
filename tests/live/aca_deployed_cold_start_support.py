"""Pure helpers for the manual deployed cold-start qualification."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Protocol

from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError

_SAMPLES_ENV = "AZURE_FUNCTIONS_AGENTS_ACA_COLD_START_SAMPLES"
_DEFAULT_SAMPLES = 3
_MIN_SAMPLES = 1
_MAX_SAMPLES = 5
FIRST_ATTEMPT_ACCEPTANCE_SLO_SECONDS = 35.0
SETUP_ATTEMPT_TIMEOUT_SECONDS = 45.0
ADMISSION_WINDOW_SECONDS = 180.0
SSE_TERMINAL_WINDOW_SECONDS = 240.0
PUBLIC_TERMINAL_WINDOW_SECONDS = 45.0
SAMPLE_WINDOW_SECONDS = (
    ADMISSION_WINDOW_SECONDS + SSE_TERMINAL_WINDOW_SECONDS + PUBLIC_TERMINAL_WINDOW_SECONDS
)
FINAL_RECOVERY_WINDOW_SECONDS = 60.0


class _PytestConfig(Protocol):
    def getoption(self, name: str) -> object: ...


@dataclass(frozen=True, slots=True)
class ColdStartMetrics:
    """Aggregate p50/p95/max metrics in milliseconds."""

    first_attempt_acceptance_ms: tuple[float, float, float]
    total_acceptance_ms: tuple[float, float, float]
    first_event_ms: tuple[float, float, float]
    terminal_ms: tuple[float, float, float]


def cold_start_samples_from_option_or_environment(config: _PytestConfig) -> int:
    """Resolve an explicitly configured safe sample count, defaulting to three."""
    option_value = config.getoption("aca_cold_start_samples")
    raw = option_value if isinstance(option_value, str) and option_value.strip() else None
    source = "--aca-cold-start-samples"
    if raw is None:
        raw = os.environ.get(_SAMPLES_ENV)
        source = _SAMPLES_ENV
    if raw is None or not raw.strip():
        return _DEFAULT_SAMPLES
    try:
        value = int(raw)
    except ValueError as exc:
        raise AcaSmokeEnvironmentError(
            f"{source} must be an integer between {_MIN_SAMPLES} and {_MAX_SAMPLES}."
        ) from exc
    if not _MIN_SAMPLES <= value <= _MAX_SAMPLES:
        raise AcaSmokeEnvironmentError(
            f"{source} must be between {_MIN_SAMPLES} and {_MAX_SAMPLES}."
        )
    return value


def first_attempt_slo_failure(
    *,
    status: int,
    elapsed_seconds: float,
    typed_setup_deadline: bool,
) -> str | None:
    """Classify the first setup attempt without forgiving a later retry."""
    if typed_setup_deadline:
        return "typed_setup_deadline_exceeded"
    if status != 202:
        return f"first_attempt_http_{status}"
    if elapsed_seconds > FIRST_ATTEMPT_ACCEPTANCE_SLO_SECONDS:
        return "first_attempt_acceptance_exceeded"
    return None


def cold_start_metrics(
    first_attempt_acceptance_seconds: list[float],
    total_acceptance_seconds: list[float],
    first_event_seconds: list[float],
    terminal_seconds: list[float],
) -> ColdStartMetrics:
    """Calculate nearest-rank p50/p95/max metrics without retaining request details."""
    return ColdStartMetrics(
        first_attempt_acceptance_ms=_percentiles(first_attempt_acceptance_seconds),
        total_acceptance_ms=_percentiles(total_acceptance_seconds),
        first_event_ms=_percentiles(first_event_seconds),
        terminal_ms=_percentiles(terminal_seconds),
    )


def render_cold_start_report(
    *,
    sample_count: int,
    retries: int,
    metrics: ColdStartMetrics | None,
    cleanup_complete: bool,
) -> str:
    """Render aggregate-only evidence; never include IDs, prompts, or model output."""
    metric_text = "not-available"
    if metrics is not None:
        metric_text = (
            f"first_attempt_acceptance_ms={_format(metrics.first_attempt_acceptance_ms)} "
            f"total_acceptance_ms={_format(metrics.total_acceptance_ms)} "
            f"first_event_ms={_format(metrics.first_event_ms)} "
            f"terminal_ms={_format(metrics.terminal_ms)}"
        )
    return (
        "ACA deployed cold-start qualification: "
        f"samples={sample_count} retries={retries} {metric_text} "
        f"cleanup={'complete' if cleanup_complete else 'incomplete'}"
    )


def maximum_cold_start_budget_seconds(
    sample_count: int,
    *,
    controller_cleanup_seconds: float,
) -> float:
    """Return the bounded worst case including final recovery and controller cleanup."""
    if not _MIN_SAMPLES <= sample_count <= _MAX_SAMPLES:
        raise ValueError("cold-start sample count is outside the safe bound")
    return (
        sample_count * SAMPLE_WINDOW_SECONDS
        + FINAL_RECOVERY_WINDOW_SECONDS
        + sample_count * controller_cleanup_seconds
    )


def _percentiles(values: list[float]) -> tuple[float, float, float]:
    if not values:
        raise AssertionError("Cold-start percentiles require at least one value.")
    ordered = sorted(value * 1000 for value in values)
    p50, p95 = (
        ordered[math.ceil(percentile * len(ordered) / 100) - 1] for percentile in (50, 95)
    )
    return p50, p95, ordered[-1]


def _format(values: tuple[float, float, float]) -> str:
    return f"p50={values[0]:.1f},p95={values[1]:.1f},max={values[2]:.1f}"
