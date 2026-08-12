from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, get_type_hints
from unittest.mock import AsyncMock, MagicMock, Mock

import azure.durable_functions as df
import azure.functions as func
import pytest
from agent_framework import Agent

from azure_functions_agents import AiApp as ExportedAiApp
from azure_functions_agents import DurableAiApp as ExportedDurableAiApp
from azure_functions_agents import bindings as bindings_module
from azure_functions_agents.bindings import (
    AiApp,
    DurableAiAgent,
    DurableAiApp,
    agent_input,
)
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

    @app.agent_input(arg_name="order_agent", agent_name="order-fulfillment")
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

    @app.agent_input(arg_name="agent", agent_name="order-fulfillment")
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
    agent = _agent_double()

    from azure_functions_agents.config.paths import set_app_root

    set_app_root(tmp_path)
    monkeypatch.setattr(AgentBlueprint, "build", lambda *_args: agent)

    @agent_input(app, arg_name="agent", agent_name="order_fulfillment")
    async def handler(value: str, agent: Agent[Any]) -> tuple[str, Agent[Any]]:
        return value, agent

    value, injected = await handler("ok")
    assert value == "ok"
    assert injected is agent
    assert list(inspect.signature(handler).parameters) == ["value"]
    agent.__aexit__.assert_awaited_once()


def test_free_decorator_rejects_sync_handler(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = func.FunctionApp()

    from azure_functions_agents.config.paths import set_app_root

    set_app_root(tmp_path)

    with pytest.raises(TypeError, match=r"requires an async def handler"):

        @agent_input(app, arg_name="agent", agent_name="order_fulfillment")
        def handler(value: str, agent: Agent[Any]) -> str:
            return value


def test_reverse_decorator_order_is_rejected(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = AiApp(app_root=tmp_path)

    with pytest.raises(TypeError, match="innermost decorator"):

        @app.agent_input(arg_name="agent", agent_name="order-fulfillment")
        @app.route(route="wrong")
        async def wrong_order(req: func.HttpRequest, agent: Agent[Any]) -> str:
            return req.method


def test_orchestrator_facade_schedules_json_activity_once(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    @app.agent_input(
        arg_name="planner",
        agent_name="order-fulfillment",
        mode="orchestrator",
    )
    def orchestrator(
        context: df.DurableOrchestrationContext,
        planner: DurableAiAgent,
    ):
        result = yield planner.run({"order": 42})
        return result

    app.orchestration_trigger(context_name="context")(orchestrator)
    context = Mock(spec=df.DurableOrchestrationContext)
    context.instance_id = "instance-1"
    context.call_activity.return_value = object()
    generator = orchestrator(context)
    task = next(generator)
    assert task is context.call_activity.return_value
    context.call_activity.assert_called_once_with(
        "_afa_agent_binding_run",
        {
            "agent_slug": "order_fulfillment",
            "messages": {"order": 42},
            "options": None,
            "instance_id": "instance-1",
        },
    )

    functions = app.get_functions()
    activity_bindings = [
        binding.get_dict_repr()
        for function in functions
        for binding in function.get_bindings()
        if binding.get_dict_repr().get("type") == "activityTrigger"
    ]
    assert len(activity_bindings) == 1
    assert activity_bindings[0]["activity"] == "_afa_agent_binding_run"
    internal_activity = next(
        function
        for function in functions
        if function.get_function_name() == "_afa_agent_binding_run"
    ).get_user_function()
    assert get_type_hints(internal_activity)["payload"] is dict


def test_orchestrator_proxy_rejects_streaming_and_non_json_input(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)
    captured: list[DurableAiAgent] = []

    @app.agent_input(
        arg_name="planner",
        agent_name="order-fulfillment",
        mode="orchestrator",
    )
    def orchestrator(
        context: df.DurableOrchestrationContext,
        planner: DurableAiAgent,
    ):
        captured.append(planner)
        yield None

    context = Mock(spec=df.DurableOrchestrationContext)
    context.instance_id = "instance-1"
    next(orchestrator(context))

    with pytest.raises(ValueError, match="does not support streaming"):
        captured[0].run("hello", stream=True)
    with pytest.raises(ValueError, match="JSON-serializable"):
        captured[0].run(object())
    context.call_activity.assert_not_called()


@pytest.mark.asyncio
async def test_durable_activity_injects_raw_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)
    agent = _agent_double()
    monkeypatch.setattr(AgentBlueprint, "build", lambda *_args: agent)

    @app.agent_input(
        arg_name="agent",
        agent_name="order-fulfillment",
        mode="activity",
    )
    async def activity(payload: str, agent: Agent[Any]) -> tuple[str, Agent[Any]]:
        return payload, agent

    payload, injected = await activity("work")
    assert payload == "work"
    assert isinstance(injected, Agent)
    agent.__aexit__.assert_awaited_once()


def test_sync_activity_and_entity_mode_are_rejected(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    with pytest.raises(TypeError, match=r"requires an async def handler"):

        @app.agent_input(
            arg_name="activity_agent",
            agent_name="order-fulfillment",
            mode="activity",
        )
        def activity(payload: str, activity_agent: Agent[Any]) -> str:
            return payload

    with pytest.raises(ValueError, match=r"'function', 'activity', or 'orchestrator'"):
        app.agent_input(
            arg_name="entity_agent",
            agent_name="order-fulfillment",
            mode="entity",  # type: ignore[arg-type]
        )


def test_multiple_orchestrators_register_one_internal_activity(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    def first(context: df.DurableOrchestrationContext, agent: DurableAiAgent):
        yield agent.run("first")

    def second(context: df.DurableOrchestrationContext, agent: DurableAiAgent):
        yield agent.run("second")

    app.agent_input(
        arg_name="agent",
        agent_name="order-fulfillment",
        mode="orchestrator",
    )(first)
    app.agent_input(
        arg_name="agent",
        agent_name="order-fulfillment",
        mode="orchestrator",
    )(second)

    activity_bindings = [
        binding.get_dict_repr()
        for function in app.get_functions()
        for binding in function.get_bindings()
        if binding.get_dict_repr().get("type") == "activityTrigger"
    ]
    assert len(activity_bindings) == 1


def test_durable_modes_require_df_app(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = AiApp(app_root=tmp_path)

    with pytest.raises(TypeError, match="require DurableAiApp"):
        app.agent_input(
            arg_name="agent",
            agent_name="order-fulfillment",
            mode="activity",
        )


def test_binding_app_types_are_exported() -> None:
    assert ExportedAiApp is AiApp
    assert ExportedDurableAiApp is DurableAiApp


def test_app_instances_own_separate_agent_blueprints(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    first_app = AiApp(app_root=tmp_path)
    second_app = AiApp(app_root=tmp_path)

    @first_app.agent_input(arg_name="agent", agent_name="order-fulfillment")
    async def first(agent: Agent[Any]) -> Agent[Any]:
        return agent

    @second_app.agent_input(arg_name="agent", agent_name="order-fulfillment")
    async def second(agent: Agent[Any]) -> Agent[Any]:
        return agent

    first_blueprint = bindings_module._runtime_for(first_app)._blueprints["order_fulfillment"]
    second_blueprint = bindings_module._runtime_for(second_app)._blueprints["order_fulfillment"]
    assert first_blueprint is not second_blueprint


def test_concurrent_orchestrator_decorators_register_one_activity(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = DurableAiApp(app_root=tmp_path)

    def decorate(index: int) -> None:
        def orchestrator(
            context: df.DurableOrchestrationContext,
            agent: DurableAiAgent,
        ):
            yield agent.run(str(index))

        app.agent_input(
            arg_name="agent",
            agent_name="order-fulfillment",
            mode="orchestrator",
        )(orchestrator)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(decorate, range(8)))

    activity_bindings = [
        binding.get_dict_repr()
        for function in app.get_functions()
        for binding in function.get_bindings()
        if binding.get_dict_repr().get("type") == "activityTrigger"
    ]
    assert len(activity_bindings) == 1