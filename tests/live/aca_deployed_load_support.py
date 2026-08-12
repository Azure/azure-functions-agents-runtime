"""Pure helpers for the manual deployed ACA load qualification."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import pytest
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError

_LOAD_CONCURRENCY_ENV = "AZURE_FUNCTIONS_AGENTS_ACA_LOAD_CONCURRENCY"
_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 100


class _PytestConfig(Protocol):
    def getoption(self, name: str) -> object: ...


@dataclass(frozen=True, slots=True)
class CommonActiveInterval:
    """Conservative overlapping observation bounds for every admitted active run."""

    started_at: datetime
    ended_at: datetime


@dataclass(frozen=True, slots=True)
class LoadLatencyMetrics:
    """Redacted latency quantiles in milliseconds."""

    submission_ms: tuple[float, float, float]
    first_event_ms: tuple[float, float, float]
    terminal_ms: tuple[float, float, float]


def load_concurrency_from_option_or_environment(config: _PytestConfig) -> int | None:
    """Resolve the explicit CLI option before the optional environment fallback."""
    option_value = config.getoption("aca_load_concurrency")
    raw = option_value if isinstance(option_value, str) and option_value.strip() else None
    source = "--aca-load-concurrency"
    if raw is None:
        raw = os.environ.get(_LOAD_CONCURRENCY_ENV)
        source = _LOAD_CONCURRENCY_ENV
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise AcaSmokeEnvironmentError(
            f"{source} must be an integer between {_MIN_CONCURRENCY} and {_MAX_CONCURRENCY}."
        ) from exc
    if not _MIN_CONCURRENCY <= value <= _MAX_CONCURRENCY:
        raise AcaSmokeEnvironmentError(
            f"{source} must be between {_MIN_CONCURRENCY} and {_MAX_CONCURRENCY}."
        )
    return value


def require_load_concurrency(config: _PytestConfig) -> int:
    """Skip the expensive qualification unless an operator provided a concurrency."""
    concurrency = load_concurrency_from_option_or_environment(config)
    if concurrency is None:
        pytest.skip(
            "Set --aca-load-concurrency N or "
            "AZURE_FUNCTIONS_AGENTS_ACA_LOAD_CONCURRENCY=N to run the manual ACA load "
            "qualification."
        )
    return concurrency


def latency_metrics(
    submission_seconds: list[float],
    first_event_seconds: list[float],
    terminal_seconds: list[float],
) -> LoadLatencyMetrics:
    """Calculate nearest-rank p50/p95/p99 latencies without retaining request data."""
    return LoadLatencyMetrics(
        submission_ms=_percentiles(submission_seconds),
        first_event_ms=_percentiles(first_event_seconds),
        terminal_ms=_percentiles(terminal_seconds),
    )


def _percentiles(values: list[float]) -> tuple[float, float, float]:
    if not values:
        raise AssertionError("Latency percentiles require at least one value.")
    ordered = sorted(value * 1000 for value in values)
    p50, p95, p99 = (
        ordered[math.ceil(percentile * len(ordered) / 100) - 1] for percentile in (50, 95, 99)
    )
    return p50, p95, p99


def render_load_report(
    *,
    concurrency: int,
    common_interval: CommonActiveInterval | None,
    admitted_count: int,
    succeeded_count: int,
    metrics: LoadLatencyMetrics | None,
    replay_count: int,
    active_run_conflict_count: int,
    retry_count: int,
    unclassified_service_throttle_count: int,
    unresolved_idempotency_count: int,
    cleanup_complete: bool,
    admission_failure_categories: tuple[tuple[str, int], ...] = (),
) -> str:
    """Render only aggregate, redacted evidence suitable for an operator log."""
    interval = (
        f"{common_interval.started_at.isoformat()}..{common_interval.ended_at.isoformat()}"
        if common_interval is not None
        else "not-observed"
    )
    metric_text = "not-available"
    if metrics is not None:
        metric_text = (
            f"submission_ms={_format_quantiles(metrics.submission_ms)} "
            f"first_event_ms={_format_quantiles(metrics.first_event_ms)} "
            f"terminal_ms={_format_quantiles(metrics.terminal_ms)}"
        )
    failure_categories = (
        ",".join(f"{category}={count}" for category, count in admission_failure_categories)
        if admission_failure_categories
        else "none"
    )
    return (
        "ACA deployed load qualification: "
        f"N={concurrency} common_active_interval={interval} "
        f"admitted={admitted_count} succeeded={succeeded_count} {metric_text} "
        f"idempotent_replays={replay_count} active_run_conflicts={active_run_conflict_count} "
        f"retries={retry_count} "
        f"unclassified_service_throttles={unclassified_service_throttle_count} "
        f"unresolved_idempotencies={unresolved_idempotency_count} "
        f"admission_failure_categories={failure_categories} "
        f"cleanup={'complete' if cleanup_complete else 'incomplete'}"
    )


def utc_now() -> datetime:
    """Return a timezone-aware timestamp for operator-visible interval evidence."""
    return datetime.now(UTC)


def _format_quantiles(values: tuple[float, float, float]) -> str:
    return f"p50={values[0]:.1f},p95={values[1]:.1f},p99={values[2]:.1f}"
