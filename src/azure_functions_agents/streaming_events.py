"""Harness-neutral events emitted while an agent runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AgentStreamEventKind(StrEnum):
    """Stable event kinds emitted by harness-neutral agent streaming."""

    SESSION = "session"
    DELTA = "delta"
    MESSAGE = "message"
    INTERMEDIATE = "intermediate"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    STREAM_TRUNCATED = "stream_truncated"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True)
class AgentStreamEvent:
    """One structured event from an agent stream."""

    kind: AgentStreamEventKind
    session_id: str | None = None
    content: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: Any = None
    result: Any = None
    dropped_events: int | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentStreamEvent:
        """Create an event from the runner's existing payload shape."""
        return cls(
            AgentStreamEventKind(payload["type"]),
            session_id=payload.get("session_id"),
            content=payload.get("content"),
            tool_call_id=payload.get("tool_call_id"),
            tool_name=payload.get("tool_name"),
            arguments=payload.get("arguments"),
            result=payload.get("result"),
            dropped_events=payload.get("dropped_events"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the existing public stream payload shape."""
        payload: dict[str, Any] = {"type": self.kind.value}
        if self.kind is AgentStreamEventKind.SESSION:
            payload["session_id"] = self.session_id
        elif self.kind in {
            AgentStreamEventKind.DELTA,
            AgentStreamEventKind.MESSAGE,
            AgentStreamEventKind.INTERMEDIATE,
            AgentStreamEventKind.ERROR,
        }:
            payload["content"] = self.content
        elif self.kind is AgentStreamEventKind.TOOL_START:
            payload.update(
                {
                    "tool_call_id": self.tool_call_id,
                    "tool_name": self.tool_name,
                    "arguments": self.arguments,
                }
            )
        elif self.kind is AgentStreamEventKind.TOOL_END:
            payload.update(
                {
                    "tool_call_id": self.tool_call_id,
                    "tool_name": self.tool_name,
                    "result": self.result,
                }
            )
        elif self.kind is AgentStreamEventKind.STREAM_TRUNCATED:
            payload["dropped_events"] = self.dropped_events
        return payload


def agent_event_to_sse(event: AgentStreamEvent) -> str:
    """Serialize a neutral event using the existing HTTP SSE contract."""
    return f"data: {json.dumps(event.to_dict(), default=str)}\n\n"


HostedSkillEventKind = AgentStreamEventKind
HostedSkillEvent = AgentStreamEvent