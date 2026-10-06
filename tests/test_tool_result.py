from __future__ import annotations

from datetime import date

import pytest
from agent_framework import FunctionTool
from pydantic import BaseModel

from azure_functions_agents._tool_result import tool_result_text


class _MappingResult:
    text = "to_dict takes precedence"

    def to_dict(self):
        return {"value": 1, "observed_on": date(2026, 10, 6)}


class _TextResult:
    text = "custom text"


class _NonTextResult:
    text = 1

    def __str__(self):
        return "ordinary fallback"


class _ModelResult(BaseModel):
    observed_on: date


class _StringResult(str):
    def to_dict(self):
        return {"nested": "string extension"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        None, "text", 1, True, date(2026, 10, 6),
        _MappingResult(), _TextResult(), _NonTextResult(),
        _ModelResult(observed_on=date(2026, 10, 6)),
        [_TextResult(), {"nested": _MappingResult()}],
        {"nested": _ModelResult(observed_on=date(2026, 10, 6))},
        (_MappingResult(), _TextResult()),
        _StringResult("original text"), [_StringResult("nested")],
    ],
)
async def test_shared_ordinary_conversion_matches_actual_maf_invocation(value):
    native = FunctionTool(name="result", description="Result", func=lambda: value)
    parsed = await native.invoke(arguments={})
    assert len(parsed) == 1
    assert tool_result_text(value) == parsed[0].text


def test_adapter_can_reject_nested_unsupported_values_before_conversion():
    forbidden = _MappingResult()
    seen = []

    def validate(value):
        seen.append(value)
        if value is forbidden:
            raise TypeError("unsupported result")

    with pytest.raises(TypeError, match="unsupported result"):
        tool_result_text({"nested": forbidden}, validate_result=validate)
    assert seen == [{"nested": forbidden}, forbidden]
