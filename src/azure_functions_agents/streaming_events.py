"""Harness-neutral events emitted while an agent runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class HostedSkillEventKind(StrEnum):
    """Stable event kinds exposed by HostedSkill streaming."""

    SESSION = "session"
    DELTA = "delta"
    MESSAGE = "message"
    INTERMEDIATE = "intermediate"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True)
class HostedSkillEvent:
    """One structured event from an agent stream."""

    kind: HostedSkillEventKind
    session_id: str | None = None
    content: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: Any = None
    result: Any = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> HostedSkillEvent:
        """Create an event from the runner's existing payload shape."""
        return cls(
            HostedSkillEventKind(payload["type"]),
            session_id=payload.get("session_id"),
            content=payload.get("content"),
            tool_call_id=payload.get("tool_call_id"),
            tool_name=payload.get("tool_name"),
            arguments=payload.get("arguments"),
            result=payload.get("result"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the existing public stream payload shape."""
        payload: dict[str, Any] = {"type": self.kind.value}
        if self.kind is HostedSkillEventKind.SESSION:
            payload["session_id"] = self.session_id
        elif self.kind in {
            HostedSkillEventKind.DELTA,
            HostedSkillEventKind.MESSAGE,
            HostedSkillEventKind.INTERMEDIATE,
            HostedSkillEventKind.ERROR,
        }:
            payload["content"] = self.content
        elif self.kind is HostedSkillEventKind.TOOL_START:
            payload.update(
                {
                    "tool_call_id": self.tool_call_id,
                    "tool_name": self.tool_name,
                    "arguments": self.arguments,
                }
            )
        elif self.kind is HostedSkillEventKind.TOOL_END:
            payload.update(
                {
                    "tool_call_id": self.tool_call_id,
                    "tool_name": self.tool_name,
                    "result": self.result,
                }
            )
        return payload

    def to_sse(self) -> str:
        """Serialize this event using the existing HTTP SSE contract."""
        return f"data: {json.dumps(self.to_dict(), default=str)}\n\n"