from __future__ import annotations

import asyncio
import gc
import inspect
import weakref
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import azure.functions as func
import pytest
from agent_framework import Agent

from azure_functions_agents import AiApp as ExportedAiApp
from azure_functions_agents import DurableAiApp as ExportedDurableAiApp
from azure_functions_agents import agent as exported_agent
from azure_functions_agents import bindings as bindings_module
from azure_functions_agents.bindings import (
    AiApp,
    DurableAiApp,
    agent,
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

    @app.agent(arg_name="order_agent", agent_name="order-fulfillment")
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

    @app.agent(arg_name="agent", agent_name="order-fulfillment")
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

    @agent(app, arg_name="agent", agent_name="order_fulfillment")
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

        @agent(app, arg_name="agent", agent_name="order_fulfillment")
        def handler(value: str, agent: Agent[Any]) -> str:
            return value


def test_reverse_decorator_order_is_rejected(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    app = AiApp(app_root=tmp_path)

    with pytest.raises(TypeError, match="innermost decorator"):

        @app.agent(arg_name="agent", agent_name="order-fulfillment")
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

        @app.agent(arg_name="agent", agent_name="order-fulfillment")
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

    @app.agent(
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

        @app.agent(
            arg_name="activity_agent",
            agent_name="order-fulfillment",
        )
        def activity(payload: str, activity_agent: Agent[Any]) -> str:
            return payload


def test_binding_app_types_are_exported() -> None:
    assert ExportedAiApp is AiApp
    assert ExportedDurableAiApp is DurableAiApp
    assert exported_agent is agent
    assert not hasattr(bindings_module, "agent_input")
    assert "mode" not in inspect.signature(agent).parameters
    assert "mode" not in inspect.signature(AiApp.agent).parameters
    assert "mode" not in inspect.signature(DurableAiApp.agent).parameters


def test_app_instances_own_separate_agent_blueprints(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    first_app = AiApp(app_root=tmp_path)
    second_app = AiApp(app_root=tmp_path)

    @first_app.agent(arg_name="agent", agent_name="order-fulfillment")
    async def first(agent: Agent[Any]) -> Agent[Any]:
        return agent

    @second_app.agent(arg_name="agent", agent_name="order-fulfillment")
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
