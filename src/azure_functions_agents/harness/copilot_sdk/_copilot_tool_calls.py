"""Ordinary custom-tool results with explicit rejection of harness SDK values."""

from __future__ import annotations

from ..._tool_descriptor import is_harness_object
from ..._tool_result import tool_result_text as _ordinary_tool_result_text


def _reject_harness_result(value: object) -> None:
    if is_harness_object(value):
        raise TypeError(
            "Copilot custom tools must return ordinary Python values, not harness SDK objects."
        )


def tool_result_text(result: object) -> str:
    """Preserve ordinary text, empty, and JSON tool-result representations."""
    return _ordinary_tool_result_text(result, validate_result=_reject_harness_result)
