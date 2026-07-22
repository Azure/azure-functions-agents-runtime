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
* Chat-time sub-agent delegation (FRD 0007): when the resolved agent
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
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ._blob_history import build_blob_provider_from_environment
from ._function_tool import FunctionTool, tool
from ._logger import logger
from ._observability import (
    FaultDomain,
    LifecycleStage,
    RuntimeSpan,
    current_span,
    record_delegate_call,
    start_span,
)
from ._slug import delegate_tool_name
from .client_manager import get_client_manager
from .config import ResolvedAgent, SubagentRef
from .config.env import runtime_env_value
from .config.paths import get_app_root, resolve_config_dir
from .config.schema import HarnessAgentConfig
from .discovery.mcp import discover_mcp_servers
from .discovery.tools import discover_user_tools

# `_handlers` is always fully imported as a side effect of the
# `.registration.*` imports above, so importing this shared tool-error
# heuristic here (rather than duplicating it) creates no new import cycle:
# `_handlers.py` has no module-level dependency back on `runner.py` (its own
# need for `run_agent`/`run_agent_stream` uses a lazy, call-time import).
from .registration._handlers import _looks_like_tool_error
from .registration.capabilities import AgentCapabilities
from .registration.catalog import AgentCatalog, CatalogEntry

if TYPE_CHECKING:
    # Type-only: the runtime values are always obtained via the lazy,
    # call-time `from agent_framework import ...` imports below (this
    # module's established pattern for the heavier agent-construction
    # symbols), so this adds no import-time cost.
    from agent_framework import Agent, AgentResponse, ContextProvider, SupportsChatGetResponse

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


@contextlib.asynccontextmanager
async def _session_lock_bounded_by(session_id: str, deadline: float) -> AsyncIterator[None]:
    """Acquire the per-session lock with the wait bounded by ``deadline``, and always release.

    A concurrent turn on the same session id can hold the lock for a while,
    so the acquire *wait* must be bounded by the caller's own absolute
    deadline too — otherwise total wall-clock time could exceed the
    caller's timeout by however long that wait took. `TimeoutError` from
    the bounded acquire propagates to the caller: the lock was never
    acquired, so `release()` is neither reachable nor needed on that path.
    Once acquired, `release()` always runs in `finally`, including on
    cancellation.
    """
    lock = await _get_session_lock(session_id)
    loop = asyncio.get_running_loop()
    await asyncio.wait_for(lock.acquire(), timeout=max(0.0, deadline - loop.time()))
    try:
        yield
    finally:
        lock.release()


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
    # sanitized to free text (FRD 0007 Decision #12) and wouldn't be
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
# Chat-time sub-agent delegation (FRD 0007)
# ---------------------------------------------------------------------------
#
# A coordinator agent that declares ``subagents:`` gets one hand-written
# ``delegate_<slug>`` function tool per reference (:func:`_build_delegate_tool`
# — the same ``@tool(schema=...)`` pattern as the ``web_request``/
# ``execute_python`` system tools, not MAF's ``BaseAgent.as_tool()``) and run
# inside the coordinator's normal ``agent.run()`` tool-calling loop — no
# ``HandoffBuilder``, no HITL (out of scope for v1; see FRD 0007 §2).
#
# Delegation is single-level (Decision #6): a specialist built here is always
# built in the *delegated* execution role (:func:`_build_delegated_agent`),
# which never reads ``resolved.subagents`` and therefore can never itself gain
# ``delegate_*`` tools. This is a structural guarantee, not a runtime depth
# counter — there is no code path through which a delegated agent's own
# ``build_subagent_tools`` could ever run.


class _DelegateErrorTracker:
    """Per-request counter of *recoverable* ``delegate_<slug>`` failures.

    Shared by every delegate tool for one coordinator run;
    ``AgentResult.delegate_error_count`` reads :attr:`count` when the run
    completes. Only recovered failures count — a propagated cancellation
    never reaches ``record_error`` (Decision #12).
    """

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def record_error(self) -> None:
        self.count += 1


def _build_role_agent(
    chat_client: SupportsChatGetResponse[Any],
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
    history_provider: ContextProvider | None,
    delegate_tools: list[FunctionTool] | None,
) -> Agent[Any]:
    """Assemble the final tool list + context providers and build the MAF ``Agent``.

    Shared tail for both execution roles (Decisions #13/#15):

    * ``direct`` — a coordinator or any agent invoked through its own
      trigger/endpoint. Gets a real ``history_provider`` and, if it declares
      ``subagents``, ``delegate_tools``.
    * ``delegated`` — a specialist invoked as a ``delegate_<slug>`` tool.
      Gets ``history_provider=None`` and ``delegate_tools=None``; sandbox and
      main-only Dynamic-Workflow tools are simply never passed for this role
      (see :func:`_build_delegated_agent`), so they're naturally absent
      rather than stripped from a shared list.
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
        resolved_tools.extend(delegate_tools)

    context_providers: list[ContextProvider] = []
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


def _build_delegated_agent(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> Agent[Any]:
    """Build one specialist's MAF ``Agent`` in the *delegated* execution role.

    Runs as itself: own instructions, model, and static tools, but never a
    per-request sandbox or main-only Dynamic-Workflow tools (naturally
    absent — never passed to :func:`_build_role_agent`, not stripped).
    ``resolved.subagents`` is deliberately never read — the structural
    enforcement of single-level delegation (Decision #6).
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
        # The slug, not `resolved.name` (the display name) — this becomes
        # the MAF span's `gen_ai.agent.name`, matching the `delegate_<slug>`
        # tool name so a trace viewer can correlate the two directly.
        agent_name=resolved.slug,
        resolved_id=None,
        history_provider=None,
        delegate_tools=None,
    )


def _sanitize_delegate_failure(slug: str, exc: BaseException) -> str:
    """Sanitized, model-facing message for a recovered delegate failure.

    Deliberately generic and class-independent — never varies by exception
    type, so the coordinator's model learns nothing about the specialist's
    internals from wording alone. Real exception detail goes only to
    telemetry (Decision #12).
    """
    return (
        f"The '{slug}' specialist could not complete this task. "
        "Consider trying again, rephrasing the request, or proceeding without it."
    )


async def _finalize_maf_stream(stream: Any, exc: BaseException) -> None:
    """Best-effort finalize a MAF ``ResponseStream`` chain on cancel/timeout.

    ``ResponseStream.__anext__`` only runs its cleanup hooks (closing the
    OTel span, flushing usage) on success/ordinary-exception, never on
    cancellation — so this force-runs them so spans close deterministically
    instead of via GC. Used by ``run_agent_stream`` only; the non-streaming
    delegate path doesn't need it (FRD 0007 §5 Decision #20). Known gap: one
    chat-level span MAF never exposes externally can only close via GC — no
    workaround exists. Defensive throughout: safe if ``stream`` is ``None``.
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


def _record_generic_delegate_failure(
    span: RuntimeSpan, tracker: _DelegateErrorTracker, slug: str, exc: BaseException
) -> str:
    """Record a recoverable delegate failure and return the sanitized model-facing string."""
    tracker.record_error()
    record_delegate_call(error=True)
    span.set_attribute("af.delegate.outcome", "error")
    # `record_exception` also sets error status + fault domain, preserving
    # the real exception type/detail in telemetry instead of a flattened
    # string.
    span.record_exception(exc, fault_domain=FaultDomain.DELEGATE)
    return _sanitize_delegate_failure(slug, exc)


def _record_delegate_timeout(
    span: RuntimeSpan, tracker: _DelegateErrorTracker, slug: str, effective_timeout: float, exc: BaseException
) -> str:
    """Record a recoverable delegate timeout (deadline or specialist-raised) and return the model-facing string."""
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


class _DelegateTaskParams(BaseModel):
    """Argument schema for a ``delegate_<slug>`` tool call: a single ``task`` string."""

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
) -> FunctionTool:
    """Build one ``delegate_<slug>`` ``FunctionTool`` for the reference ``ref``.

    A hand-written ``@tool(schema=...)`` function tool (not MAF's
    ``BaseAgent.as_tool()`` — see FRD 0007 §5 Decision #20): the handler
    builds a fresh specialist :class:`agent_framework.Agent` per call and
    awaits its plain, non-streaming ``run(task)`` directly, so no lock,
    monkeypatch, or stream capture is needed.
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
        loop = asyncio.get_running_loop()
        task_text = params.task

        span = current_span()
        span.set_attribute("af.delegate.specialist", slug)
        span.set_attribute("af.delegate.task_bytes", len(task_text))
        span.set_content("af.delegate.task", task_text)

        # effective_timeout = min(specialist, coordinator remaining) per
        # Decision #12. Checked before building the specialist `Agent` at
        # all — a run that can never be attempted shouldn't be built either.
        remaining = max(0.0, coordinator_deadline - loop.time())
        effective_timeout = min(specialist_timeout, remaining)
        if effective_timeout <= 0:
            exc = TimeoutError(f"delegate_{slug}: coordinator budget exhausted before dispatch")
            return _record_delegate_timeout(span, tracker, slug, effective_timeout, exc)

        try:
            # Building the specialist `Agent` is inside this `try` too, so a
            # construction failure (e.g. a misconfigured specialist model)
            # is just as recoverable as a run failure, instead of
            # propagating unhandled and aborting the coordinator turn.
            specialist_agent = _build_delegated_agent(resolved, capabilities)
            response = await asyncio.wait_for(specialist_agent.run(task_text), timeout=effective_timeout)
        except asyncio.CancelledError:
            # Parent/request cancellation — never a recoverable delegate
            # error (Decision #12), but still a dispatched call, so it's
            # counted in the call metric (not the error metric) before
            # re-raising to propagate and abort the run.
            record_delegate_call(error=False)
            span.set_attribute("af.delegate.outcome", "cancelled")
            raise
        except TimeoutError as exc:
            # Covers both a genuine `wait_for` deadline expiry and any
            # `TimeoutError` the specialist's own code happens to raise —
            # both are recoverable specialist-side timeouts either way.
            return _record_delegate_timeout(span, tracker, slug, effective_timeout, exc)
        except Exception as exc:
            return _record_generic_delegate_failure(span, tracker, slug, exc)

        result = response.text
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
) -> tuple[list[FunctionTool], _DelegateErrorTracker]:
    """Build one ``delegate_<slug>`` tool per ``subagents`` reference.

    The tool wrapper (schema/closure) is built once per reference, here.
    The specialist's ``Agent`` object is different: each call builds a
    FRESH one in the *delegated* role (:func:`_build_delegated_agent`) — not
    once here — reusing the process-wide ``ClientManager`` but never a
    cached agent instance. MAF's ``Agent.run()`` self-mutates, so per-call
    construction is required; it also means concurrent calls to the same
    specialist need no lock (Decision #20).

    Returns ``(tools, tracker)``; ``tracker`` counts recoverable delegate
    failures (see :class:`_DelegateErrorTracker`).
    """
    tracker = _DelegateErrorTracker()
    tools: list[FunctionTool] = []
    if not subagents:
        return tools, tracker
    # Guarded for a hand-rolled call site; app.py's composition root always
    # threads a real catalog whenever any agent declares subagents.
    assert catalog is not None, "subagents declared but no AgentCatalog was provided"

    for ref in subagents:
        entry = catalog.get(ref.agent)
        # Guarded for a hand-rolled call site; validate_subagent_references
        # already rejects unknown references at startup.
        assert entry is not None, f"subagents reference `{ref.agent}` was not found in the AgentCatalog"
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

    Returns ``(agent, session, resolved_session_id, delegate_error_tracker)``;
    ``delegate_error_tracker`` is ``None`` unless ``subagents`` is non-empty.
    When non-empty, one ``delegate_<slug>`` tool per reference
    (:func:`build_subagent_tools`) is appended to this (coordinator,
    ``direct``-role) agent's own tools — never the reverse.
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
            else asyncio.get_running_loop().time() + DEFAULT_TIMEOUT
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
        return (await _build_agent_session_history(
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
        ))[:3]

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
        matter (FRD 0007). Each reference gets a ``delegate_<slug>`` tool
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
    # left" (FRD 0007 Decision #12: "effective timeout = min(specialist,
    # coordinator remaining)"). `loop` is reused below (M1) to bound the
    # session-lock wait itself by this same absolute deadline.
    loop = asyncio.get_running_loop()
    coordinator_deadline = loop.time() + timeout

    if harness_config is not None:
        agent, session, resolved_id = await _build_harness_agent_session(
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
            harness_config=harness_config,
        )
        delegate_error_tracker = None
    else:
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

    try:
        async with _session_lock_bounded_by(resolved_id, coordinator_deadline):
            # Re-derive the remaining budget *after* the lock wait instead of
            # reusing the original full `timeout` — otherwise a long lock
            # wait plus a full fresh `timeout` window could run well past
            # `coordinator_deadline`.
            remaining_after_lock = max(0.0, coordinator_deadline - loop.time())
            if remaining_after_lock <= 0:
                raise TimeoutError
            response: AgentResponse[Any] = await asyncio.wait_for(
                agent.run(
                    prompt,
                    session=session,
                    options=_build_chat_options_from_environment(),
                ),
                timeout=remaining_after_lock,
            )
    except TimeoutError:
        raise RuntimeError(f"Agent run timed out after {timeout}s") from None

    # Extract assistant text from the final response.
    text = ""
    try:
        text = response.text
    except Exception:
        text = ""
    if not text:
        # Fallback: walk messages → contents and pick out text items.
        try:
            for msg in response.messages:
                for item in getattr(msg, "contents", None) or []:
                    if _content_type(item) == "text":
                        text += _content_text(item)
        except Exception as exc:
            logger.debug("Failed to extract response text: %s", exc)

    # Walk content items for tool-call records (best-effort metadata for callers).
    tool_calls: list[dict[str, Any]] = []
    try:
        for msg in response.messages:
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
    harness_config: HarnessAgentConfig | None = None,
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
    * ``subagents``/``catalog`` add ``delegate_<slug>`` tools (FRD 0007), one
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
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    try:
        if harness_config is not None:
            agent, session, resolved_id = await _build_harness_agent_session(
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
                harness_config=harness_config,
            )
            delegate_error_tracker = None
        else:
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
            async with _session_lock_bounded_by(resolved_id, deadline):
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
        except TimeoutError:
            span.set_attribute("af.agent.outcome", "error")
            span.record_exception(
                TimeoutError(f"Timeout after {timeout}s"), fault_domain=FaultDomain.RUNTIME
            )
            yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
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
