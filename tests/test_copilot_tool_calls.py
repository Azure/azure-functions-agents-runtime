from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from copilot.session_events import (
    ToolExecutionCompleteData,
    ToolExecutionCompleteError,
    ToolExecutionCompleteResult,
    ToolExecutionCompleteToolDescription,
    ToolExecutionStartData,
)
from copilot.tools import ToolInvocation, ToolResult
from pydantic import BaseModel

import azure_functions_agents._observability as observability
from azure_functions_agents._agent_run_diagnostics import _tool_error_count
from azure_functions_agents.harness.copilot_sdk._copilot_tool_calls import (
    CopilotToolCalls,
    _timestamp_ns,
    tool_result_text,
)


class _JsonResult(BaseModel):
    value: str


class _DatedJsonResult(BaseModel):
    observed_on: date
    observed_at: datetime


class _DictionaryResult:
    def to_dict(self):
        return {"value": "structured", "items": [1, True, None]}


class _TextResult:
    text = "ordinary custom text"


def test_timestamp_ns_uses_exact_integer_epoch_conversion() -> None:
    moment = datetime(2042, 3, 4, 5, 6, 7, 890123, tzinfo=UTC)

    assert _timestamp_ns(moment) == 2_277_522_367_890_123_000


@pytest.mark.parametrize(
    "value",
    [
        _DictionaryResult(), _TextResult(),
        {"nested": _DictionaryResult()},
        [_TextResult(), {"nested": _DictionaryResult()}],
    ],
)
def test_ordinary_custom_results_match_public_maf_default_parser(value):
    from agent_framework import FunctionTool

    expected = FunctionTool.parse_result(value)
    assert len(expected) == 1
    assert tool_result_text(value) == expected[0].text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (float("nan"), "NaN"),
        (float("inf"), "Infinity"),
        (float("-inf"), "-Infinity"),
        ({"values": [float("nan"), float("inf")]}, '{"values": [NaN, Infinity]}'),
        ("ordinary text", "ordinary text"),
        ({"message": "café", "values": [1, True, None]}, '{"message": "caf\\u00e9", "values": [1, true, null]}'),
        ([1, {"ok": True}], '[1, {"ok": true}]'),
        (_JsonResult(value="ok"), '{"value": "ok"}'),
        ({"nested": _JsonResult(value="ok")}, '{"nested": {"value": "ok"}}'),
        (date(2026, 10, 2), '"2026-10-02"'),
        (datetime(2026, 10, 2, 1, 2, 3, tzinfo=UTC), '"2026-10-02 01:02:03+00:00"'),
        (
            _DatedJsonResult(
                observed_on=date(2026, 10, 2),
                observed_at=datetime(2026, 10, 2, 1, 2, 3, tzinfo=UTC),
            ),
            '{"observed_on": "2026-10-02", "observed_at": "2026-10-02 01:02:03+00:00"}',
        ),
    ],
)
def test_ordinary_results_preserve_text_and_json_shape(value, expected):
    assert tool_result_text(value) == expected


def test_sdk_results_use_the_same_python_conversion_as_other_values():
    from agent_framework import Content

    from azure_functions_agents.harness.copilot_sdk._tool_result import (
        tool_result_text as ordinary_conversion,
    )

    values = [
        Content.from_text("SDK-owned content"),
        ToolResult(text_result_for_llm="SDK-owned content"),
        [Content.from_text("SDK-owned content")],
        {"nested": ToolResult(text_result_for_llm="SDK-owned content")},
    ]
    for value in values:
        assert tool_result_text(value) == ordinary_conversion(value)


def _start(call_id="native-1", name="skill", arguments=None, **kwargs):
    return ToolExecutionStartData(
        tool_call_id=call_id, tool_name=name, arguments=arguments, **kwargs
    )


def _complete(call_id="native-1", content="ok", *, success=True, **kwargs):
    return ToolExecutionCompleteData(
        tool_call_id=call_id,
        success=success,
        result=ToolExecutionCompleteResult(content=content),
        **kwargs,
    )


@pytest.mark.parametrize("name", ["skill", "view", "bash", "remote_lookup"])
def test_native_start_and_completion_are_one_public_call(name):
    recorded = CopilotToolCalls()
    recorded.start_native(_start(name=name, arguments={"value": "safe"}))
    recorded.complete_native(_complete(content='{"ok":true}'))

    assert recorded.calls == [{
        "type": "tool_start",
        "tool_call_id": "native-1",
        "tool_name": name,
        "arguments": {"value": "safe"},
        "result": '{"ok":true}',
        "success": True,
    }]
    assert _tool_error_count(recorded.calls) == 0


def test_duplicate_native_events_do_not_repeat_or_overwrite_the_call():
    recorded = CopilotToolCalls()
    recorded.start_native(_start(name="bash", arguments={"command": "first"}))
    recorded.start_native(_start(name="view", arguments={"command": "duplicate"}))
    recorded.complete_native(_complete(content="first result"))
    recorded.complete_native(_complete(content="duplicate failure", success=False))

    assert len(recorded.calls) == 1
    assert recorded.calls[0]["tool_name"] == "bash"
    assert recorded.calls[0]["arguments"] == {"command": "first"}
    assert recorded.calls[0]["result"] == "first result"


def test_completion_before_start_keeps_the_result_and_fills_in_start_fields():
    recorded = CopilotToolCalls()
    recorded.complete_native(_complete(content="completed first"))
    recorded.start_native(_start(name="view", arguments={"path": "reference.txt"}))
    recorded.complete_native(_complete(content="duplicate"))

    assert len(recorded.calls) == 1
    assert recorded.calls[0]["tool_name"] == "view"
    assert recorded.calls[0]["arguments"] == {"path": "reference.txt"}
    assert recorded.calls[0]["result"] == "completed first"


@pytest.mark.parametrize("include_description", [False, True])
def test_completion_only_is_counted_without_inventing_missing_fields(include_description):
    recorded = CopilotToolCalls()
    description = (
        ToolExecutionCompleteToolDescription(name="remote_lookup")
        if include_description else None
    )
    recorded.complete_native(_complete(tool_description=description))

    assert len(recorded.calls) == 1
    assert recorded.calls[0]["tool_name"] == ("remote_lookup" if include_description else None)
    assert recorded.calls[0]["arguments"] is None
    assert recorded.calls[0]["result"] == "ok"


def test_native_failures_and_denials_use_the_existing_error_result_accounting():
    recorded = CopilotToolCalls()
    recorded.complete_native(_complete(
        call_id="denied",
        success=False,
        content="private native output",
        error=ToolExecutionCompleteError(message="Authorization: private-token"),
        mcp_meta={"credential": "private-token"},
        tool_telemetry={"reasoning": "hidden"},
    ))
    recorded.complete_native(_complete(call_id="denied", success=False))
    recorded.start_native(_start(call_id="failed", name="remote_lookup"))
    recorded.complete_native(_complete(call_id="failed", success=False))

    assert len(recorded.calls) == 2
    assert _tool_error_count(recorded.calls) == 2
    assert all(json.loads(call["result"])["error"] for call in recorded.calls)
    assert "private" not in repr(recorded.calls)
    assert "reasoning" not in repr(recorded.calls)
    assert set(recorded.calls[0]) == {
        "type", "tool_call_id", "tool_name", "arguments", "result", "success",
    }
    assert all(call["success"] is False for call in recorded.calls)


def test_native_results_project_only_public_text_not_envelopes_or_hidden_content():
    recorded = CopilotToolCalls()
    recorded.complete_native(ToolExecutionCompleteData(
        tool_call_id="mcp",
        success=True,
        result=ToolExecutionCompleteResult(
            content='{"answer":"public"}',
            detailed_content="hidden detail",
            mcp_meta={"Authorization": "secret"},
            structured_content={"not": "the public text"},
        ),
        tool_telemetry={"reasoning": "hidden"},
    ))

    assert recorded.calls[0]["result"] == '{"answer":"public"}'
    assert "hidden" not in repr(recorded.calls)
    assert "secret" not in repr(recorded.calls)


def test_configured_headers_and_credentials_are_redacted_from_native_records():
    recorded = CopilotToolCalls()
    recorded.protect_headers({
        "Authorization": "Bearer credential-sentinel",
        "X-API-Key": "api-key-sentinel",
    })
    recorded.start_native(_start(
        name="remote_lookup",
        arguments={"nested": ["credential-sentinel", {"header": "api-key-sentinel"}]},
    ))
    recorded.complete_native(_complete(
        content="Bearer credential-sentinel / api-key-sentinel"
    ))

    assert "credential-sentinel" not in repr(recorded.calls)
    assert "api-key-sentinel" not in repr(recorded.calls)
    assert recorded.calls[0]["result"] == "[REDACTED] / [REDACTED]"


def test_header_redaction_preserves_json_numbers_and_result_structure():
    recorded = CopilotToolCalls()
    recorded.protect_headers({
        "X-Version": "1",
        "Authorization": "Bearer credential-sentinel",
    })
    recorded.complete_native(_complete(
        content='{"count": 10, "values": [1, true, null], "credential": "credential-sentinel"}'
    ))

    assert json.loads(recorded.calls[0]["result"]) == {
        "count": 10,
        "values": [1, True, None],
        "credential": "[REDACTED]",
    }
    assert _tool_error_count(recorded.calls) == 0


def test_json_result_format_is_unchanged_when_no_redaction_is_needed():
    recorded = CopilotToolCalls()
    content = '{ "count" : 10,\n  "message": "ordinary" }'
    recorded.complete_native(_complete(content=content))

    assert recorded.calls[0]["result"] == content


def test_redaction_preserves_existing_json_error_counting():
    recorded = CopilotToolCalls()
    recorded.protect_headers({"X-Version": "1", "Authorization": "Bearer credential-sentinel"})
    recorded.complete_native(_complete(
        content='{"error": 1, "credential": "credential-sentinel"}'
    ))

    assert json.loads(recorded.calls[0]["result"]) == {
        "error": 1, "credential": "[REDACTED]",
    }
    assert _tool_error_count(recorded.calls) == 1


@pytest.mark.parametrize("native_first", [False, True])
def test_custom_wrapper_owns_its_record_and_deduplicates_native_events(native_first):
    recorded = CopilotToolCalls()
    invocation = ToolInvocation(
        tool_call_id="custom",
        tool_name="host_tool",
        arguments={"host": "arguments"},
    )
    if native_first:
        recorded.start_native(_start(call_id="custom", name="host_tool"))
        recorded.complete_native(_complete(call_id="custom", success=False))
    recorded.start_custom(invocation)
    recorded.complete_custom("custom", '{"host":"result"}')
    recorded.start_native(_start(call_id="custom", name="host_tool"))
    recorded.complete_native(_complete(call_id="custom", content="native duplicate", success=False))

    assert recorded.calls == [{
        "type": "tool_start",
        "tool_call_id": "custom",
        "tool_name": "host_tool",
        "arguments": {"host": "arguments"},
        "result": '{"host":"result"}',
    }]
    assert _tool_error_count(recorded.calls) == 0


def test_start_only_preserves_an_uncompleted_call_and_first_seen_order():
    recorded = CopilotToolCalls()
    recorded.start_native(_start(call_id="first", name="view"))
    recorded.complete_native(_complete(call_id="second"))
    recorded.start_native(_start(call_id="first", name="view"))

    assert [call["tool_call_id"] for call in recorded.calls] == ["first", "second"]
    assert "result" not in recorded.calls[0]
    assert recorded.calls[1]["result"] == "ok"


@pytest.mark.parametrize("mcp_metadata", [False, True])
def test_native_tool_pair_creates_one_explicitly_parented_span(monkeypatch, mcp_metadata):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-tool-calls-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=2)
    start_event_id = uuid4()
    end_event_id = uuid4()
    parent = tracer.start_span("agent.run main")

    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
            agent_name="main",
            session_id="session-123",
        )
        calls.start_native(
            _start(
                name="remote_lookup",
                **(
                    {"mcp_server_name": "fixture-server", "mcp_tool_name": "lookup"}
                    if mcp_metadata
                    else {}
                ),
            ),
            event_id=start_event_id,
            timestamp=start,
        )
        calls.complete_native(
            _complete(),
            event_id=end_event_id,
            timestamp=end,
        )
        calls.complete_native(
            _complete(content="duplicate"),
            event_id=end_event_id,
            timestamp=end,
        )
        calls.finish_telemetry()

    tool_spans = [
        span for span in exporter.get_finished_spans()
        if span.name == "execute_tool remote_lookup"
    ]
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    start_delta = start - epoch
    end_delta = end - epoch
    expected_start_ns = (
        (start_delta.days * 86_400 + start_delta.seconds) * 1_000_000_000
        + start_delta.microseconds * 1_000
    )
    expected_end_ns = (
        (end_delta.days * 86_400 + end_delta.seconds) * 1_000_000_000
        + end_delta.microseconds * 1_000
    )
    assert len(tool_spans) == 1
    assert tool_spans[0].parent.span_id != 0
    assert tool_spans[0].start_time == expected_start_ns
    assert tool_spans[0].end_time == expected_end_ns
    assert tool_spans[0].attributes["gen_ai.operation.name"] == "execute_tool"
    assert tool_spans[0].attributes["gen_ai.tool.call.id"] == "native-1"
    assert tool_spans[0].attributes["af.tool.source"] == "native"
    assert tool_spans[0].attributes["gen_ai.agent.name"] == "main"
    assert tool_spans[0].attributes["gen_ai.agent.id"] == "local/main"
    assert tool_spans[0].attributes["gen_ai.conversation.id"] == "session-123"
    assert tool_spans[0].attributes["gen_ai.tool.name"] == "remote_lookup"
    assert tool_spans[0].attributes["af.tool.outcome"] == "success"
    assert "af.tool.name" not in tool_spans[0].attributes
    if mcp_metadata:
        assert tool_spans[0].attributes["mcp.method.name"] == "tools/call"
    else:
        assert "mcp.method.name" not in tool_spans[0].attributes


def test_failed_native_tool_sets_error_status_without_exception_content(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-native-tool-error-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=2)
    parent = tracer.start_span("agent.run main")

    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
            agent_name="main",
            session_id="session-123",
        )
        calls.start_native(
            _start(name="remote_lookup"),
            event_id=uuid4(),
            timestamp=start,
        )
        calls.complete_native(
            _complete(
                success=False,
                content="private failure detail",
                error=ToolExecutionCompleteError(message="private error detail"),
            ),
            event_id=uuid4(),
            timestamp=end,
        )
        calls.finish_telemetry()

    [failed_span] = [
        span for span in exporter.get_finished_spans()
        if span.name == "execute_tool remote_lookup"
    ]
    assert failed_span.attributes["af.tool.outcome"] == "error"
    assert failed_span.attributes["error.type"] == "tool_execution_error"
    assert failed_span.status.status_code.name == "ERROR"
    assert failed_span.status.description == "Native tool failed or was denied."
    assert failed_span.attributes["af.fault_domain"] == "unknown"
    assert all(event.name != "exception" for event in failed_span.events)
    assert "private failure detail" not in repr(failed_span.events)
    assert "private error detail" not in repr(failed_span.events)


@pytest.mark.parametrize(
    ("start_event_id", "start_time", "end_event_id", "end_time", "reason"),
    [
        (
            None, datetime(2026, 1, 1, tzinfo=UTC), uuid4(),
            datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC), "missing_event_id",
        ),
        (
            uuid4(), datetime(2026, 1, 1), uuid4(),
            datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC), "invalid_interval",
        ),
        (
            uuid4(), datetime(2026, 1, 1, tzinfo=UTC), uuid4(),
            datetime(2025, 12, 31, tzinfo=UTC), "invalid_interval",
        ),
    ],
)
def test_invalid_native_timing_emits_incomplete_metadata_not_a_span(
    monkeypatch, start_event_id, start_time, end_event_id, end_time, reason,
):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-tool-calls-invalid-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    parent = tracer.start_span("agent.run main")

    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
        )
        calls.start_native(
            _start(name="remote_lookup"),
            event_id=start_event_id,
            timestamp=start_time,
        )
        calls.complete_native(
            _complete(),
            event_id=end_event_id,
            timestamp=end_time,
        )
        calls.finish_telemetry()

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["agent.run main"]
    [incomplete] = spans[0].events
    assert incomplete.name == "af.tool.incomplete"
    assert incomplete.attributes["af.tool.incomplete_reason"] == reason


@pytest.mark.parametrize("native_first", [False, True])
def test_custom_tool_wrapper_claims_reserved_native_call_once(monkeypatch, native_first):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-custom-tool-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    parent = tracer.start_span("agent.run main")

    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
            custom_tool_names=("host_tool",),
            agent_name="main",
            session_id="session-123",
        )

        def emit_native_pair():
            start_time = datetime.now(UTC)
            calls.start_native(
                _start(name="host_tool"),
                event_id=uuid4(),
                timestamp=start_time,
            )
            calls.complete_native(
                _complete(),
                event_id=uuid4(),
                timestamp=start_time + timedelta(seconds=1),
            )

        if native_first:
            emit_native_pair()
        invocation = ToolInvocation(
            tool_call_id="native-1",
            tool_name="host_tool",
            arguments={},
        )
        claimed = calls.start_custom(invocation)
        with calls.custom_span(invocation, claimed=claimed) as span:
            span.set_attribute("af.tool.outcome", "success")
        calls.complete_custom("native-1", "safe result", success=True)
        if not native_first:
            emit_native_pair()
        calls.finish_telemetry()

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans].count("execute_tool host_tool") == 1
    [custom_span] = [span for span in spans if span.name == "execute_tool host_tool"]
    assert custom_span.attributes["gen_ai.operation.name"] == "execute_tool"
    assert custom_span.attributes["gen_ai.tool.call.id"] == "native-1"
    assert custom_span.attributes["af.tool.source"] == "wrapper"
    assert custom_span.attributes["gen_ai.tool.name"] == "host_tool"
    assert custom_span.attributes["gen_ai.conversation.id"] == "session-123"
    assert "af.tool.name" not in custom_span.attributes


def test_duplicate_custom_start_claims_one_wrapper_span(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-duplicate-wrapper-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    parent = tracer.start_span("agent.run main")
    invocation = ToolInvocation(
        tool_call_id="same-call",
        tool_name="host_tool",
        arguments={},
    )

    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(parent_context=observability.capture_current_context())
        first_claim = calls.start_custom(invocation)
        with calls.custom_span(invocation, claimed=first_claim):
            pass
        second_claim = calls.start_custom(invocation)
        with calls.custom_span(invocation, claimed=second_claim) as duplicate_span:
            duplicate_span.set_attribute("af.tool.duplicate", True)
        calls.finish_telemetry()

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans].count("execute_tool host_tool") == 1


def test_reused_native_event_id_is_incomplete_not_timed(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-reused-native-event-id-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    event_id = uuid4()
    start = datetime.now(UTC)
    parent = tracer.start_span("agent.run main")

    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
        )
        calls.start_native(
            _start(name="remote_lookup"),
            event_id=event_id,
            timestamp=start,
        )
        calls.complete_native(
            _complete(),
            event_id=event_id,
            timestamp=start + timedelta(seconds=1),
        )
        calls.finish_telemetry()

    [run_span] = exporter.get_finished_spans()
    [incomplete] = run_span.events
    assert incomplete.name == "af.tool.incomplete"
    assert incomplete.attributes["af.tool.incomplete_reason"] == "duplicate_event_id"


def test_duplicate_native_starts_are_incomplete_not_timed(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-duplicate-native-start-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    parent = tracer.start_span("agent.run main")

    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
        )
        calls.start_native(_start(name="remote_lookup"), event_id=uuid4(), timestamp=start)
        calls.start_native(
            _start(name="remote_lookup"),
            event_id=uuid4(),
            timestamp=start + timedelta(milliseconds=10),
        )
        calls.complete_native(
            _complete(),
            event_id=uuid4(),
            timestamp=start + timedelta(seconds=1),
        )
        calls.finish_telemetry()

    [run_span] = exporter.get_finished_spans()
    [incomplete] = run_span.events
    assert incomplete.name == "af.tool.incomplete"
    assert incomplete.attributes["af.tool.incomplete_reason"] == "ambiguous_start_events"


def test_native_start_without_terminal_marks_incomplete_during_cleanup(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-missing-native-terminal-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    parent = tracer.start_span("agent.run main")

    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
        )
        calls.start_native(
            _start(name="remote_lookup"),
            event_id=uuid4(),
            timestamp=datetime.now(UTC),
        )
        calls.finish_telemetry()

    [run_span] = exporter.get_finished_spans()
    [incomplete] = run_span.events
    assert incomplete.name == "af.tool.incomplete"
    assert incomplete.attributes["af.tool.incomplete_reason"] == "missing_completion"


def test_native_completion_before_start_remains_incomplete(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-completion-before-start-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    start = datetime.now(UTC)
    parent = tracer.start_span("agent.run main")

    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
        )
        calls.complete_native(
            _complete(),
            event_id=uuid4(),
            timestamp=start + timedelta(seconds=1),
        )
        calls.start_native(
            _start(name="remote_lookup"),
            event_id=uuid4(),
            timestamp=start,
        )
        calls.finish_telemetry()

    [run_span] = exporter.get_finished_spans()
    [incomplete] = run_span.events
    assert incomplete.name == "af.tool.incomplete"
    assert incomplete.attributes["af.tool.incomplete_reason"] == "invalid_interval"
