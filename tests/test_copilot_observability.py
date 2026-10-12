from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from copilot.session_events import AssistantUsageData

from azure_functions_agents._observability import RuntimeSpan
from azure_functions_agents.harness.copilot_sdk._copilot_observability import (
    CopilotEventTelemetry,
)


class _RecordingSpan:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object] | None]] = []

    def add_event(self, name: str, *, attributes: dict[str, object] | None = None) -> None:
        self.events.append((name, attributes))


def test_copilot_usage_event_uses_gen_ai_attributes_and_deduplicates() -> None:
    raw_span = _RecordingSpan()
    telemetry = CopilotEventTelemetry(RuntimeSpan(raw_span))
    event_id = uuid4()
    usage = AssistantUsageData(
        model="fixture-model",
        input_tokens=0,
        output_tokens=8,
        reasoning_tokens=3,
        cache_read_tokens=0,
        cache_write_tokens=2,
        duration=timedelta(seconds=1),
        time_to_first_token=None,
        output_ttft=None,
        inter_token_latency=None,
    )

    assert telemetry.observe_usage(event_id, usage) is True
    assert telemetry.observe_usage(event_id, usage) is False

    [event] = raw_span.events
    assert event[0] == "af.copilot.model.usage"
    assert event[1] == {
        "gen_ai.request.model": "fixture-model",
        "gen_ai.usage.input_tokens": 0,
        "gen_ai.usage.output_tokens": 8,
        "gen_ai.usage.reasoning.output_tokens": 3,
        "gen_ai.usage.cache_read.input_tokens": 0,
        "gen_ai.usage.cache_write.input_tokens": 2,
        "af.copilot.usage.duration_ms": 1000,
    }


def test_copilot_skill_event_keeps_only_sanitized_name() -> None:
    raw_span = _RecordingSpan()
    telemetry = CopilotEventTelemetry(RuntimeSpan(raw_span))

    telemetry.observe_skill("review/private skill")

    assert raw_span.events == [
        ("af.copilot.skill.invoked", {"af.skill.name": "review_private_skill"})
    ]
