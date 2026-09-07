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


@pytest.mark.parametrize("duration", (-1.0, float("inf"), float("nan")))
def test_durable_progress_rejects_invalid_duration(duration: float) -> None:
    with pytest.raises(ValueError, match="non-negative and finite"):
        record_durable_loop_event(
            DurableLoopPhase.RUN,
            DurableLoopOutcome.FAILED,
            duration_seconds=duration,
        )
