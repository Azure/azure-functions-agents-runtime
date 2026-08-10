"""Typed semantic traces that omit provider-specific response details."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..strict_json import DuplicateJsonKeyError, decode_json_object

_OMITTED_DATA_FIELDS = frozenset(
    {
        "content",
        "duration",
        "latency",
        "message",
        "model",
        "provider",
        "provider_metadata",
        "reasoning",
        "reasoning_content",
        "text",
        "timestamp",
        "timing",
        "usage",
    }
)


class TraceValidationError(Exception):
    """A conformance trace has an unsupported shape."""


class TraceEvent(BaseModel):
    """One normalized event emitted by a harness run."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    type: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class SemanticTrace(BaseModel):
    """The stable result of normalizing a complete harness trace."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    capabilities: tuple[str, ...] = ()
    events: tuple[TraceEvent, ...]
    terminal_state: str = Field(min_length=1)


def parse_trace(payload: bytes | str | Mapping[str, object]) -> SemanticTrace:
    """Strictly parse one external trace document before semantic normalization."""

    try:
        decoded = _decode_trace(payload)
        return _parse_trace_mapping(decoded)
    except (
        DuplicateJsonKeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValidationError,
    ):
        raise TraceValidationError("Conformance trace is invalid.") from None


def normalize_trace(trace: SemanticTrace) -> SemanticTrace:
    """Drop unstable reasoning, wording, timing, and provider metadata."""

    events = tuple(
        TraceEvent(type=event.type, data=_normalize_data(event.data))
        for event in trace.events
    )
    return SemanticTrace(
        name=trace.name,
        capabilities=tuple(sorted(set(trace.capabilities))),
        events=events,
        terminal_state=trace.terminal_state,
    )


def _decode_trace(payload: bytes | str | Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(payload, Mapping):
        return payload
    return decode_json_object(payload)


def _parse_trace_mapping(payload: Mapping[str, object]) -> SemanticTrace:
    normalized = dict(payload)
    capabilities = normalized.get("capabilities", [])
    events = normalized.get("events")
    if not isinstance(capabilities, list) or not isinstance(events, list):
        raise TypeError("Trace capabilities and events must be JSON arrays.")
    normalized["capabilities"] = tuple(capabilities)
    normalized["events"] = tuple(events)
    return SemanticTrace.model_validate(normalized)


def _normalize_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_data(nested)
            for key, nested in value.items()
            if key.casefold() not in _OMITTED_DATA_FIELDS
        }
    if isinstance(value, list):
        return [_normalize_data(item) for item in value]
    return value
