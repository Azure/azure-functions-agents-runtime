from collections.abc import Mapping

import pytest

from azure_functions_agents.experimental import hybrid_observability
from azure_functions_agents.experimental.hybrid_observability import (
    HybridProgressPhase,
    HybridProgressStatus,
    record_hybrid_progress,
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


def test_progress_event_has_only_bounded_content_free_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = _Span()
    monkeypatch.setattr(hybrid_observability, "current_span", lambda: span)

    record_hybrid_progress(
        HybridProgressPhase.PACKAGE_VERIFY,
        HybridProgressStatus.COMPLETED,
        duration_seconds=0.125,
    )

    assert span.events == [
        (
            "hybrid.progress",
            {
                "duration_ms": 125.0,
                "phase": "package_verify",
                "status": "completed",
            },
        )
    ]


@pytest.mark.parametrize("duration", (-1.0, float("inf"), float("nan")))
def test_progress_event_rejects_invalid_duration(duration: float) -> None:
    with pytest.raises(ValueError, match="non-negative and finite"):
        record_hybrid_progress(
            HybridProgressPhase.CLEANUP_COMPLETE,
            HybridProgressStatus.FAILED,
            duration_seconds=duration,
        )
