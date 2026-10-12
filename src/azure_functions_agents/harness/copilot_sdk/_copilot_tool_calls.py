"""One public tool-call record per SDK invocation, including native failures."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..._observability import (
    ATTR_GEN_AI_TOOL_NAME,
    ATTR_MCP_METHOD_NAME,
    MCP_METHOD_TOOLS_CALL,
    LifecycleStage,
    RuntimeSpan,
    record_timed_span,
    start_span,
)
from ..._tool_descriptor import is_harness_object
from .._stream_events import ToolEndEvent, ToolStartEvent
from ._tool_result import tool_result_text as _ordinary_tool_result_text

if TYPE_CHECKING:
    from copilot.session_events import ToolExecutionCompleteData, ToolExecutionStartData
    from copilot.tools import ToolInvocation
    from opentelemetry.context import Context

    from ...runner import ToolCallEvidence


_NATIVE_TOOL_FAILURE = '{"error":"Native tool failed or was denied."}'
_REDACTED = "[REDACTED]"
_METADATA_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _safe_name(value: str | None) -> str | None:
    if not value:
        return None
    return _METADATA_NAME_PATTERN.sub("_", value)[:128]


def _timestamp_ns(value: datetime | None) -> int | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    delta = value.astimezone(UTC) - _EPOCH
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _reject_harness_result(value: object) -> None:
    if is_harness_object(value):
        raise TypeError(
            "Copilot custom tools must return ordinary Python values, not harness SDK objects."
        )


def tool_result_text(result: object) -> str:
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
    start_emitted: bool = False
    end_emitted: bool = False
    telemetry_reserved: bool = False
    telemetry_custom_started: bool = False
    telemetry_native_start_id: UUID | None = None
    telemetry_native_start_ns: int | None = None
    telemetry_native_start_ambiguous: bool = False
    telemetry_native_complete: bool = False
    telemetry_native_event_ids: set[UUID] = field(default_factory=set)
    telemetry_incomplete_emitted: bool = False
    telemetry_mcp: bool = False

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

    def __init__(
        self,
        *,
        parent_context: Context | None = None,
        run_span: RuntimeSpan | None = None,
        custom_tool_names: tuple[str, ...] = (),
    ) -> None:
        self._records: dict[str, _ToolCall] = {}
        self._protected_values: set[str] = set()
        self._parent_context = parent_context
        self._run_span = run_span or RuntimeSpan(None)
        self._custom_tool_names = frozenset(custom_tool_names)

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

    def is_custom(self, tool_call_id: str) -> bool:
        record = self._records.get(tool_call_id)
        return record.custom if record is not None else False

    def start_event(
        self, tool_call_id: str, *, synthesize_arguments: bool = False
    ) -> ToolStartEvent | None:
        record = self._records.get(tool_call_id)
        if record is None or record.start_emitted:
            return None
        record.start_emitted = True
        arguments = record.arguments
        if arguments is None and synthesize_arguments:
            arguments = {}
        return ToolStartEvent(
            type="tool_start",
            tool_call_id=tool_call_id,
            tool_name=record.tool_name,
            arguments=arguments,
        )

    def end_event(self, tool_call_id: str) -> ToolEndEvent | None:
        record = self._records.get(tool_call_id)
        if record is None or not record.completed or record.end_emitted:
            return None
        record.end_emitted = True
        return ToolEndEvent(
            type="tool_end",
            tool_call_id=tool_call_id,
            tool_name=record.tool_name,
            result=record.result or "",
        )

    def start_custom(self, invocation: ToolInvocation) -> bool:
        record = self._record(invocation.tool_call_id)
        record.tool_name = invocation.tool_name
        record.arguments = invocation.arguments
        record.result = None
        record.custom = True
        record.started = True
        record.telemetry_reserved = True
        claimed = not record.telemetry_custom_started
        record.telemetry_custom_started = True
        return claimed

    @contextmanager
    def custom_span(self, invocation: ToolInvocation, *, claimed: bool) -> Iterator[RuntimeSpan]:
        if not claimed:
            yield RuntimeSpan(None)
            return
        name = _safe_name(invocation.tool_name) or "unknown"
        with start_span(
            f"execute_tool {name}",
            lifecycle_stage=LifecycleStage.TOOL_EXECUTION,
            parent_context=self._parent_context,
            record_exception=False,
            attributes={
                ATTR_GEN_AI_TOOL_NAME: name,
                "af.tool.name": name,
                "af.tool.source": "copilot.wrapper",
            },
        ) as span:
            yield span

    def complete_custom(
        self, tool_call_id: str, result: str, *, success: bool | None = None
    ) -> None:
        record = self._record(tool_call_id)
        record.result = result
        record.completed = True
        record.success = success

    def start_native(
        self,
        data: ToolExecutionStartData,
        *,
        event_id: UUID | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        record = self._record(data.tool_call_id)
        record.telemetry_mcp = (
            data.mcp_server_name is not None or data.mcp_tool_name is not None
        )
        if data.tool_name in self._custom_tool_names:
            record.telemetry_reserved = True
        self._observe_native_event(record, event_id)
        if event_id is None and not record.telemetry_reserved:
            self._mark_incomplete(record, "missing_event_id")
        if (
            not record.telemetry_reserved
            and not record.telemetry_native_complete
            and event_id is not None
        ):
            if record.telemetry_native_start_id is None:
                record.telemetry_native_start_id = event_id
                record.telemetry_native_start_ns = _timestamp_ns(timestamp)
            elif record.telemetry_native_start_id != event_id:
                record.telemetry_native_start_ambiguous = True
        if record.custom or record.started:
            return
        record.tool_name = data.tool_name
        record.arguments = self._redacted(data.arguments)
        record.started = True

    def complete_native(
        self,
        data: ToolExecutionCompleteData,
        *,
        event_id: UUID | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        record = self._record(data.tool_call_id)
        if data.tool_description is not None and data.tool_description.name in self._custom_tool_names:
            record.telemetry_reserved = True
        unique_event = self._observe_native_event(record, event_id)
        if event_id is None and not record.telemetry_reserved:
            record.telemetry_native_complete = True
            self._mark_incomplete(record, "missing_event_id")
        elif (
            not unique_event
            and not record.telemetry_reserved
            and not record.telemetry_custom_started
            and not record.telemetry_native_complete
        ):
            record.telemetry_native_complete = True
            self._mark_incomplete(record, "duplicate_event_id")
        if (
            unique_event
            and not record.telemetry_reserved
            and not record.telemetry_custom_started
            and not record.telemetry_native_complete
        ):
            self._record_native_span(record, data, event_id, timestamp)
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

    def finish_telemetry(self) -> None:
        for record in self._records.values():
            if (
                not record.telemetry_reserved
                and not record.telemetry_custom_started
                and record.telemetry_native_start_id is not None
                and not record.telemetry_native_complete
            ):
                reason = (
                    "invalid_interval"
                    if record.telemetry_native_start_ns is None
                    else "missing_completion"
                )
                self._mark_incomplete(record, reason)

    @staticmethod
    def _observe_native_event(record: _ToolCall, event_id: UUID | None) -> bool:
        if event_id is None:
            return False
        if event_id in record.telemetry_native_event_ids:
            return False
        record.telemetry_native_event_ids.add(event_id)
        return True

    def _record_native_span(
        self,
        record: _ToolCall,
        data: ToolExecutionCompleteData,
        event_id: UUID | None,
        timestamp: datetime | None,
    ) -> None:
        record.telemetry_native_complete = True
        if self._parent_context is None:
            return
        start_ns = record.telemetry_native_start_ns
        end_ns = _timestamp_ns(timestamp)
        if record.telemetry_native_start_ambiguous:
            self._mark_incomplete(record, "ambiguous_start_events")
            return
        if (
            record.telemetry_native_start_id is None
            or event_id is None
            or event_id == record.telemetry_native_start_id
            or start_ns is None
            or end_ns is None
            or end_ns <= start_ns
        ):
            self._mark_incomplete(record, "invalid_interval")
            return
        name = _safe_name(record.tool_name) or "unknown"
        attributes: dict[str, Any] = {
            ATTR_GEN_AI_TOOL_NAME: name,
            "af.tool.name": name,
            "af.tool.source": "copilot.native",
            "af.tool.outcome": (
                "success" if data.success and data.error is None else "error"
            ),
        }
        if record.telemetry_mcp:
            attributes[ATTR_MCP_METHOD_NAME] = MCP_METHOD_TOOLS_CALL
        emitted = record_timed_span(
            f"execute_tool {name}",
            parent_context=self._parent_context,
            start_time_ns=start_ns,
            end_time_ns=end_ns,
            lifecycle_stage=LifecycleStage.TOOL_EXECUTION,
            attributes=attributes,
        )
        if not emitted:
            self._mark_incomplete(record, "unavailable_interval")

    def _mark_incomplete(self, record: _ToolCall, reason: str) -> None:
        if record.telemetry_incomplete_emitted:
            return
        record.telemetry_incomplete_emitted = True
        name = _safe_name(record.tool_name)
        self._run_span.add_event(
            "af.copilot.tool.incomplete",
            {
                "af.tool.name": name,
                "af.tool.incomplete_reason": reason,
            },
        )
