"""Copilot SDK event telemetry attached to the active agent invocation."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from ..._agent_identity import agent_id
from ..._observability import (
    ATTR_AGENT_OUTCOME,
    ATTR_GEN_AI_AGENT_ID,
    ATTR_GEN_AI_AGENT_NAME,
    ATTR_GEN_AI_CONVERSATION_ID,
    ATTR_GEN_AI_OPERATION_NAME,
    ATTR_GEN_AI_REQUEST_MODEL,
    ATTR_GEN_AI_TOOL_CALL_ID,
    ATTR_GEN_AI_TOOL_NAME,
    ATTR_GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
    ATTR_GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS,
    ATTR_GEN_AI_USAGE_INPUT_TOKENS,
    ATTR_GEN_AI_USAGE_OUTPUT_TOKENS,
    ATTR_GEN_AI_USAGE_REASONING_OUTPUT_TOKENS,
    ATTR_TOOL_SOURCE,
    FaultDomain,
    GenAiOperationName,
    RuntimeSpan,
    SpanOutcome,
    TelemetryAttributeValue,
    ToolSpanSource,
    current_agent_run_span,
    gen_ai_agent_name,
    safe_telemetry_name,
    start_span,
)

if TYPE_CHECKING:
    from copilot.session_events import AssistantUsageData

    from .._harness_binding import HarnessRequest

_USAGE_EVENT_NAME = "af.agent.model.usage"


def invocation_span_attributes(
    request: HarnessRequest,
) -> dict[str, TelemetryAttributeValue]:
    """Build the semantic invocation attributes from stable runtime identity."""
    return {
        ATTR_GEN_AI_OPERATION_NAME: GenAiOperationName.INVOKE_AGENT,
        ATTR_GEN_AI_AGENT_ID: agent_id(request.agent_slug),
        ATTR_GEN_AI_AGENT_NAME: gen_ai_agent_name(request.agent_slug),
        ATTR_GEN_AI_CONVERSATION_ID: request.session_id,
        ATTR_GEN_AI_REQUEST_MODEL: request.model,
    }


@contextmanager
def invocation_span(request: HarnessRequest) -> Iterator[RuntimeSpan]:
    """Own one semantic execution while preserving runtime observation ownership."""
    runtime_span = current_agent_run_span()
    name = gen_ai_agent_name(request.agent_slug)
    with start_span(
        f"invoke_agent {name}",
        record_exception=False,
        attributes=invocation_span_attributes(request),
    ) as span:
        try:
            yield runtime_span if runtime_span is not None else span
        except asyncio.CancelledError:
            span.set_attribute(ATTR_AGENT_OUTCOME, SpanOutcome.CANCELLED)
            raise
        except Exception as exc:
            span.set_attribute(ATTR_AGENT_OUTCOME, SpanOutcome.ERROR)
            span.record_exception(exc, fault_domain=FaultDomain.RUNTIME, infer_error_type=True)
            raise
        else:
            span.set_attribute(ATTR_AGENT_OUTCOME, SpanOutcome.SUCCESS)


def _duration_ms(value: timedelta | None) -> float | None:
    return value.total_seconds() * 1000 if value is not None else None


def tool_span_attributes(
    *,
    tool_name: str | None,
    tool_call_id: str | None,
    tool_source: ToolSpanSource,
    agent_name: str | None = None,
    session_id: str | None = None,
) -> dict[str, TelemetryAttributeValue]:
    """Build Copilot execute_tool attributes without exporting tool content."""
    return {
        ATTR_GEN_AI_OPERATION_NAME: GenAiOperationName.EXECUTE_TOOL,
        ATTR_GEN_AI_TOOL_NAME: tool_name,
        ATTR_GEN_AI_TOOL_CALL_ID: tool_call_id,
        ATTR_GEN_AI_AGENT_NAME: gen_ai_agent_name(agent_name),
        ATTR_GEN_AI_AGENT_ID: agent_id(agent_name) if agent_name else None,
        ATTR_GEN_AI_CONVERSATION_ID: session_id,
        ATTR_TOOL_SOURCE: tool_source,
    }


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
            "af.agent.usage.duration_ms": _duration_ms(usage.duration),
            "af.agent.usage.time_to_first_token_ms": _duration_ms(
                usage.time_to_first_token
            ),
            "af.agent.usage.output_ttft_ms": _duration_ms(usage.output_ttft),
            "af.agent.usage.inter_token_latency_ms": _duration_ms(
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
                "af.skill.invoked",
                {"af.skill.name": safe_name},
            )
