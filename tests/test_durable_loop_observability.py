from collections.abc import Mapping

import pytest

from azure_functions_agents.experimental import durable_loop_observability
from azure_functions_agents.experimental.durable_loop_observability import (
    DurableLoopOutcome,
    DurableLoopPhase,
    record_durable_loop_event,
)


class _Span:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object] | None]] = []

    def add_event(
        self,
        name: str,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        self.events.append((name, attributes))


def test_durable_progress_is_bounded_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = _Span()
    monkeypatch.setattr(durable_loop_observability, "current_span", lambda: span)

    record_durable_loop_event(
        DurableLoopPhase.TOOL_STEP,
        DurableLoopOutcome.COMPLETED,
        provenance="remote",
        duration_seconds=0.125,
    )

    assert span.events == [
        (
            "durable_loop.progress",
            {
                "duration_ms": 125.0,
                "outcome": "completed",
                "phase": "tool_step",
                "provenance": "remote",
            },
        )
    ]


def test_layer_two_phase_vocabulary_is_fixed_and_content_free() -> None:
    assert {
        DurableLoopPhase.MODEL_START.value,
        DurableLoopPhase.MODEL_POLL.value,
        DurableLoopPhase.MCP_CALL.value,
        DurableLoopPhase.SANDBOX_CAPACITY_WAIT.value,
        DurableLoopPhase.SANDBOX_CREATE.value,
        DurableLoopPhase.SANDBOX_RESTORE.value,
        DurableLoopPhase.SANDBOX_EXECUTE.value,
        DurableLoopPhase.SANDBOX_EXPORT.value,
        DurableLoopPhase.SANDBOX_DELETE.value,
        DurableLoopPhase.CLEANUP.value,
        DurableLoopPhase.RETRY.value,
    } == {
        "model_start",
        "model_poll",
        "mcp_call",
        "sandbox_capacity_wait",
        "sandbox_create",
        "sandbox_restore",
        "sandbox_execute",
        "sandbox_export",
        "sandbox_delete",
        "cleanup",
        "retry",
    }


def test_metrics_use_only_bounded_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_framework.observability

    class Instrument:
        def __init__(self) -> None:
            self.records: list[tuple[float, dict[str, str]]] = []

        def add(self, value: float, attributes: dict[str, str]) -> None:
            self.records.append((value, dict(attributes)))

        def record(self, value: float, attributes: dict[str, str]) -> None:
            self.records.append((value, dict(attributes)))

    class Meter:
        def __init__(self) -> None:
            self.counter = Instrument()
            self.duration = Instrument()

        def create_counter(self, _name: str) -> Instrument:
            return self.counter

        def create_histogram(self, _name: str, *, unit: str) -> Instrument:
            assert unit == "s"
            return self.duration

    meter = Meter()
    span = _Span()
    monkeypatch.setattr(durable_loop_observability, "current_span", lambda: span)
    monkeypatch.setattr(
        agent_framework.observability,
        "get_meter",
        lambda: meter,
    )
    monkeypatch.setattr(durable_loop_observability, "_ready", False)
    monkeypatch.setattr(durable_loop_observability, "_meter", None)
    monkeypatch.setattr(durable_loop_observability, "_counter", None)
    monkeypatch.setattr(durable_loop_observability, "_duration", None)

    record_durable_loop_event(
        DurableLoopPhase.MODEL_POLL,
        DurableLoopOutcome.COMPLETED,
        provenance="apim",
        duration_seconds=0.25,
    )

    expected = {
        "outcome": "completed",
        "phase": "model_poll",
        "provenance": "apim",
    }
    assert meter.counter.records == [(1, expected)]
    assert meter.duration.records == [(0.25, expected)]
    with pytest.raises(ValueError, match="bounded"):
        record_durable_loop_event(
            DurableLoopPhase.MODEL_POLL,
            DurableLoopOutcome.FAILED,
            provenance="prompt-secret-run-123",
        )


@pytest.mark.parametrize("duration", (-1.0, float("inf"), float("nan")))
def test_durable_progress_rejects_invalid_duration(duration: float) -> None:
    with pytest.raises(ValueError, match="non-negative and finite"):
        record_durable_loop_event(
            DurableLoopPhase.RUN,
            DurableLoopOutcome.FAILED,
            duration_seconds=duration,
        )
