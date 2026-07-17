"""Tests for chat-time sub-agent delegation (FRD 0006 v1).

Covers the pieces added to :mod:`azure_functions_agents.runner` for
delegation: the ``direct``/``delegated`` execution-role split
(``_build_role_agent`` / ``_build_delegated_agent``), single-level
structural enforcement (Decision #6), ``build_subagent_tools``'s guard
clauses, the ``delegate_<slug>`` tool's failure/cancellation split
(Decision #12), per-call specialist construction giving cross-specialist
AND same-specialist parallelism with no shared-instance lock (Decision #14,
revised), and delegation observability enrichment (Decision #19, §4.12).

Fake specialist harness
------------------------

The ``delegate_<slug>`` tool built by :func:`runner._build_delegate_tool`
is a hand-written ``@tool(schema=...)`` function tool (FRD 0006 §5
Decision #20) whose handler calls ``await specialist_agent.run(task)`` —
plain, non-streaming ``agent_framework.Agent`` usage, never MAF's
``BaseAgent.as_tool()``. So a usable fake specialist only needs an
``async def run(self, task, **kwargs)`` returning an object with a
``.text`` attribute (mirroring ``agent_framework.AgentResponse.text``) —
no ``BaseAgent`` subclassing, streaming, or ``get_final_response()``
plumbing required. ``_FakeSpecialistAgent`` below is that minimal double.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from agent_framework import MCPStreamableHTTPTool, tool

import azure_functions_agents._observability as obs
import azure_functions_agents.runner as runner
from azure_functions_agents.client_manager import (
    ClientManager,
    get_client_manager,
    set_client_manager,
)
from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    SubagentRef,
    ToolsFilter,
)
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog

# ---------------------------------------------------------------------------
# Shared scaffolding
# ---------------------------------------------------------------------------


def _make_resolved(**overrides: Any) -> ResolvedAgent:
    """Build a minimal, valid ``ResolvedAgent`` — mirrors test_config_validation.py's helper."""
    defaults: dict[str, Any] = {
        "name": "Agent",
        "slug": "agent",
        "description": "desc",
        "trigger": None,
        "instructions": "x",
        "is_main": False,
        "builtin_endpoints": BuiltinEndpointsConfig(),
        "model": None,
        "timeout": 1.0,
        "enabled_mcp_names": [],
        "enabled_skills_names": [],
        "tool_filter": ToolsFilter(),
        "subagents": [],
        "sandbox_config": None,
        "input_schema": None,
        "response_schema": None,
        "response_example": None,
        "metadata": {},
        "source_file": "agent.agent.md",
    }
    defaults.update(overrides)
    return ResolvedAgent(**defaults)  # type: ignore[arg-type]


class _FakeClientManager(ClientManager):
    """A ``ClientManager`` that never touches a real model provider."""

    def resolve_model(self, requested: str | None) -> str:
        return requested or "fake-model"

    def build_chat_client(self, model: str | None) -> Any:
        return SimpleNamespace(model=self.resolve_model(model))


class _RunnableFakeChatClient:
    """A minimal chat client that actually satisfies ``agent_framework``'s
    ``SupportsChatGetResponse`` protocol, unlike ``_FakeClientManager``'s bare
    ``SimpleNamespace`` (good enough for construction/tool-listing assertions,
    but not runnable).

    Supports both ``stream=False`` (used by the rewritten, non-streaming
    ``delegate_<slug>`` handler's own ``agent.run(task)`` call) and
    ``stream=True`` (still used by ``test_real_maf_agent_run_raises_on_
    expanded_mcp_function_collision`` below, which drives a real *coordinator*
    ``Agent`` — an unrelated, streaming code path this module's delegation
    changes do not touch) so a REAL ``agent_framework.Agent`` can be driven
    end to end through MAF's own machinery either way (including
    ``AgentTelemetryLayer``, which is what actually stamps ``gen_ai.agent.name``
    on the ``invoke_agent`` span).
    """

    additional_properties: ClassVar[dict[str, Any]] = {}

    def __init__(self, text: str = "specialist response") -> None:
        self._text = text

    def get_response(self, messages: Any, *, stream: bool = False, **kwargs: Any) -> Any:
        from agent_framework import (
            ChatResponse,
            ChatResponseUpdate,
            Content,
            Message,
            ResponseStream,
        )

        if stream:

            async def _stream() -> Any:
                yield ChatResponseUpdate(contents=[Content.from_text(self._text)], role="assistant")

            return ResponseStream(_stream())

        async def _get_response() -> Any:
            return ChatResponse(messages=[Message("assistant", [self._text])])

        return _get_response()


class _RunnableFakeClientManager(ClientManager):
    """A ``ClientManager`` whose chat client can actually run a real ``Agent``.

    Used only by the real-instrumentation test below — everything else in
    this module uses ``_FakeClientManager``, which is sufficient for the
    construction/tool-listing assertions the other tests make but cannot
    drive a real ``Agent.run()``.
    """

    def resolve_model(self, requested: str | None) -> str:
        return requested or "fake-model"

    def build_chat_client(self, model: str | None) -> Any:
        return _RunnableFakeChatClient()


@pytest.fixture(autouse=True)
def _restore_client_manager() -> Any:
    """Snapshot/restore the process-wide ``ClientManager`` singleton around every test.

    ``_build_delegated_agent`` calls ``get_client_manager().build_chat_client(...)``;
    tests that exercise it install ``_FakeClientManager`` and must not leak that
    substitution into unrelated tests/modules.
    """
    original = get_client_manager()
    yield
    set_client_manager(original)


class _RecordingSpan:
    """Fake ``RuntimeSpan`` — mirrors test_web_request.py's ``_CapturedSpan``.

    Stands in for ``current_span()``'s return value: the delegate adapter
    *annotates* the tool-call span MAF already opened rather than starting a
    new one (FRD 0006 §4.12), so this fake only needs to record attributes/
    errors, not manage a span lifecycle.
    """

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.errors: list[tuple[str, str]] = []
        # Populated only via `record_exception` — kept separate from
        # `errors` (which any `set_error` call also appends to) so tests can
        # assert the *actual* exception object/type was captured (B4:
        # structured exception detail, not just a flattened string).
        self.exceptions: list[tuple[BaseException, str | None]] = []

    def set_attribute(self, key: str, value: Any) -> None:
        if value is not None:
            self.attributes[key] = value

    def set_content(self, key: str, value: str) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        pass

    def set_error(self, message: str, *, fault_domain: str) -> None:
        self.errors.append((message, fault_domain))

    def record_exception(self, exc: BaseException, *, fault_domain: str | None = None) -> None:
        """Mirror the real ``RuntimeSpan.record_exception``: sets status + fault domain too.

        The real implementation calls the OTel span's ``record_exception``
        (attaching type/traceback as a span event) *and* sets an error
        status carrying ``str(exc)`` plus the fault domain — i.e. a strict
        superset of what ``set_error`` records. Mirrored here so existing
        assertions against ``errors`` keep working regardless of which
        method produced the entry, while ``exceptions`` lets a test assert
        the real exception object was preserved.
        """
        self.exceptions.append((exc, fault_domain))
        self.errors.append((str(exc), fault_domain or obs.FaultDomain.UNKNOWN))


def _install_span_capture(monkeypatch: pytest.MonkeyPatch) -> _RecordingSpan:
    span = _RecordingSpan()
    monkeypatch.setattr(runner, "current_span", lambda: span)
    return span


def _install_counter_capture(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []
    monkeypatch.setattr(runner, "record_delegate_call", lambda *, error: calls.append(error))
    return calls


class _FakeSpecialistAgent:
    """A specialist double for the rewritten, non-streaming delegate handler.

    ``_build_delegate_tool``'s handler calls ``await specialist_agent.run
    (task)`` directly — plain, non-streaming ``agent_framework.Agent`` usage,
    never MAF's ``BaseAgent.as_tool()`` / ``stream=True`` calling convention —
    so this fake only needs a minimal async ``run()`` returning an object
    with a ``.text`` attribute (mirroring ``agent_framework.AgentResponse
    .text``). No ``BaseAgent`` subclassing or stream shape required.
    """

    def __init__(self, slug: str, respond: Callable[[str], Awaitable[str]]) -> None:
        self.slug = slug
        self._respond = respond

    async def run(self, task: Any = None, **kwargs: Any) -> Any:
        text = await self._respond(str(task or ""))
        return SimpleNamespace(text=text)


def _tool_names(agent: Any) -> set[str]:
    return {str(getattr(tool, "name", "")) for tool in agent.default_options.get("tools", [])}


def _catalog_of(*entries: tuple[str, ResolvedAgent]) -> Any:
    return build_catalog(
        {slug: CatalogEntry(resolved=resolved, capabilities=AgentCapabilities()) for slug, resolved in entries}
    )


# ---------------------------------------------------------------------------
# Pure helpers: _DelegateErrorTracker, _sanitize_delegate_failure,
# _check_delegate_tool_name_collisions
# ---------------------------------------------------------------------------


def test_delegate_error_tracker_starts_at_zero_and_increments() -> None:
    tracker = runner._DelegateErrorTracker()
    assert tracker.count == 0
    tracker.record_error()
    tracker.record_error()
    assert tracker.count == 2


def test_sanitize_delegate_failure_includes_slug_but_not_type_or_raw_detail() -> None:
    """The model-facing message must be class-independent (FRD 0006 Decision #12).

    Neither the raw exception detail NOR ``type(exc).__name__`` may leak
    into the string returned to the coordinator's model context — only the
    specialist's slug and a generic phrasing. The exception type/detail are
    telemetry-only, via ``RuntimeSpan.record_exception``.
    """
    message = runner._sanitize_delegate_failure("billing", RuntimeError("db password is hunter2"))

    assert "billing" in message
    assert "RuntimeError" not in message
    assert "hunter2" not in message  # raw exception detail must never leak to the model


def test_sanitize_delegate_failure_message_identical_across_exception_classes() -> None:
    """Two different exception classes must produce the exact same message shape.

    A reviewer must never be able to distinguish which internal exception
    class failed by reading the model-facing string alone.
    """
    message_value_error = runner._sanitize_delegate_failure("billing", ValueError("boom"))
    message_runtime_error = runner._sanitize_delegate_failure("billing", RuntimeError("kaboom"))

    assert message_value_error == message_runtime_error


def test_check_delegate_tool_name_collisions_raises_on_collision() -> None:
    existing_tools = [SimpleNamespace(name="delegate_billing")]

    with pytest.raises(ValueError, match="delegate_billing"):
        runner._check_delegate_tool_name_collisions(existing_tools, ["delegate_billing"])


def test_check_delegate_tool_name_collisions_passes_when_no_overlap() -> None:
    existing_tools = [SimpleNamespace(name="some_user_tool")]

    runner._check_delegate_tool_name_collisions(existing_tools, ["delegate_billing"])  # no raise


# ---------------------------------------------------------------------------
# _build_role_agent: direct vs. delegated tool/context-provider composition
# ---------------------------------------------------------------------------


def test_build_role_agent_delegated_role_has_only_its_own_tools() -> None:
    chat_client = SimpleNamespace(model="fake-model")
    user_tool = SimpleNamespace(name="own_user_tool")
    mcp_tool = SimpleNamespace(name="own_mcp_tool")

    agent = runner._build_role_agent(
        chat_client,
        instructions="be a specialist",
        tools=[user_tool],
        mcp_tools=[mcp_tool],
        skill_paths=None,
        sandbox_tools=None,
        web_request_tools=None,
        system_addendum=None,
        workflow_enabled=False,
        workflow_durable_client=None,
        agent_name="billing",
        resolved_id=None,
        history_provider=None,
        delegate_tools=None,
    )

    # Own static tools are present; per-request sandbox and main-only
    # Dynamic-Workflow tools are naturally absent (never passed), and there
    # are no delegate_* tools (delegate_tools=None) — Decisions #13/#15/#6.
    assert _tool_names(agent) == {"own_user_tool", "own_mcp_tool"}
    assert agent.context_providers == []


def test_build_role_agent_direct_role_has_full_tool_superset() -> None:
    chat_client = SimpleNamespace(model="fake-model")
    user_tool = SimpleNamespace(name="own_user_tool")
    mcp_tool = SimpleNamespace(name="own_mcp_tool")
    sandbox_tool = SimpleNamespace(name="run_code")
    web_request_tool = SimpleNamespace(name="web_request")
    delegate_tool = SimpleNamespace(name="delegate_billing")
    history_provider = SimpleNamespace()

    agent = runner._build_role_agent(
        chat_client,
        instructions="be a coordinator",
        tools=[user_tool],
        mcp_tools=[mcp_tool],
        skill_paths=None,
        sandbox_tools=[sandbox_tool],
        web_request_tools=[web_request_tool],
        system_addendum=None,
        workflow_enabled=True,
        workflow_durable_client=None,
        agent_name="coordinator",
        resolved_id="session-1",
        history_provider=history_provider,
        delegate_tools=[delegate_tool],
    )

    tool_names = _tool_names(agent)
    assert {
        "own_user_tool",
        "own_mcp_tool",
        "run_code",
        "web_request",
        "delegate_billing",
        "start_workflow",
        "get_workflow_status",
        "list_workflows",
    } <= tool_names
    assert history_provider in agent.context_providers


def test_build_role_agent_raises_on_delegate_tool_name_collision() -> None:
    chat_client = SimpleNamespace(model="fake-model")
    mcp_tool = SimpleNamespace(name="delegate_billing")  # collides with the delegate tool below
    delegate_tool = SimpleNamespace(name="delegate_billing")

    with pytest.raises(ValueError, match="delegate_billing"):
        runner._build_role_agent(
            chat_client,
            instructions=None,
            tools=[],
            mcp_tools=[mcp_tool],
            skill_paths=None,
            sandbox_tools=None,
            web_request_tools=None,
            system_addendum=None,
            workflow_enabled=False,
            workflow_durable_client=None,
            agent_name="coordinator",
            resolved_id=None,
            history_provider=None,
            delegate_tools=[delegate_tool],
        )


# ---------------------------------------------------------------------------
# _build_delegated_agent: "runs as itself" + never wires its own subagents
# ---------------------------------------------------------------------------


def test_build_delegated_agent_never_wires_its_own_declared_subagents() -> None:
    set_client_manager(_FakeClientManager())

    resolved = _make_resolved(
        slug="billing",
        subagents=[SubagentRef(agent="shipping")],
        instructions="handle billing",
    )

    agent = runner._build_delegated_agent(resolved, AgentCapabilities())

    # Structural proof of single-level delegation (Decision #6): even though
    # `resolved.subagents` is non-empty, _build_delegated_agent's signature
    # never accepts a catalog at all, so it has no way to build delegate_*
    # tools for "billing" regardless of what it declares.
    assert not any(name.startswith("delegate_") for name in _tool_names(agent))


def test_build_delegated_agent_uses_specialists_own_model_instructions_tools_and_skills_not_coordinators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Runs as itself" (FRD 0006 §5 Decisions #13/#15) — a deeper, end-to-end check.

    ``test_build_delegated_agent_never_wires_its_own_declared_subagents``
    above proves the *no-subagents-leak* half of "runs as itself". It does
    not, however, prove the other half the FRD's §6 test plan explicitly
    calls for: that a delegated specialist's *own* model/instructions/tools/
    skills are what actually land on the built ``Agent`` — as opposed to,
    say, a coordinator's values leaking in via a wiring mistake in
    ``build_subagent_tools``/``_build_delegate_tool``. Every ``model=``
    passed to ``_make_resolved()`` elsewhere in this file is the default
    ``None``, so no prior test could have caught that kind of regression.

    This builds two roles with deliberately *different* model/instructions/
    tool/skill values — a specialist via the real ``_build_delegated_agent``
    and a contrasting "coordinator" via the same ``_build_role_agent`` tail
    that ``_build_agent_session_history`` uses for the direct role — and
    asserts the specialist's build reflects only its own values.
    """
    set_client_manager(_FakeClientManager())

    # `_build_skills_provider` requires real `SKILL.md`-bearing directories
    # on disk (it is MAF's `SkillsProvider.from_paths`, an I/O-touching,
    # experimental API) — irrelevant to what we're proving here, which is
    # only that `enabled_skill_paths` is threaded through unchanged and
    # per-role. Monkeypatched to a pure, side-effect-free recorder.
    captured_skill_paths: list[list[Path] | None] = []

    def _fake_build_skills_provider(skill_paths: list[Path] | None) -> Any:
        captured_skill_paths.append(skill_paths)
        return f"skills-provider:{skill_paths}" if skill_paths else None

    monkeypatch.setattr(runner, "_build_skills_provider", _fake_build_skills_provider)

    coordinator_only_tool = tool(lambda: "ignored", name="coordinator_only_tool")
    billing_only_tool = tool(lambda: "ignored", name="billing_only_tool")
    coordinator_skill_path = Path("coordinator-skills")
    billing_skill_path = Path("billing-skills")

    billing_resolved = _make_resolved(
        slug="billing",
        model="billing-model",
        instructions="handle billing precisely",
    )
    billing_capabilities = AgentCapabilities(
        filtered_user_tools=[billing_only_tool],
        enabled_skill_paths=[billing_skill_path],
    )

    # A contrasting "coordinator" build via the exact same shared tail
    # (`_build_role_agent`) that `_build_agent_session_history` uses for the
    # `direct` role — not a hardcoded second value, but a second concrete,
    # independently-built artifact to compare against.
    coordinator_chat_client = get_client_manager().build_chat_client("coordinator-model")
    coordinator_agent = runner._build_role_agent(
        coordinator_chat_client,
        instructions="be a coordinator",
        tools=[coordinator_only_tool],
        mcp_tools=[],
        skill_paths=[coordinator_skill_path],
        sandbox_tools=None,
        web_request_tools=None,
        system_addendum=None,
        workflow_enabled=False,
        workflow_durable_client=None,
        agent_name="coordinator",
        resolved_id=None,
        history_provider=None,
        delegate_tools=None,
    )

    billing_agent = runner._build_delegated_agent(billing_resolved, billing_capabilities)

    # Model: the specialist's chat client resolves to *its own* model.
    # (MAF's `Agent` stores the client passed to its constructor as
    # `.client` — the `chat_client` name above is only this test's/
    # `_build_role_agent`'s local variable name for it.)
    assert billing_agent.client.model == "billing-model"
    assert billing_agent.client.model != coordinator_agent.client.model

    # Instructions: its own text only.
    assert billing_agent.default_options["instructions"] == "handle billing precisely"
    assert billing_agent.default_options["instructions"] != coordinator_agent.default_options["instructions"]

    # Tools: only the specialist's own filtered_user_tools — never the
    # coordinator's, and vice versa.
    assert _tool_names(billing_agent) == {"billing_only_tool"}
    assert _tool_names(coordinator_agent) == {"coordinator_only_tool"}

    # Skills: `_build_skills_provider` was called once per role, each with
    # that role's own `enabled_skill_paths` — never swapped.
    assert [billing_skill_path] in captured_skill_paths
    assert [coordinator_skill_path] in captured_skill_paths
    assert captured_skill_paths.count([billing_skill_path]) == 1
    assert captured_skill_paths.count([coordinator_skill_path]) == 1


@pytest.mark.asyncio
async def test_single_level_delegation_end_to_end_with_mutual_subagents_refs_does_not_recurse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end (not just ``_build_delegated_agent`` in isolation): proves
    single-level structural enforcement (Decision #6) holds through the real
    ``build_subagent_tools`` -> ``_build_delegate_tool`` -> handler ->
    ``_build_delegated_agent`` chain, including an actual specialist call —
    not merely that the tool was *built*.

    Since each call now builds its specialist ``Agent`` fresh, INSIDE the
    handler, rather than at tool-build time (FRD 0006 §5 Decision #20),
    ``_build_delegated_agent`` is not called at all until the tool is
    actually invoked — this test calls it via ``_RunnableFakeClientManager``
    so the specialist's ``run()`` genuinely succeeds end to end.
    """
    set_client_manager(_RunnableFakeClientManager())

    resolved_a = _make_resolved(slug="a", subagents=[SubagentRef(agent="b")])
    resolved_b = _make_resolved(slug="b", subagents=[SubagentRef(agent="a")])
    catalog = _catalog_of(("a", resolved_a), ("b", resolved_b))

    built_agents: list[Any] = []
    real_build_delegated_agent = runner._build_delegated_agent

    def _capturing_build_delegated_agent(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> Any:
        agent = real_build_delegated_agent(resolved, capabilities)
        built_agents.append(agent)
        return agent

    monkeypatch.setattr(runner, "_build_delegated_agent", _capturing_build_delegated_agent)

    loop = asyncio.get_event_loop()
    tools, tracker = await runner.build_subagent_tools(
        [SubagentRef(agent="a")], catalog, coordinator_deadline=loop.time() + 30
    )

    # The coordinator gets exactly delegate_a — B is never touched. Building
    # the tool itself builds no specialist Agent yet: that now happens per
    # CALL, not at tool-build time.
    assert [t.name for t in tools] == ["delegate_a"]
    assert built_agents == []

    result = await tools[0].func(task="go")

    # Calling delegate_a built exactly one specialist Agent, for A. Despite A
    # declaring `subagents: [b]` itself, `_build_delegated_agent` never reads
    # `resolved.subagents`, so A was built with zero tools at all and has no
    # way to further delegate to B.
    assert result == "specialist response"
    assert tracker.count == 0
    assert len(built_agents) == 1
    assert _tool_names(built_agents[0]) == set()


# ---------------------------------------------------------------------------
# build_subagent_tools: guard clauses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_subagent_tools_returns_empty_when_no_subagents_declared() -> None:
    loop = asyncio.get_event_loop()
    tools, tracker = await runner.build_subagent_tools(None, None, coordinator_deadline=loop.time() + 30)

    assert tools == []
    assert tracker.count == 0

    tools_empty_list, _tracker2 = await runner.build_subagent_tools(
        [], None, coordinator_deadline=loop.time() + 30
    )
    assert tools_empty_list == []


@pytest.mark.asyncio
async def test_build_subagent_tools_raises_when_catalog_missing_but_subagents_declared() -> None:
    loop = asyncio.get_event_loop()

    with pytest.raises(RuntimeError, match="AgentCatalog"):
        await runner.build_subagent_tools(
            [SubagentRef(agent="billing")], None, coordinator_deadline=loop.time() + 30
        )


@pytest.mark.asyncio
async def test_build_subagent_tools_raises_on_unknown_reference() -> None:
    catalog = _catalog_of(("shipping", _make_resolved(slug="shipping")))
    loop = asyncio.get_event_loop()

    with pytest.raises(RuntimeError, match="billing"):
        await runner.build_subagent_tools(
            [SubagentRef(agent="billing")], catalog, coordinator_deadline=loop.time() + 30
        )


# ---------------------------------------------------------------------------
# Adapter behavior: success / failure / timeout / effective-timeout /
# cancellation (Decision #12)
# ---------------------------------------------------------------------------


async def _build_single_delegate_tool(
    monkeypatch: pytest.MonkeyPatch,
    *,
    slug: str,
    respond: Callable[[str], Awaitable[str]],
    resolved_timeout: float = 5.0,
    coordinator_deadline: float | None = None,
) -> tuple[Any, Any]:
    """Build one real ``delegate_<slug>`` tool with a ``_FakeSpecialistAgent`` swapped in."""
    monkeypatch.setattr(
        runner, "_build_delegated_agent", lambda resolved, caps: _FakeSpecialistAgent(slug, respond)
    )
    catalog = _catalog_of((slug, _make_resolved(slug=slug, timeout=resolved_timeout)))
    loop = asyncio.get_event_loop()
    deadline = coordinator_deadline if coordinator_deadline is not None else loop.time() + 30
    tools, tracker = await runner.build_subagent_tools(
        [SubagentRef(agent=slug)], catalog, coordinator_deadline=deadline
    )
    return tools[0], tracker


@pytest.mark.asyncio
async def test_delegate_adapter_success_records_span_and_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _install_span_capture(monkeypatch)
    calls = _install_counter_capture(monkeypatch)

    async def respond(task: str) -> str:
        return f"handled: {task}"

    tool, tracker = await _build_single_delegate_tool(monkeypatch, slug="billing", respond=respond)

    result = await tool.func(task="invoice #42")

    assert result == "handled: invoice #42"
    assert tracker.count == 0
    assert calls == [False]
    assert span.attributes["af.delegate.specialist"] == "billing"
    assert span.attributes["af.delegate.outcome"] == "success"
    assert span.errors == []


@pytest.mark.asyncio
async def test_delegate_adapter_recovers_from_specialist_exception_with_sanitized_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = _install_span_capture(monkeypatch)
    calls = _install_counter_capture(monkeypatch)

    async def respond(task: str) -> str:
        raise RuntimeError("db password is hunter2")

    tool, tracker = await _build_single_delegate_tool(monkeypatch, slug="billing", respond=respond)

    result = await tool.func(task="invoice #42")

    assert "billing" in result
    assert "hunter2" not in result  # sanitized: raw detail never reaches the model
    assert tracker.count == 1
    assert calls == [True]
    assert span.attributes["af.delegate.outcome"] == "error"
    assert span.errors and span.errors[0][1] == obs.FaultDomain.DELEGATE


@pytest.mark.asyncio
async def test_delegate_adapter_specialist_timeout_is_recoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _install_span_capture(monkeypatch)
    calls = _install_counter_capture(monkeypatch)

    async def slow_respond(task: str) -> str:
        await asyncio.sleep(10)
        return "too late"

    tool, tracker = await _build_single_delegate_tool(
        monkeypatch, slug="billing", respond=slow_respond, resolved_timeout=0.05
    )

    result = await tool.func(task="invoice #42")

    assert "did not respond in time" in result
    assert tracker.count == 1
    assert calls == [True]
    assert span.attributes["af.delegate.outcome"] == "timeout"


@pytest.mark.asyncio
async def test_delegate_adapter_effective_timeout_uses_coordinator_remaining_when_smaller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A near-expired coordinator deadline times out the delegate call immediately,
    even when the specialist's own configured timeout is generous — proving
    effective_timeout = min(specialist_timeout, coordinator_remaining).

    This is the *pre-dispatch* budget-exhausted path
    (``effective_timeout <= 0`` before the specialist ``Agent`` is even
    built, proven here by ``body_ran is False``) — unambiguously a genuine
    deadline expiry, never an inner specialist exception, so it must ALWAYS
    classify as ``outcome=timeout`` regardless of the elapsed-time heuristic
    used to distinguish the other two cases (a real ``wait_for`` expiry vs.
    an inner ``TimeoutError`` the specialist's own code happens to raise) —
    the heuristic doesn't even apply here since the specialist was never
    built or called at all.
    """
    span = _install_span_capture(monkeypatch)
    _install_counter_capture(monkeypatch)

    body_ran = False

    async def respond(task: str) -> str:
        nonlocal body_ran
        body_ran = True
        return "should never get here"

    loop = asyncio.get_event_loop()
    tool, tracker = await _build_single_delegate_tool(
        monkeypatch,
        slug="billing",
        respond=respond,
        resolved_timeout=60.0,
        coordinator_deadline=loop.time() - 100.0,  # already expired
    )

    result = await tool.func(task="invoice #42")

    assert "did not respond in time" in result
    assert tracker.count == 1
    assert body_ran is False  # the pre-dispatch check returns before the specialist is ever built/called
    assert span.attributes["af.delegate.outcome"] == "timeout"


@pytest.mark.asyncio
async def test_delegate_adapter_propagates_cancellation_without_recording_a_delegate_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = _install_span_capture(monkeypatch)
    calls = _install_counter_capture(monkeypatch)

    async def respond(task: str) -> str:
        await asyncio.sleep(10)
        return "unreachable"

    tool, tracker = await _build_single_delegate_tool(monkeypatch, slug="billing", respond=respond)

    task = asyncio.ensure_future(tool.func(task="invoice #42"))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Parent/request cancellation propagates + aborts (Decision #12) — it is
    # never mistaken for a recoverable specialist failure.
    assert tracker.count == 0
    assert calls == []
    # ... but it IS still annotated on the span (B4): telemetry should be
    # able to tell a cancelled delegate call apart from one that simply
    # never got another outcome recorded.
    assert span.attributes["af.delegate.outcome"] == "cancelled"
    assert span.errors == []  # cancellation is not an "error" outcome
    assert span.exceptions == []  # nor a `record_exception` call


# ---------------------------------------------------------------------------
# Concurrency (Decision #14, revised): each call builds its own specialist
# instance, so same-specialist AND cross-specialist calls both run in
# parallel — no shared-instance lock.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_adapter_concurrent_calls_to_same_specialist_run_on_independent_instances_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRD 0006 §5 Decision #20 (revised #14): no per-specialist lock.

    Because ``_build_delegate_tool``'s handler builds a FRESH specialist
    ``Agent`` on every call (:func:`runner._build_delegated_agent`), two
    concurrent calls to the *same* declared specialist never share a live
    agent instance to race — or serialize — on. This replaces the old
    lock-based design's ``test_delegate_adapter_serializes_concurrent_calls_
    to_same_specialist`` test with the simpler reality: both calls run
    concurrently, each on its own instance, and both produce correct,
    independent results.
    """
    _install_span_capture(monkeypatch)
    _install_counter_capture(monkeypatch)

    log: list[str] = []
    built_instances: list[_FakeSpecialistAgent] = []

    def _fake_build_delegated_agent(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> Any:
        async def respond(task: str) -> str:
            log.append(f"start:{task}")
            await asyncio.sleep(0.05)
            log.append(f"end:{task}")
            return f"handled:{task}"

        agent = _FakeSpecialistAgent(resolved.slug, respond)
        built_instances.append(agent)
        return agent

    monkeypatch.setattr(runner, "_build_delegated_agent", _fake_build_delegated_agent)

    catalog = _catalog_of(("billing", _make_resolved(slug="billing")))
    loop = asyncio.get_event_loop()
    tools, tracker = await runner.build_subagent_tools(
        [SubagentRef(agent="billing")], catalog, coordinator_deadline=loop.time() + 30
    )
    tool = tools[0]

    results = await asyncio.gather(tool.func(task="first"), tool.func(task="second"))

    assert set(results) == {"handled:first", "handled:second"}
    assert tracker.count == 0
    # Two calls to the same specialist -> two independently built agent
    # instances, never one shared/reused object.
    assert len(built_instances) == 2
    assert built_instances[0] is not built_instances[1]
    # Both calls actually overlapped in time -- proving no lock serialized
    # them: each starts before the other ends.
    assert log.index("start:first") < log.index("end:second")
    assert log.index("start:second") < log.index("end:first")


@pytest.mark.asyncio
async def test_delegate_adapter_runs_different_specialists_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_span_capture(monkeypatch)
    _install_counter_capture(monkeypatch)

    log: list[str] = []

    async def respond_billing(task: str) -> str:
        log.append("start:billing")
        await asyncio.sleep(0.05)
        log.append("end:billing")
        return "billing-done"

    async def respond_shipping(task: str) -> str:
        log.append("start:shipping")
        await asyncio.sleep(0.01)
        log.append("end:shipping")
        return "shipping-done"

    def _fake_build_delegated_agent(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> Any:
        respond = respond_billing if resolved.slug == "billing" else respond_shipping
        return _FakeSpecialistAgent(resolved.slug, respond)

    monkeypatch.setattr(runner, "_build_delegated_agent", _fake_build_delegated_agent)

    catalog = _catalog_of(
        ("billing", _make_resolved(slug="billing")),
        ("shipping", _make_resolved(slug="shipping")),
    )
    loop = asyncio.get_event_loop()
    tools, _tracker = await runner.build_subagent_tools(
        [SubagentRef(agent="billing"), SubagentRef(agent="shipping")],
        catalog,
        coordinator_deadline=loop.time() + 30,
    )

    results = await asyncio.gather(*(t.func(task="go") for t in tools))

    assert set(results) == {"billing-done", "shipping-done"}
    # Different specialists run concurrently: billing's whole run overlaps
    # with shipping's, regardless of exact micro-ordering — proven by each
    # starting before the other ends.
    assert log.index("start:billing") < log.index("end:shipping")
    assert log.index("start:shipping") < log.index("end:billing")


# ---------------------------------------------------------------------------
# Real-span sharing under concurrent asyncio.gather (FRD 0006 §4.12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_spans_share_one_trace_id_under_concurrent_gather(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test-multi-agent-delegation")

    # current_span() early-returns a no-op RuntimeSpan unless the runtime
    # believes observability is enabled — use the *real* current_span/
    # record_delegate_call here (not the fakes) to prove the actual
    # production code correctly nests under whatever span is ambient.
    monkeypatch.setattr(obs, "_enabled", True)

    async def respond_a(task: str) -> str:
        await asyncio.sleep(0.01)
        return "a-done"

    async def respond_b(task: str) -> str:
        await asyncio.sleep(0.01)
        return "b-done"

    def _fake_build_delegated_agent(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> Any:
        respond = respond_a if resolved.slug == "a" else respond_b
        return _FakeSpecialistAgent(resolved.slug, respond)

    monkeypatch.setattr(runner, "_build_delegated_agent", _fake_build_delegated_agent)

    catalog = _catalog_of(("a", _make_resolved(slug="a")), ("b", _make_resolved(slug="b")))
    loop = asyncio.get_event_loop()
    tools, _tracker = await runner.build_subagent_tools(
        [SubagentRef(agent="a"), SubagentRef(agent="b")], catalog, coordinator_deadline=loop.time() + 30
    )

    async def _call_with_nested_span(tool: Any) -> str:
        # Mimics MAF's FunctionTool.invoke(), which auto-nests an
        # `execute_tool <name>` span around every tool call — the delegate
        # handler's current_span() then annotates *that* span rather than
        # opening a second one (FRD 0006 §4.12).
        with tracer.start_as_current_span(f"execute_tool {tool.name}"):
            return str(await tool.func(task="do it"))

    with tracer.start_as_current_span("agent.run coordinator"):
        results = await asyncio.gather(*(_call_with_nested_span(t) for t in tools))

    assert results == ["a-done", "b-done"]

    finished = exporter.get_finished_spans()
    trace_ids = {span.context.trace_id for span in finished}
    assert len(trace_ids) == 1  # root + both nested delegate spans share one trace

    by_name = {span.name: span for span in finished}
    assert {"agent.run coordinator", "execute_tool delegate_a", "execute_tool delegate_b"} <= set(by_name)
    assert by_name["execute_tool delegate_a"].attributes["af.delegate.specialist"] == "a"
    assert by_name["execute_tool delegate_a"].attributes["af.delegate.outcome"] == "success"
    assert by_name["execute_tool delegate_b"].attributes["af.delegate.specialist"] == "b"
    assert by_name["execute_tool delegate_b"].attributes["af.delegate.outcome"] == "success"


# ---------------------------------------------------------------------------
# Real MAF `Agent` instrumentation (FRD 0006 §5 Decision #19) — B2
# ---------------------------------------------------------------------------
#
# Every other test in this module that inspects delegate telemetry uses
# `_FakeSpecialistAgent`, a minimal double that never mixes in
# `agent_framework.observability.AgentTelemetryLayer` and therefore never
# actually creates MAF's own `invoke_agent` span. Those tests prove this
# repo's *own* `af.delegate.*` span attributes are correct, but they cannot
# prove the separate MAF-side contract this file's other assertions rely on:
# that `_build_delegated_agent` wires a real `Agent`'s `name=` so MAF's
# `AgentTelemetryLayer` stamps `gen_ai.agent.name` with the specialist's
# *slug*. The test below goes through the real (unmocked)
# `_build_delegated_agent` -> `_build_role_agent` -> `Agent(...)` path with a
# real `agent_framework.Agent` and a minimal-but-protocol-correct fake chat
# client, and inspects the actual span MAF's `AgentTelemetryLayer` produces.


@pytest.mark.asyncio
async def test_real_maf_agent_invoke_span_reports_specialist_slug_as_agent_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REAL ``agent_framework.Agent`` specialist's own ``invoke_agent`` OTel
    span (created by MAF's ``AgentTelemetryLayer``, not by any code in this
    repo) must carry the specialist's *slug* as ``gen_ai.agent.name`` — proving
    ``_build_delegated_agent`` passes ``agent_name=resolved.slug`` (not
    ``resolved.name``, the human-facing display name) all the way into MAF's
    ``Agent(name=...)`` constructor call in ``_build_role_agent``.

    Goes through the real ``build_subagent_tools`` -> ``_build_delegate_tool``
    -> ``_build_delegated_agent`` -> ``_build_role_agent`` chain (no
    monkeypatching of any of those, unlike every other test in this module)
    so the specialist really is a MAF ``Agent`` instance, and invokes the
    resulting ``delegate_billing`` tool's handler exactly as the coordinator's
    model would. ``_RunnableFakeClientManager`` supplies the only fake in this
    test: a chat client, standing in for the network call to a real model
    provider.
    """
    import agent_framework.observability as maf_observability
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # MAF's own get_tracer() (used by AgentTelemetryLayer to create the
    # invoke_agent span) is a thin wrapper over
    # opentelemetry.trace.get_tracer_provider() with no per-call injection
    # point, so the provider has to be swapped globally for the duration of
    # this test — the same technique test_observability.py already uses for
    # this repo's own get_tracer(), which resolves through the identical
    # opentelemetry.trace.get_tracer_provider() call.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    # MAF only creates spans at all when this (module-level singleton)
    # instance attribute is truthy; it is read fresh on every invocation, so
    # a plain monkeypatch (auto-restored) is sufficient — no need to touch
    # this repo's own (separate) `_observability._enabled` flag, since this
    # test only needs to prove MAF's span, not this repo's.
    monkeypatch.setattr(maf_observability.OBSERVABILITY_SETTINGS, "enable_instrumentation", True)

    set_client_manager(_RunnableFakeClientManager())

    resolved = _make_resolved(
        name="Billing Specialist",
        slug="billing",
        instructions="Handle billing questions.",
    )
    catalog = _catalog_of(("billing", resolved))
    loop = asyncio.get_event_loop()
    tools, _tracker = await runner.build_subagent_tools(
        [SubagentRef(agent="billing")], catalog, coordinator_deadline=loop.time() + 30
    )
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "delegate_billing"

    result = await tool.func(task="Explain this month's invoice.")

    assert result == "specialist response"

    finished = exporter.get_finished_spans()
    invoke_spans = [span for span in finished if span.name.startswith("invoke_agent")]
    assert len(invoke_spans) == 1, f"expected exactly one invoke_agent span, got: {[s.name for s in finished]}"
    invoke_span = invoke_spans[0]

    # The slug — never the display name `resolved.name` ("Billing
    # Specialist") — is what MAF's AgentTelemetryLayer must see as
    # `Agent.name`, since it is what both the span name and the
    # `gen_ai.agent.name` attribute are derived from.
    assert invoke_span.name == "invoke_agent billing"
    assert invoke_span.attributes is not None
    assert invoke_span.attributes.get("gen_ai.agent.name") == "billing"
    assert invoke_span.attributes.get("gen_ai.agent.name") != resolved.name


# ---------------------------------------------------------------------------
# MCP dynamic tool-name collisions against real `agent_framework` MCP shape
# (FRD 0006 §4.2 re-check-at-assembly-time requirement) — S5
# ---------------------------------------------------------------------------
#
# `_check_delegate_tool_name_collisions` (like `registration.capabilities
# .existing_tool_names`, its composition-time counterpart) only inspects an
# MCP tool object's own `.name` — the *server connection's* name, set once
# at `discover_mcp_servers()` time (see `discovery/mcp.py`). The individual
# remote tools/functions a connected MCP server exposes are a completely
# different, dynamically-populated collection: `agent_framework.MCPTool
# .functions` (a property backed by `._functions`, filled in by
# `MCPTool.load_tools()` — see `agent_framework/_mcp.py`). A remote tool
# literally named e.g. "delegate_billing" therefore cannot be seen by this
# repo's own guard at all: it inspects the server object, never its
# eventual `.functions`.
#
# The two tests below use a fake MCP server subclassing the real
# `agent_framework.MCPStreamableHTTPTool` (marked pre-connected, with
# `._functions` pre-populated to stand in for a completed `load_tools()`
# round-trip, so no real network server is needed) to prove, against real
# `agent_framework` code rather than a guess about its behavior: (1) this
# repo's own guard really does miss the collision (documenting its actual,
# narrower-than-the-old-docstring-implied scope), and (2) this is not a
# silent hole in practice — MAF's own `Agent.run()` independently re-checks
# tool-name uniqueness once it expands `MCPTool.functions` into the final
# tool list (`agent_framework._agents.BaseAgent._prepare_run_context` ->
# `agent_framework._tools._append_unique_tools`), raising `ValueError`
# before any model or tool call happens. Given that backstop already exists
# in MAF and fires with a clear, if differently-worded, error, the guard
# here is kept as a best-effort, earlier check for the cases it *does* see
# (a colliding MCP server connection name, or any non-MCP tool) rather than
# duplicated/expanded to replicate MAF's own runtime check.


class _FakeMCPServerWithExpandedFunctions(MCPStreamableHTTPTool):
    """A stand-in for an already-*connected* MCP server (see module comment above).

    Its own connection ``.name`` ("billing-mcp-server") deliberately does
    NOT collide with anything; only one of its *expanded* ``.functions``
    does. ``load_tools=False`` plus manually setting ``is_connected``/
    ``._functions`` skips the real network handshake `MCPTool.load_tools()`
    would otherwise perform, while still using the real ``MCPTool.functions``
    property (unmodified) to expose them.
    """

    def __init__(self, colliding_function_name: str) -> None:
        super().__init__(name="billing-mcp-server", url="https://example.invalid/mcp", load_tools=False)
        self.is_connected = True
        self._functions = [tool(lambda: "ignored", name=colliding_function_name)]


def test_check_delegate_tool_name_collisions_misses_expanded_mcp_remote_function_names() -> None:
    """Documents the guard's real scope: it does not see expanded remote tool names.

    ``mcp_server.name`` ("billing-mcp-server") does not collide with
    "delegate_billing", and the guard never looks at ``.functions`` — so it
    does not raise even though a real connected server here would expose a
    colliding remote tool once loaded. See the test immediately below for
    why this is not a silent gap in practice.
    """
    mcp_server = _FakeMCPServerWithExpandedFunctions("delegate_billing")

    runner._check_delegate_tool_name_collisions([mcp_server], ["delegate_billing"])  # does not raise


@pytest.mark.asyncio
async def test_real_maf_agent_run_raises_on_expanded_mcp_function_collision() -> None:
    """MAF's own tool assembly is the actual backstop for the gap documented above.

    Builds a real ``agent_framework.Agent`` through the real
    ``_build_role_agent`` (construction itself does not raise — confirming
    the previous test's finding holds through the full path a coordinator
    actually goes through) with a fake, pre-connected MCP server whose one
    expanded remote function is named ``delegate_billing`` — the exact same
    name as the coordinator's own ``delegate_billing`` tool. Running the
    agent (the point at which MAF expands ``MCPTool.functions`` into the
    final tool list) must raise ``ValueError`` from MAF's own
    ``_append_unique_tools``, proving the system fails loudly with an
    actionable message rather than silently double-registering the name or
    letting one tool shadow the other.
    """
    mcp_server = _FakeMCPServerWithExpandedFunctions("delegate_billing")
    delegate_tool = tool(lambda: "ignored", name="delegate_billing")

    agent = runner._build_role_agent(
        _RunnableFakeChatClient(),
        instructions="be a coordinator",
        tools=[],
        mcp_tools=[mcp_server],
        skill_paths=None,
        sandbox_tools=None,
        web_request_tools=None,
        system_addendum=None,
        workflow_enabled=False,
        workflow_durable_client=None,
        agent_name="coordinator",
        resolved_id=None,
        history_provider=None,
        delegate_tools=[delegate_tool],
    )

    stream = agent.run("hello", stream=True)
    with pytest.raises(ValueError, match="Duplicate tool name 'delegate_billing'"):
        await stream.get_final_response()


# ---------------------------------------------------------------------------
# Real MAF span finalization on timeout/cancellation (FRD 0006 §5 Decision #20)
# ---------------------------------------------------------------------------
#
# `_FakeSpecialistAgent` (used by every other test in this module) never
# touches a real MAF `Agent`/OTel span, so it cannot prove that a specialist
# call stopped by `asyncio.wait_for`'s timeout/cancellation still finalizes
# MAF's own `invoke_agent` span deterministically. The tests below go through
# the real (unmocked) `_build_delegated_agent` -> `_build_role_agent` ->
# `Agent(...)` chain, exactly like B2's
# `test_real_maf_agent_invoke_span_reports_specialist_slug_as_agent_name`,
# with a chat client whose non-streaming response never resolves — modeling a
# hung specialist call — so the handler's own `wait_for`/outer cancellation is
# what actually stops it. Unlike the old streaming design, no explicit
# finalize call is needed in the handler for this to work deterministically
# (verified in STEP 1 against installed `agent-framework-core==1.3.0`): a
# non-streaming `agent.run()`'s OTel spans are opened with an ordinary
# `with`/context-manager (`AgentTelemetryLayer._run` /
# `ChatTelemetryLayer._get_response`), which closes on *any* exception,
# `asyncio.CancelledError` included, via the standard `with` statement's
# `__exit__` guarantee — so these tests inspect the real exported span to
# prove that guarantee holds in practice, not just on paper.


class _NeverRespondingChatClient:
    """Mirrors ``_RunnableFakeChatClient``, but its non-streaming response never resolves.

    Models a specialist call that hangs until an outer timeout/cancellation
    forces it to stop. ``additional_properties`` matches
    ``_RunnableFakeChatClient`` (required by MAF's construction path).
    """

    additional_properties: ClassVar[dict[str, Any]] = {}

    def get_response(self, messages: Any, *, stream: bool = False, **kwargs: Any) -> Any:
        from agent_framework import ResponseStream

        if stream:

            async def _stream() -> Any:
                await asyncio.sleep(30.0)
                yield None  # pragma: no cover - never reached within any test's timeout budget

            return ResponseStream(_stream())

        async def _get_response() -> Any:
            await asyncio.sleep(30.0)
            return None  # pragma: no cover - never reached within any test's timeout budget

        return _get_response()


class _NeverRespondingClientManager(ClientManager):
    """A ``ClientManager`` whose chat client hangs forever — see ``_NeverRespondingChatClient``."""

    def resolve_model(self, requested: str | None) -> str:
        return requested or "fake-model"

    def build_chat_client(self, model: str | None) -> Any:
        return _NeverRespondingChatClient()


def _install_maf_tracer(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Swap MAF's OTel tracer provider for an in-memory exporter and force MAF's own
    instrumentation on, for the duration of one test.

    Extracts the boilerplate
    ``test_real_maf_agent_invoke_span_reports_specialist_slug_as_agent_name`` (B2)
    already uses once: MAF's ``get_tracer()`` (used by ``AgentTelemetryLayer``/
    ``get_function_span`` to create ``invoke_agent``/``execute_tool`` spans) is a
    thin wrapper over ``opentelemetry.trace.get_tracer_provider()`` with no
    per-call injection point, so the provider has to be swapped globally; MAF
    only creates spans at all when
    ``agent_framework.observability.OBSERVABILITY_SETTINGS.enable_instrumentation``
    is truthy (read fresh on every call, so a plain monkeypatch is enough — no
    need to touch this repo's own, separate ``_observability._enabled`` flag).
    Returns the ``InMemorySpanExporter`` so a test can inspect finished spans.
    """
    import agent_framework.observability as maf_observability
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(maf_observability.OBSERVABILITY_SETTINGS, "enable_instrumentation", True)
    return exporter


@pytest.mark.asyncio
async def test_delegate_handler_finalizes_real_maf_agent_span_on_specialist_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A specialist call that times out must still close MAF's own
    ``invoke_agent`` span deterministically — rather than leaving it open
    until a nondeterministic GC-timed ``weakref.finalize`` safety net
    eventually runs.

    ``_NeverRespondingClientManager`` makes the specialist's non-streaming
    response never resolve, forcing the handler's own ``asyncio.wait_for`` to
    time out while awaiting ``specialist_agent.run(task)`` — the
    ``except TimeoutError`` branch this proves exercises returns the
    recoverable, model-facing "did not respond in time" text. Unlike the old
    streaming design, the handler makes no explicit finalize call for this to
    happen: a non-streaming ``agent.run()``'s OTel spans are opened with an
    ordinary ``with``/context-manager, which closes deterministically on any
    exception — ``asyncio.CancelledError`` included (see STEP 1 verification,
    FRD 0006 §5 Decision #20).
    """
    exporter = _install_maf_tracer(monkeypatch)
    span = _install_span_capture(monkeypatch)
    calls = _install_counter_capture(monkeypatch)

    set_client_manager(_NeverRespondingClientManager())

    resolved = _make_resolved(
        name="Billing Specialist",
        slug="billing",
        instructions="Handle billing questions.",
        timeout=0.05,
    )
    catalog = _catalog_of(("billing", resolved))
    loop = asyncio.get_event_loop()
    tools, tracker = await runner.build_subagent_tools(
        [SubagentRef(agent="billing")], catalog, coordinator_deadline=loop.time() + 30
    )
    tool = tools[0]

    result = await tool.func(task="Explain this month's invoice.")

    assert "did not respond in time" in result
    assert tracker.count == 1
    assert calls == [True]
    assert span.attributes["af.delegate.outcome"] == "timeout"

    finished = exporter.get_finished_spans()
    invoke_spans = [s for s in finished if s.name.startswith("invoke_agent")]
    assert len(invoke_spans) == 1, (
        f"expected exactly one finalized invoke_agent span, got: {[s.name for s in finished]}"
    )
    # A span only appears in `get_finished_spans()` once `.end()` has actually
    # been called on it (the in-memory exporter is fed by a `SimpleSpanProcessor`,
    # which exports `on_end`) — proving the span closed deterministically on
    # `asyncio.wait_for`'s timeout, with no adapter-side finalize call at all.
    assert invoke_spans[0].end_time is not None


@pytest.mark.asyncio
async def test_delegate_handler_finalizes_real_maf_agent_span_on_outer_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the timeout test above, but the
    delegate call is stopped by an OUTER task cancellation (e.g. the whole
    coordinator run itself being cancelled) rather than the handler's own
    ``wait_for`` expiring — proving the real ``invoke_agent`` span still
    closes deterministically from the ``except asyncio.CancelledError``
    branch too, not only the ``TimeoutError`` branch, with no explicit
    finalize call needed either way. Cancellation is never a recoverable
    delegate failure (Decision #12), so the tracker/counter must NOT record
    anything.
    """
    exporter = _install_maf_tracer(monkeypatch)
    span = _install_span_capture(monkeypatch)
    calls = _install_counter_capture(monkeypatch)

    set_client_manager(_NeverRespondingClientManager())

    resolved = _make_resolved(
        name="Billing Specialist",
        slug="billing",
        instructions="Handle billing questions.",
        timeout=30.0,
    )
    catalog = _catalog_of(("billing", resolved))
    loop = asyncio.get_event_loop()
    tools, tracker = await runner.build_subagent_tools(
        [SubagentRef(agent="billing")], catalog, coordinator_deadline=loop.time() + 30
    )
    tool = tools[0]

    task = asyncio.ensure_future(tool.func(task="Explain this month's invoice."))
    await asyncio.sleep(0.1)  # let the call actually reach the never-responding chat client
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert tracker.count == 0
    assert calls == []
    assert span.attributes["af.delegate.outcome"] == "cancelled"

    finished = exporter.get_finished_spans()
    invoke_spans = [s for s in finished if s.name.startswith("invoke_agent")]
    assert len(invoke_spans) == 1, (
        f"expected exactly one finalized invoke_agent span, got: {[s.name for s in finished]}"
    )
    assert invoke_spans[0].end_time is not None


# ---------------------------------------------------------------------------
# Telemetry preserves the exact caught timeout exception object (round-3 S3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_adapter_timeout_records_the_exact_wait_for_exception_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3: telemetry for a ``wait_for``-expiry timeout must preserve the EXACT
    exception object ``asyncio.wait_for`` raised, not a freshly-constructed
    ``TimeoutError`` built purely from the specialist's slug/timeout.

    Spies on the real ``asyncio.wait_for`` (wrapping it, not replacing its
    behavior — the actual timeout still fires) to capture the exact instance
    it raises on expiry, then asserts the span's recorded exception object
    IS that instance (identity, not just str/type equality).
    """
    span = _install_span_capture(monkeypatch)
    _install_counter_capture(monkeypatch)

    real_wait_for = asyncio.wait_for
    captured: list[BaseException] = []

    async def spy_wait_for(*args: Any, **kwargs: Any) -> Any:
        try:
            return await real_wait_for(*args, **kwargs)
        except TimeoutError as exc:
            captured.append(exc)
            raise

    monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)

    async def slow_respond(task: str) -> str:
        await asyncio.sleep(10)
        return "too late"

    tool, tracker = await _build_single_delegate_tool(
        monkeypatch, slug="billing", respond=slow_respond, resolved_timeout=0.05
    )

    result = await tool.func(task="invoice #42")

    assert "did not respond in time" in result
    assert tracker.count == 1
    assert len(captured) == 1
    assert span.exceptions
    recorded_exc, fault_domain = span.exceptions[-1]
    assert recorded_exc is captured[0]
    assert fault_domain == obs.FaultDomain.DELEGATE


@pytest.mark.asyncio
async def test_delegate_adapter_preserves_inner_timeout_error_instance_in_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3/S3b: a ``TimeoutError`` the specialist's OWN code raises internally
    (for an unrelated reason, not ``wait_for`` expiry — here, raised
    essentially instantly, far inside the 5s budget) must be preserved as the
    recorded exception object rather than replaced by a synthetic one,
    proving the fix isn't special-cased to only the ``wait_for``-expiry
    shape. Python 3.11+ unifies ``asyncio.TimeoutError`` with the builtin
    ``TimeoutError``, so the two cases are structurally indistinguishable by
    type; this only works because ``except TimeoutError as exc: ...
    record_exception(exc, ...)`` preserves whichever object was ACTUALLY
    caught, regardless of its origin.

    S3b (round 4): this case must also be *classified* as an ordinary
    recoverable delegate failure (``outcome=error``), not as a coordinator
    deadline expiry (``outcome=timeout``) — the specialist raised this well
    before ``effective_timeout`` elapsed, so it is not actually a budget/
    deadline event at all, even though it happens to be a ``TimeoutError``
    instance. Distinguished from a genuine ``wait_for`` expiry by elapsed-time
    heuristic (see ``_INNER_TIMEOUT_MISCLASSIFICATION_TOLERANCE_SECONDS``),
    since the two cases are, again, structurally indistinguishable by type.
    """
    span = _install_span_capture(monkeypatch)
    calls = _install_counter_capture(monkeypatch)

    marker = "specialist's own unrelated timeout"

    async def respond(task: str) -> str:
        raise TimeoutError(marker)

    tool, tracker = await _build_single_delegate_tool(monkeypatch, slug="billing", respond=respond)

    result = await tool.func(task="invoice #42")

    assert "did not respond in time" not in result  # not a deadline/timeout outcome (S3b)
    assert "could not complete this task" in result  # generic sanitized failure message instead
    assert tracker.count == 1
    assert calls == [True]
    assert span.attributes["af.delegate.outcome"] == "error"
    assert span.exceptions
    recorded_exc, fault_domain = span.exceptions[-1]
    assert recorded_exc is not None
    assert str(recorded_exc) == marker
    assert fault_domain == obs.FaultDomain.DELEGATE


# ---------------------------------------------------------------------------
# End-to-end span tree through MAF's real `FunctionTool.invoke()` (round-3 S4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_delegate_tool_invoke_produces_nested_execute_tool_and_invoke_agent_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S4: a real end-to-end span tree, going through MAF's actual
    ``FunctionTool.invoke()`` entry point (parameter validation/injection)
    rather than calling ``tool.func`` directly, under an ACTIVE coordinator
    span. Asserts 3-level nesting:

    * ``agent.run coordinator``      — opened by this test, standing in for
      the coordinator's own run-level span (``run_agent``/``run_agent_stream``
      open an equivalent span around the whole turn in production).
    * ``execute_tool delegate_billing`` — opened by MAF's own
      ``FunctionTool.invoke()`` (via ``get_function_span``) as a child of it.
    * ``invoke_agent billing``       — opened by MAF's ``AgentTelemetryLayer``
      when the specialist's real ``Agent.run()`` executes, as a child of
      THAT.

    All three must share one trace id.
    """
    exporter = _install_maf_tracer(monkeypatch)
    _install_span_capture(monkeypatch)
    _install_counter_capture(monkeypatch)

    from opentelemetry import trace as ot_trace

    set_client_manager(_RunnableFakeClientManager())

    resolved = _make_resolved(
        name="Billing Specialist", slug="billing", instructions="Handle billing questions."
    )
    catalog = _catalog_of(("billing", resolved))
    loop = asyncio.get_event_loop()
    tools, _tracker = await runner.build_subagent_tools(
        [SubagentRef(agent="billing")], catalog, coordinator_deadline=loop.time() + 30
    )
    tool = tools[0]

    tracer = ot_trace.get_tracer("test-coordinator")
    with tracer.start_as_current_span("agent.run coordinator"):
        # `skip_parsing=True` returns the delegate tool's raw `str` return
        # value directly (matching `tool.func(...)`'s own return shape used
        # by every other test in this module) instead of MAF's default
        # `list[Content]` wrapping — the wrapping itself is orthogonal to
        # what this test is proving (the span tree).
        result = await tool.invoke(task="Explain this month's invoice.", skip_parsing=True)

    assert result == "specialist response"

    finished = exporter.get_finished_spans()
    by_name = {s.name: s for s in finished}
    assert {"agent.run coordinator", "execute_tool delegate_billing", "invoke_agent billing"} <= set(
        by_name
    )

    coordinator_span = by_name["agent.run coordinator"]
    execute_tool_span = by_name["execute_tool delegate_billing"]
    invoke_agent_span = by_name["invoke_agent billing"]

    assert execute_tool_span.parent is not None
    assert execute_tool_span.parent.span_id == coordinator_span.context.span_id
    assert invoke_agent_span.parent is not None
    assert invoke_agent_span.parent.span_id == execute_tool_span.context.span_id

    trace_ids = {
        coordinator_span.context.trace_id,
        execute_tool_span.context.trace_id,
        invoke_agent_span.context.trace_id,
    }
    assert len(trace_ids) == 1  # coordinator + execute_tool + invoke_agent share one trace
