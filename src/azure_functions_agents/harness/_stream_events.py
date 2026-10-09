"""Typed SSE event payloads shared by streaming harness implementations."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class SessionEvent(TypedDict):
    type: Literal["session"]
    session_id: str


class ContentEvent(TypedDict):
    type: Literal["delta", "intermediate", "message", "error"]
    content: str


class StreamTruncatedEvent(TypedDict):
    type: Literal["stream_truncated"]
    dropped_events: int


class DoneEvent(TypedDict):
    type: Literal["done"]


class ToolStartEvent(TypedDict):
    type: Literal["tool_start"]
    tool_call_id: str
    tool_name: str | None
    arguments: NotRequired[object]


class ToolEndEvent(TypedDict):
    type: Literal["tool_end"]
    tool_call_id: str
    tool_name: str | None
    result: str


type StreamEvent = (
    SessionEvent
    | ContentEvent
    | StreamTruncatedEvent
    | DoneEvent
    | ToolStartEvent
    | ToolEndEvent
)
