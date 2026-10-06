from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from copilot.tools import ToolResult
from pydantic import BaseModel

from azure_functions_agents.harness.copilot_sdk._copilot_tool_calls import tool_result_text


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
