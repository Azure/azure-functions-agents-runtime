"""One public tool-call record per SDK invocation, including native failures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..._tool_result import tool_result_text as _ordinary_tool_result_text

if TYPE_CHECKING:
    from copilot.session_events import ToolExecutionCompleteData, ToolExecutionStartData
    from copilot.tools import ToolInvocation

    from ...runner import ToolCallEvidence


_NATIVE_TOOL_FAILURE = '{"error":"Native tool failed or was denied."}'
_REDACTED = "[REDACTED]"
_HARNESS_RESULT_MODULES = ("agent_framework", "copilot")


def _reject_harness_result(value: Any) -> None:
    if any(
        base.__module__ == module or base.__module__.startswith(f"{module}.")
        for base in type(value).__mro__
        for module in _HARNESS_RESULT_MODULES
    ):
        raise TypeError(
            "Copilot custom tools must return ordinary Python values, not harness SDK objects."
        )


def tool_result_text(result: Any) -> str:
    """Preserve ordinary text, empty, and JSON tool-result representations."""
    return _ordinary_tool_result_text(result, validate_result=_reject_harness_result)


@dataclass
class _ToolCall:
    tool_call_id: str
    tool_name: str | None = None
    arguments: Any = None
    result: str | None = None
    custom: bool = False
    started: bool = False
    completed: bool = False
    success: bool | None = None

    def public_record(self) -> ToolCallEvidence:
        record: ToolCallEvidence = {
            "type": "tool_start",
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
        }
        if self.result is not None:
            record["result"] = self.result
        if self.success is not None:
            record["success"] = self.success
        return record


class CopilotToolCalls:
    """Correlate native events and custom wrappers without counting metadata."""

    def __init__(self) -> None:
        self._records: dict[str, _ToolCall] = {}
        self._protected_values: set[str] = set()

    @property
    def calls(self) -> list[ToolCallEvidence]:
        return [record.public_record() for record in self._records.values()]

    def protect_headers(self, headers: dict[str, str]) -> None:
        """Exclude configured header values and bearer credentials from native records."""
        for name, value in headers.items():
            if value:
                self._protected_values.add(value)
            if name.casefold() == "authorization":
                _scheme, _separator, credential = value.partition(" ")
                if credential:
                    self._protected_values.add(credential)

    def _redacted_text(self, value: str) -> str:
        for protected in sorted(self._protected_values, key=len, reverse=True):
            value = value.replace(protected, _REDACTED)
        return value

    def _redacted(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redacted_text(value)
        if isinstance(value, dict):
            return {key: self._redacted(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redacted(item) for item in value]
        return value

    def _redacted_result(self, content: str) -> str:
        try:
            decoded = json.loads(content)
        except (ValueError, RecursionError):
            return self._redacted_text(content)
        redacted = self._redacted(decoded)
        if redacted == decoded:
            return content
        return json.dumps(redacted, ensure_ascii=False)

    def _record(self, tool_call_id: str) -> _ToolCall:
        record = self._records.get(tool_call_id)
        if record is None:
            record = _ToolCall(tool_call_id)
            self._records[tool_call_id] = record
        return record

    def start_custom(self, invocation: ToolInvocation) -> None:
        record = self._record(invocation.tool_call_id)
        record.tool_name = invocation.tool_name
        record.arguments = invocation.arguments
        record.result = None
        record.custom = True
        record.started = True

    def complete_custom(
        self, tool_call_id: str, result: str, *, success: bool | None = None
    ) -> None:
        record = self._record(tool_call_id)
        record.result = result
        record.completed = True
        record.success = success

    def start_native(self, data: ToolExecutionStartData) -> None:
        record = self._record(data.tool_call_id)
        if record.custom or record.started:
            return
        record.tool_name = data.tool_name
        record.arguments = self._redacted(data.arguments)
        record.started = True

    def complete_native(self, data: ToolExecutionCompleteData) -> None:
        record = self._record(data.tool_call_id)
        if record.custom or record.completed:
            return
        if record.tool_name is None and data.tool_description is not None:
            record.tool_name = data.tool_description.name
        result = data.result.content if data.result is not None else ""
        record.result = (
            _NATIVE_TOOL_FAILURE
            if not data.success or data.error is not None
            else self._redacted_result(result)
        )
        record.completed = True
        record.success = data.success and data.error is None
