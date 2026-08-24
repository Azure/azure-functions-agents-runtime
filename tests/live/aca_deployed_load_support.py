"""Pure helpers for the manual deployed ACA load qualification."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Protocol

import pytest
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live.aca_deployed_agent_support import (
    optional_retry_after_seconds,
)

_LOAD_CONCURRENCY_ENV = "AZURE_FUNCTIONS_AGENTS_ACA_LOAD_CONCURRENCY"
_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 100
_PROVISION_CONCURRENCY_ENV = "AZURE_FUNCTIONS_AGENTS_ACA_PROVISION_CONCURRENCY"
_DEFAULT_PROVISION_CONCURRENCY = 4
_MIN_PROVISION_CONCURRENCY = 1
_MAX_PROVISION_CONCURRENCY = 4
THROTTLE_RETRY_AFTER_MAXIMUM_SECONDS = 10.0


def throttle_retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Return Retry-After if present and within bounds, else None."""
    return optional_retry_after_seconds(
        headers, maximum_seconds=THROTTLE_RETRY_AFTER_MAXIMUM_SECONDS
    )


THROTTLED_ADMISSION_STATUSES = frozenset({429, 503})


def throttled_admission_retry_delay(
    status: int,
    headers: Mapping[str, str],
    *,
    is_final_attempt: bool,
) -> float | None:
    """Return the delay before retrying a throttled admission, or None to give up.

    Honoring the server's backpressure is what a correct client does, but the
    caller must still fail when admissions never recover, so the final attempt
    always declines to retry.
    """
    if status not in THROTTLED_ADMISSION_STATUSES:
        return None
    if is_final_attempt:
        return None
    return throttle_retry_after_seconds(headers)


# Events delivered by a single journal poll are parsed from the stream
# microseconds apart. This window is wide enough to hold one such burst together
# and far narrower than any plausible poll interval, so it separates bursts
# without merging genuinely distinct polls. Grouping on exact timestamp equality
# instead would put every real event in its own batch.
_BATCH_WINDOW_SECONDS = 0.05


class _PytestConfig(Protocol):
    def getoption(self, name: str) -> object: ...


@dataclass(frozen=True, slots=True)
class CommonActiveInterval:
    """Conservative overlapping observation bounds for every admitted active run."""

    started_at: datetime
    ended_at: datetime


@dataclass(frozen=True, slots=True)
class LoadLatencyMetrics:
    """Redacted latency and client-observed streaming quantiles in milliseconds."""

    submission_ms: tuple[float, float, float]
    first_event_ms: tuple[float, float, float]
    terminal_ms: tuple[float, float, float]
    visibility_gap_ms: tuple[float, float, float] | None
    visibility_gap_all_ms: tuple[float, float, float] | None
    observed_poll_cadence_ms: tuple[float, float, float] | None
    events_per_batch: tuple[tuple[int, int], ...]
    visibility_gap_sample_count: int
    visibility_gap_all_sample_count: int
    observed_poll_cadence_sample_count: int
    event_batch_count: int
    observed_event_count: int


@dataclass(frozen=True, slots=True)
class ObservedEventBatch:
    """Client-side arrival burst summary."""

    observed_at: float
    event_count: int


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


def provision_concurrency_from_option_or_environment(config: _PytestConfig) -> int:
    """Resolve the optional provisioning batch size, defaulting to the local-safe value."""
    option_value = config.getoption("aca_provision_concurrency")
    raw = option_value if isinstance(option_value, str) and option_value.strip() else None
    source = "--aca-provision-concurrency"
    if raw is None:
        raw = os.environ.get(_PROVISION_CONCURRENCY_ENV)
        source = _PROVISION_CONCURRENCY_ENV
    if raw is None or not raw.strip():
        return _DEFAULT_PROVISION_CONCURRENCY
    try:
        value = int(raw)
    except ValueError as exc:
        raise AcaSmokeEnvironmentError(
            f"{source} must be an integer between {_MIN_PROVISION_CONCURRENCY} and "
            f"{_MAX_PROVISION_CONCURRENCY}."
        ) from exc
    if not _MIN_PROVISION_CONCURRENCY <= value <= _MAX_PROVISION_CONCURRENCY:
        raise AcaSmokeEnvironmentError(
            f"{source} must be between {_MIN_PROVISION_CONCURRENCY} and "
            f"{_MAX_PROVISION_CONCURRENCY}."
        )
    return value


def latency_metrics(
    submission_seconds: list[float],
    first_event_seconds: list[float],
    terminal_seconds: list[float],
    observed_event_timestamp_sequences: Sequence[Sequence[float]] = (),
) -> LoadLatencyMetrics:
    """Calculate nearest-rank p50/p95/p99 latencies without retaining request data."""
    primary_visibility_gaps = [
        gap
        for timestamps in observed_event_timestamp_sequences
        for gap in visibility_gap_seconds(timestamps, waiting_only=True)
    ]
    all_visibility_gaps = [
        gap
        for timestamps in observed_event_timestamp_sequences
        for gap in visibility_gap_seconds(timestamps, waiting_only=False)
    ]
    observed_poll_cadences = [
        cadence
        for timestamps in observed_event_timestamp_sequences
        for cadence in observed_poll_cadence_seconds(timestamps)
    ]
    batch_counts: dict[int, int] = {}
    event_batch_count = 0
    observed_event_count = 0
    for timestamps in observed_event_timestamp_sequences:
        batches = observed_event_batches(timestamps)
        event_batch_count += len(batches)
        observed_event_count += sum(batch.event_count for batch in batches)
        for batch in batches:
            batch_counts[batch.event_count] = batch_counts.get(batch.event_count, 0) + 1
    return LoadLatencyMetrics(
        submission_ms=_percentiles(submission_seconds),
        first_event_ms=_percentiles(first_event_seconds),
        terminal_ms=_percentiles(terminal_seconds),
        visibility_gap_ms=_optional_percentiles(primary_visibility_gaps),
        visibility_gap_all_ms=_optional_percentiles(all_visibility_gaps),
        observed_poll_cadence_ms=_optional_percentiles(observed_poll_cadences),
        events_per_batch=tuple(sorted(batch_counts.items())),
        visibility_gap_sample_count=len(primary_visibility_gaps),
        visibility_gap_all_sample_count=len(all_visibility_gaps),
        observed_poll_cadence_sample_count=len(observed_poll_cadences),
        event_batch_count=event_batch_count,
        observed_event_count=observed_event_count,
    )


def observed_event_batches(
    observed_event_timestamps: Sequence[float],
    *,
    batch_window_seconds: float = _BATCH_WINDOW_SECONDS,
) -> tuple[ObservedEventBatch, ...]:
    """Group client-side event observation timestamps into arrival bursts.

    Events delivered by one poll are read from the stream microseconds apart, so
    they share an arrival *window* rather than an identical timestamp. Grouping
    on exact equality would put every event in its own batch, which silently
    empties the waiting-only series and collapses the measured cadence to the
    cost of parsing two adjacent events -- numbers that look plausible and mean
    nothing. The window is what makes a burst detectable in real data.
    """
    ordered = sorted(observed_event_timestamps)
    if not ordered:
        return ()
    batches: list[ObservedEventBatch] = []
    batch_start = ordered[0]
    previous = ordered[0]
    current_count = 1
    for timestamp in ordered[1:]:
        # Compare against the previous event, not the batch start, so a steady
        # trickle is not merged into one ever-growing batch.
        if timestamp - previous <= batch_window_seconds:
            current_count += 1
            previous = timestamp
            continue
        batches.append(ObservedEventBatch(batch_start, current_count))
        batch_start = timestamp
        previous = timestamp
        current_count = 1
    batches.append(ObservedEventBatch(batch_start, current_count))
    return tuple(batches)


def visibility_gap_seconds(
    observed_event_timestamps: Sequence[float],
    *,
    waiting_only: bool,
) -> tuple[float, ...]:
    """Return client-observed streaming gaps, optionally requiring waiting-event evidence."""
    batches = observed_event_batches(observed_event_timestamps)
    if waiting_only:
        return tuple(
            current.observed_at - previous.observed_at
            for previous, current in pairwise(batches)
            if current.event_count >= 2
        )
    ordered = sorted(observed_event_timestamps)
    return tuple(current - previous for previous, current in pairwise(ordered))


def observed_poll_cadence_seconds(
    observed_event_timestamps: Sequence[float],
) -> tuple[float, ...]:
    """Return inter-batch client arrival spacing measured from observed bursts."""
    batches = observed_event_batches(observed_event_timestamps)
    return tuple(
        current.observed_at - previous.observed_at
        for previous, current in pairwise(batches)
    )


def events_per_batch(observed_event_timestamps: Sequence[float]) -> tuple[int, ...]:
    """Return the number of events in each client-observed arrival burst."""
    return tuple(batch.event_count for batch in observed_event_batches(observed_event_timestamps))


def _percentiles(values: list[float]) -> tuple[float, float, float]:
    if not values:
        raise AssertionError("Latency percentiles require at least one value.")
    ordered = sorted(value * 1000 for value in values)
    p50, p95, p99 = (
        ordered[math.ceil(percentile * len(ordered) / 100) - 1] for percentile in (50, 95, 99)
    )
    return p50, p95, p99


def _optional_percentiles(values: list[float]) -> tuple[float, float, float] | None:
    return _percentiles(values) if values else None


def render_load_report(
    *,
    concurrency: int,
    prepared_count: int,
    provision_concurrency: int,
    provisioning_duration_seconds: float | None,
    provisioning_attempt_count: int,
    provisioning_retry_count: int,
    suspended_prepared_count: int,
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
            f"terminal_ms={_format_quantiles(metrics.terminal_ms)} "
            f"visibility_gap_ms={_format_optional_quantiles(metrics.visibility_gap_ms)} "
            f"visibility_gap_samples={metrics.visibility_gap_sample_count} "
            f"visibility_gap_all_ms={_format_optional_quantiles(metrics.visibility_gap_all_ms)} "
            f"visibility_gap_all_samples={metrics.visibility_gap_all_sample_count} "
            f"observed_poll_cadence_ms="
            f"{_format_optional_quantiles(metrics.observed_poll_cadence_ms)} "
            f"observed_poll_cadence_samples={metrics.observed_poll_cadence_sample_count} "
            f"events_per_batch={_format_batch_distribution(metrics.events_per_batch)} "
            f"event_batches={metrics.event_batch_count} observed_events={metrics.observed_event_count} "
            f"visibility_attribution={_visibility_attribution(metrics)} "
            f"{_visibility_warning(metrics)}"
        )
    failure_categories = (
        ",".join(f"{category}={count}" for category, count in admission_failure_categories)
        if admission_failure_categories
        else "none"
    )
    provisioning_duration = (
        f"{provisioning_duration_seconds:.1f}s"
        if provisioning_duration_seconds is not None
        else "not-available"
    )
    return (
        "ACA deployed load qualification: "
        f"N={concurrency} prepared={prepared_count} "
        f"provision_concurrency={provision_concurrency} "
        f"provisioning_duration={provisioning_duration} "
        f"provisioning_attempts={provisioning_attempt_count} "
        f"provisioning_retries={provisioning_retry_count} "
        f"suspended_prepared={suspended_prepared_count} "
        f"common_active_interval={interval} "
        f"admitted={admitted_count} succeeded={succeeded_count} {metric_text} "
        f"idempotent_replays={replay_count} active_run_conflicts={active_run_conflict_count} "
        f"retries={retry_count} "
        f"unclassified_service_throttles={unclassified_service_throttle_count} "
        f"unresolved_idempotencies={unresolved_idempotency_count} "
        f"admission_failure_categories={failure_categories} "
        f"cleanup={'complete' if cleanup_complete else 'incomplete'}"
        " visibility_proxy_note=client-only observed arrival gaps; does not capture true "
        "sandbox-write-to-client-observe delta because sandbox and CI clocks are not on a "
        "common basis and clock-skew correction would add error comparable to the 2s budget."
    )


def utc_now() -> datetime:
    """Return a timezone-aware timestamp for operator-visible interval evidence."""
    return datetime.now(UTC)


def _format_quantiles(values: tuple[float, float, float]) -> str:
    return f"p50={values[0]:.1f},p95={values[1]:.1f},p99={values[2]:.1f}"


def _format_optional_quantiles(values: tuple[float, float, float] | None) -> str:
    return _format_quantiles(values) if values is not None else "not-available"


def _format_batch_distribution(events_per_batch_distribution: tuple[tuple[int, int], ...]) -> str:
    if not events_per_batch_distribution:
        return "not-available"
    return ",".join(
        f"{event_count}x{batch_count}"
        for event_count, batch_count in events_per_batch_distribution
    )


def _visibility_warning(metrics: LoadLatencyMetrics) -> str:
    if metrics.visibility_gap_ms is not None and metrics.visibility_gap_ms[1] > 2000:
        return "visibility_warning=p95_exceeds_2s"
    return "visibility_warning=none"


def _visibility_attribution(metrics: LoadLatencyMetrics) -> str:
    if metrics.visibility_gap_ms is None or metrics.observed_poll_cadence_ms is None:
        return "not-available"
    gap_p95 = metrics.visibility_gap_ms[1]
    cadence_p95 = metrics.observed_poll_cadence_ms[1]
    if abs(gap_p95 - cadence_p95) <= max(250.0, cadence_p95 * 0.25):
        return "poll_timing_dominates"
    if gap_p95 > cadence_p95:
        return "transport_exceeds_cadence"
    return "below_observed_cadence"
