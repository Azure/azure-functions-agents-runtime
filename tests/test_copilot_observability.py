from __future__ import annotations

import builtins
from datetime import timedelta
from uuid import uuid4

import pytest
from copilot.session_events import AssistantUsageData

from azure_functions_agents._observability import RuntimeSpan
from azure_functions_agents.harness._harness_binding import HarnessRequest
from azure_functions_agents.harness.copilot_sdk._copilot_observability import (
    CopilotEventTelemetry,
    invocation_span,
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
    assert event[0] == "af.agent.model.usage"
    assert event[1] == {
        "gen_ai.request.model": "fixture-model",
        "gen_ai.usage.input_tokens": 0,
        "gen_ai.usage.output_tokens": 8,
        "gen_ai.usage.reasoning.output_tokens": 3,
        "gen_ai.usage.cache_read.input_tokens": 0,
        "gen_ai.usage.cache_write.input_tokens": 2,
        "af.agent.usage.duration_ms": 1000,
    }


def test_copilot_skill_event_keeps_only_sanitized_name() -> None:
    raw_span = _RecordingSpan()
    telemetry = CopilotEventTelemetry(RuntimeSpan(raw_span))

    telemetry.observe_skill("review/private skill")

    assert raw_span.events == [
        ("af.skill.invoked", {"af.skill.name": "review_private_skill"})
    ]


def test_uninstrumented_execution_does_not_require_otel_sdk(monkeypatch) -> None:
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext

    import azure_functions_agents._observability as observability

    original_import = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name == "opentelemetry.sdk" or name.startswith("opentelemetry.sdk."):
            raise ModuleNotFoundError("Optional OpenTelemetry SDK is not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_sdk)
    monkeypatch.setattr(observability, "_enabled", False)
    request = HarnessRequest(
        prompt="private", instructions=None, agent_slug="main", session_id="public",
        new_session=True, model="model", tools=(), max_output_tokens=None, deadline=1,
    )
    caller = NonRecordingSpan(SpanContext(trace_id=1, span_id=2, is_remote=True))
    with trace.use_span(caller), invocation_span(request) as observations:
        observations.add_event("af.skill.invoked", {"af.skill.name": "review"})
    assert trace.get_current_span() is not caller


@pytest.mark.parametrize("failed", [False, True])
def test_semantic_invocation_survives_local_azure_envelope_conversion(monkeypatch, failed):
    from azure.monitor.opentelemetry.exporter.export.trace._exporter import (
        _convert_span_to_envelope,
    )
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import azure_functions_agents._observability as observability

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-envelope-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    monkeypatch.setenv("WEBSITE_SITE_NAME", "Envelope-App")
    request = HarnessRequest(
        prompt="private", instructions=None, agent_slug="main", session_id="public",
        new_session=True, model="model", tools=(), max_output_tokens=None, deadline=1,
    )
    try:
        with tracer.start_as_current_span("caller") as caller:
            try:
                with invocation_span(request):
                    if failed:
                        raise ValueError("synthetic failure")
            except ValueError:
                pass
        invocation = next(
            span for span in exporter.get_finished_spans() if span.name.startswith("invoke_agent ")
        )
        envelope = _convert_span_to_envelope(invocation)
        data = envelope.data.base_data
        assert envelope.data.base_type == "RemoteDependencyData"
        assert data.name == "invoke_agent Envelope-App/main"
        assert data.id == format(invocation.context.span_id, "016x")
        assert envelope.tags["ai.operation.id"] == format(caller.get_span_context().trace_id, "032x")
        assert envelope.tags["ai.operation.parentId"] == format(caller.get_span_context().span_id, "016x")
        assert data.success is not failed
        assert data.properties["gen_ai.operation.name"] == "invoke_agent"
        assert data.properties["gen_ai.agent.id"] == "envelope-app/main"
        assert data.properties["gen_ai.conversation.id"] == "public"
        assert data.properties["gen_ai.request.model"] == "model"
        assert "private" not in repr(data.properties)
        if failed:
            assert invocation.attributes["error.type"] == "ValueError"
            assert data.properties["af.fault_domain"] == "runtime"
    finally:
        provider.shutdown()
