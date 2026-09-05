"""Low-cardinality metrics for the private hybrid execution spike."""

from __future__ import annotations

import math
import time
from enum import StrEnum
from typing import Any

from .._observability import current_span


class HybridProgressPhase(StrEnum):
    """Fixed, content-free phases exposed to the demo trace."""

    SANDBOX_CREATE = "sandbox_create"
    PACKAGE_UPLOAD = "package_upload"
    PACKAGE_VERIFY = "package_verify"
    EXECUTOR_READY = "executor_ready"
    DISCOVERY = "discovery"
    TOOL_EXECUTION = "tool_execution"
    CLEANUP_HANDOFF = "cleanup_handoff"
    CLEANUP_COMPLETE = "cleanup_complete"


class HybridProgressStatus(StrEnum):
    """Fixed status vocabulary for hybrid progress events."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HybridMetric(StrEnum):
    """Fixed hybrid instrument vocabulary."""

    REQUESTS = "requests"
    REQUEST_FAILURES = "request_failures"
    REQUEST_DURATION = "request_duration"
    FUNCTION_COLD_START = "function_cold_start"
    MODEL_CALLS = "model_calls"
    MODEL_FAILURES = "model_failures"
    MODEL_DURATION = "model_duration"
    STREAM_TTFT = "stream_ttft"
    SANDBOX_CREATES = "sandbox_creates"
    SANDBOX_CREATE_FAILURES = "sandbox_create_failures"
    SANDBOX_CREATE_DURATION = "sandbox_create_duration"
    PACKAGE_UPLOAD_DURATION = "package_upload_duration"
    PACKAGE_VERIFY_DURATION = "package_verify_duration"
    PACKAGE_VERIFY_FAILURES = "package_verify_failures"
    EXECUTOR_READY_DURATION = "executor_ready_duration"
    DISCOVERY_DURATION = "discovery_duration"
    TOOL_CALLS = "tool_calls"
    TOOL_FAILURES = "tool_failures"
    TOOL_QUEUE_DURATION = "tool_queue_duration"
    TOOL_EXECUTION_DURATION = "tool_execution_duration"
    TOOL_TRANSFER_DURATION = "tool_transfer_duration"
    SANDBOX_DELETES = "sandbox_deletes"
    SANDBOX_DELETE_FAILURES = "sandbox_delete_failures"
    SANDBOX_DELETE_DURATION = "sandbox_delete_duration"
    SANDBOX_DELETE_REQUESTS_ACCEPTED = "sandbox_delete_requests_accepted"
    SANDBOX_DELETE_FALLBACKS = "sandbox_delete_fallbacks"
    SANDBOX_LIFECYCLE_HANDOFFS = "sandbox_lifecycle_handoffs"
    SANDBOX_LIFECYCLE_HANDOFF_FAILURES = "sandbox_lifecycle_handoff_failures"
    SANDBOX_LIFECYCLE_HANDOFF_DURATION = "sandbox_lifecycle_handoff_duration"
    SANDBOX_REAPED = "sandbox_reaped"


_DURATION_METRICS = frozenset(
    {
        HybridMetric.REQUEST_DURATION,
        HybridMetric.FUNCTION_COLD_START,
        HybridMetric.MODEL_DURATION,
        HybridMetric.STREAM_TTFT,
        HybridMetric.SANDBOX_CREATE_DURATION,
        HybridMetric.PACKAGE_UPLOAD_DURATION,
        HybridMetric.PACKAGE_VERIFY_DURATION,
        HybridMetric.EXECUTOR_READY_DURATION,
        HybridMetric.DISCOVERY_DURATION,
        HybridMetric.TOOL_QUEUE_DURATION,
        HybridMetric.TOOL_EXECUTION_DURATION,
        HybridMetric.TOOL_TRANSFER_DURATION,
        HybridMetric.SANDBOX_DELETE_DURATION,
        HybridMetric.SANDBOX_LIFECYCLE_HANDOFF_DURATION,
    }
)
_COUNT_METRICS = frozenset(set(HybridMetric) - set(_DURATION_METRICS))
_meter: Any | None = None
_histograms: dict[HybridMetric, Any] = {}
_counters: dict[HybridMetric, Any] = {}
_ready = False


def record_hybrid_duration(metric: HybridMetric, started_at: float) -> None:
    """Record elapsed seconds on one fixed histogram."""
    if metric not in _DURATION_METRICS:
        raise ValueError(f"{metric.value} is not a duration metric.")
    _ensure_instruments()
    instrument = _histograms.get(metric)
    if instrument is not None:
        instrument.record(max(0.0, time.perf_counter() - started_at))


def record_hybrid_value(metric: HybridMetric, seconds: float) -> None:
    """Record an already-measured non-negative duration."""
    if metric not in _DURATION_METRICS:
        raise ValueError(f"{metric.value} is not a duration metric.")
    _ensure_instruments()
    instrument = _histograms.get(metric)
    if instrument is not None:
        instrument.record(max(0.0, seconds))


def record_hybrid_count(metric: HybridMetric) -> None:
    """Increment one fixed counter without request identifiers."""
    if metric not in _COUNT_METRICS:
        raise ValueError(f"{metric.value} is not a counter metric.")
    _ensure_instruments()
    instrument = _counters.get(metric)
    if instrument is not None:
        instrument.add(1)


def record_hybrid_progress(
    phase: HybridProgressPhase,
    status: HybridProgressStatus,
    *,
    duration_seconds: float | None = None,
) -> None:
    """Add one bounded, content-free progress event to the current span."""
    attributes: dict[str, str | float] = {
        "phase": phase.value,
        "status": status.value,
    }
    if duration_seconds is not None:
        if not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError("Hybrid progress duration must be non-negative and finite.")
        attributes["duration_ms"] = duration_seconds * 1000.0
    current_span().add_event("hybrid.progress", attributes)


def _ensure_instruments() -> None:
    global _meter, _ready
    if _ready:
        return
    _ready = True
    try:
        from agent_framework.observability import get_meter

        _meter = get_meter()
        for metric in _DURATION_METRICS:
            _histograms[metric] = _meter.create_histogram(
                f"azure_functions_agents.hybrid.{metric.value}",
                unit="s",
            )
        for metric in _COUNT_METRICS:
            _counters[metric] = _meter.create_counter(
                f"azure_functions_agents.hybrid.{metric.value}",
            )
    except Exception:
        _meter = None
        _histograms.clear()
        _counters.clear()
