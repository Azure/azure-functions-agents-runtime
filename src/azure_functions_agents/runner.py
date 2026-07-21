"""Agent execution layer — runs prompts through the Microsoft Agent Framework.

This module is the single entry point for "execute a prompt against an agent".
Both the HTTP chat endpoints and triggered-agent handlers go through
:func:`run_agent` (one-shot) or :func:`run_agent_stream` (SSE).

Architecture
------------

* The chat client comes from a pluggable :class:`ClientManager` (today: only
  :class:`MAFClientManager` — see :mod:`.client_manager`).
* For each call we build a fresh :class:`agent_framework.Agent` so that
  per-request tool sets (sandbox, connectors) and the resolved chat-session id
  are closed over correctly. Building an Agent is cheap because the underlying
  chat client is reused across requests.
* Chat history is persisted to Azure Blob Storage via
  :class:`BlobHistoryProvider` when ``AzureWebJobsStorage`` is configured
  (either as a connection string or via the identity-based
  ``AzureWebJobsStorage__blobServiceUri`` setting). Otherwise — for purely
  local development — it falls back to MAF's :class:`FileHistoryProvider`
  writing to ``{config_dir}/agent-sessions/{session_id}.jsonl``.
* Streaming maps MAF's :class:`AgentResponseUpdate` content items into the
  existing SSE vocabulary (``session`` / ``delta`` / ``message`` /
  ``intermediate`` / ``tool_start`` / ``tool_end`` / ``done`` / ``error``)
  so the chat UI doesn't change.

Concurrency
-----------

Two simultaneous turns against the same chat session would race writes to the
same history record. We serialize them with a per-session
:class:`asyncio.Lock` keyed by the chat-session id. Cross-instance distributed
locking is intentionally out of scope — the documented contract is "one
active turn per chat-session id". ``BlobHistoryProvider`` uses Append Blobs
whose ``append_block`` is atomic on the server, so concurrent writes from
two instances cannot interleave within a single block, but turn-level
ordering across instances is still the caller's responsibility.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._blob_history import build_blob_provider_from_environment
from ._logger import logger
from .client_manager import get_client_manager
from .config.env import runtime_env_value
from .config.paths import get_app_root, resolve_config_dir
from .config.schema import HarnessAgentConfig
from .discovery.mcp import discover_mcp_servers
from .discovery.tools import discover_user_tools

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def _runtime_timeout_default() -> float:
    env_timeout = runtime_env_value("AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS")
    if env_timeout:
        try:
            return float(env_timeout)
        except ValueError:
            logger.warning(
                "Ignoring invalid AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS value: %s",
                env_timeout,
            )
    return 900.0


DEFAULT_TIMEOUT = _runtime_timeout_default()
DEFAULT_MODEL: str | None = runtime_env_value("AZURE_FUNCTIONS_AGENTS_MODEL") or None

# Validated session-id pattern. The id is used as a filename component, so
# refuse anything that could escape the session directory.
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


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


def _resolve_sessions_dir() -> Path:
    """Resolve the directory used by :class:`FileHistoryProvider` for local sessions.

    Returns ``{config_dir}/agent-sessions`` (creating it if needed). This is
    the *directory* path — :class:`FileHistoryProvider` itself appends
    ``{session_id}.jsonl`` per session.
    """
    base = Path(resolve_config_dir()).resolve() / "agent-sessions"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _build_history_provider() -> Any:
    """Choose the history provider to use for this turn.

    Prefers :class:`BlobHistoryProvider` when the Azure Functions storage
    binding is configured (either ``AzureWebJobsStorage`` connection string
    or the identity-based ``AzureWebJobsStorage__blobServiceUri`` setting),
    which gives true multi-instance support without any extra resources.
    Falls back to :class:`FileHistoryProvider` for pure local development.
    """
    from agent_framework import FileHistoryProvider

    blob_provider = build_blob_provider_from_environment()
    if blob_provider is not None:
        return blob_provider
    return FileHistoryProvider(storage_path=_resolve_sessions_dir())


def _build_chat_options_from_environment() -> dict[str, Any] | None:
    """Build provider chat options from supported runtime environment variables."""
    reasoning: dict[str, str] = {}
    effort = runtime_env_value("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT")
    if effort:
        reasoning["effort"] = effort
    summary = runtime_env_value("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY")
    if summary:
        reasoning["summary"] = summary
    if not reasoning:
        return None
    return {"reasoning": reasoning}


# ---------------------------------------------------------------------------
# Agent + session construction
# ---------------------------------------------------------------------------


def _build_skills_provider(skill_paths: list[Path] | None) -> Any:
    """Return a :class:`SkillsProvider` for the given skill directories, or ``None``."""
    if not skill_paths:
        return None
    # ``SkillsProvider`` is marked experimental in MAF; constructing it emits an
    # ``ExperimentalWarning``. We acknowledge the experimental status — it is
    # the documented integration point for SKILL.md-based progressive disclosure —
    # and suppress just that one warning so cold-start logs stay quiet.
    import warnings

    from agent_framework import SkillsProvider
    from agent_framework._feature_stage import ExperimentalWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=ExperimentalWarning)
        return SkillsProvider.from_paths(list(skill_paths))


async def _build_agent_session_history(
    *,
    instructions: str | None,
    session_id: str | None,
    tools: list[Any] | None,
    mcp_tools: list[Any] | None,
    skill_paths: list[Path] | None,
    model: str | None,
    sandbox_tools: list[Any] | None,
    system_addendum: str | None,
    workflow_enabled: bool,
    workflow_durable_client: Any | None,
    agent_name: str | None,
    web_request_tools: list[Any] | None = None,
) -> tuple[Any, Any, str]:
    """Construct the chat client, agent, AgentSession, and history provider.

    Returns ``(agent, session, resolved_session_id)``.
    """
    # Imported here so a missing optional dependency surfaces only when actually
    # needed (e.g. tests that don't run the runtime path).
    from agent_framework import (
        Agent,
        AgentSession,
    )

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

    history_provider = _build_history_provider()

    # Tool list: resolved user tools + per-call sandbox/workflow tools + resolved MCP tools.
    app_root = get_app_root()
    resolved_tools: list[Any] = (
        discover_user_tools(app_root).tools if tools is None else list(tools)
    )

    if sandbox_tools:
        resolved_tools.extend(sandbox_tools)

    if web_request_tools:
        resolved_tools.extend(web_request_tools)

    if workflow_enabled:
        from .workflows.tools import build_workflow_tools

        resolved_tools.extend(
            build_workflow_tools(
                session_id=resolved_id,
                agent_name=agent_name or "main",
                durable_client=workflow_durable_client,
            )
        )

    resolved_mcp_tools = (
        list(discover_mcp_servers(app_root).servers.values()) if mcp_tools is None else list(mcp_tools)
    )
    if resolved_mcp_tools:
        # MAF's Agent.tools accepts a heterogeneous list of FunctionTool and MCP tools.
        resolved_tools.extend(resolved_mcp_tools)

    context_providers: list[Any] = [history_provider]
    skills_provider = _build_skills_provider(skill_paths)
    if skills_provider is not None:
        context_providers.append(skills_provider)

    effective_instructions = instructions.strip() if instructions and instructions.strip() else None
    if system_addendum:
        effective_instructions = (effective_instructions or "") + system_addendum

    agent = Agent(
        chat_client,
        instructions=effective_instructions,
        tools=resolved_tools,
        context_providers=context_providers,
    )

    return agent, session, resolved_id


async def _build_harness_agent_session(
    *,
    instructions: str | None,
    session_id: str | None,
    tools: list[Any] | None,
    mcp_tools: list[Any] | None,
    skill_paths: list[Path] | None,
    model: str | None,
    sandbox_tools: list[Any] | None,
    system_addendum: str | None,
    workflow_enabled: bool,
    workflow_durable_client: Any | None,
    agent_name: str | None,
    web_request_tools: list[Any] | None = None,
    harness_config: HarnessAgentConfig | None = None,
) -> tuple[Any, Any, str]:
    """Construct an agent/session using MAF's ``create_harness_agent``.

    Falls back to :func:`_build_agent_session_history` (plain ``Agent``) with a
    warning when ``create_harness_agent`` is not available in the installed
    version of ``agent_framework``.

    Returns ``(agent, session, resolved_session_id)``.
    """
    import warnings

    try:
        from agent_framework import create_harness_agent  # type: ignore[attr-defined]
        from agent_framework._feature_stage import ExperimentalWarning
    except ImportError:
        logger.warning(
            "create_harness_agent is not available in the installed agent_framework "
            "version; falling back to plain Agent"
        )
        return await _build_agent_session_history(
            instructions=instructions,
            session_id=session_id,
            tools=tools,
            mcp_tools=mcp_tools,
            skill_paths=skill_paths,
            model=model,
            sandbox_tools=sandbox_tools,
            system_addendum=system_addendum,
            workflow_enabled=workflow_enabled,
            workflow_durable_client=workflow_durable_client,
            agent_name=agent_name,
            web_request_tools=web_request_tools,
        )

    resolved_config = harness_config if harness_config is not None else HarnessAgentConfig()

    from agent_framework import AgentSession

    client_manager = get_client_manager()
    chat_client = client_manager.build_chat_client(model)

    validated_id = _validate_session_id(session_id)
    if validated_id is None:
        session = AgentSession()
        resolved_id = session.session_id
    else:
        resolved_id = validated_id
        session = AgentSession(session_id=resolved_id)

    history_provider = _build_history_provider()

    # Tool list: identical assembly logic to _build_agent_session_history.
    app_root = get_app_root()
    resolved_tools: list[Any] = (
        discover_user_tools(app_root).tools if tools is None else list(tools)
    )

    if sandbox_tools:
        resolved_tools.extend(sandbox_tools)

    if web_request_tools:
        resolved_tools.extend(web_request_tools)

    if workflow_enabled:
        from .workflows.tools import build_workflow_tools

        resolved_tools.extend(
            build_workflow_tools(
                session_id=resolved_id,
                agent_name=agent_name or "main",
                durable_client=workflow_durable_client,
            )
        )

    resolved_mcp_tools = (
        list(discover_mcp_servers(app_root).servers.values()) if mcp_tools is None else list(mcp_tools)
    )
    if resolved_mcp_tools:
        resolved_tools.extend(resolved_mcp_tools)

    effective_instructions = instructions.strip() if instructions and instructions.strip() else None
    if system_addendum:
        effective_instructions = (effective_instructions or "") + system_addendum

    # create_harness_agent takes skills_paths natively; no SkillsProvider in context_providers.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=ExperimentalWarning)
        agent = create_harness_agent(
            chat_client,
            agent_instructions=effective_instructions,
            tools=resolved_tools or None,
            history_provider=history_provider,
            skills_paths=skill_paths or None,
            disable_tool_auto_approval=True,
            disable_web_search=True,
            max_context_window_tokens=resolved_config.max_context_window_tokens,
            max_output_tokens=resolved_config.max_output_tokens,
            disable_file_memory=resolved_config.disable_file_memory,
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


def _merge_tool_arguments(previous: Any, current: Any) -> Any:
    if previous is None:
        return current
    if current is None:
        return previous
    if isinstance(previous, str) and isinstance(current, str):
        if current.startswith(previous):
            return current
        return previous + current
    return current


def _is_complete_json_argument(value: Any) -> bool:
    if not isinstance(value, str):
        return value is not None
    text = value.strip()
    if not text:
        return False
    try:
        json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return False
    return True


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
    timeout: float | None = None,
    tools: list[Any] | None = None,
    mcp_tools: list[Any] | None = None,
    skill_paths: list[Path] | None = None,
    model: str | None = None,
    session_id: str | None = None,
    sandbox_tools: list[Any] | None = None,
    system_addendum: str | None = None,
    workflow_enabled: bool = False,
    workflow_durable_client: Any | None = None,
    agent_name: str | None = None,
    web_request_tools: list[Any] | None = None,
    harness_config: HarnessAgentConfig | None = None,
) -> AgentResult:
    """Execute a single prompt against the configured agent backend.

    Parameters
    ----------
    prompt:
        Prompt text. Sent as a user message.
    instructions:
        Per-call agent instructions (typically the body of an ``*.agent.md``
        file). Used verbatim as the agent's system prompt.
    timeout:
        Maximum time to wait for the agent response, in seconds. Defaults to
        :data:`DEFAULT_TIMEOUT`.
    tools:
        Optional user-tool override. ``None`` auto-discovers user tools from
        the app root. When a list is provided (including ``[]``), that exact
        list becomes the user-tool set. Sandbox tools and MCP tools are
        controlled separately and may still be added.
    mcp_tools:
        Optional MCP tool list. ``None`` auto-discovers tools from
        ``mcp.json``; an explicit list is used as-is. Pass ``[]`` to disable
        MCP tools entirely.
    skill_paths:
        Optional list of skill directories to expose via MAF's
        :class:`SkillsProvider`. ``None`` or ``[]`` disables skills.
    model:
        Optional model/deployment override. When omitted the
        :class:`ClientManager` resolves the value from environment variables.
    session_id:
        Optional session id for resuming a prior conversation. Must match
        ``[A-Za-z0-9._-]{1,128}``. When omitted, a fresh session is created
        and its id is returned in :class:`AgentResult`.
    sandbox_tools:
        Optional list of tools created via :func:`create_sandbox_tools` —
        bound to a specific ACA session pool. ``None`` adds no sandbox tools;
        pass a list to enable them. Per-call because the ACA session id is
        baked into each tool's closure.
    web_request_tools:
        Optional list of tools created via :func:`create_web_request_tools` —
        a dedicated channel parallel to ``sandbox_tools``, built once per
        agent at registration (stateless, no per-session binding needed).
        ``None``/``[]`` adds no ``web_request`` tool.

    Notes
    -----
    To fully disable all tools from a direct API call, pass
    ``tools=[], mcp_tools=[], sandbox_tools=None, web_request_tools=None``.
    """
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT

    _builder = _build_harness_agent_session if harness_config is not None else _build_agent_session_history
    agent, session, resolved_id = await _builder(
        instructions=instructions,
        session_id=session_id,
        tools=tools,
        mcp_tools=mcp_tools,
        skill_paths=skill_paths,
        model=model,
        sandbox_tools=sandbox_tools,
        system_addendum=system_addendum,
        workflow_enabled=workflow_enabled,
        workflow_durable_client=workflow_durable_client,
        agent_name=agent_name,
        web_request_tools=web_request_tools,
        **({"harness_config": harness_config} if harness_config is not None else {}),
    )

    lock = await _get_session_lock(resolved_id)
    async with lock:
        try:
            response = await asyncio.wait_for(
                agent.run(
                    prompt,
                    session=session,
                    options=_build_chat_options_from_environment(),
                ),
                timeout=timeout,
            )
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
    timeout: float | None = None,
    tools: list[Any] | None = None,
    mcp_tools: list[Any] | None = None,
    skill_paths: list[Path] | None = None,
    model: str | None = None,
    session_id: str | None = None,
    sandbox_tools: list[Any] | None = None,
    system_addendum: str | None = None,
    workflow_enabled: bool = False,
    workflow_durable_client: Any | None = None,
    agent_name: str | None = None,
    web_request_tools: list[Any] | None = None,
    harness_config: HarnessAgentConfig | None = None,
) -> AsyncIterator[str]:
    """SSE-formatted async generator yielding ``data: {...}\\n\\n`` lines.

    Tool-selection semantics match :func:`run_agent`:

    * ``tools`` controls the user tool set. ``None`` auto-discovers user
      tools from the app root; a provided list (including ``[]``) is used
      exactly as that user-tool set.
    * ``mcp_tools`` separately controls MCP tools. ``None`` auto-discovers
      from ``mcp.json``; pass ``[]`` to disable MCP tools.
    * ``sandbox_tools`` separately controls sandbox tools. ``None`` adds no
      sandbox tools; pass a list to enable them.
    * ``web_request_tools`` separately controls the ``web_request`` tool —
      a dedicated channel parallel to ``sandbox_tools``. ``None`` adds no
      ``web_request`` tool; pass a list to enable it.
    * ``skill_paths`` enables MAF's :class:`SkillsProvider` for the listed
      directories. ``None`` or ``[]`` disables skills.
    * To fully disable all tools from a direct API call, pass
      ``tools=[], mcp_tools=[], sandbox_tools=None, web_request_tools=None``.

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
        _builder = _build_harness_agent_session if harness_config is not None else _build_agent_session_history
        agent, session, resolved_id = await _builder(
            instructions=instructions,
            session_id=session_id,
            tools=tools,
            mcp_tools=mcp_tools,
            skill_paths=skill_paths,
            model=model,
            sandbox_tools=sandbox_tools,
            system_addendum=system_addendum,
            workflow_enabled=workflow_enabled,
            workflow_durable_client=workflow_durable_client,
            agent_name=agent_name,
            web_request_tools=web_request_tools,
            **({"harness_config": harness_config} if harness_config is not None else {}),
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
        pending_tool_calls: dict[str, dict[str, Any]] = {}
        emitted_tool_calls: set[str] = set()

        def buffer_function_call(item: Any) -> tuple[str | None, dict[str, Any]]:
            event = _function_call_event(item)
            call_id = event.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                return None, event

            pending = pending_tool_calls.setdefault(
                call_id,
                {
                    "type": "tool_start",
                    "tool_call_id": call_id,
                    "tool_name": event.get("tool_name"),
                    "arguments": None,
                },
            )
            if event.get("tool_name"):
                pending["tool_name"] = event["tool_name"]
            pending["arguments"] = _merge_tool_arguments(
                pending.get("arguments"),
                event.get("arguments"),
            )
            return call_id, pending

        async def emit_tool_start_if_ready(
            call_id: str, event: dict[str, Any]
        ) -> AsyncIterator[str]:
            if call_id in emitted_tool_calls:
                return
            if not _is_complete_json_argument(event.get("arguments")):
                return
            emitted_tool_calls.add(call_id)
            yield f"data: {json.dumps(event)}\n\n"

        async def emit_tool_start_before_result(call_id: str | None) -> AsyncIterator[str]:
            if call_id is None or call_id in emitted_tool_calls:
                return
            event = pending_tool_calls.get(call_id)
            if event is None:
                return
            emitted_tool_calls.add(call_id)
            yield f"data: {json.dumps(event)}\n\n"

        try:
            stream = agent.run(
                prompt,
                stream=True,
                session=session,
                options=_build_chat_options_from_environment(),
            )
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
                        call_id, event = buffer_function_call(item)
                        if call_id is None:
                            yield f"data: {json.dumps(event)}\n\n"
                        else:
                            async for output in emit_tool_start_if_ready(call_id, event):
                                yield output
                    elif ctype == "function_result":
                        call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
                        async for output in emit_tool_start_before_result(
                            call_id if isinstance(call_id, str) else None
                        ):
                            yield output
                        yield f"data: {json.dumps(_function_result_event(item), default=str)}\n\n"
                    # Unknown content types are intentionally ignored — the
                    # SSE vocabulary is fixed and the UI doesn't render them.
            for call_id, event in pending_tool_calls.items():
                if call_id not in emitted_tool_calls:
                    emitted_tool_calls.add(call_id)
                    yield f"data: {json.dumps(event)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except TimeoutError:
            yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
        except Exception as exc:
            logger.error("Agent stream failed: %s", exc, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
