"""Copilot SDK event telemetry attached to the active agent invocation."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from ..._observability import (
    ATTR_GEN_AI_REQUEST_MODEL,
    ATTR_GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
    ATTR_GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS,
    ATTR_GEN_AI_USAGE_INPUT_TOKENS,
    ATTR_GEN_AI_USAGE_OUTPUT_TOKENS,
    ATTR_GEN_AI_USAGE_REASONING_OUTPUT_TOKENS,
    RuntimeSpan,
    TelemetryAttributeValue,
    safe_telemetry_name,
)

if TYPE_CHECKING:
    from copilot.session_events import AssistantUsageData

_USAGE_EVENT_NAME = "af.copilot.model.usage"


def _duration_ms(value: timedelta | None) -> float | None:
    return value.total_seconds() * 1000 if value is not None else None


class CopilotEventTelemetry:
    """Deduplicate reported usage and emit content-free SDK event metadata."""

    def __init__(self, run_span: RuntimeSpan) -> None:
        self._run_span = run_span
        self._usage_event_ids: set[UUID] = set()

    def observe_usage(self, event_id: UUID, usage: AssistantUsageData) -> bool:
        """Emit each public usage event once within this invocation.

        Return ``False`` for a repeated event ID. Observation deduplication does
        not change invocation-counter accumulation.
        """
        if event_id in self._usage_event_ids:
            return False
        self._usage_event_ids.add(event_id)
        attributes: dict[str, TelemetryAttributeValue] = {
            ATTR_GEN_AI_REQUEST_MODEL: usage.model,
            ATTR_GEN_AI_USAGE_INPUT_TOKENS: usage.input_tokens,
            ATTR_GEN_AI_USAGE_OUTPUT_TOKENS: usage.output_tokens,
            ATTR_GEN_AI_USAGE_REASONING_OUTPUT_TOKENS: usage.reasoning_tokens,
            ATTR_GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS: usage.cache_read_tokens,
            ATTR_GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS: usage.cache_write_tokens,
            "af.copilot.usage.duration_ms": _duration_ms(usage.duration),
            "af.copilot.usage.time_to_first_token_ms": _duration_ms(
                usage.time_to_first_token
            ),
            "af.copilot.usage.output_ttft_ms": _duration_ms(usage.output_ttft),
            "af.copilot.usage.inter_token_latency_ms": _duration_ms(
                usage.inter_token_latency
            ),
        }
        self._run_span.add_event(_USAGE_EVENT_NAME, attributes)
        return True

    def observe_skill(self, name: str) -> None:
        """Record only the sanitized skill name, never its prompt or file contents."""
        safe_name = safe_telemetry_name(name)
        if safe_name:
            self._run_span.add_event(
                "af.copilot.skill.invoked",
                {"af.skill.name": safe_name},
            )
