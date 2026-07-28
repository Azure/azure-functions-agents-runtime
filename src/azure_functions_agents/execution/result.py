"""Compatibility result shape retained for the direct runner API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentResult:
    """Result of a completed non-streaming agent run."""

    session_id: str
    content: str
    content_intermediate: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    # Delegate failures are tracked separately because their sanitized text is
    # not necessarily recognizable from a tool result payload.
    delegate_error_count: int = 0
