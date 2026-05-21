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
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict, cast

from ._logger import logger
from .client_manager import get_client_manager
from .config.paths import get_app_root, resolve_config_dir
from .config.schema import AgentConfiguration
from .discovery.builtin_tools import BUILTIN_TOOLS, add_allowed_read_dir
from .discovery.mcp import discover_mcp_servers
from .discovery.skills import discover_skills
from .discovery.tools import discover_user_tools
from .system_tools.connectors.cache import get_connector_tools

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


class _ChatOptionsKwargs(TypedDict, total=False):
    temperature: float
    top_p: float
    max_tokens: int


# ---------------------------------------------------------------------------
# Per-session locks (single-process scope)
# ---------------------------------------------------------------------------

_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
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
    content_intermediate: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Session id validation + path resolution
# ---------------------------------------------------------------------------


def _validate_session_id(session_id: str | None) -> str | None:
    """Return ``session_id`` if it matches the safe pattern; raise on invalid input."""
    if session_id is None:
        return None
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.match(session_id):
        raise ValueError(f"Invalid session_id (must match {_SESSION_ID_PATTERN.pattern})")
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


def _compose_instructions(
    agent_instructions: str | None, skills_text: str | None = None
) -> str | None:
    """Combine the tool-restriction prefix, agent instructions, and skills text."""
    parts: list[str] = [_TOOL_RESTRICTION_PREFIX.rstrip()]
    if agent_instructions and agent_instructions.strip():
        parts.append(agent_instructions.strip())
    if skills_text is None:
        skills_text = discover_skills(get_app_root())
    if skills_text:
        parts.append("# Project skills\n\n" + skills_text)
    composed = "\n\n".join(parts).strip()
    return composed or None


def _build_chat_options(agent_configuration: AgentConfiguration) -> Any:
    """Build MAF ChatOptions from the universal agent-configuration knobs."""
    from agent_framework import ChatOptions

    kwargs: _ChatOptionsKwargs = {}
    if agent_configuration.temperature is not None:
        kwargs["temperature"] = agent_configuration.temperature
    if agent_configuration.top_p is not None:
        kwargs["top_p"] = agent_configuration.top_p
    if agent_configuration.max_tokens is not None:
        kwargs["max_tokens"] = agent_configuration.max_tokens
    return cast(Any, ChatOptions)(**kwargs)


# ---------------------------------------------------------------------------
# Agent + session construction
# ---------------------------------------------------------------------------


async def _build_agent_session_history(
    *,
    instructions: str | None,
    agent_configuration: AgentConfiguration,
    session_id: str | None,
    tools: list[Any] | None,
    mcp_tools: list[Any] | None,
    skills_text: str | None,
    use_connector_tools: bool,
    sandbox_tools: list[Any] | None,
) -> tuple[Any, Any, str]:
    """Construct the chat client, agent, AgentSession, and history provider.

    Returns ``(agent, session, resolved_session_id)``.
    """
    # Imported here so a missing optional dependency surfaces only when actually
    # needed (e.g. tests that don't run the runtime path).
    from agent_framework import (
        Agent,
        AgentSession,
        FileHistoryProvider,
    )

    # Build the chat client first so configuration errors surface BEFORE any
    # filesystem state is created.
    client_manager = get_client_manager()
    chat_client = client_manager.get_chat_client(agent_configuration)

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

    # Tool list: resolved tools + optional connector tools + per-call sandbox
    # tools + resolved MCP tools. Order chosen so that the most general/safe
    # tools appear first; LLMs that ignore order are unaffected.
    app_root = get_app_root()
    skills_dir = app_root / "skills"
    if skills_dir.is_dir():
        add_allowed_read_dir(str(skills_dir))

    resolved_tools: list[Any]
    if tools is None:
        resolved_tools = list(discover_user_tools(app_root)) + list(BUILTIN_TOOLS)
    else:
        resolved_tools = list(tools)

    connectors = await get_connector_tools() if use_connector_tools else None
    if connectors:
        resolved_tools.extend(connectors)
    if sandbox_tools:
        resolved_tools.extend(sandbox_tools)

    resolved_mcp_tools = (
        list(discover_mcp_servers(app_root).values()) if mcp_tools is None else list(mcp_tools)
    )
    if resolved_mcp_tools:
        # MAF's Agent.tools accepts a heterogeneous list of FunctionTool and MCP tools.
        resolved_tools.extend(resolved_mcp_tools)  # type: ignore[arg-type]

    agent = Agent(
        chat_client,
        instructions=_compose_instructions(instructions, skills_text=skills_text),
        tools=resolved_tools,
        default_options=_build_chat_options(agent_configuration),
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


def _function_call_event(item: Any) -> dict[str, Any]:
    return {
        "type": "tool_start",
        "tool_call_id": getattr(item, "call_id", None) or getattr(item, "id", None),
        "tool_name": getattr(item, "name", None),
        "arguments": getattr(item, "arguments", None),
    }


def _function_result_event(item: Any) -> dict[str, Any]:
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
    instructions: str | None = None,
    agent_configuration: AgentConfiguration,
    tools: list[Any] | None = None,
    mcp_tools: list[Any] | None = None,
    skills_text: str | None = None,
    use_connector_tools: bool = True,
    session_id: str | None = None,
    sandbox_tools: list[Any] | None = None,
) -> AgentResult:
    """Execute a single prompt against the configured agent backend.

    Parameters
    ----------
    prompt:
        Prompt text. Sent as a user message.
    instructions:
        Per-call agent instructions (typically the body of an ``*.agent.md``
        file). Combined with the tool-restriction prefix and any skills text.
    agent_configuration:
        Resolved provider selection and universal generation knobs. The runner
        forwards this object to ``ClientManager.get_chat_client(...)`` and uses
        ``timeout`` / ``temperature`` / ``top_p`` / ``max_tokens`` from it.
    tools:
        Optional user/built-in tool override. ``None`` auto-discovers user
        tools and includes built-ins. When a list is provided (including
        ``[]``), that exact list becomes the user-tool set. Connector tools,
        sandbox tools, and MCP tools are controlled separately and may still be
        added.
    mcp_tools:
        Optional MCP tool list. ``None`` auto-discovers tools from
        ``mcp.json``; an explicit list is used as-is. Pass ``[]`` to disable
        MCP tools entirely.
    skills_text:
        Optional pre-loaded skills text. ``None`` preserves legacy discovery;
        ``""`` disables skills injection.
    use_connector_tools:
        Whether to include connector tools discovered from the shared cache.
        This is separate from ``tools``. ``run_agent()`` defaults to ``True``;
        higher-level config-driven callers can treat ``None`` as "use the
        configured default" before calling this function.
    session_id:
        Optional session id for resuming a prior conversation. Must match
        ``[A-Za-z0-9._-]{1,128}``. When omitted, a fresh session is created
        and its id is returned in :class:`AgentResult`.
    sandbox_tools:
        Optional list of tools created via :func:`create_sandbox_tools` —
        bound to a specific ACA session pool. ``None`` adds no sandbox tools;
        pass a list to enable them. Per-call because the ACA session id is
        baked into each tool's closure.

    Notes
    -----
    To fully disable all tools from a direct API call, pass
    ``tools=[], mcp_tools=[], sandbox_tools=None, use_connector_tools=False``.
    """
    timeout = float(agent_configuration.timeout) if agent_configuration.timeout is not None else None

    agent, session, resolved_id = await _build_agent_session_history(
        instructions=instructions,
        agent_configuration=agent_configuration,
        session_id=session_id,
        tools=tools,
        mcp_tools=mcp_tools,
        skills_text=skills_text,
        use_connector_tools=use_connector_tools,
        sandbox_tools=sandbox_tools,
    )

    lock = await _get_session_lock(resolved_id)
    async with lock:
        try:
            if timeout is None:
                response = await agent.run(prompt, session=session)
            else:
                response = await asyncio.wait_for(agent.run(prompt, session=session), timeout=timeout)
        except TimeoutError:
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
    tool_calls: list[dict[str, Any]] = []
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
    instructions: str | None = None,
    agent_configuration: AgentConfiguration,
    tools: list[Any] | None = None,
    mcp_tools: list[Any] | None = None,
    skills_text: str | None = None,
    use_connector_tools: bool = True,
    session_id: str | None = None,
    sandbox_tools: list[Any] | None = None,
) -> AsyncIterator[str]:
    """SSE-formatted async generator yielding ``data: {...}\\n\\n`` lines.

    Tool-selection semantics match :func:`run_agent`:

    * ``tools`` controls only the user/built-in tool set. ``None``
      auto-discovers user tools and includes built-ins; a provided list
      (including ``[]``) is used exactly as that user-tool set.
    * ``use_connector_tools`` separately controls connector tools. Callers that
      want config-driven defaults can treat ``None`` as "use the configured
      default" before calling this function.
    * ``mcp_tools`` separately controls MCP tools. ``None`` auto-discovers
      from ``mcp.json``; pass ``[]`` to disable MCP tools.
    * ``sandbox_tools`` separately controls sandbox tools. ``None`` adds no
      sandbox tools; pass a list to enable them.
    * To fully disable all tools from a direct API call, pass
      ``tools=[], mcp_tools=[], sandbox_tools=None, use_connector_tools=False``.

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
    timeout = float(agent_configuration.timeout) if agent_configuration.timeout is not None else None

    try:
        agent, session, resolved_id = await _build_agent_session_history(
            instructions=instructions,
            agent_configuration=agent_configuration,
            session_id=session_id,
            tools=tools,
            mcp_tools=mcp_tools,
            skills_text=skills_text,
            use_connector_tools=use_connector_tools,
            sandbox_tools=sandbox_tools,
        )
    except Exception as exc:
        logger.error("Failed to build agent session: %s", exc, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return

    yield f"data: {json.dumps({'type': 'session', 'session_id': resolved_id})}\n\n"

    lock = await _get_session_lock(resolved_id)
    async with lock:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        try:
            stream = agent.run(prompt, stream=True, session=session)
            async for update in stream:
                if deadline is not None and loop.time() > deadline:
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
        except TimeoutError:
            yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
        except Exception as exc:
            logger.error("Agent stream failed: %s", exc, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"


# ---------------------------------------------------------------------------
# Removed-API stubs (one release of clear errors before symbol removal)
# ---------------------------------------------------------------------------


def run_copilot_agent(*_args: object, **_kwargs: object) -> None:  # pragma: no cover - stub
    """Removed in 1.0.0. Use :func:`run_agent` instead."""
    raise RuntimeError(
        "run_copilot_agent was removed in azure-functions-agents 1.0.0. "
        "The runtime now uses the Microsoft Agent Framework. Migrate to "
        "azure_functions_agents.run_agent."
    )


def run_copilot_agent_stream(*_args: object, **_kwargs: object) -> None:  # pragma: no cover - stub
    """Removed in 1.0.0. Use :func:`run_agent_stream` instead."""
    raise RuntimeError(
        "run_copilot_agent_stream was removed in azure-functions-agents 1.0.0. "
        "The runtime now uses the Microsoft Agent Framework. Migrate to "
        "azure_functions_agents.run_agent_stream."
    )
