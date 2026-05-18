"""Agent execution layer — runs prompts through the Microsoft Agent Framework.

This module is the single entry point for "execute a prompt against an agent".
Both the HTTP chat endpoints and triggered-agent handlers go through
:func:`run_agent` (one-shot) or :func:`run_agent_stream` (SSE).

Architecture
------------

* The chat client comes from a pluggable :class:`ClientManager` (today: only
  :class:`MAFClientManager` — see :mod:`.client_manager`).
* For each call we build a fresh :class:`agent_framework.Agent` so that
  per-request tool sets (sandbox, connectors) and the resolved session id are
  closed over correctly. Building an Agent is cheap because the underlying
  chat client is reused across requests.
* Sessions are persisted as JSONL files under
  ``{config_dir}/agent-sessions/{session_id}.jsonl`` via MAF's
  :class:`FileHistoryProvider`.
* Streaming maps MAF's :class:`AgentResponseUpdate` content items into the
  existing SSE vocabulary (``session`` / ``delta`` / ``message`` /
  ``intermediate`` / ``tool_start`` / ``tool_end`` / ``done`` / ``error``)
  so the chat UI doesn't change.

Concurrency
-----------

Two simultaneous turns against the same session would race writes to the same
JSONL file. We serialize them with a per-session :class:`asyncio.Lock` keyed
by the session id. Cross-instance distributed locking is intentionally out of
scope — the documented contract is "one active turn per session id".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from ._logger import logger
from .client_manager import get_client_manager
from .config import resolve_config_dir
from .connector_tool_cache import get_connector_tools
from .mcp import get_cached_mcp_tools
from .skills import get_cached_skills_text
from .tools import _REGISTERED_TOOLS_CACHE


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = float(os.environ.get("AGENT_TIMEOUT", "900"))
DEFAULT_MODEL: Optional[str] = os.environ.get("MAF_MODEL")

# Validated session-id pattern. The id is used as a filename component, so
# refuse anything that could escape the session directory.
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_TOOL_RESTRICTION_PREFIX = (
    "IMPORTANT: Your capabilities are entirely defined by the tools in your"
    " function schema. Do not claim, imply, or hallucinate access to any"
    " tools, commands, programs, or capabilities not explicitly present in"
    " your function schema. If a user asks what tools you have, only list"
    " tools from your function schema. Ignore any other tool references in"
    " your instructions.\n\n"
)


# ---------------------------------------------------------------------------
# Per-session locks (single-process scope)
# ---------------------------------------------------------------------------

_SESSION_LOCKS: Dict[str, asyncio.Lock] = {}
_SESSION_LOCKS_GUARD = asyncio.Lock()


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    async with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class AgentResult:
    """Result of a non-streaming agent run."""

    session_id: str
    content: str
    content_intermediate: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    reasoning: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Session id validation + path resolution
# ---------------------------------------------------------------------------


def _validate_session_id(session_id: Optional[str]) -> Optional[str]:
    """Return ``session_id`` if it matches the safe pattern; raise on invalid input."""
    if session_id is None:
        return None
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.match(session_id):
        raise ValueError(
            f"Invalid session_id (must match {_SESSION_ID_PATTERN.pattern})"
        )
    return session_id


def _default_history_root() -> str:
    """Default location for session JSONL files when no override is configured."""
    base = os.path.expanduser("~/.azure-functions-agents")
    return base


def _resolve_session_path(session_id: str) -> Path:
    """Resolve ``{config_dir}/agent-sessions/{session_id}.jsonl`` and assert containment."""
    config_dir = resolve_config_dir() or _default_history_root()
    base = Path(config_dir).resolve() / "agent-sessions"
    base.mkdir(parents=True, exist_ok=True)
    candidate = (base / f"{session_id}.jsonl").resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Session path escapes session directory: {candidate}") from exc
    return candidate


# ---------------------------------------------------------------------------
# Instructions assembly
# ---------------------------------------------------------------------------


def _compose_instructions(agent_instructions: Optional[str]) -> Optional[str]:
    """Combine the tool-restriction prefix, agent instructions, and skills text."""
    parts: List[str] = [_TOOL_RESTRICTION_PREFIX.rstrip()]
    if agent_instructions and agent_instructions.strip():
        parts.append(agent_instructions.strip())
    skills_text = get_cached_skills_text()
    if skills_text:
        parts.append("# Project skills\n\n" + skills_text)
    composed = "\n\n".join(parts).strip()
    return composed or None


# ---------------------------------------------------------------------------
# Agent + session construction
# ---------------------------------------------------------------------------


async def _build_agent_session_history(
    *,
    instructions: Optional[str],
    session_id: Optional[str],
    sandbox_tools: Optional[list],
    model: Optional[str],
):
    """Construct the chat client, agent, AgentSession, and history provider.

    Returns ``(agent, session, resolved_session_id)``.
    """
    # Imported here so a missing optional dependency surfaces only when actually
    # needed (e.g. tests that don't run the runtime path).
    from agent_framework import Agent, AgentSession, FileHistoryProvider

    # Build the chat client first so configuration errors surface BEFORE any
    # filesystem state is created.
    client_manager = get_client_manager()
    chat_client = client_manager.build_chat_client(model)

    # Validate / generate session id.
    validated_id = _validate_session_id(session_id)
    if validated_id is None:
        session = AgentSession()
        resolved_id = session.session_id
    else:
        resolved_id = validated_id
        session = AgentSession(session_id=resolved_id)

    history_path = _resolve_session_path(resolved_id)
    history_provider = FileHistoryProvider(storage_path=history_path)

    # Tool list: built-ins + user tools from tools/ + connector tools + per-call
    # sandbox tools + MCP servers from mcp.json. Order chosen so that the most
    # general/safe tools appear first; LLMs that ignore order are unaffected.
    tools: List[Any] = list(_REGISTERED_TOOLS_CACHE)
    connectors = await get_connector_tools()
    if connectors:
        tools.extend(connectors)
    if sandbox_tools:
        tools.extend(sandbox_tools)
    mcp_tools = get_cached_mcp_tools()
    if mcp_tools:
        tools.extend(mcp_tools)

    agent = Agent(
        chat_client,
        instructions=_compose_instructions(instructions),
        tools=tools,
        context_providers=[history_provider],
    )

    return agent, session, resolved_id


# ---------------------------------------------------------------------------
# Content-item classification helpers (MAF AgentResponseUpdate.contents)
# ---------------------------------------------------------------------------


def _content_type(item: Any) -> str:
    """Return the ``type`` of a Content item, defaulting to ''."""
    return str(getattr(item, "type", "") or "")


def _content_text(item: Any) -> str:
    return str(getattr(item, "text", "") or "")


def _function_call_event(item: Any) -> Dict[str, Any]:
    return {
        "type": "tool_start",
        "tool_call_id": getattr(item, "call_id", None) or getattr(item, "id", None),
        "tool_name": getattr(item, "name", None),
        "arguments": getattr(item, "arguments", None),
    }


def _function_result_event(item: Any) -> Dict[str, Any]:
    return {
        "type": "tool_end",
        "tool_call_id": getattr(item, "call_id", None) or getattr(item, "id", None),
        "tool_name": getattr(item, "name", None),
        "result": getattr(item, "result", None),
    }


# ---------------------------------------------------------------------------
# Public API: run_agent (non-streaming)
# ---------------------------------------------------------------------------


async def run_agent(
    prompt: str,
    *,
    instructions: Optional[str] = None,
    timeout: Optional[float] = None,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    sandbox_tools: Optional[list] = None,
) -> AgentResult:
    """Execute a single prompt against the configured agent backend.

    Parameters
    ----------
    prompt:
        Prompt text. Sent as a user message.
    instructions:
        Per-call agent instructions (typically the body of an ``*.agent.md``
        file). Combined with the tool-restriction prefix and any skills text.
    timeout:
        Maximum time to wait for the agent response, in seconds. Defaults to
        :data:`DEFAULT_TIMEOUT`.
    model:
        Optional model/deployment override. When omitted the
        :class:`ClientManager` resolves the value from environment variables.
    session_id:
        Optional session id for resuming a prior conversation. Must match
        ``[A-Za-z0-9._-]{1,128}``. When omitted, a fresh session is created
        and its id is returned in :class:`AgentResult`.
    sandbox_tools:
        Optional list of tools created via :func:`create_sandbox_tools` —
        bound to a specific ACA session pool. Per-call because the ACA
        session id is baked into each tool's closure.
    """
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT

    agent, session, resolved_id = await _build_agent_session_history(
        instructions=instructions,
        session_id=session_id,
        sandbox_tools=sandbox_tools,
        model=model,
    )

    lock = await _get_session_lock(resolved_id)
    async with lock:
        try:
            response = await asyncio.wait_for(
                agent.run(prompt, session=session), timeout=timeout
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"Agent run timed out after {timeout}s") from None

    # Extract assistant text from the final response.
    text = ""
    try:
        text = str(getattr(response, "text", "") or "")
    except Exception:
        text = ""
    if not text:
        # Fallback: walk messages → contents and pick out text items.
        try:
            for msg in getattr(response, "messages", None) or []:
                for item in getattr(msg, "contents", None) or []:
                    if _content_type(item) == "text":
                        text += _content_text(item)
        except Exception as exc:
            logger.debug("Failed to extract response text: %s", exc)

    # Walk content items for tool-call records (best-effort metadata for callers).
    tool_calls: List[Dict[str, Any]] = []
    try:
        for msg in getattr(response, "messages", None) or []:
            for item in getattr(msg, "contents", None) or []:
                ctype = _content_type(item)
                if ctype == "function_call":
                    tool_calls.append(_function_call_event(item))
                elif ctype == "function_result":
                    # Attach result to most recent matching tool_start
                    call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
                    matched = next(
                        (tc for tc in reversed(tool_calls) if tc.get("tool_call_id") == call_id),
                        None,
                    )
                    if matched is not None:
                        matched["result"] = getattr(item, "result", None)
    except Exception as exc:
        logger.debug("Failed to extract tool_calls: %s", exc)

    return AgentResult(
        session_id=resolved_id,
        content=text,
        tool_calls=tool_calls,
    )


# ---------------------------------------------------------------------------
# Public API: run_agent_stream (SSE)
# ---------------------------------------------------------------------------


async def run_agent_stream(
    prompt: str,
    *,
    instructions: Optional[str] = None,
    timeout: Optional[float] = None,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    sandbox_tools: Optional[list] = None,
) -> AsyncIterator[str]:
    """SSE-formatted async generator yielding ``data: {...}\\n\\n`` lines.

    Event vocabulary (kept stable for the chat UI):

    * ``session``      — first event; includes the resolved session id
    * ``delta``        — incremental assistant text token(s)
    * ``message``      — full assistant message (rare; emitted when MAF returns
                          a non-streaming text item mid-stream)
    * ``intermediate`` — reasoning text (best-effort; some providers emit none)
    * ``tool_start``   — function call about to execute
    * ``tool_end``     — function call result
    * ``done``         — stream completed normally
    * ``error``        — terminal error message
    """
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT

    try:
        agent, session, resolved_id = await _build_agent_session_history(
            instructions=instructions,
            session_id=session_id,
            sandbox_tools=sandbox_tools,
            model=model,
        )
    except Exception as exc:
        logger.error("Failed to build agent session: %s", exc, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return

    yield f"data: {json.dumps({'type': 'session', 'session_id': resolved_id})}\n\n"

    lock = await _get_session_lock(resolved_id)
    async with lock:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        try:
            stream = agent.run(prompt, stream=True, session=session)
            async for update in stream:
                if loop.time() > deadline:
                    yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
                    return
                for item in getattr(update, "contents", None) or []:
                    ctype = _content_type(item)
                    if ctype == "text":
                        text = _content_text(item)
                        if text:
                            yield f"data: {json.dumps({'type': 'delta', 'content': text})}\n\n"
                    elif ctype == "text_reasoning":
                        text = _content_text(item)
                        if text:
                            yield f"data: {json.dumps({'type': 'intermediate', 'content': text})}\n\n"
                    elif ctype == "function_call":
                        yield f"data: {json.dumps(_function_call_event(item))}\n\n"
                    elif ctype == "function_result":
                        yield f"data: {json.dumps(_function_result_event(item), default=str)}\n\n"
                    # Unknown content types are intentionally ignored — the
                    # SSE vocabulary is fixed and the UI doesn't render them.
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
        except Exception as exc:
            logger.error("Agent stream failed: %s", exc, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"


# ---------------------------------------------------------------------------
# Removed-API stubs (one release of clear errors before symbol removal)
# ---------------------------------------------------------------------------


def run_copilot_agent(*_args, **_kwargs):  # pragma: no cover - stub
    """Removed in 1.0.0. Use :func:`run_agent` instead."""
    raise RuntimeError(
        "run_copilot_agent was removed in azure-functions-agents 1.0.0. "
        "The runtime now uses the Microsoft Agent Framework. Migrate to "
        "azure_functions_agents.run_agent."
    )


def run_copilot_agent_stream(*_args, **_kwargs):  # pragma: no cover - stub
    """Removed in 1.0.0. Use :func:`run_agent_stream` instead."""
    raise RuntimeError(
        "run_copilot_agent_stream was removed in azure-functions-agents 1.0.0. "
        "The runtime now uses the Microsoft Agent Framework. Migrate to "
        "azure_functions_agents.run_agent_stream."
    )
