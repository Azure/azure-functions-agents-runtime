"""Copilot SDK event telemetry attached to the active agent invocation."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from ..._agent_identity import agent_id
from ..._observability import (
    ATTR_AGENT_MODEL,
    ATTR_AGENT_SESSION_ID,
    ATTR_GEN_AI_AGENT_ID,
    ATTR_GEN_AI_AGENT_NAME,
    ATTR_GEN_AI_CONVERSATION_ID,
    ATTR_GEN_AI_OPERATION_NAME,
    ATTR_GEN_AI_PROVIDER_NAME,
    ATTR_GEN_AI_REQUEST_MODEL,
    ATTR_GEN_AI_TOOL_CALL_ID,
    ATTR_GEN_AI_TOOL_NAME,
    ATTR_GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
    ATTR_GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS,
    ATTR_GEN_AI_USAGE_INPUT_TOKENS,
    ATTR_GEN_AI_USAGE_OUTPUT_TOKENS,
    ATTR_GEN_AI_USAGE_REASONING_OUTPUT_TOKENS,
    ATTR_TOOL_SOURCE,
    GenAiOperationName,
    RuntimeSpan,
    TelemetryAttributeValue,
    ToolSpanSource,
    gen_ai_agent_name,
    safe_telemetry_name,
)
from ..._observability import (
    agent_run_attributes as common_agent_run_attributes,
)
from .._provider_config import ProviderKind

if TYPE_CHECKING:
    from copilot.session_events import AssistantUsageData

_USAGE_EVENT_NAME = "af.copilot.model.usage"


def _duration_ms(value: timedelta | None) -> float | None:
    return value.total_seconds() * 1000 if value is not None else None


def _gen_ai_provider_name(provider_kind: ProviderKind | None) -> str | None:
    """Return the pinned semconv provider value when the runtime knows it exactly."""
    match provider_kind:
        case ProviderKind.OPENAI:
            return "openai"
        case ProviderKind.AZURE_OPENAI:
            return "azure.ai.openai"
        case _:
            return None


def agent_run_span_name(*, agent_name: str | None) -> str:
    """Return the Copilot runtime invocation span name."""
    name = gen_ai_agent_name(agent_name)
    return f"invoke_agent {name}" if name else GenAiOperationName.INVOKE_AGENT


def agent_run_attributes(
    *,
    agent_name: str | None,
    display_name: str | None,
    trigger_type: str | None,
    session_id: str | None,
    model: str | None,
    provider_kind: ProviderKind | None,
    gen_ai_default: str | None = None,
) -> dict[str, TelemetryAttributeValue]:
    """Build Copilot invocation attributes using standard GenAI fields where they match."""
    attributes = common_agent_run_attributes(
        agent_name=agent_name,
        display_name=display_name,
        trigger_type=trigger_type,
        session_id=None,
        model=None,
    )
    attributes.pop(ATTR_AGENT_SESSION_ID, None)
    attributes.pop(ATTR_AGENT_MODEL, None)
    return {
        **attributes,
        ATTR_GEN_AI_OPERATION_NAME: GenAiOperationName.INVOKE_AGENT,
        ATTR_GEN_AI_PROVIDER_NAME: _gen_ai_provider_name(provider_kind),
        ATTR_GEN_AI_CONVERSATION_ID: session_id,
        ATTR_GEN_AI_REQUEST_MODEL: model,
        ATTR_GEN_AI_AGENT_NAME: gen_ai_agent_name(agent_name, default=gen_ai_default),
        ATTR_GEN_AI_AGENT_ID: agent_id(agent_name) if agent_name else None,
    }


def set_agent_session_attribute(span: RuntimeSpan, *, session_id: str | None) -> None:
    """Set Copilot's standard conversation attribute on one invocation span."""
    span.set_attribute(ATTR_GEN_AI_CONVERSATION_ID, session_id)


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
