"""Harness-neutral helpers shared by agent execution surfaces."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import Any

from ._observability import LifecycleStage, RuntimeSpan, start_span


def start_agent_run_span(
    agent_name: str,
    *,
    display_name: str,
    model: str | None,
    trigger_type: str | None = None,
    execution_surface: str | None = None,
    session_id: str | None = None,
) -> AbstractContextManager[RuntimeSpan]:
    """Start a run-level span with the shared agent identity attributes."""
    return start_span(
        f"agent.run {agent_name}",
        lifecycle_stage=LifecycleStage.AGENT_RUN,
        attributes={
            "af.agent.name": agent_name,
            "af.agent.display_name": display_name,
            "af.agent.trigger_type": trigger_type,
            "af.agent.execution_surface": execution_surface,
            "af.agent.session_id": session_id,
            "af.agent.model": model,
        },
    )


def _looks_like_tool_error(result: Any) -> bool:
    """Return whether a recorded tool result uses a known failure shape."""
    if not isinstance(result, str):
        return False
    try:
        parsed = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    if parsed.get("error"):
        return True
    stderr = parsed.get("stderr")
    return bool(isinstance(stderr, str) and stderr.strip())


def _tool_error_count(tool_calls: list[dict[str, Any]] | None) -> int:
    if not tool_calls:
        return 0
    return sum(1 for call in tool_calls if _looks_like_tool_error(call.get("result")))


def _total_tool_error_count(result: Any) -> int:
    """Combine result-shape heuristics with explicit delegate-error accounting."""
    tool_calls = list(getattr(result, "tool_calls", None) or [])
    delegate_error_count = int(getattr(result, "delegate_error_count", 0) or 0)
    return _tool_error_count(tool_calls) + delegate_error_count


def _set_run_result_attributes(span: Any, result: Any) -> None:
    """Attach non-sensitive run-summary attributes; content only when opted in."""
    tool_calls = list(getattr(result, "tool_calls", None) or [])
    content = str(getattr(result, "content", "") or "")
    span.set_attribute("af.agent.tool_call_count", len(tool_calls))
    span.set_attribute("af.agent.tool_error_count", _total_tool_error_count(result))
    span.set_attribute("af.agent.response_bytes", len(content))
    span.set_content("af.agent.response", content)