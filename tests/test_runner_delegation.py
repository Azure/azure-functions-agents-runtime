"""Tests for chat-time sub-agent delegation (FRD 0006 v1).

Covers the pieces added to :mod:`azure_functions_agents.runner` for
delegation: the ``direct``/``delegated`` execution-role split
(``_build_role_agent`` / ``_build_delegated_agent``), single-level
structural enforcement (Decision #6), ``build_subagent_tools``'s guard
clauses, the ``delegate_<slug>`` adapter's failure/cancellation split
(Decision #12), per-specialist serialization vs. cross-specialist
parallelism (Decision #14), and delegation observability enrichment
(Decision #19, §4.12).

Fake specialist harness
------------------------

MAF's ``BaseAgent.as_tool()`` is only defined on ``BaseAgent`` (not on the
``SupportsAgentRun`` protocol), so a usable fake specialist must subclass
``agent_framework.BaseAgent``. ``as_tool()``'s wrapper calls
``self.run(..., stream=True, ...)`` *without* ``await`` and then
``await``s the returned object's ``get_final_response()`` — so
``_FakeSpecialistAgent.run()`` below is a plain (non-``async``) method
returning a ``_FakeStream``, matching that exact calling convention.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from agent_framework import BaseAgent

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
        pass


def _install_span_capture(monkeypatch: pytest.MonkeyPatch) -> _RecordingSpan:
    span = _RecordingSpan()
    monkeypatch.setattr(runner, "current_span", lambda: span)
    return span


def _install_counter_capture(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []
    monkeypatch.setattr(runner, "record_delegate_call", lambda *, error: calls.append(error))
    return calls


class _FakeStream:
    """Stands in for MAF's ``ResponseStream`` — only ``get_final_response()`` is used."""

    def __init__(self, message: str, respond: Callable[[str], Awaitable[str]]) -> None:
        self._message = message
        self._respond = respond

    async def get_final_response(self) -> Any:
        text = await self._respond(self._message)
        return SimpleNamespace(text=text, user_input_requests=[])


class _FakeSpecialistAgent(BaseAgent):
    """A specialist double: subclasses ``BaseAgent`` (required for ``as_tool()``).

    ``run()`` is a plain, non-``async`` method — see module docstring for why
    this exact shape (rather than ``async def run``) is required to match
    ``as_tool()``'s ``_agent_wrapper`` calling convention.
    """

    def __init__(self, slug: str, respond: Callable[[str], Awaitable[str]]) -> None:
        super().__init__(id=slug, name=slug, description=f"{slug} specialist")
        self._respond = respond

    def run(
        self,
        messages: Any = None,
        *,
        stream: bool = False,
        session: Any = None,
        function_invocation_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        return _FakeStream(str(messages or ""), self._respond)

    async def create_session(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def get_session(self, session_id: str, **kwargs: Any) -> Any:
        raise NotImplementedError


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


def test_sanitize_delegate_failure_includes_slug_and_type_not_raw_detail() -> None:
    message = runner._sanitize_delegate_failure("billing", RuntimeError("db password is hunter2"))

    assert "billing" in message
    assert "RuntimeError" in message
    assert "hunter2" not in message  # raw exception detail must never leak to the model


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


@pytest.mark.asyncio
async def test_single_level_delegation_end_to_end_with_mutual_subagents_refs_does_not_recurse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_client_manager(_FakeClientManager())

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

    # The coordinator gets exactly delegate_a — B is never touched, and A
    # (despite declaring `subagents: [b]` itself) was built with zero tools
    # at all, so it has no way to further delegate to B.
    assert [t.name for t in tools] == ["delegate_a"]
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

    result = await tool.func(SimpleNamespace(kwargs={}), task="invoice #42")

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

    result = await tool.func(SimpleNamespace(kwargs={}), task="invoice #42")

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

    result = await tool.func(SimpleNamespace(kwargs={}), task="invoice #42")

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
    effective_timeout = min(specialist_timeout, coordinator_remaining)."""
    _install_span_capture(monkeypatch)
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

    result = await tool.func(SimpleNamespace(kwargs={}), task="invoice #42")

    assert "did not respond in time" in result
    assert tracker.count == 1
    assert body_ran is False  # wait_for(timeout<=0) cancels before the body ever runs


@pytest.mark.asyncio
async def test_delegate_adapter_propagates_cancellation_without_recording_a_delegate_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_span_capture(monkeypatch)
    calls = _install_counter_capture(monkeypatch)

    async def respond(task: str) -> str:
        await asyncio.sleep(10)
        return "unreachable"

    tool, tracker = await _build_single_delegate_tool(monkeypatch, slug="billing", respond=respond)

    task = asyncio.ensure_future(tool.func(SimpleNamespace(kwargs={}), task="invoice #42"))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Parent/request cancellation propagates + aborts (Decision #12) — it is
    # never mistaken for a recoverable specialist failure.
    assert tracker.count == 0
    assert calls == []


# ---------------------------------------------------------------------------
# Concurrency (Decision #14): serialize same-specialist, parallel different
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_adapter_serializes_concurrent_calls_to_same_specialist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_span_capture(monkeypatch)
    _install_counter_capture(monkeypatch)

    log: list[str] = []

    async def respond(task: str) -> str:
        log.append("start")
        await asyncio.sleep(0.05)
        log.append("end")
        return "ok"

    tool, _tracker = await _build_single_delegate_tool(monkeypatch, slug="billing", respond=respond)
    ctx = SimpleNamespace(kwargs={})

    await asyncio.gather(
        tool.func(ctx, task="first"),
        tool.func(ctx, task="second"),
    )

    # The per-specialist asyncio.Lock guarantees the second call's body
    # cannot begin until the first has fully finished.
    assert log == ["start", "end", "start", "end"]


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
    ctx = SimpleNamespace(kwargs={})

    results = await asyncio.gather(*(t.func(ctx, task="go") for t in tools))

    assert set(results) == {"billing-done", "shipping-done"}
    # Different specialists run concurrently: billing's whole run overlaps
    # with shipping's, regardless of exact micro-ordering of lock
    # acquisition — proven by each starting before the other ends.
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
    ctx = SimpleNamespace(kwargs={})

    async def _call_with_nested_span(tool: Any) -> str:
        # Mimics MAF's FunctionTool.invoke(), which auto-nests an
        # `execute_tool <name>` span around every tool call — the delegate
        # adapter's current_span() then annotates *that* span rather than
        # opening a second one (FRD 0006 §4.12).
        with tracer.start_as_current_span(f"execute_tool {tool.name}"):
            return str(await tool.func(ctx, task="do it"))

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
