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
* Chat-time sub-agent delegation (FRD 0006): when the resolved agent
  declares ``subagents``, :func:`build_subagent_tools` builds one
  ``delegate_<slug>`` :class:`~agent_framework.FunctionTool` per reference
  (via MAF's ``BaseAgent.as_tool()``) and appends it to that agent's own
  tool list, so the coordinator can call a specialist from inside its
  normal ``agent.run()`` tool-calling loop. Specialists are built fresh per
  request, in the isolated *delegated* execution role — see
  :func:`_build_delegated_agent` — and never expand their own ``subagents``
  (single-level delegation).

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
from ._observability import FaultDomain, current_span, record_delegate_call
from ._slug import delegate_tool_name
from .client_manager import get_client_manager
from .config import ResolvedAgent, SubagentRef
from .config.env import runtime_env_value
from .config.paths import get_app_root, resolve_config_dir
from .discovery.mcp import discover_mcp_servers
from .discovery.tools import discover_user_tools
from .registration.capabilities import AgentCapabilities
from .registration.catalog import AgentCatalog, CatalogEntry

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
    # Delegate (``delegate_<slug>``) calls that failed or timed out this run.
    # Tracked separately from ``tool_calls`` because a specialist failure is
    # sanitized to free text (FRD 0006 Decision #12) and wouldn't be
    # recognized by ``_looks_like_tool_error``'s JSON heuristic — see
    # ``registration._handlers._total_tool_error_count``.
    delegate_error_count: int = 0


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


# ---------------------------------------------------------------------------
# Chat-time sub-agent delegation (FRD 0006)
# ---------------------------------------------------------------------------
#
# A coordinator agent that declares ``subagents:`` gets one ``delegate_<slug>``
# function tool per reference, built via MAF's ``BaseAgent.as_tool()`` and run
# inside the coordinator's normal ``agent.run()`` tool-calling loop — no
# ``HandoffBuilder``, no HITL (out of scope for v1; see FRD 0006 §2).
#
# Delegation is single-level (Decision #6): a specialist built here is always
# built in the *delegated* execution role (:func:`_build_delegated_agent`),
# which never reads ``resolved.subagents`` and therefore can never itself gain
# ``delegate_*`` tools. This is a structural guarantee, not a runtime depth
# counter — there is no code path through which a delegated agent's own
# ``build_subagent_tools`` could ever run.


class _DelegateErrorTracker:
    """Per-request counter of *recoverable* ``delegate_<slug>`` failures.

    One instance is shared by every ``delegate_<slug>`` adapter built for a
    single coordinator run. ``AgentResult.delegate_error_count`` (and the
    streaming path's equivalent bookkeeping) reads :attr:`count` once the run
    completes. Only failures the adapter *recovers* from (specialist error or
    specialist-local timeout — FRD 0006 Decision #12) are counted; a
    propagated cancellation is not a "delegate error" and is never recorded
    here because the adapter never reaches its ``except`` clause for that case.
    """

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def record_error(self) -> None:
        self.count += 1


def _check_delegate_tool_name_collisions(
    resolved_tools: list[Any], delegate_tool_names: list[str]
) -> None:
    """Fail fast when a ``delegate_<slug>`` name collides with a live tool.

    ``registration.capabilities.validate_subagent_tool_names`` already runs
    this check at composition time, but only against *statically known* tool
    names. MCP server connections expose their actual tool names dynamically
    (discovered via the server's own ``tools/list``, invisible to any
    composition-time analysis — see ``discovery/mcp.py``), so the runtime
    re-checks collisions against the actual live tool list right before final
    assembly (FRD 0006 §4.2: "The runtime checks tool-name collisions again
    during final tool assembly because MCP and sandbox tool names may not be
    known earlier").
    """
    existing = {str(getattr(tool, "name", "") or "") for tool in resolved_tools}
    existing.discard("")
    for delegate_name in delegate_tool_names:
        if delegate_name in existing:
            raise ValueError(
                f"Tool name `{delegate_name}` collides with an existing tool "
                "on this agent, discovered while assembling the final tool "
                "list for this request. See docs/front-matter-spec.md#subagents."
            )


def _build_role_agent(
    chat_client: Any,
    *,
    instructions: str | None,
    tools: list[Any] | None,
    mcp_tools: list[Any] | None,
    skill_paths: list[Path] | None,
    sandbox_tools: list[Any] | None,
    web_request_tools: list[Any] | None,
    system_addendum: str | None,
    workflow_enabled: bool,
    workflow_durable_client: Any | None,
    agent_name: str | None,
    resolved_id: str | None,
    history_provider: Any | None,
    delegate_tools: list[Any] | None,
) -> Any:
    """Assemble the final tool list + context providers and build the MAF ``Agent``.

    Shared tail for both agent execution roles (FRD 0006 §4.6, Decisions
    #13/#15):

    * ``direct`` — a coordinator, or any agent invoked through its own
      trigger/endpoint. Callers pass a real ``history_provider`` and, when
      the resolved agent declares ``subagents``, ``delegate_tools``.
    * ``delegated`` — a specialist invoked *as* a ``delegate_<slug>`` tool by
      a coordinator. Callers pass ``history_provider=None`` (an isolated,
      session-less run — enforced independently by MAF's own
      ``propagate_session=False``, mirrored here so a delegated agent never
      even gets a *local* history context provider) and
      ``delegate_tools=None``. Per-request sandbox tools and main-only
      Dynamic-Workflow tools are simply never passed for this role
      (``sandbox_tools=None``, ``workflow_enabled=False`` — see
      :func:`_build_delegated_agent`), so they are naturally absent rather
      than stripped from a shared list.
    """
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
                session_id=resolved_id or "",
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

    if delegate_tools:
        delegate_tool_names = [str(getattr(tool, "name", "") or "") for tool in delegate_tools]
        _check_delegate_tool_name_collisions(resolved_tools, delegate_tool_names)
        resolved_tools.extend(delegate_tools)

    context_providers: list[Any] = []
    if history_provider is not None:
        context_providers.append(history_provider)
    skills_provider = _build_skills_provider(skill_paths)
    if skills_provider is not None:
        context_providers.append(skills_provider)

    effective_instructions = instructions.strip() if instructions and instructions.strip() else None
    if system_addendum:
        effective_instructions = (effective_instructions or "") + system_addendum

    from agent_framework import Agent

    return Agent(
        chat_client,
        instructions=effective_instructions,
        tools=resolved_tools,
        context_providers=context_providers,
    )


def _build_delegated_agent(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> Any:
    """Build one specialist's MAF ``Agent`` in the *delegated* execution role.

    "Runs as itself" (FRD 0006 §5 Decisions #13/#15): its own instructions,
    model, and static user/MCP/skills tools — but never a per-request sandbox
    tool (bound to the *coordinator's* chat session/ACA pool, not the
    specialist's own) and never main-only Dynamic-Workflow tools. Both are
    naturally absent here because this helper never receives them, not
    because anything is stripped from a shared tool list.
    ``resolved.subagents`` is deliberately never read: this is the structural
    enforcement of single-level delegation (Decision #6) — a delegated
    specialist cannot itself gain ``delegate_*`` tools, with no runtime depth
    counter required.
    """
    client_manager = get_client_manager()
    chat_client = client_manager.build_chat_client(resolved.model)
    return _build_role_agent(
        chat_client,
        instructions=resolved.instructions,
        tools=list(capabilities.filtered_user_tools or []),
        mcp_tools=list(capabilities.filtered_mcp_tools or []),
        skill_paths=capabilities.enabled_skill_paths,
        sandbox_tools=None,
        web_request_tools=capabilities.web_request_tools,
        system_addendum=None,
        workflow_enabled=False,
        workflow_durable_client=None,
        agent_name=resolved.name,
        resolved_id=None,
        history_provider=None,
        delegate_tools=None,
    )


def _sanitize_delegate_failure(slug: str, exc: BaseException) -> str:
    """Sanitized, model-facing message for a recovered delegate failure.

    Deliberately generic — the *real* exception detail goes to telemetry via
    :meth:`RuntimeSpan.set_error` (in the ``execute_tool delegate_<slug>``
    span) and :func:`record_delegate_call`, never to the coordinator's model
    context (FRD 0006 Decision #12: "full detail to telemetry, sanitized
    string to the model").
    """
    return (
        f"The '{slug}' specialist could not complete this task ({type(exc).__name__}). "
        "Consider trying again, rephrasing the request, or proceeding without it."
    )


def _build_delegate_tool(
    ref: SubagentRef,
    entry: CatalogEntry,
    *,
    coordinator_deadline: float,
    tracker: _DelegateErrorTracker,
) -> Any:
    """Build one ``delegate_<slug>`` ``FunctionTool`` for the reference ``ref``.

    Calls MAF's ``BaseAgent.as_tool()`` to get the real, MAF-shaped
    ``FunctionTool`` (JSON-schema argument model, name, the ``ctx``-parameter
    injection ``FunctionTool.invoke()`` relies on) and then swaps its
    ``.func`` for a thin adapter implementing the failure/cancellation split
    and per-specialist serialization (FRD 0006 §4.6, §5 Decision #12).
    ``FunctionTool`` is a plain mutable object in the pinned
    ``agent-framework-core``; ``FunctionTool.__call__`` reads ``self.func``
    fresh on every invocation, so reassigning it after construction is safe
    as long as the replacement's first parameter keeps the name ``ctx`` (the
    only thing MAF's own parameter-injection cached from the original
    function's signature).

    The specialist ``Agent`` is built once, here, for this single tool
    (eagerly, per FRD 0006 §4.7 — "build eagerly; run the specialist only if
    the coordinator selects it"); the adapter re-invokes MAF's own
    ``_agent_wrapper`` (captured as ``original_func``) on every call, so
    ``propagate_session=False`` and the JSON-schema argument handling MAF
    already implements are reused as-is — this adapter only adds the
    timeout/cancellation split, serialization, and observability enrichment
    on top.
    """
    resolved = entry.resolved
    slug = ref.agent
    tool_name = delegate_tool_name(slug)
    description = ref.when or resolved.description

    specialist_agent = _build_delegated_agent(resolved, entry.capabilities)
    tool = specialist_agent.as_tool(
        name=tool_name,
        description=description,
        arg_name="task",
        arg_description=(
            "A complete, self-contained instruction for the specialist. The "
            "specialist does not see the coordinator's conversation history "
            "or any other context — include every fact, detail, and "
            "requirement the specialist needs to complete the task."
        ),
        approval_mode="never_require",
        propagate_session=False,
    )
    original_func = tool.func
    specialist_lock = asyncio.Lock()
    specialist_timeout = resolved.timeout

    async def _delegate_adapter(ctx: Any, **kwargs: Any) -> str:
        loop = asyncio.get_event_loop()
        remaining = max(0.0, coordinator_deadline - loop.time())
        effective_timeout = min(specialist_timeout, remaining)
        task_text = str(kwargs.get("task", "") or "")

        span = current_span()
        span.set_attribute("af.delegate.specialist", slug)
        span.set_attribute("af.delegate.task_bytes", len(task_text))
        span.set_content("af.delegate.task", task_text)

        # Only the actual specialist run is serialized per-specialist
        # (Decision #14): different specialists always run in parallel;
        # concurrent calls to the *same* specialist wait their turn here.
        async with specialist_lock:
            try:
                # `original_func` is MAF's own `_agent_wrapper` (from
                # `as_tool()`), which always resolves to a `str`
                # (`AgentRunResponse.text`); the explicit `str(...)` only
                # satisfies mypy, since `tool.func` is typed `Any`.
                result = str(
                    await asyncio.wait_for(original_func(ctx, **kwargs), timeout=effective_timeout)
                )
            except TimeoutError:
                # Specialist-local timeout: recoverable — the coordinator
                # continues (Decision #12). A propagated ambient/request
                # cancellation is `asyncio.CancelledError`, a BaseException
                # that this `except TimeoutError` (and the `except Exception`
                # below) never catches, so it is left to propagate untouched.
                tracker.record_error()
                record_delegate_call(error=True)
                span.set_attribute("af.delegate.outcome", "timeout")
                span.set_error(
                    f"delegate_{slug} timed out after {effective_timeout:.1f}s",
                    fault_domain=FaultDomain.DELEGATE,
                )
                return (
                    f"The '{slug}' specialist did not respond in time and was "
                    "stopped. Consider a narrower request, trying again, or "
                    "proceeding without it."
                )
            except Exception as exc:
                tracker.record_error()
                record_delegate_call(error=True)
                span.set_attribute("af.delegate.outcome", "error")
                span.set_error(f"delegate_{slug} failed: {exc}", fault_domain=FaultDomain.DELEGATE)
                return _sanitize_delegate_failure(slug, exc)

        record_delegate_call(error=False)
        span.set_attribute("af.delegate.outcome", "success")
        span.set_attribute("af.delegate.response_bytes", len(result))
        span.set_content("af.delegate.result", result)
        return result

    tool.func = _delegate_adapter
    return tool


async def build_subagent_tools(
    subagents: list[SubagentRef] | None,
    catalog: AgentCatalog | None,
    *,
    coordinator_deadline: float,
) -> tuple[list[Any], _DelegateErrorTracker]:
    """Build one ``delegate_<slug>`` tool per ``subagents`` reference.

    Each specialist ``Agent`` is built FRESH for this call, in the
    *delegated* role (:func:`_build_delegated_agent`), reusing the
    process-wide :class:`ClientManager` but never a live agent instance
    cached from a previous request — MAF's ``Agent.run()`` self-mutates, so
    per-request construction is the only safe option, and it is cheap (FRD
    0006 §4.7). Tools are built eagerly for every reference; a specialist
    only actually *runs* if the coordinator's model selects the
    corresponding ``delegate_<slug>`` tool call.

    Returns ``(tools, tracker)``. ``tracker`` is incremented by each
    adapter on a *recoverable* specialist failure/timeout — see
    :class:`_DelegateErrorTracker`.
    """
    tracker = _DelegateErrorTracker()
    tools: list[Any] = []
    if not subagents:
        return tools, tracker
    if catalog is None:
        # Unreachable through app.py's composition root, which always builds
        # and threads a non-None AgentCatalog whenever any agent declares
        # subagents. Guarded so a wiring mistake (e.g. a hand-rolled call
        # site) fails with a clear message instead of a confusing KeyError.
        raise RuntimeError(
            "This agent declares `subagents` but no AgentCatalog was "
            "provided to build_subagent_tools(). This is an internal "
            "consistency error: the composition root (app.py) should "
            "always thread a non-None catalog to any agent with subagents."
        )

    for ref in subagents:
        entry = catalog.get(ref.agent)
        if entry is None:
            # Also unreachable in a correctly composed app:
            # `validate_subagent_references` already rejects unknown
            # references at startup (FRD 0006 §4.4). Guarded here only so a
            # programming error surfaces as a clear message instead of a
            # bare KeyError deep inside tool assembly.
            raise RuntimeError(
                f"subagents reference `{ref.agent}` was not found in the "
                "AgentCatalog. This should have been rejected at startup; "
                "this is an internal consistency error."
            )
        tools.append(
            _build_delegate_tool(
                ref,
                entry,
                coordinator_deadline=coordinator_deadline,
                tracker=tracker,
            )
        )
    return tools, tracker


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
    subagents: list[SubagentRef] | None = None,
    catalog: AgentCatalog | None = None,
    coordinator_deadline: float | None = None,
) -> tuple[Any, Any, str, _DelegateErrorTracker | None]:
    """Construct the chat client, agent, AgentSession, and history provider.

    Returns ``(agent, session, resolved_session_id, delegate_error_tracker)``.
    ``delegate_error_tracker`` is ``None`` unless ``subagents`` is non-empty.

    When ``subagents`` is non-empty, one ``delegate_<slug>`` tool is built
    per reference (:func:`build_subagent_tools`) and appended to this
    (coordinator, ``direct``-role) agent's own tool list — never the other
    way around; the tools built here are for *this* agent to call its
    specialists, not tools the specialists get.
    """
    # Imported here so a missing optional dependency surfaces only when actually
    # needed (e.g. tests that don't run the runtime path).
    from agent_framework import AgentSession

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

    delegate_tools: list[Any] | None = None
    delegate_error_tracker: _DelegateErrorTracker | None = None
    if subagents:
        effective_deadline = (
            coordinator_deadline
            if coordinator_deadline is not None
            else asyncio.get_event_loop().time() + DEFAULT_TIMEOUT
        )
        delegate_tools, delegate_error_tracker = await build_subagent_tools(
            subagents, catalog, coordinator_deadline=effective_deadline
        )

    agent = _build_role_agent(
        chat_client,
        instructions=instructions,
        tools=tools,
        mcp_tools=mcp_tools,
        skill_paths=skill_paths,
        sandbox_tools=sandbox_tools,
        web_request_tools=web_request_tools,
        system_addendum=system_addendum,
        workflow_enabled=workflow_enabled,
        workflow_durable_client=workflow_durable_client,
        agent_name=agent_name,
        resolved_id=resolved_id,
        history_provider=history_provider,
        delegate_tools=delegate_tools,
    )

    return agent, session, resolved_id, delegate_error_tracker


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
    subagents: list[SubagentRef] | None = None,
    catalog: AgentCatalog | None = None,
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
    subagents:
        Optional ``subagents:`` references resolved from this agent's front
        matter (FRD 0006). Each reference gets a ``delegate_<slug>`` tool
        appended to this agent's tool list, built from ``catalog`` — see
        :func:`build_subagent_tools`. ``None``/``[]`` adds no delegation
        tools.
    catalog:
        The process-wide :class:`AgentCatalog` (slug -> resolved specialist +
        capabilities) used to build any ``subagents`` reference. Required
        whenever ``subagents`` is non-empty; ignored otherwise.

    Notes
    -----
    To fully disable all tools from a direct API call, pass
    ``tools=[], mcp_tools=[], sandbox_tools=None, web_request_tools=None``.
    """
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    # Computed before building the agent so a delegate tool's adapter can cap
    # its own specialist timeout at "however much of *this* run's budget is
    # left" (FRD 0006 Decision #12: "effective timeout = min(specialist,
    # coordinator remaining)").
    coordinator_deadline = asyncio.get_event_loop().time() + timeout

    agent, session, resolved_id, delegate_error_tracker = await _build_agent_session_history(
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
        subagents=subagents,
        catalog=catalog,
        coordinator_deadline=coordinator_deadline,
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
        delegate_error_count=delegate_error_tracker.count if delegate_error_tracker else 0,
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
    subagents: list[SubagentRef] | None = None,
    catalog: AgentCatalog | None = None,
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
    * ``subagents``/``catalog`` add ``delegate_<slug>`` tools (FRD 0006), one
      per reference — see :func:`run_agent`. Delegate calls surface through
      the same ``tool_start``/``tool_end`` events as any other tool call; the
      per-run delegate-error count is not currently surfaced in the SSE
      vocabulary (only in :class:`AgentResult` for the non-streaming path),
      since telemetry (spans/metrics) already captures it identically
      regardless of whether the coordinator's own run is streamed.
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
    # Computed before building the agent (see run_agent) so a delegate tool's
    # adapter can cap its own specialist timeout at this run's remaining
    # budget. Reused, unchanged, as `deadline` further down for the existing
    # per-update stream timeout check.
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout

    try:
        agent, session, resolved_id, _delegate_error_tracker = await _build_agent_session_history(
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
            subagents=subagents,
            catalog=catalog,
            coordinator_deadline=deadline,
        )
    except Exception as exc:
        logger.error("Failed to build agent session: %s", exc, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return

    yield f"data: {json.dumps({'type': 'session', 'session_id': resolved_id})}\n\n"

    lock = await _get_session_lock(resolved_id)
    async with lock:
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
