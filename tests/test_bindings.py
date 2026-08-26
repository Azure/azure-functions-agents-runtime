from __future__ import annotations

import asyncio
import gc
import inspect
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import azure.durable_functions as df
import azure.functions as func
import pytest
from agent_framework import Agent

from azure_functions_agents import AiApp as ExportedAiApp
from azure_functions_agents import DurableAgentContext
from azure_functions_agents import DurableAiApp as ExportedDurableAiApp
from azure_functions_agents import bindings as bindings_module
from azure_functions_agents import markdown_agent as exported_markdown_agent
from azure_functions_agents.bindings import (
    AiApp,
    DurableAiApp,
    markdown_agent,
)
from azure_functions_agents.durable import _INTERNAL_AGENT_ACTIVITY_NAME
from azure_functions_agents.hydration import AgentBlueprint


def _write_agent(root: Path) -> None:
    (root / "order-fulfillment.agent.md").write_text(
        "---\nname: Orders\ndescription: Processes orders\n---\nBe useful.\n",
        encoding="utf-8",
    )


def _agent_double() -> Any:
    agent = MagicMock(spec=Agent)
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock(return_value=None)
    return agent


@pytest.mark.asyncio
async def test_ai_app_hides_and_injects_async_parameter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent(tmp_path)
    app = AiApp(app_root=tmp_path)
    seen: list[Any] = []
    agents: list[Any] = []

    def build(_blueprint: AgentBlueprint, _invocation: Any = None) -> Any:
        agent = _agent_double()
        agents.append(agent)
        return agent

    monkeypatch.setattr(AgentBlueprint, "build", build)

    @app.markdown_agent(arg_name="order_agent", agent_name="order-fulfillment")
    async def process_order(req: func.HttpRequest, order_agent: Agent[Any]) -> str:
        seen.append(order_agent)
        return req.method

    app.route(route="orders", methods=["POST"])(process_order)
    assert list(inspect.signature(process_order).parameters) == ["req"]
    request = func.HttpRequest("POST", "https://example.test/orders", body=b"")
    assert await process_order(request) == "POST"
    assert await process_order(request) == "POST"
    assert len(seen) == len(agents) == 2
    assert seen == agents
    assert agents[0] is not agents[1]
    assert all(isinstance(agent, Agent) for agent in agents)
    assert all(agent.__aenter__.await_count == 1 for agent in agents)
    assert all(agent.__aexit__.await_count == 1 for agent in agents)

    [registered] = app.get_functions()
    assert registered.get_function_name() == "process_order"
    bindings = [binding.get_dict_repr() for binding in registered.get_bindings()]
    assert any(binding.get("type") == "httpTrigger" for binding in bindings)
    assert all(binding.get("name") != "order_agent" for binding in bindings)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_async_handler_failure_or_cancellation_closes_agent(
    error: BaseException,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent(tmp_path)
    app = AiApp(app_root=tmp_path)
    agent = _agent_double()
    monkeypatch.setattr(AgentBlueprint, "build", lambda *_args: agent)

    @app.markdown_agent(arg_name="agent", agent_name="order-fulfillment")
    async def handler(agent: Agent[Any]) -> None:
        raise error

    with pytest.raises(type(error)):
        await handler()

    agent.__aexit__.assert_awaited_once()
    assert agent.__aexit__.await_args.args[0] is type(error)


@pytest.mark.asyncio
async def test_free_decorator_supports_existing_app_and_async_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent(tmp_path)
    app = func.FunctionApp()
    built_agent = _agent_double()

    from azure_functions_agents.config.paths import set_app_root

    set_app_root(tmp_path)
    monkeypatch.setattr(AgentBlueprint, "build", lambda *_args: built_agent)

    @markdown_agent(app, arg_name="agent", agent_name="order_fulfillment")
    async def handler(value: str, agent: Agent[Any]) -> tuple[str, Agent[Any]]:
        return value, agent

    value, injected = await handler("ok")
    assert value == "ok"
    assert injected is built_agent
    assert list(inspect.signature(handler).parameters) == ["value"]
    built_agent.__aexit__.assert_awaited_once()


def test_free_decorator_rejects_sync_handler(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = func.FunctionApp()

    from azure_functions_agents.config.paths import set_app_root

    set_app_root(tmp_path)

    with pytest.raises(TypeError, match=r"requires an async def handler"):

        @markdown_agent(app, arg_name="agent", agent_name="order_fulfillment")
        def handler(value: str, agent: Agent[Any]) -> str:
            return value


def test_reverse_decorator_order_is_rejected(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = AiApp(app_root=tmp_path)

    with pytest.raises(TypeError, match="innermost decorator"):

        @app.markdown_agent(arg_name="agent", agent_name="order-fulfillment")
        @app.route(route="wrong")
        async def wrong_order(req: func.HttpRequest, agent: Agent[Any]) -> str:
            return req.method


def test_binding_rejects_app_wide_discovery_failures(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "broken_tool.py").write_text("def broken(\n", encoding="utf-8")
    app = AiApp(app_root=tmp_path)

    with pytest.raises(
        ValueError,
        match=(
            r"Agent binding app-wide capability discovery failed.*"
            r"broken_tool\.py.*discover app-level tools, skills, and MCP servers.*"
            r"before global tool exclusions.*no per-agent capability filters.*"
            r"Fix or remove the failing assets"
        ),
    ):

        @app.markdown_agent(arg_name="agent", agent_name="order-fulfillment")
        async def handler(agent: Agent[Any]) -> None:
            pass


@pytest.mark.asyncio
async def test_durable_activity_injects_raw_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)
    agent = _agent_double()
    monkeypatch.setattr(AgentBlueprint, "build", lambda *_args: agent)

    @app.markdown_agent(
        arg_name="agent",
        agent_name="order-fulfillment",
    )
    async def activity(payload: str, agent: Agent[Any]) -> tuple[str, Agent[Any]]:
        return payload, agent

    payload, injected = await activity("work")
    assert payload == "work"
    assert isinstance(injected, Agent)
    agent.__aexit__.assert_awaited_once()


def test_sync_activity_is_rejected(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    with pytest.raises(TypeError, match=r"requires an async def handler"):

        @app.markdown_agent(
            arg_name="activity_agent",
            agent_name="order-fulfillment",
        )
        def activity(payload: str, activity_agent: Agent[Any]) -> str:
            return payload


def test_binding_app_types_are_exported() -> None:
    assert ExportedAiApp is AiApp
    assert ExportedDurableAiApp is DurableAiApp
    assert exported_markdown_agent is markdown_agent
    assert not hasattr(bindings_module, "agent")
    assert not hasattr(bindings_module, "agent_input")
    assert "mode" not in inspect.signature(markdown_agent).parameters
    assert "mode" not in inspect.signature(AiApp.markdown_agent).parameters
    assert "mode" not in inspect.signature(DurableAiApp.markdown_agent).parameters


def test_app_instances_own_separate_agent_blueprints(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    first_app = AiApp(app_root=tmp_path)
    second_app = AiApp(app_root=tmp_path)

    @first_app.markdown_agent(arg_name="agent", agent_name="order-fulfillment")
    async def first(agent: Agent[Any]) -> Agent[Any]:
        return agent

    @second_app.markdown_agent(arg_name="agent", agent_name="order-fulfillment")
    async def second(agent: Agent[Any]) -> Agent[Any]:
        return agent

    first_blueprint = bindings_module._runtime_for(first_app)._blueprints["order_fulfillment"]
    second_blueprint = bindings_module._runtime_for(second_app)._blueprints["order_fulfillment"]
    assert first_blueprint is not second_blueprint


def test_runtime_registry_does_not_retain_app_or_cached_runtime(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = AiApp(app_root=tmp_path)
    runtime = bindings_module._runtime_for(app, tmp_path)
    runtime.resolve("order-fulfillment")
    app_ref = weakref.ref(app)
    runtime_ref = weakref.ref(runtime)

    del app
    del runtime
    gc.collect()

    assert app_ref() is None
    assert runtime_ref() is None
    assert not hasattr(bindings_module, "DurableAiAgent")


def test_durable_orchestrators_share_one_hidden_agent_activity(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)
    received_contexts: list[DurableAgentContext] = []

    @app.orchestration_trigger(context_name="context")
    def first_orchestrator(context: DurableAgentContext) -> Any:
        received_contexts.append(context)
        return (yield context.call_agent("order-fulfillment", {"order": 42}))

    @app.orchestration_trigger(context_name="context")
    def second_orchestrator(context: DurableAgentContext) -> Any:
        return (yield context.call_agent("order-fulfillment", "plan"))

    functions = app.get_functions()
    names = [function.get_function_name() for function in functions]
    assert names.count(_INTERNAL_AGENT_ACTIVITY_NAME) == 1
    assert set(names) == {
        _INTERNAL_AGENT_ACTIVITY_NAME,
        "first_orchestrator",
        "second_orchestrator",
    }
    hidden = next(
        function
        for function in functions
        if function.get_function_name() == _INTERNAL_AGENT_ACTIVITY_NAME
    )
    assert any(
        binding.get_dict_repr().get("type") == "activityTrigger"
        for binding in hidden.get_bindings()
    )
    assert [
        function.get_function_name() for function in app.get_functions()
    ] == names

    first = next(
        function
        for function in functions
        if function.get_function_name() == "first_orchestrator"
    ).get_user_function()
    source = first.orchestrator_function
    context = MagicMock(spec=df.DurableOrchestrationContext)
    context.instance_id = "instance-42"
    context.call_activity.return_value = "agent-task"

    generator = source(context)
    assert next(generator) == "agent-task"
    with pytest.raises(StopIteration) as completed:
        generator.send("assessment")

    assert completed.value.value == "assessment"
    assert len(received_contexts) == 1
    assert isinstance(received_contexts[0], DurableAgentContext)
    context.call_activity.assert_called_once_with(
        _INTERNAL_AGENT_ACTIVITY_NAME,
        {
            "schema_version": 1,
            "agent_name": "order-fulfillment",
            "input": {"order": 42},
            "durable_instance_id": "instance-42",
        },
    )


def test_durable_orchestrator_preserves_supported_input_type(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    @app.orchestration_trigger(context_name="context", input_type=dict)
    def orchestrator(context: DurableAgentContext) -> Any:
        return (yield context.call_agent("order-fulfillment", context.get_input()))

    registered = next(
        function
        for function in app.get_functions()
        if function.get_function_name() == "orchestrator"
    ).get_user_function()

    assert registered._df_input_type is dict


@pytest.mark.asyncio
async def test_hidden_agent_activity_resolves_and_runs_statelessly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    @app.orchestration_trigger(context_name="context")
    def orchestrator(context: DurableAgentContext) -> Any:
        return (yield context.call_agent("order-fulfillment", "prompt"))

    run = AsyncMock(return_value=MagicMock(text="Agent result"))
    monkeypatch.setattr(bindings_module, "run_blueprint", run)
    hidden = next(
        function
        for function in app.get_functions()
        if function.get_function_name() == _INTERNAL_AGENT_ACTIVITY_NAME
    ).get_user_function()
    function_context = MagicMock(spec=func.Context)
    function_context.function_name = _INTERNAL_AGENT_ACTIVITY_NAME
    function_context.invocation_id = "invocation-1"

    result = await hidden(
        {
            "schema_version": 1,
            "agent_name": "order-fulfillment",
            "input": {"z": 2, "a": 1},
            "durable_instance_id": "instance-1",
        },
        function_context,
    )

    assert result == "Agent result"
    blueprint, prompt = run.await_args.args
    assert isinstance(blueprint, AgentBlueprint)
    assert blueprint.slug == "order_fulfillment"
    assert prompt == '{"a":1,"z":2}'
    assert run.await_args.kwargs["enable_persistent_history"] is False
    invocation = run.await_args.kwargs["invocation"]
    assert invocation.function_name == _INTERNAL_AGENT_ACTIVITY_NAME
    assert invocation.invocation_id == "invocation-1"
    assert invocation.durable_instance_id == "instance-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [object(), SimpleNamespace(text=None), SimpleNamespace(text=42)],
)
async def test_hidden_agent_activity_requires_text_response(
    response: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    @app.orchestration_trigger(context_name="context")
    def orchestrator(context: DurableAgentContext) -> Any:
        return (yield context.call_agent("order-fulfillment", "prompt"))

    monkeypatch.setattr(
        bindings_module,
        "run_blueprint",
        AsyncMock(return_value=response),
    )
    hidden = next(
        function
        for function in app.get_functions()
        if function.get_function_name() == _INTERNAL_AGENT_ACTIVITY_NAME
    ).get_user_function()

    with pytest.raises(TypeError, match=r"response\.text must be a string"):
        await hidden(
            {
                "schema_version": 1,
                "agent_name": "order-fulfillment",
                "input": "prompt",
                "durable_instance_id": "instance-1",
            },
            MagicMock(spec=func.Context),
        )


@pytest.mark.asyncio
async def test_hidden_agent_activity_rejects_unknown_dynamic_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    @app.orchestration_trigger(context_name="context")
    def orchestrator(context: DurableAgentContext) -> Any:
        return (yield context.call_agent("dynamic-name", "prompt"))

    run = AsyncMock()
    monkeypatch.setattr(bindings_module, "run_blueprint", run)
    hidden = next(
        function
        for function in app.get_functions()
        if function.get_function_name() == _INTERNAL_AGENT_ACTIVITY_NAME
    ).get_user_function()

    with pytest.raises(
        ValueError,
        match=r"Agent definition 'missing-agent' was not found.*filename stem.*slug",
    ):
        await hidden(
            {
                "schema_version": 1,
                "agent_name": "missing-agent",
                "input": "prompt",
                "durable_instance_id": "instance-1",
            },
            MagicMock(spec=func.Context),
        )

    run.assert_not_awaited()


def test_durable_orchestrator_rejects_non_generator(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    with pytest.raises(TypeError, match="synchronous generator"):

        @app.orchestration_trigger(context_name="context")
        def invalid_orchestrator(context: DurableAgentContext) -> str:
            return context.instance_id


def test_durable_orchestrator_rejects_keyword_only_context(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    with pytest.raises(TypeError, match="positional-or-keyword"):

        @app.orchestration_trigger(context_name="context")
        def invalid_orchestrator(*, context: DurableAgentContext) -> Any:
            return (yield context.call_agent("order-fulfillment", "prompt"))


def test_reserved_hidden_activity_name_collision_is_actionable(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    @app.activity_trigger(input_name="payload")
    def azure_functions_agents_run_markdown_agent(payload: str) -> str:
        return payload

    @app.orchestration_trigger(context_name="context")
    def orchestrator(context: DurableAgentContext) -> Any:
        return (yield context.call_agent("order-fulfillment", "prompt"))

    with pytest.raises(
        ValueError,
        match=r"reserved by DurableAiApp\.call_agent.*rename the conflicting",
    ):
        app.get_functions()
