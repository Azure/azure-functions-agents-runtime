from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agent_framework import (
    AgentResponse,
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    Content,
    FunctionTool,
    Message,
    UsageDetails,
)

from azure_functions_agents import runner
from azure_functions_agents._agent_identity import agent_id
from azure_functions_agents.client_manager import InferenceTarget
from azure_functions_agents.config.schema import (
    AgentConfiguration,
    AgentFrameworkCompactionConfig,
    AgentFrameworkConfiguration,
    SubagentRef,
)
from azure_functions_agents.discovery.mcp import MCPServerDescriptor
from azure_functions_agents.harness import _harness_execution as shared
from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
from azure_functions_agents.harness.agent_framework import _maf_execution as maf
from azure_functions_agents.harness.agent_framework import _maf_mcp


@pytest.fixture(autouse=True)
def isolated_execution(monkeypatch):
    monkeypatch.setattr(shared, "_SESSION_LOCKS", {})
    monkeypatch.setattr(shared, "_SESSION_LOCKS_GUARD", asyncio.Lock())
    monkeypatch.setattr(runner, "get_harness", lambda: AppHarness(HarnessKind.MAF, Path.cwd()))
    for name in (
        "WEBSITE_OWNER_NAME",
        "WEBSITE_DEPLOYMENT_ID",
        "WEBSITE_SITE_NAME",
        "AzureWebJobsStorage",
        "AzureWebJobsStorage__blobServiceUri",
        "AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT",
        "AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY",
    ):
        monkeypatch.delenv(name, raising=False)


def usage_records(caplog):
    return [
        json.loads(record.getMessage().removeprefix("Agent token usage: "))
        for record in caplog.records
        if record.getMessage().startswith("Agent token usage: ")
    ]


def install_agent(monkeypatch, agent, *, tracker=None):
    build = AsyncMock(
        return_value=(agent, object(), "session", tracker, InferenceTarget("openai", "fixture"))
    )
    monkeypatch.setattr(runner, "_build_agent_session", build)
    return build


@pytest.mark.parametrize(
    ("public_name", "adapter_name"),
    [("run_agent", "run"), ("run_agent_stream", "run_stream")],
)
def test_public_entrypoints_keep_explicit_parameters_and_adapters_accept_neutral_requests(
    public_name, adapter_name
):
    public = inspect.signature(getattr(runner, public_name))
    selected = inspect.signature(getattr(maf, adapter_name))
    assert list(selected.parameters)[:2] == ["harness", "request"]
    assert selected.parameters["request"].annotation == "HarnessRequest"
    assert public.parameters["tools"].default is None
    assert public.parameters["skills"].default is None
    assert public.parameters["skill_catalog"].default is None
    for signature in (public, selected):
        assert all(
            parameter.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
            for parameter in signature.parameters.values()
        )


def test_backend_callbacks_and_public_results_use_the_canonical_facade_and_accounting():
    assert maf._runner is runner
    assert runner._AgentUsageRecorder is shared._AgentUsageRecorder
    assert runner._get_session_lock is shared._get_session_lock
    assert runner._session_lock_bounded_by is shared._session_lock_bounded_by


def test_role_constructor_keeps_identity_options_compaction_and_skills(monkeypatch, tmp_path):
    import agent_framework

    monkeypatch.setenv("WEBSITE_SITE_NAME", " Fixture-Site ")
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "fixture-owner")
    create = Mock(return_value=object())
    skills = Mock(return_value=object())
    monkeypatch.setattr(agent_framework, "create_harness_agent", create)
    monkeypatch.setattr(agent_framework.SkillsProvider, "from_paths", skills)
    configuration = AgentConfiguration(
        max_output_tokens=128,
        agent_framework=AgentFrameworkConfiguration(
            compaction=AgentFrameworkCompactionConfig(max_context_window_tokens=4096)
        ),
    )
    tools = [object()]
    history = object()
    client = object()
    maf._build_role_agent(
        client,
        agent_instructions="instructions",
        tools=tools,
        skill_paths=[tmp_path],
        agent_name="billing",
        history_provider=history,
        agent_configuration=configuration,
    )
    create.assert_called_once_with(
        client,
        id=agent_id("billing"),
        name="Fixture-Site/billing",
        harness_instructions="",
        agent_instructions="instructions",
        tools=tools,
        history_provider=history,
        skills_provider=skills.return_value,
        disable_tool_auto_approval=True,
        disable_web_search=True,
        disable_todo=True,
        disable_mode=True,
        max_context_window_tokens=4096,
        max_output_tokens=128,
        disable_file_memory=True,
        default_options={"store": False},
    )
    skills.assert_called_once_with(
        [tmp_path],
        disable_load_skill_approval=True,
        disable_read_skill_resource_approval=True,
        disable_run_skill_script_approval=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [False, True])
async def test_session_builder_consumes_actual_host_hooks_and_none_vs_empty(
    monkeypatch, overrides
):
    user_tool = FunctionTool(name="user", func=lambda: "user")
    web_tool = FunctionTool(name="web", func=lambda: "web")
    delegate_tool = FunctionTool(name="delegate_specialist", func=lambda: "specialist")
    mcp_tool = object()
    server = MCPServerDescriptor.create(name="server", url="https://fixture.invalid/mcp")
    users = Mock(return_value=SimpleNamespace(tools=[user_tool]))
    mcps = Mock(return_value=SimpleNamespace(servers={"server": server}))
    monkeypatch.setattr(_maf_mcp, "build_maf_mcp_tools", lambda servers: [mcp_tool])
    monkeypatch.setattr(maf, "discover_user_tools", users)
    monkeypatch.setattr(maf, "discover_mcp_servers", mcps)
    tracker = runner._DelegateErrorTracker()
    delegates = AsyncMock(return_value=([delegate_tool], tracker))
    monkeypatch.setattr(runner, "build_subagent_tools", delegates)
    assemble = Mock(wraps=runner._assemble_agent_inputs)
    monkeypatch.setattr(runner, "_assemble_agent_inputs", assemble)
    manager = SimpleNamespace(
        build_chat_client_with_target=Mock(return_value=(object(), InferenceTarget()))
    )
    monkeypatch.setattr(maf, "get_client_manager", lambda: manager)
    history = object()
    monkeypatch.setattr(runner, "_build_history_provider", lambda slug: history)
    role = Mock(return_value=object())
    monkeypatch.setattr(runner, "_build_role_agent", role)
    references = [SubagentRef(agent="specialist")]
    catalog = object()

    _, session, session_id, returned_tracker, _ = await maf._build_agent_session(
        instructions="  host instructions  ",
        session_id="shared",
        tools=[] if overrides else None,
        mcp_tools=[] if overrides else None,
        skill_paths=None,
        model=None,
        sandbox_tools=None,
        system_addendum="\naddendum",
        workflow_enabled=False,
        workflow_durable_client=None,
        agent_name="billing",
        web_request_tools=[web_tool],
        subagents=references,
        catalog=catalog,
        coordinator_deadline=123.0,
    )
    assert session.session_id == session_id == "shared"
    assert returned_tracker is tracker
    manager.build_chat_client_with_target.assert_called_once_with(None)
    delegates.assert_awaited_once_with(
        references, catalog, coordinator_deadline=123.0, _harness=None
    )
    assemble.assert_called_once()
    expected = [web_tool, delegate_tool] if overrides else [user_tool, web_tool, mcp_tool, delegate_tool]
    assert role.call_args.kwargs["tools"] == expected
    assert role.call_args.kwargs["agent_instructions"] == "host instructions\naddendum"
    assert role.call_args.kwargs["history_provider"] is history
    assert users.call_count == mcps.call_count == (0 if overrides else 1)


class _RecordingChatClient(ChatMiddlewareLayer, BaseChatClient):
    def __init__(self):
        super().__init__()
        self.calls = []

    def _inner_get_response(
        self, *, messages: Sequence[Message], stream: bool, options: Mapping, **kwargs
    ):
        assert not stream
        self.calls.append([message.text for message in messages])

        async def response():
            return ChatResponse(messages=[Message("assistant", ["reply"])])

        return response()


@pytest.mark.asyncio
async def test_real_maf_turns_reload_scoped_history_from_fresh_sessions(monkeypatch, tmp_path):
    client = _RecordingChatClient()
    manager = SimpleNamespace(
        build_chat_client_with_target=lambda model: (client, InferenceTarget())
    )
    monkeypatch.setattr(maf, "get_client_manager", lambda: manager)
    monkeypatch.setattr(runner, "resolve_config_dir", lambda: tmp_path)
    first = await runner.run_agent(
        "one", tools=[], mcp_tools=[], session_id="shared", agent_name="billing", timeout=5
    )
    second = await runner.run_agent(
        "two", tools=[], mcp_tools=[], session_id="shared", agent_name="billing", timeout=5
    )
    assert type(first) is type(second) is runner.AgentResult
    assert first.content == second.content == "reply"
    assert client.calls == [["one"], ["one", "reply", "two"]]
    assert (tmp_path / "agent-sessions" / "billing" / "shared.jsonl").is_file()


def test_blob_selection_does_not_import_local_or_copilot_history(monkeypatch):
    from azure_functions_agents.harness.agent_framework import _maf_blob_history

    provider = object()
    monkeypatch.setattr(
        _maf_blob_history, "build_blob_provider_from_environment", lambda **kwargs: provider
    )
    monkeypatch.setitem(
        sys.modules, "azure_functions_agents.harness.agent_framework._maf_file_history", None
    )
    monkeypatch.setitem(
        sys.modules, "azure_functions_agents.harness.copilot_sdk._copilot_session_fs", None
    )
    assert maf._build_history_provider("billing") is provider


def test_configured_blob_history_failure_never_falls_back(monkeypatch):
    from azure_functions_agents.harness.agent_framework import _maf_blob_history

    fail = Mock(side_effect=ValueError("fixture invalid Blob configuration"))
    monkeypatch.setattr(_maf_blob_history, "build_blob_provider_from_environment", fail)
    local = Mock()
    monkeypatch.setattr(maf, "_resolve_sessions_dir", local)
    with pytest.raises(ValueError, match="invalid Blob"):
        maf._build_history_provider("billing")
    local.assert_not_called()


@pytest.mark.asyncio
async def test_direct_result_and_usage_use_public_result_and_shared_recorder(monkeypatch, caplog):
    response = AgentResponse(
        messages=[Message("assistant", [
            Content("text", text="fallback"),
            Content("function_call", call_id="call", name="tool", arguments="{}"),
            Content("function_result", call_id="call", result="result"),
        ])],
        usage_details=UsageDetails(input_token_count=3, output_token_count=2),
    )
    agent = SimpleNamespace(run=AsyncMock(return_value=response))
    build = install_agent(monkeypatch, agent, tracker=SimpleNamespace(count=2))
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", " low ")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY", "auto")
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        result = await runner.run_agent("prompt", tools=[], mcp_tools=[], agent_name="billing")
    assert type(result) is runner.AgentResult
    assert result.content == "fallback"
    assert result.delegate_error_count == 2
    assert result.tool_calls == [{
        "type": "tool_start", "tool_call_id": "call", "tool_name": "tool",
        "arguments": "{}", "result": "result", "turn_id": "response-0", "success": True,
    }]
    assert result.model == "fixture"
    assert agent.run.call_args.kwargs["options"] == {
        "reasoning": {"effort": "low", "summary": "auto"}
    }
    assert build.call_args.kwargs["tools"] == build.call_args.kwargs["mcp_tools"] == ()
    assert ("billing", "session") in shared._SESSION_LOCKS
    assert len(usage_records(caplog)) == 1
    assert usage_records(caplog)[0]["input_tokens"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_lock_wait_exhaustion_emits_no_invocation_usage(monkeypatch, caplog, streaming):
    agent = SimpleNamespace(run=Mock(side_effect=AssertionError("No invocation expected.")))
    install_agent(monkeypatch, agent)
    lock = await shared._get_session_lock("session", "billing")
    await lock.acquire()
    try:
        with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
            if streaming:
                events = [
                    json.loads(chunk.removeprefix("data: "))
                    async for chunk in runner.run_agent_stream("prompt", timeout=0.01, agent_name="billing")
                ]
                assert [event["type"] for event in events] == ["session", "error"]
            else:
                with pytest.raises(RuntimeError, match="timed out"):
                    await runner.run_agent("prompt", timeout=0.01, agent_name="billing")
        assert usage_records(caplog) == []
        assert lock.locked()
        agent.run.assert_not_called()
    finally:
        lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["delegate", "workflow_subagent"])
async def test_leaf_invocations_are_fresh_and_use_common_accounting(monkeypatch, caplog, role):
    agents = [SimpleNamespace(run=AsyncMock(return_value=AgentResponse(
        messages=[Message("assistant", ["reply"])],
        usage_details=UsageDetails(output_token_count=2),
    ))) for _ in range(2)]
    build = Mock(side_effect=[(agent, InferenceTarget()) for agent in agents])
    monkeypatch.setattr(runner, "_build_delegated_agent", build)
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        for _ in agents:
            assert await maf.run_leaf_agent_task(
                SimpleNamespace(slug="specialist"), object(), "task", timeout=1, execution_role=role
            ) == "reply"
    assert build.call_count == 2
    assert [record["execution_role"] for record in usage_records(caplog)] == [role, role]
    assert shared._SESSION_LOCKS == {}


class _Stream:
    _inner_stream = None

    def __init__(self, contents, *, failure=None, stall=False):
        self.updates = [SimpleNamespace(contents=items) for items in contents]
        self.failure = failure
        self.stall = stall
        self.started = asyncio.Event()
        self._stream_error = None
        self.cleanup_errors = []
        self.final_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.started.set()
        if self.stall:
            await asyncio.Event().wait()
        if self.updates:
            return self.updates.pop(0)
        if self.failure is not None:
            raise self.failure
        raise StopAsyncIteration

    async def _run_cleanup_hooks(self):
        self.cleanup_errors.append(self._stream_error)

    async def get_final_response(self):
        self.final_calls += 1
        return AgentResponse(
            messages=[],
            usage_details=UsageDetails(input_token_count=4, output_token_count=3),
        )


def item(kind, **kwargs):
    return Content(kind, **kwargs)


def install_stream(monkeypatch, stream, *, tracker=None):
    agent = SimpleNamespace(run=Mock(return_value=stream))
    install_agent(monkeypatch, agent, tracker=tracker)
    span = SimpleNamespace(set_attribute=Mock(), record_exception=Mock())

    @contextlib.contextmanager
    def start(*args, **kwargs):
        yield span

    monkeypatch.setattr(runner, "start_span", start)
    return agent, span


@pytest.mark.asyncio
async def test_stream_order_tool_fragments_reasoning_and_done_before_usage(monkeypatch, caplog):
    stream = _Stream([
        [item("text", text="answer"), item("text_reasoning", text="reasoning")],
        [item("function_call", call_id="call", name="tool", arguments='{"x":')],
        [item("function_call", call_id="call", name="tool", arguments="1}")],
        [item("function_result", call_id="call", result='{"error":"fixture"}')],
    ])
    _, span = install_stream(monkeypatch, stream, tracker=SimpleNamespace(count=2))
    generator = runner.run_agent_stream("prompt", agent_name="billing", display_name="Billing")
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        events = []
        while True:
            event = json.loads((await anext(generator)).removeprefix("data: "))
            events.append(event)
            if event["type"] == "done":
                break
        assert stream.final_calls == 0
        await generator.aclose()
    assert [event["type"] for event in events] == [
        "session", "delta", "intermediate", "tool_start", "tool_end", "done"
    ]
    assert events[3]["arguments"] == '{"x":1}'
    assert stream.final_calls == 1
    assert usage_records(caplog)[0]["input_tokens"] == 4
    span.set_attribute.assert_any_call("af.agent.tool_error_count", 3)
    assert not (await shared._get_session_lock("session", "billing")).locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("fixture"), RuntimeError("fixture")])
async def test_stream_terminal_errors_finalize_and_record_one_attempt(monkeypatch, caplog, failure):
    stream = _Stream([[item("text", text="partial")]], failure=failure)
    _, span = install_stream(monkeypatch, stream)
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        events = [
            json.loads(chunk.removeprefix("data: "))
            async for chunk in runner.run_agent_stream("prompt", timeout=1)
        ]
    assert [event["type"] for event in events] == ["session", "delta", "error"]
    assert len(stream.cleanup_errors) == 1
    assert stream._stream_error is None
    assert len(usage_records(caplog)) == 1
    span.record_exception.assert_called_once()


@pytest.mark.asyncio
async def test_stream_close_at_yield_finalizes_chain_and_releases_lock(monkeypatch, caplog):
    stream = _Stream([[item("text", text="partial")]])
    inner = _Stream([])
    stream._inner_stream = inner
    install_stream(monkeypatch, stream)
    generator = runner.run_agent_stream("prompt")
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        await anext(generator)
        await anext(generator)
        await generator.aclose()
    assert isinstance(stream.cleanup_errors[0], GeneratorExit)
    assert inner.cleanup_errors == stream.cleanup_errors
    assert len(usage_records(caplog)) == 1
    assert not (await shared._get_session_lock("session")).locked()


@pytest.mark.asyncio
async def test_stream_cancellation_while_waiting_preserves_cleanup_and_propagation(monkeypatch, caplog):
    stream = _Stream([], stall=True)
    install_stream(monkeypatch, stream)
    generator = runner.run_agent_stream("prompt")
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        await anext(generator)
        task = asyncio.create_task(anext(generator))
        await stream.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert isinstance(stream.cleanup_errors[0], asyncio.CancelledError)
    assert len(usage_records(caplog)) == 1
    assert not (await shared._get_session_lock("session")).locked()
