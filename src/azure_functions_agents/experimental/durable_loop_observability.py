"""Content-free telemetry for the private durable-loop foundation."""

from __future__ import annotations

import math
import time
from enum import StrEnum
from typing import Any

from .._observability import current_span


class DurableLoopPhase(StrEnum):
    """Bounded progress phases emitted by loop activities."""

    RUN = "run"
    MODEL_STEP = "model_step"
    MODEL_START = "model_start"
    MODEL_POLL = "model_poll"
    TOOL_STEP = "tool_step"
    TOOL_QUEUE = "tool_queue"
    MCP_CALL = "mcp_call"
    SANDBOX_CAPACITY_WAIT = "sandbox_capacity_wait"
    SANDBOX_CREATE = "sandbox_create"
    SANDBOX_RESTORE = "sandbox_restore"
    SANDBOX_EXECUTE = "sandbox_execute"
    SANDBOX_EXPORT = "sandbox_export"
    SANDBOX_DELETE = "sandbox_delete"
    CLEANUP = "cleanup"
    RETRY = "retry"
    HUMAN_WAIT = "human_wait"
    REPLAY = "replay"
    COMPACTION = "compaction"
    BACKGROUND_POLL = "background_poll"
    CANCELLATION = "cancellation"
    COMMIT = "commit"


class DurableLoopOutcome(StrEnum):
    """Bounded operation outcomes."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING = "waiting"
    DEDUPLICATED = "deduplicated"
    AMBIGUOUS = "ambiguous"


_meter: Any | None = None
_counter: Any | None = None
_duration: Any | None = None
_ready = False
_PROVENANCE_VALUES = frozenset(
    {
        "apim",
        "apim_429",
        "local",
        "model",
        "per_call",
        "remote",
        "retained_session",
        "runtime",
        "sandbox",
    }
)


def record_durable_loop_event(
    phase: DurableLoopPhase,
    outcome: DurableLoopOutcome,
    *,
    provenance: str | None = None,
    duration_seconds: float | None = None,
) -> None:
    """Emit bounded span progress and matching low-cardinality metrics."""
    attributes: dict[str, str | float] = {
        "phase": phase.value,
        "outcome": outcome.value,
    }
    metric_attributes: dict[str, str] = {
        "phase": phase.value,
        "outcome": outcome.value,
    }
    if provenance is not None:
        if provenance not in _PROVENANCE_VALUES:
            raise ValueError("durable-loop provenance is not a bounded value")
        metric_attributes["provenance"] = provenance
        attributes["provenance"] = provenance
    if duration_seconds is not None:
        if not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError("durable-loop duration must be non-negative and finite")
        attributes["duration_ms"] = duration_seconds * 1000.0
    current_span().add_event("durable_loop.progress", attributes)
    _ensure_instruments()
    if _counter is not None:
        _counter.add(1, metric_attributes)
    if _duration is not None and duration_seconds is not None:
        _duration.record(duration_seconds, metric_attributes)


class DurableLoopTimer:
    """Small helper for consistent content-free duration events."""

    __slots__ = ("_phase", "_provenance", "_started_at")

    def __init__(
        self,
        phase: DurableLoopPhase,
        *,
        provenance: str | None = None,
    ) -> None:
        self._phase = phase
        self._provenance = provenance
        self._started_at = time.perf_counter()

    def finish(self, outcome: DurableLoopOutcome) -> None:
        """Record the elapsed duration with one terminal outcome."""
        record_durable_loop_event(
            self._phase,
            outcome,
            provenance=self._provenance,
            duration_seconds=max(0.0, time.perf_counter() - self._started_at),
        )


def _ensure_instruments() -> None:
    global _counter, _duration, _meter, _ready
    if _ready:
        return
    _ready = True
    try:
        from agent_framework.observability import get_meter

        _meter = get_meter()
        _counter = _meter.create_counter(
            "azure_functions_agents.durable_loop.operations"
        )
        _duration = _meter.create_histogram(
            "azure_functions_agents.durable_loop.duration",
            unit="s",
        )
    except Exception:
        _meter = None
        _counter = None
        _duration = None
