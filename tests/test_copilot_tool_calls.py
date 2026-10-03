from __future__ import annotations

import json
from datetime import UTC, date, datetime

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

from azure_functions_agents._copilot_tool_calls import CopilotToolCalls, tool_result_text
from azure_functions_agents.registration._handlers import _tool_error_count


class _JsonResult(BaseModel):
    value: str


class _DatedJsonResult(BaseModel):
    observed_on: date
    observed_at: datetime


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
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


def test_harness_sdk_results_are_rejected_instead_of_flattened():
    from agent_framework import Content

    values = [
        Content.from_text("SDK-owned content"),
        ToolResult(text_result_for_llm="SDK-owned content"),
        [Content.from_text("SDK-owned content")],
        {"nested": ToolResult(text_result_for_llm="SDK-owned content")},
    ]
    for value in values:
        with pytest.raises(TypeError, match="SDK objects"):
            tool_result_text(value)


def _start(call_id="native-1", name="skill", arguments=None):
    return ToolExecutionStartData(
        tool_call_id=call_id, tool_name=name, arguments=arguments
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
        "type", "tool_call_id", "tool_name", "arguments", "result",
    }


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
