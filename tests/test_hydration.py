from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import azure_functions_agents.hydration as hydration
from azure_functions_agents.composition import (
    BindingAgentDefinition,
    BindingAgentEntry,
    DiscoveryInventory,
)
from azure_functions_agents.config.schema import GlobalConfig
from azure_functions_agents.hydration import (
    AgentBlueprint,
    open_agent,
    run_blueprint,
)


def _entry(
    tmp_path: Path,
    slug: str = "main",
    *,
    timeout: float | None = None,
) -> BindingAgentEntry:
    definition = BindingAgentDefinition(
        name="Main",
        description="Main agent",
        instructions="Be useful.",
        source_file=tmp_path / f"{slug}.agent.md",
        filename_stem=slug,
        slug=slug,
    )
    discovery = DiscoveryInventory((), (), (), (), ())
    return BindingAgentEntry(definition, GlobalConfig(timeout=timeout), discovery)


class _FakeAgent:
    active_total = 0
    max_active_total = 0

    def __init__(self, *, delay: float = 0.01) -> None:
        self.delay = delay
        self.enter_count = 0
        self.exit_count = 0
        self.exit_args: tuple[Any, ...] | None = None
        self.sessions: list[Any] = []

    async def __aenter__(self) -> _FakeAgent:
        self.enter_count += 1
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.exit_count += 1
        self.exit_args = args

    async def run(self, _messages: Any, **kwargs: Any) -> Any:
        self.sessions.append(kwargs["session"])
        type(self).active_total += 1
        type(self).max_active_total = max(
            type(self).max_active_total,
            type(self).active_total,
        )
        try:
            await asyncio.sleep(self.delay)
        finally:
            type(self).active_total -= 1
        return SimpleNamespace(text="done")


class _EnteredAgent(_FakeAgent):
    def __init__(self, entered: _FakeAgent) -> None:
        super().__init__()
        self.entered = entered

    async def __aenter__(self) -> _FakeAgent:
        self.enter_count += 1
        return self.entered


class _FailingEnterAgent(_FakeAgent):
    async def __aenter__(self) -> _FakeAgent:
        self.enter_count += 1
        raise RuntimeError("enter failed")


@pytest.fixture(autouse=True)
def reset_fake_concurrency() -> None:
    _FakeAgent.active_total = 0
    _FakeAgent.max_active_total = 0


def test_blueprint_build_materializes_fresh_mutable_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clients: list[object] = []
    mcp_tools: list[object] = []
    histories: list[object] = []
    builds: list[dict[str, Any]] = []

    class Manager:
        def build_chat_client_with_target(self, _model: str | None) -> tuple[object, object]:
            client = object()
            clients.append(client)
            return client, object()

    class MCPDefinition:
        def build_tool(self) -> object:
            tool = object()
            mcp_tools.append(tool)
            return tool

    def build_history() -> object:
        history = object()
        histories.append(history)
        return history

    def build_role_agent(client: object, **kwargs: Any) -> _FakeAgent:
        builds.append({"client": client, **kwargs})
        return _FakeAgent()

    definition = BindingAgentDefinition(
        name="Main",
        description="Main agent",
        instructions="Be useful.",
        source_file=tmp_path / "main.agent.md",
        filename_stem="main",
        slug="main",
    )
    discovery = DiscoveryInventory(
        (SimpleNamespace(name="project_tool"),),
        (),
        (("skill", tmp_path / "skills" / "skill"),),
        (("server", MCPDefinition()),),  # type: ignore[arg-type]
        (),
    )
    blueprint = AgentBlueprint(BindingAgentEntry(definition, GlobalConfig(), discovery))
    monkeypatch.setattr(hydration, "get_client_manager", lambda: Manager())
    monkeypatch.setattr(hydration, "_build_history_provider", build_history)
    monkeypatch.setattr(hydration, "_build_role_agent", build_role_agent)
    monkeypatch.setattr(
        hydration,
        "create_web_request_tools",
        lambda _config: [object()],
    )

    first = blueprint.build()
    second = blueprint.build()

    assert first is not second
    assert len(clients) == len(mcp_tools) == len(histories) == len(builds) == 2
    assert builds[0]["client"] is not builds[1]["client"]
    assert builds[0]["tools"] is not builds[1]["tools"]
    assert builds[0]["mcp_tools"][0] is not builds[1]["mcp_tools"][0]
    assert builds[0]["web_request_tools"][0] is not builds[1]["web_request_tools"][0]
    assert builds[0]["history_provider"] is not builds[1]["history_provider"]


@pytest.mark.asyncio
async def test_run_blueprint_builds_fresh_agents_for_concurrent_invocations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agents: list[_FakeAgent] = []

    def build(
        _blueprint: AgentBlueprint,
        _invocation: Any = None,
    ) -> _FakeAgent:
        agent = _FakeAgent()
        agents.append(agent)
        return agent

    monkeypatch.setattr(AgentBlueprint, "build", build)
    blueprint = AgentBlueprint(_entry(tmp_path))

    first, second = await asyncio.gather(
        run_blueprint(blueprint, "one"),
        run_blueprint(blueprint, "two"),
    )

    assert first.text == second.text == "done"
    assert len(agents) == 2
    assert all(agent.enter_count == agent.exit_count == 1 for agent in agents)
    assert agents[0].sessions[0] is not agents[1].sessions[0]
    assert agents[0].sessions[0].session_id != agents[1].sessions[0].session_id
    assert _FakeAgent.max_active_total == 2


@pytest.mark.asyncio
async def test_open_agent_exits_context_owner_not_entered_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entered = _FakeAgent()
    owner = _EnteredAgent(entered)
    monkeypatch.setattr(AgentBlueprint, "build", lambda *_args: owner)

    async with open_agent(AgentBlueprint(_entry(tmp_path))) as agent:
        assert agent is entered

    assert owner.exit_count == 1
    assert entered.exit_count == 0


@pytest.mark.asyncio
async def test_open_agent_closes_owner_with_handler_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = _FakeAgent()
    monkeypatch.setattr(AgentBlueprint, "build", lambda *_args: owner)

    with pytest.raises(ValueError, match="handler failed"):
        async with open_agent(AgentBlueprint(_entry(tmp_path))):
            raise ValueError("handler failed")

    assert owner.exit_count == 1
    assert owner.exit_args is not None
    assert owner.exit_args[0] is ValueError


@pytest.mark.asyncio
async def test_failed_enter_rolls_back_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = _FailingEnterAgent()
    monkeypatch.setattr(AgentBlueprint, "build", lambda *_args: owner)

    with pytest.raises(RuntimeError, match="enter failed"):
        async with open_agent(AgentBlueprint(_entry(tmp_path))):
            pass

    assert owner.exit_count == 1


@pytest.mark.asyncio
async def test_run_blueprint_enforces_managed_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = _FakeAgent(delay=0.05)
    monkeypatch.setattr(AgentBlueprint, "build", lambda *_args: owner)

    with pytest.raises(RuntimeError, match=r"timed out after 0\.01s"):
        await run_blueprint(AgentBlueprint(_entry(tmp_path, timeout=0.01)), "hello")

    assert owner.exit_count == 1

