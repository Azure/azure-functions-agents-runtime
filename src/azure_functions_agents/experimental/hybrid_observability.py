"""Low-cardinality metrics for the private hybrid execution spike."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any


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
    SANDBOX_REAPED = "sandbox_reaped"


_DURATION_METRICS = frozenset(
    {
        HybridMetric.REQUEST_DURATION,
        HybridMetric.FUNCTION_COLD_START,
        HybridMetric.MODEL_DURATION,
        HybridMetric.STREAM_TTFT,
        HybridMetric.SANDBOX_CREATE_DURATION,
        HybridMetric.PACKAGE_UPLOAD_DURATION,
        HybridMetric.EXECUTOR_READY_DURATION,
        HybridMetric.DISCOVERY_DURATION,
        HybridMetric.TOOL_QUEUE_DURATION,
        HybridMetric.TOOL_EXECUTION_DURATION,
        HybridMetric.TOOL_TRANSFER_DURATION,
        HybridMetric.SANDBOX_DELETE_DURATION,
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
