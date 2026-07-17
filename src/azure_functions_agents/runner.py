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
  hand-written ``delegate_<slug>`` :class:`~agent_framework.FunctionTool`
  per reference (the same ``@tool(schema=...)`` pattern as the
  ``web_request``/``execute_python`` system tools — see
  :mod:`.system_tools.web_request` — not MAF's ``BaseAgent.as_tool()``) and
  appends it to that agent's own tool list, so the coordinator can call a
  specialist from inside its normal ``agent.run()`` tool-calling loop. A
  delegate only ever needs the specialist's final answer as a single
  string, so its handler builds a FRESH specialist :class:`agent_framework.
  Agent`, in the isolated *delegated* execution role — see
  :func:`_build_delegated_agent` — on every call and awaits its
  non-streaming ``agent.run(task)`` directly. Building fresh per call (not
  once per request) means concurrent calls, including repeated calls to the
  *same* specialist, never share a live agent instance, so no per-specialist
  lock is needed. Specialists never expand their own ``subagents`` (single-
  level delegation).

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
import contextlib
import json
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ._blob_history import build_blob_provider_from_environment
from ._function_tool import tool
from ._logger import logger
from ._observability import (
    FaultDomain,
    LifecycleStage,
    current_span,
    record_delegate_call,
    start_span,
)
from ._slug import delegate_tool_name
from .client_manager import get_client_manager
from .config import ResolvedAgent, SubagentRef
from .config.env import runtime_env_value
from .config.paths import get_app_root, resolve_config_dir
from .discovery.mcp import discover_mcp_servers
from .discovery.tools import discover_user_tools

# `_handlers` is always fully imported as a side effect of the
# `.registration.*` imports above (`registration/__init__.py` imports
# `.endpoints`, which imports `._handlers`), so importing this shared
# tool-error-detection helper here — rather than duplicating the JSON
# error-envelope/stderr heuristic inline (M3) — does not introduce a new
# import cycle: `_handlers.py` itself has no module-level dependency back on
# `runner.py` (its own dependency on `run_agent`/`run_agent_stream` is a
# lazy, call-time `importlib.import_module`, specifically to avoid that).
from .registration._handlers import _looks_like_tool_error
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
# A coordinator agent that declares ``subagents:`` gets one hand-written
# ``delegate_<slug>`` function tool per reference (:func:`_build_delegate_tool`
# — the same ``@tool(schema=...)`` pattern as the ``web_request``/
# ``execute_python`` system tools, not MAF's ``BaseAgent.as_tool()``) and run
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
    """Fail fast when a ``delegate_<slug>`` name collides with a known tool name.

    ``registration.capabilities.validate_subagent_tool_names`` already runs
    an equivalent check at composition time; this is the runtime's re-check
    right before final tool assembly (FRD 0006 §4.2: "The runtime checks
    tool-name collisions again during final tool assembly because MCP and
    sandbox tool names may not be known earlier"), covering sandbox/workflow
    tool names this repo only finalizes at this point.

    Scope note — this only inspects each tool object's own ``.name`` (e.g. an
    MCP server *connection's* configured name from ``mcp.json``, such as
    "billing-mcp-server"). It does **not** see the individual remote
    tools/functions an MCP server exposes once connected: those are a
    dynamically-populated, separate collection
    (``agent_framework.MCPTool.functions``, populated by ``MCPTool
    .load_tools()`` — see ``discovery/mcp.py``) that this repo never expands
    itself, and this check runs before any such connection happens. A remote
    tool literally named e.g. ``delegate_billing`` is therefore invisible to
    this guard. That gap is not a silent hole in practice: MAF's own
    ``Agent.run()`` independently re-checks tool-name uniqueness once it
    expands ``MCPTool.functions`` into its final tool list
    (``agent_framework._agents.BaseAgent._prepare_run_context`` ->
    ``agent_framework._tools._append_unique_tools``), raising ``ValueError``
    before any model or tool call happens — see
    ``test_real_maf_agent_run_raises_on_expanded_mcp_function_collision`` in
    ``tests/test_runner_delegation.py`` for a real (non-mocked) proof of that
    backstop.
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
      session-less run — the delegate handler's own ``agent.run(task)`` call
      never passes a ``session=`` argument either, so a delegated agent
      never even gets a *local* history context provider) and
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
        name=agent_name,
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
        # The *slug* (not `resolved.name`, the display name) — this becomes
        # the MAF `Agent`'s `name=`, which MAF uses for its `invoke_agent`
        # OTel span attribution (`gen_ai.agent.name`). The delegate tool is
        # named `delegate_<slug>` (see `_build_delegate_tool`), so using the
        # slug here lets a trace viewer correlate "the tool call named
        # `delegate_<slug>`" with "the nested `invoke_agent {slug}` span it
        # produced" (FRD 0006 §5 Decision #19) without a name/slug lookup.
        agent_name=resolved.slug,
        resolved_id=None,
        history_provider=None,
        delegate_tools=None,
    )


def _sanitize_delegate_failure(slug: str, exc: BaseException) -> str:
    """Sanitized, model-facing message for a recovered delegate failure.

    Deliberately generic *and class-independent* — this string must not vary
    by the internal exception type (e.g. it must read identically whether
    the specialist raised a ``ValueError`` or a ``ConnectionError``), so the
    coordinator's model never learns anything about the specialist's
    internals from wording alone. The *real* exception detail (type,
    message, traceback) goes only to telemetry, via
    :meth:`RuntimeSpan.record_exception` (in the ``execute_tool
    delegate_<slug>`` span) and :func:`record_delegate_call` — never to the
    coordinator's model context (FRD 0006 Decision #12: "full detail to
    telemetry, sanitized string to the model").
    """
    return (
        f"The '{slug}' specialist could not complete this task. "
        "Consider trying again, rephrasing the request, or proceeding without it."
    )


async def _finalize_maf_stream(stream: Any, exc: BaseException) -> None:
    """Best-effort finalize a MAF ``ResponseStream`` chain after timeout/cancellation.

    ``ResponseStream.__anext__`` (``agent_framework._types``) only runs its
    registered cleanup hooks — which close the underlying ``invoke_agent``
    OTel span, flush usage stats, and invoke provider callbacks — from its
    own ``except StopAsyncIteration`` / ``except Exception`` branches. It has
    no handler for ``BaseException``, so ``asyncio.CancelledError`` (which is
    exactly what ``asyncio.wait_for`` injects into whichever task is
    currently awaiting ``__anext__()`` when its timeout expires) bypasses
    that cleanup entirely. Without it, the span backing that stream is only
    ever closed by a separate ``weakref.finalize`` GC safety net that never
    records the exception/outcome and fires at a nondeterministic time.

    Call this from the ``except`` handler that catches that timeout/
    cancellation for the coordinator's own streaming pull (``run_agent_stream``)
    so the stream's own finalization still runs deterministically (FRD 0006
    round-3 M2 fix). The ``delegate_<slug>`` adapter (``_build_delegate_tool``)
    no longer has any use for this: it runs its specialist through plain
    non-streaming ``agent.run(task)``, and a non-streaming run's OTel spans
    are opened with an ordinary ``with``/context-manager (``AgentTelemetryLayer
    ._run`` / ``ChatTelemetryLayer._get_response`` in ``agent_framework
    .observability``), which — unlike ``ResponseStream.__anext__``'s bespoke
    per-branch cleanup-hook protocol — closes deterministically on *any*
    exception, ``asyncio.CancelledError`` included, via the standard
    ``with`` statement's ``__exit__`` guarantee. No delegate-side workaround
    is needed (verified against installed ``agent-framework-core==1.3.0``;
    see FRD 0006 §5 Decision #20).

    Round-4 (B2c) also walks ``stream._inner_stream`` to finalize any
    *further* ``ResponseStream`` this one already resolved a reference to —
    e.g. ``Agent.run(stream=True)``'s agent-level ``.map()``-transform
    stream (``agent_framework._agents.RawAgent._parse_streaming_response``,
    installed ``agent-framework-core==1.3.0``, lines 1102-1112:
    ``stream_response.map(transform=..., finalizer=...)``; the returned
    stream's inner reference is set via ``ResponseStream.map``,
    ``_types.py`` lines 2962-2964, and resolved into ``_inner_stream`` on
    first pull, ``_types.py`` lines 3004-3006). This is unconditionally
    safe: a stream that never wraps another (including every non-MAF fake
    used in this package's own tests) simply has ``_inner_stream is None``
    (the ``_types.py`` line 2890 default), so the loop body still runs
    exactly once for it — identical to this function's pre-round-4
    behavior.

    Known, *verified* residual gap this cannot close: when the concrete
    chat client's MRO stacks ``FunctionInvocationLayer`` above
    ``ChatTelemetryLayer`` — which is exactly what this repository's real
    chat clients do (``agent_framework.openai.OpenAIChatClient`` /
    ``agent_framework.foundry.FoundryChatClient``; confirmed via the
    installed ``agent-framework-openai`` package's ``_chat_client.py``
    lines 2878-2881: ``class OpenAIChatClient(FunctionInvocationLayer,
    ChatMiddlewareLayer, ChatTelemetryLayer, BaseChatClient)``) — and
    function invocation is enabled (the default), each per-turn
    ``chat {model}`` span's backing ``ResponseStream`` (the one
    ``ChatTelemetryLayer.get_response`` attaches its own
    ``_record_duration``/``_finalize_stream`` cleanup hooks to,
    ``observability.py`` lines 1366-1372) is created and consumed as a
    *purely local variable* inside ``FunctionInvocationLayer.get_response``'s
    own internal streaming closure (installed ``agent-framework-core``
    package, ``_tools.py``, the ``_stream()`` async generator starting at
    line 2513 — see the per-iteration ``inner_stream`` at lines 2543-2564
    and the final iteration's ``final_inner_stream`` at lines 2642-2657).
    That local variable is never assigned to ``_inner_stream`` or exposed
    via any other externally reachable attribute on the object this (or
    any other) caller can hold a reference to, and the ``_stream()``
    generator has no ``try``/``except``/``finally`` of its own around that
    consumption either (verified: none appears anywhere in its body, lines
    2513-2670). So on an abrupt cancellation/timeout, that specific span
    can only ever close via the generator running to natural completion or
    via ``ChatTelemetryLayer``'s own ``weakref.finalize`` GC safety net
    (``observability.py`` line 1372, mirroring the agent-level net at line
    1642) firing nondeterministically, without recording the timeout/
    cancellation outcome. This is a genuine upstream MAF architecture
    limitation with no discoverable public or private workaround from
    outside ``agent_framework`` itself — there is nothing further this
    function can safely do about it for that composition. (The walk above
    still has real, verifiable effect for compositions that do *not*
    interpose ``FunctionInvocationLayer`` — e.g. function invocation
    explicitly disabled, or a ``ChatTelemetryLayer`` + ``BaseChatClient``
    client used directly — where the chat-level stream *is* reachable via
    ``_inner_stream``.)

    Deliberately defensive throughout: ``stream`` may be ``None`` (nothing
    was captured yet), and MAF's private cleanup surface
    (``_run_cleanup_hooks``/``_stream_error``/``_inner_stream``) is not part
    of any public contract this package depends on, so any failure while
    probing/calling it is swallowed rather than allowed to mask the
    *original* timeout/cancellation being handled.
    """
    while stream is not None:
        # Read `_inner_stream` before running this level's cleanup hooks
        # (which may, defensively, mutate this object's state) so the next
        # hop is captured regardless of what this level's hooks do.
        next_stream = getattr(stream, "_inner_stream", None)
        run_cleanup_hooks = getattr(stream, "_run_cleanup_hooks", None)
        if callable(run_cleanup_hooks):
            with contextlib.suppress(Exception):
                has_stream_error_slot = hasattr(stream, "_stream_error")
                if has_stream_error_slot and stream._stream_error is None:
                    # Mirrors what `ResponseStream.__anext__`'s own `except
                    # Exception` branch does on a normal failure: stash the
                    # error so the registered cleanup hook (MAF's
                    # `_finalize_stream`, see `agent_framework.observability`)
                    # can `capture_exception` it on the span instead of
                    # silently treating this as a clean finish.
                    stream._stream_error = exc
                    try:
                        await run_cleanup_hooks()
                    finally:
                        stream._stream_error = None
                else:
                    await run_cleanup_hooks()
        stream = next_stream


# Timing tolerance (S3b) for telling a genuine `asyncio.wait_for` deadline
# expiry apart from a `TimeoutError` the specialist happened to raise on its
# own, well inside the budget. `asyncio.TimeoutError is TimeoutError` as of
# Python 3.11, so there is no type-based way to distinguish the two — timing
# is the only signal available. This is not perfectly precise under heavy
# event-loop scheduling delay (a genuine expiry could in principle be
# observed a little early), but it correctly separates the common cases: an
# inner exception typically fires far earlier than the deadline, while a
# real `wait_for` expiry fires essentially exactly at it.
_INNER_TIMEOUT_MISCLASSIFICATION_TOLERANCE_SECONDS = 0.05


def _record_generic_delegate_failure(
    span: Any, tracker: _DelegateErrorTracker, slug: str, exc: BaseException
) -> str:
    """Shared bookkeeping for a recoverable, non-deadline delegate failure.

    Used by the adapter's ``except Exception`` branch, and (S3b) by its
    ``except TimeoutError`` branch when the timing heuristic indicates the
    caught ``TimeoutError`` was raised by the specialist's own code rather
    than by ``asyncio.wait_for``'s deadline actually expiring.
    """
    tracker.record_error()
    record_delegate_call(error=True)
    span.set_attribute("af.delegate.outcome", "error")
    # `record_exception` is a strict superset of `set_error` (status + fault
    # domain + the actual exception object/traceback) — using it here (B4)
    # preserves the real exception type/detail in telemetry instead of a
    # flattened string, without double-setting status.
    span.record_exception(exc, fault_domain=FaultDomain.DELEGATE)
    return _sanitize_delegate_failure(slug, exc)


class _DelegateTaskParams(BaseModel):
    """Argument schema for a ``delegate_<slug>`` tool call — always one field.

    Mirrors ``BaseAgent.as_tool()``'s own ``arg_name="task"`` single-string
    contract, but as an ordinary Pydantic schema (this repo's own ``@tool``
    convention — see ``system_tools/web_request.py``/``system_tools/
    sandbox.py``) rather than MAF's hand-assembled raw JSON-schema dict.
    """

    task: str = Field(
        description=(
            "A complete, self-contained instruction for the specialist. The "
            "specialist does not see the coordinator's conversation history "
            "or any other context — include every fact, detail, and "
            "requirement the specialist needs to complete the task."
        )
    )


def _build_delegate_tool(
    ref: SubagentRef,
    entry: CatalogEntry,
    *,
    coordinator_deadline: float,
    tracker: _DelegateErrorTracker,
) -> Any:
    """Build one ``delegate_<slug>`` ``FunctionTool`` for the reference ``ref``.

    A hand-written function tool — the same ``@tool(schema=...)`` pattern
    this repo already uses for ``web_request``/``execute_python`` (see
    ``system_tools/web_request.py``'s ``create_web_request_tools``) — rather
    than MAF's ``BaseAgent.as_tool()``. A delegate only ever needs the
    specialist's final answer as a single string back to the coordinator; it
    never streams the specialist's tokens anywhere (surfacing specialist
    deltas is an explicit FRD 0006 non-goal — §4.12 "SSE is a black box at
    the boundary"). So there is no reason to run the specialist through
    ``Agent.run(stream=True)`` at all, which is all ``as_tool()`` ever did
    internally (via its own ``_agent_wrapper``, in ``agent_framework
    ._agents``) before ``await``-ing ``stream.get_final_response()`` right
    back into one string anyway.

    The handler below builds a FRESH specialist ``Agent`` on every call
    (:func:`_build_delegated_agent` — reusing the process-wide
    ``ClientManager``, never a live agent instance cached from a previous
    call) and awaits its plain, non-streaming ``agent.run(task)`` directly
    (FRD 0006 §4.7, §5 Decision #20). Because each call gets its own,
    unshared ``Agent`` object, there is no mutable state for concurrent
    calls to race on — including concurrent calls to the *same* specialist —
    so no per-specialist lock, monkeypatch, or captured-stream bookkeeping is
    needed (contrast the old design's ``asyncio.Lock`` + ``specialist_agent
    .run`` rebind, removed by this change).
    """
    resolved = entry.resolved
    capabilities = entry.capabilities
    slug = ref.agent
    tool_name = delegate_tool_name(slug)
    description = ref.when or resolved.description
    specialist_timeout = resolved.timeout

    @tool(
        name=tool_name,
        description=description,
        schema=_DelegateTaskParams,
        approval_mode="never_require",
    )
    async def delegate(params: _DelegateTaskParams) -> str:
        loop = asyncio.get_event_loop()
        task_text = params.task

        span = current_span()
        span.set_attribute("af.delegate.specialist", slug)
        span.set_attribute("af.delegate.task_bytes", len(task_text))
        span.set_content("af.delegate.task", task_text)

        # The coordinator's deadline is an absolute wall-clock point;
        # `effective_timeout = min(specialist, coordinator remaining)` per
        # FRD 0006 Decision #12. Checked *before* building the specialist
        # `Agent` at all: if the budget is already exhausted, skip the call
        # entirely (building an `Agent` is cheap, but there is still no
        # reason to do it for a run that can never be attempted) rather than
        # relying on `wait_for(timeout<=0)`'s cancel-before-first-step
        # behavior to prevent it from starting.
        remaining = max(0.0, coordinator_deadline - loop.time())
        effective_timeout = min(specialist_timeout, remaining)
        if effective_timeout <= 0:
            exc = TimeoutError(f"delegate_{slug}: coordinator budget exhausted before dispatch")
            tracker.record_error()
            record_delegate_call(error=True)
            span.set_attribute("af.delegate.outcome", "timeout")
            span.set_attribute("af.delegate.timeout_seconds", effective_timeout)
            span.record_exception(exc, fault_domain=FaultDomain.DELEGATE)
            return (
                f"The '{slug}' specialist did not respond in time and was "
                "stopped. Consider a narrower request, trying again, or "
                "proceeding without it."
            )

        # `call_start` stays unbound (`None`) until the specialist `Agent` is
        # actually built and `wait_for` is about to be dispatched — see the
        # `except TimeoutError` branch below, which treats a still-`None`
        # `call_start` (i.e. `_build_delegated_agent` itself raised) the same
        # as an early inner exception: never a genuine `wait_for` deadline
        # event, since the timed run never even started.
        call_start: float | None = None
        try:
            # Building the specialist `Agent` (client construction, tool
            # assembly) is inside this `try` too — not before it — so a
            # construction failure (e.g. a misconfigured specialist model)
            # is caught by the same `except Exception` branch below and
            # returned as a recoverable, sanitized failure (FRD 0006
            # Decision #12) instead of propagating unhandled out of this
            # tool call and aborting the whole coordinator turn.
            specialist_agent = _build_delegated_agent(resolved, capabilities)
            call_start = loop.time()
            response = await asyncio.wait_for(specialist_agent.run(task_text), timeout=effective_timeout)
        except asyncio.CancelledError:
            # Parent/request cancellation, not a specialist-local timeout —
            # never recorded as a (recoverable) delegate *error* (Decision #12
            # and `_DelegateErrorTracker`'s docstring are explicit that only
            # *recoverable* failures count there). It IS recorded as a
            # delegate *call*, though (`error=False` only suppresses the
            # error counter, not the call counter — see
            # `record_delegate_call`'s docstring): the specialist call was
            # genuinely dispatched (this branch is only reachable once
            # `wait_for` is actually awaiting `specialist_agent.run(...)`),
            # so it must not be invisible to the call metric. Annotate the
            # outcome for telemetry's sake too, then re-raise immediately so
            # the cancellation still propagates and aborts the run — this is
            # an observability side-effect on the way out, never a swallow.
            # No explicit stream/span finalization call is needed here
            # (unlike the old streaming design): a non-streaming
            # `agent.run()`'s OTel spans are opened with an ordinary
            # `with`/context-manager, which closes deterministically on any
            # exception — `asyncio.CancelledError` included — via the
            # standard `with` statement's `__exit__` guarantee (verified
            # against installed `agent-framework-core==1.3.0`; see FRD 0006
            # §5 Decision #20).
            record_delegate_call(error=False)
            span.set_attribute("af.delegate.outcome", "cancelled")
            raise
        except TimeoutError as exc:
            # This handler catches *both* a genuine `wait_for` deadline
            # expiry *and* any `TimeoutError` that happens to propagate from
            # inside the specialist's own tool-calling (e.g. an inner
            # HTTP-client timeout) — `asyncio.TimeoutError is TimeoutError`
            # as of Python 3.11, so there is no type-based way to tell the
            # two apart. Compare elapsed wall time against
            # `effective_timeout`: an inner exception typically surfaces
            # well before the deadline, while a real `wait_for` expiry fires
            # essentially exactly at it. `call_start is None` means
            # `_build_delegated_agent` itself raised this `TimeoutError`
            # before the timed run ever started — unambiguously not a
            # genuine deadline event either, so it takes the same generic-
            # error branch as an early inner exception.
            elapsed = None if call_start is None else loop.time() - call_start
            if (
                elapsed is None
                or elapsed < effective_timeout - _INNER_TIMEOUT_MISCLASSIFICATION_TOLERANCE_SECONDS
            ):
                # Not actually a coordinator-budget/deadline-expiry event —
                # an ordinary specialist failure (construction or inner call)
                # that happens to be a `TimeoutError` instance. Classify like
                # any other recoverable delegate failure instead of as
                # `outcome=timeout`.
                return _record_generic_delegate_failure(span, tracker, slug, exc)
            # Specialist-local (or coordinator-budget) timeout: recoverable —
            # the coordinator continues (Decision #12).
            tracker.record_error()
            record_delegate_call(error=True)
            span.set_attribute("af.delegate.outcome", "timeout")
            span.set_attribute("af.delegate.timeout_seconds", effective_timeout)
            # Record whichever `TimeoutError` instance was actually caught
            # instead of constructing a fresh, synthetic one — `exc` may be
            # `wait_for`'s own expiry error or an inner one; preserving the
            # real instance keeps whatever detail it carries.
            span.record_exception(exc, fault_domain=FaultDomain.DELEGATE)
            return (
                f"The '{slug}' specialist did not respond in time and was "
                "stopped. Consider a narrower request, trying again, or "
                "proceeding without it."
            )
        except Exception as exc:
            return _record_generic_delegate_failure(span, tracker, slug, exc)

        result = str(response.text)
        record_delegate_call(error=False)
        span.set_attribute("af.delegate.outcome", "success")
        span.set_attribute("af.delegate.response_bytes", len(result))
        span.set_content("af.delegate.result", result)
        return result

    return delegate


async def build_subagent_tools(
    subagents: list[SubagentRef] | None,
    catalog: AgentCatalog | None,
    *,
    coordinator_deadline: float,
) -> tuple[list[Any], _DelegateErrorTracker]:
    """Build one ``delegate_<slug>`` tool per ``subagents`` reference.

    The ``FunctionTool`` wrapper itself is built eagerly here, once per
    reference for this coordinator run — cheap, since it is just a
    schema/closure, and a specialist only actually *runs* if the
    coordinator's model selects the corresponding ``delegate_<slug>`` tool
    call. The specialist's ``Agent`` object is a separate matter: each
    wrapper's handler builds a FRESH one, in the *delegated* role
    (:func:`_build_delegated_agent`), on every individual tool CALL — not
    once here — reusing the process-wide :class:`ClientManager` but never a
    live agent instance cached from a previous call. MAF's ``Agent.run()``
    self-mutates, so per-call construction is the only safe option, and it
    is cheap (FRD 0006 §4.7, §5 Decision #20); it also means concurrent
    calls, including repeated calls to the *same* specialist, never share a
    live agent instance and so need no lock.

    Returns ``(tools, tracker)``. ``tracker`` is incremented by each
    delegate handler on a *recoverable* specialist failure/timeout — see
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
    # coordinator remaining)"). `loop` is reused below (M1) to bound the
    # session-lock wait itself by this same absolute deadline.
    loop = asyncio.get_event_loop()
    coordinator_deadline = loop.time() + timeout

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
    # The lock *wait* itself must also be bounded by the same absolute
    # `coordinator_deadline` (M1): a concurrent turn on the same session id
    # can hold the lock for a while, and previously nothing capped how long
    # this call would wait for it before running the agent with a *fresh*
    # `timeout` window — letting total wall-clock exceed `timeout` by however
    # long the lock wait took, and misclassifying what should be a
    # parent-budget timeout (FRD 0006 §4.6: must abort the whole run) as
    # something that only affects delegate adapters, which would see
    # `remaining <= 0` and short-circuit as a recoverable specialist timeout
    # while this outer run kept going. `asyncio.Lock.acquire()` cancelled by
    # `wait_for`'s own timeout never actually acquires the lock, so no
    # release is needed on this branch.
    try:
        await asyncio.wait_for(lock.acquire(), timeout=max(0.0, coordinator_deadline - loop.time()))
    except TimeoutError:
        raise RuntimeError(f"Agent run timed out after {timeout}s") from None
    try:
        # Re-derive the remaining budget *after* the lock wait (M1) instead
        # of reusing the original full `timeout` — otherwise a long lock
        # wait plus a full fresh `timeout` window could run well past
        # `coordinator_deadline`.
        remaining_after_lock = max(0.0, coordinator_deadline - loop.time())
        if remaining_after_lock <= 0:
            raise TimeoutError
        response = await asyncio.wait_for(
            agent.run(
                prompt,
                session=session,
                options=_build_chat_options_from_environment(),
            ),
            timeout=remaining_after_lock,
        )
    except TimeoutError:
        raise RuntimeError(f"Agent run timed out after {timeout}s") from None
    finally:
        lock.release()

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
    display_name: str | None = None,
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
      per-run delegate-error count is not surfaced in the SSE vocabulary
      itself (only in :class:`AgentResult` for the non-streaming path), but
      it IS applied to this run's own ``agent.run {name}`` span as
      ``af.agent.tool_error_count`` once the stream completes, mirroring
      what the non-streaming path does for :class:`AgentResult`.
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
            coordinator_deadline=deadline,
        )
    except Exception as exc:
        logger.error("Failed to build agent session: %s", exc, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return

    yield f"data: {json.dumps({'type': 'session', 'session_id': resolved_id})}\n\n"

    # `run_agent_stream` opens its *own* run-level span rather than relying on
    # a caller-provided one (B3): unlike the non-streaming path — where
    # `run_agent` returns synchronously and callers such as
    # `registration/_handlers.py`/`registration/endpoints.py` wrap the whole
    # call in an `agent.run {name}` span before it returns — a caller of this
    # generator (e.g. `handle_chat_stream`) typically just constructs the
    # generator and hands it to a `StreamingResponse` without ever driving it
    # itself, so no ambient span from the caller is active while this body
    # actually runs. Opening one here ensures delegate-error accounting (and
    # timeout/exception outcomes) always lands somewhere for the streaming
    # surface too, matching the non-streaming path's `AgentResult.delegate_error_count`.
    with start_span(
        f"agent.run {agent_name or 'agent'}",
        lifecycle_stage=LifecycleStage.AGENT_RUN,
        attributes={
            "af.agent.name": agent_name,
            # S1b: mirrors what `registration/endpoints.py`'s own
            # `agent.run {name}` spans already set (`af.agent.name` = slug,
            # `af.agent.display_name` = human-readable name) for the
            # non-streaming/MCP surfaces. Those surfaces open their own span
            # around `run_agent`, which has none of its own — but nothing
            # upstream of *this* function does the same for the streaming
            # surface (see the comment above), so this span is the only
            # place `af.agent.display_name` can be recorded here.
            "af.agent.display_name": display_name,
            "af.agent.trigger_type": "stream",
            "af.agent.session_id": resolved_id,
            "af.agent.model": model,
        },
    ) as span:
        ordinary_tool_error_count = 0
        try:
            lock = await _get_session_lock(resolved_id)
            # The lock *wait* itself must also be bounded by `deadline` (M1) —
            # see the matching comment in `run_agent` for the full rationale:
            # a concurrent turn on the same session id can hold the lock long
            # enough that the absolute deadline passes before we even start
            # streaming, and previously nothing bounded that wait.
            try:
                await asyncio.wait_for(lock.acquire(), timeout=max(0.0, deadline - loop.time()))
            except TimeoutError:
                span.set_attribute("af.agent.outcome", "error")
                span.record_exception(
                    TimeoutError(f"Timeout after {timeout}s"), fault_domain=FaultDomain.RUNTIME
                )
                yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
                return
            try:
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

                # B2b: `stream`/`stream_settled` are declared here, outside
                # the `try:` below, so the `finally:` clause added at the
                # bottom of this block can always safely reference `stream`
                # (even if `agent.run(...)` itself never got a chance to
                # assign it) and can tell whether some *other* branch already
                # finalized it. `stream_settled` becomes `True` the moment
                # any branch below (the inner per-`__anext__()` handler, the
                # normal-completion path, or either outer `except`) has
                # either finalized the stream itself or reached a point where
                # finalization is not this generator's responsibility.
                stream: Any = None
                stream_settled = False
                try:
                    stream = agent.run(
                        prompt,
                        stream=True,
                        session=session,
                        options=_build_chat_options_from_environment(),
                    )
                    # Each iteration's wait for the *next* update is itself
                    # bounded by the coordinator's remaining budget (B1): the
                    # previous code only checked `loop.time() > deadline`
                    # *after* `async for` had already yielded an update, so a
                    # hung tool/model call producing no update at all could
                    # block past the deadline indefinitely. Wrapping
                    # `__anext__()` in `asyncio.wait_for` bounds that wait
                    # directly, so a stalled generator cannot exceed the
                    # absolute deadline either.
                    stream_iter = stream.__aiter__()
                    while True:
                        try:
                            # B2a: the `remaining <= 0` pre-check now lives
                            # *inside* this same `try` (it used to `raise`
                            # one level up, before this `try` even started) so
                            # a deadline that is already exhausted at the
                            # *top* of an iteration is finalized identically
                            # to one that expires *while* awaiting
                            # `__anext__()`, instead of bypassing this handler
                            # entirely and reaching the outer
                            # `except TimeoutError` below still unfinalized.
                            remaining = max(0.0, deadline - loop.time())
                            if remaining <= 0:
                                raise TimeoutError
                            update = await asyncio.wait_for(stream_iter.__anext__(), timeout=remaining)
                        except StopAsyncIteration:
                            break
                        except (TimeoutError, asyncio.CancelledError) as exc:
                            # `ResponseStream.__anext__` (agent_framework._types)
                            # only runs its registered cleanup hooks — which
                            # close the underlying OTel span, flush usage
                            # stats, and invoke provider callbacks — from its
                            # own `except StopAsyncIteration`/`except
                            # Exception` branches, never on a `BaseException`
                            # such as `asyncio.CancelledError`, which is
                            # exactly what `asyncio.wait_for` injects into
                            # this `__anext__()` call on timeout/cancellation.
                            # Finalize the stream explicitly so MAF's own
                            # span/usage bookkeeping still completes (M2).
                            await _finalize_maf_stream(stream, exc)
                            stream_settled = True
                            raise
                        for item in getattr(update, "contents", None) or []:
                            ctype = _content_type(item)
                            if ctype == "text":
                                text = _content_text(item)
                                if text:
                                    yield f"data: {json.dumps({'type': 'delta', 'content': text})}\n\n"
                            elif ctype == "text_reasoning":
                                text = _content_text(item)
                                if text:
                                    yield (
                                        f"data: {json.dumps({'type': 'intermediate', 'content': text})}\n\n"
                                    )
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
                                result_event = _function_result_event(item)
                                if _looks_like_tool_error(result_event.get("result")):
                                    ordinary_tool_error_count += 1
                                yield f"data: {json.dumps(result_event, default=str)}\n\n"
                            # Unknown content types are intentionally ignored — the
                            # SSE vocabulary is fixed and the UI doesn't render them.
                    for call_id, event in pending_tool_calls.items():
                        if call_id not in emitted_tool_calls:
                            emitted_tool_calls.add(call_id)
                            yield f"data: {json.dumps(event)}\n\n"
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    span.set_attribute("af.agent.outcome", "success")
                    stream_settled = True
                except TimeoutError as exc:
                    if not stream_settled:
                        # Reached when the deadline/cancellation surfaced
                        # from somewhere the inner handler above didn't cover
                        # (B2b) — e.g. `agent.run(...)` itself raising before
                        # the pull loop ever started. Finalize here too
                        # rather than assuming it already happened.
                        await _finalize_maf_stream(stream, exc)
                        stream_settled = True
                    span.set_attribute("af.agent.outcome", "error")
                    span.record_exception(
                        TimeoutError(f"Timeout after {timeout}s"), fault_domain=FaultDomain.RUNTIME
                    )
                    yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
                except Exception as exc:
                    if not stream_settled:
                        # Same reasoning as the `TimeoutError` branch above:
                        # an ordinary exception that reached here without
                        # passing through the inner per-`__anext__()` handler
                        # (e.g. raised directly by `agent.run(...)`, or by
                        # the per-update content processing) still needs the
                        # underlying MAF stream finalized (B2b).
                        await _finalize_maf_stream(stream, exc)
                        stream_settled = True
                    logger.error("Agent stream failed: %s", exc, exc_info=True)
                    span.set_attribute("af.agent.outcome", "error")
                    span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                    yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
                finally:
                    if not stream_settled:
                        # B2b: reached only when this async generator itself
                        # is torn down while suspended at one of the
                        # `yield`s above — e.g. the ASGI/HTTP layer closing
                        # the generator on client disconnect (`aclose()`), or
                        # the enclosing task being cancelled while suspended
                        # at a `yield` rather than while awaiting
                        # `__anext__()`. Python's async-generator protocol
                        # delivers `GeneratorExit`/cancellation *at* that
                        # suspension point — a different code path entirely
                        # from the `except` clauses above, neither of which
                        # catches `BaseException` subclasses such as
                        # `GeneratorExit` — so without this, `stream` would
                        # never be finalized here at all, only ever via a
                        # nondeterministic GC-timed safety net.
                        # `sys.exc_info()` reliably reflects that in-flight
                        # exception in this specific branch precisely
                        # *because* nothing above matched/handled it (once an
                        # `except` above runs, it sets `stream_settled = True`
                        # itself, so this branch is never reached for those
                        # cases); it is only `None` here if the generator is
                        # being torn down with no active exception at all
                        # (e.g. a bare `.aclose()` with nothing in flight), in
                        # which case a generic `CancelledError` is a
                        # reasonable stand-in for the finalize hook.
                        exc_at_teardown = sys.exc_info()[1] or asyncio.CancelledError(
                            "run_agent_stream torn down before completion"
                        )
                        await _finalize_maf_stream(stream, exc_at_teardown)
            finally:
                lock.release()
        finally:
            # Retained through generator completion regardless of outcome
            # (success/timeout/error) so the streaming surface's delegate
            # errors are always accounted for, matching what
            # `AgentResult.delegate_error_count` does for the non-streaming
            # path (B3). Ordinary (non-delegate) tool-call failures detected
            # in the `function_result` handling above are folded in too
            # (M3): the delegate tracker only counts specialist-delegation
            # failures, so a failed sandbox/web_request tool call with no
            # delegate failure at all would otherwise report zero even
            # though a tool genuinely failed.
            span.set_attribute(
                "af.agent.tool_error_count",
                (delegate_error_tracker.count if delegate_error_tracker else 0)
                + ordinary_tool_error_count,
            )
