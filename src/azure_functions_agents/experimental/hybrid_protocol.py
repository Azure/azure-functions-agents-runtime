"""Strict file-journal protocol for experimental hybrid tool execution."""

from __future__ import annotations

import json
import math
import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..strict_json import DuplicateJsonKeyError, decode_json_object

HYBRID_TOOL_PROTOCOL_VERSION: Literal["1"] = "1"

HYBRID_TOOL_MANIFEST_FILENAME = "manifest.json"
HYBRID_TOOL_READINESS_FILENAME = "ready.json"
HYBRID_TOOL_PID_FILENAME = "pid.json"
HYBRID_TOOL_REQUEST_DIRECTORY = "requests"
HYBRID_TOOL_RESULT_DIRECTORY = "results"
HYBRID_TOOL_SHUTDOWN_FILENAME = "shutdown"

MAX_HYBRID_MANIFEST_BYTES = 1024 * 1024
MAX_HYBRID_REQUEST_BYTES = 1024 * 1024
MAX_HYBRID_RESULT_BYTES = 1024 * 1024
MAX_HYBRID_ARGUMENT_BYTES = 512 * 1024
MAX_HYBRID_VALUE_BYTES = 512 * 1024
MAX_HYBRID_STREAM_TEXT_CHARS = 64 * 1024
MAX_HYBRID_TOOLS = 512
MAX_HYBRID_SCHEMA_BYTES = 128 * 1024

_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TRACEPARENT_PATTERN = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")

HybridProtocolVersion = Literal["1"]


class HybridToolProvenance(StrEnum):
    """Origin of an executable sandbox tool."""

    LOCAL = "local"
    GENERIC = "generic"


class HybridInvocationStatus(StrEnum):
    """Terminal state of a hybrid tool invocation."""

    SUCCESS = "success"
    ERROR = "error"


class HybridInvocationErrorCode(StrEnum):
    """Bounded error categories returned by the sandbox executor."""

    DEADLINE_EXCEEDED = "deadline_exceeded"
    INTERNAL_ERROR = "internal_error"
    INVALID_REQUEST = "invalid_request"
    INVALID_TOOL_RESULT = "invalid_tool_result"
    RESULT_TOO_LARGE = "result_too_large"
    TOOL_ERROR = "tool_error"
    TOOL_TIMEOUT = "tool_timeout"
    UNKNOWN_TOOL = "unknown_tool"


_ToolName = Annotated[str, Field(min_length=1, max_length=128, pattern=_TOOL_NAME_PATTERN.pattern)]
_CallId = Annotated[str, Field(min_length=1, max_length=128, pattern=_CALL_ID_PATTERN.pattern)]
_ShortText = Annotated[str, Field(max_length=2048)]
_StreamText = Annotated[str, Field(max_length=MAX_HYBRID_STREAM_TEXT_CHARS)]
_FiniteMilliseconds = Annotated[float, Field(ge=0.0, le=86_400_000.0, allow_inf_nan=False)]


class HybridProtocolDocumentError(ValueError):
    """An untrusted hybrid protocol document is invalid."""


class _HybridProtocolModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class HybridToolDescriptor(_HybridProtocolModel):
    """One sandbox-local tool exposed to the worker model loop."""

    name: _ToolName
    description: Annotated[str, Field(max_length=8192)]
    parameters: dict[str, object]
    provenance: HybridToolProvenance

    @model_validator(mode="after")
    def validate_parameter_schema(self) -> Self:
        _assert_json_value(self.parameters, "tool parameter schema")
        encoded = canonical_hybrid_json_bytes(self.parameters)
        if len(encoded) > MAX_HYBRID_SCHEMA_BYTES:
            raise ValueError("tool parameter schema exceeds the byte limit")
        if self.parameters.get("type") != "object":
            raise ValueError("tool parameter schema must describe an object")
        return self


class HybridToolManifest(_HybridProtocolModel):
    """Exact versioned inventory discovered inside one sandbox."""

    protocol_version: HybridProtocolVersion
    tools: Annotated[tuple[HybridToolDescriptor, ...], Field(max_length=MAX_HYBRID_TOOLS)]

    @model_validator(mode="after")
    def validate_unique_names(self) -> Self:
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        return self


class HybridToolInvocationRequest(_HybridProtocolModel):
    """One idempotent sandbox-local tool invocation."""

    protocol_version: HybridProtocolVersion
    call_id: _CallId
    tool_name: _ToolName
    arguments: dict[str, object]
    deadline_unix_seconds: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    traceparent: Annotated[str, Field(pattern=_TRACEPARENT_PATTERN.pattern)] | None
    operation_id: Annotated[str, Field(min_length=1, max_length=128)]

    @model_validator(mode="after")
    def validate_arguments(self) -> Self:
        _assert_json_value(self.arguments, "tool arguments")
        if len(canonical_hybrid_json_bytes(self.arguments)) > MAX_HYBRID_ARGUMENT_BYTES:
            raise ValueError("tool arguments exceed the byte limit")
        if self.traceparent is not None:
            version, trace_id, parent_id, _flags = self.traceparent.split("-")
            if version == "ff" or trace_id == "0" * 32 or parent_id == "0" * 16:
                raise ValueError("traceparent contains a forbidden all-zero field")
        return self


class HybridToolInternalTimings(_HybridProtocolModel):
    """Monotonic durations measured by the sandbox executor."""

    queue_wait_ms: _FiniteMilliseconds
    execution_ms: _FiniteMilliseconds
    serialization_ms: _FiniteMilliseconds


class HybridToolInvocationError(_HybridProtocolModel):
    """Bounded machine-readable invocation failure."""

    code: HybridInvocationErrorCode
    message: _ShortText
    retryable: bool


class HybridToolInvocationResult(_HybridProtocolModel):
    """One terminal result preserved for an invocation call identifier."""

    protocol_version: HybridProtocolVersion
    call_id: _CallId
    tool_name: _ToolName
    status: HybridInvocationStatus
    value: object | None
    stdout: _StreamText
    stderr: _StreamText
    exit_code: int | None
    error: HybridToolInvocationError | None
    timings: HybridToolInternalTimings

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> Self:
        _assert_json_value(self.value, "tool result")
        if len(canonical_hybrid_json_bytes(self.value)) > MAX_HYBRID_VALUE_BYTES:
            raise ValueError("tool result exceeds the byte limit")
        if len(self.stdout.encode("utf-8")) > MAX_HYBRID_STREAM_TEXT_CHARS:
            raise ValueError("tool stdout exceeds the byte limit")
        if len(self.stderr.encode("utf-8")) > MAX_HYBRID_STREAM_TEXT_CHARS:
            raise ValueError("tool stderr exceeds the byte limit")
        if self.status is HybridInvocationStatus.SUCCESS and self.error is not None:
            raise ValueError("successful result cannot include an error")
        if self.status is HybridInvocationStatus.ERROR and self.error is None:
            raise ValueError("error result must include an error")
        return self


def canonical_hybrid_json_bytes(value: object) -> bytes:
    """Serialize one JSON-safe value deterministically without non-finite numbers."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    _assert_json_value(value, "JSON value")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def parse_hybrid_tool_manifest(payload: bytes | str) -> HybridToolManifest:
    """Strictly parse one bounded sandbox tool manifest."""
    return _parse_hybrid_document(payload, HybridToolManifest, MAX_HYBRID_MANIFEST_BYTES)


def parse_hybrid_tool_request(payload: bytes | str) -> HybridToolInvocationRequest:
    """Strictly parse one bounded invocation request."""
    return _parse_hybrid_document(payload, HybridToolInvocationRequest, MAX_HYBRID_REQUEST_BYTES)


def parse_hybrid_tool_result(payload: bytes | str) -> HybridToolInvocationResult:
    """Strictly parse one bounded invocation result."""
    return _parse_hybrid_document(payload, HybridToolInvocationResult, MAX_HYBRID_RESULT_BYTES)


def _parse_hybrid_document[ModelT: _HybridProtocolModel](
    payload: bytes | str,
    model: type[ModelT],
    maximum_bytes: int,
) -> ModelT:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > maximum_bytes:
        raise HybridProtocolDocumentError("hybrid protocol document exceeds the byte limit")
    try:
        decoded = decode_json_object(raw)
        return model.model_validate_json(canonical_hybrid_json_bytes(decoded))
    except (
        DuplicateJsonKeyError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
    ) as exc:
        raise HybridProtocolDocumentError("invalid hybrid protocol document") from exc
    except ValueError as exc:
        raise HybridProtocolDocumentError("invalid hybrid protocol document") from exc


def _assert_json_value(value: object, kind: str, *, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError(f"{kind} is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{kind} contains a non-finite number")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _assert_json_value(item, kind, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{kind} contains a non-string object key")
            _assert_json_value(item, kind, depth=depth + 1)
        return
    raise ValueError(f"{kind} contains a non-JSON value")
