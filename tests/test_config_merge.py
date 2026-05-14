from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.config.merge import (
    DEFAULT_TIMEOUT,
    _resolve_debug,
    _resolve_model,
    _resolve_sandbox,
    _resolve_timeout,
    apply_mcp_filter,
    apply_skills_filter,
    apply_tools_filter,
    compose,
)
from azure_functions_agents.config.schema import (
    AgentSpec,
    DebugConfig,
    ExecuteInSessionsConfig,
    GlobalConfig,
    McpFilter,
    SkillsFilter,
    SystemToolsAgentOverride,
    SystemToolsConfig,
    ToolsFilter,
    ToolsFromConnectionEntry,
    TriggerSpec,
)


def test_resolve_model_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAF_MODEL", "env-model")
    global_config = GlobalConfig(model="global-model")
    assert (
        _resolve_model(AgentSpec(name="A", description="B", model="agent-model"), global_config)
        == "agent-model"
    )
    assert _resolve_model(AgentSpec(name="A", description="B"), global_config) == "global-model"
    assert _resolve_model(AgentSpec(name="A", description="B"), GlobalConfig()) == "env-model"
    monkeypatch.delenv("MAF_MODEL", raising=False)
    assert _resolve_model(AgentSpec(name="A", description="B"), GlobalConfig()) is None


def test_resolve_timeout_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TIMEOUT", "33")
    global_config = GlobalConfig(timeout=22)
    assert _resolve_timeout(AgentSpec(name="A", description="B", timeout=11), global_config) == 11
    assert _resolve_timeout(AgentSpec(name="A", description="B"), global_config) == 22
    assert _resolve_timeout(AgentSpec(name="A", description="B"), GlobalConfig()) == 33
    monkeypatch.delenv("AGENT_TIMEOUT", raising=False)
    assert _resolve_timeout(AgentSpec(name="A", description="B"), GlobalConfig()) == DEFAULT_TIMEOUT


def test_resolve_debug() -> None:
    assert _resolve_debug(AgentSpec(name="A", description="B", is_main=True)) == DebugConfig(
        chat=True, http=True, mcp=True
    )
    assert _resolve_debug(AgentSpec(name="A", description="B", is_main=False)) == DebugConfig()
    assert _resolve_debug(AgentSpec(name="A", description="B", debug=True)) == DebugConfig(
        chat=True, http=True, mcp=True
    )
    assert _resolve_debug(
        AgentSpec(name="A", description="B", debug=DebugConfig(http=True))
    ) == DebugConfig(http=True)


def test_resolve_sandbox() -> None:
    global_config = GlobalConfig(
        system_tools=SystemToolsConfig(
            execute_in_sessions=ExecuteInSessionsConfig(
                session_pool_management_endpoint="https://example.test"
            )
        )
    )
    assert global_config.system_tools is not None
    assert (
        _resolve_sandbox(AgentSpec(name="A", description="B"), global_config)
        == global_config.system_tools.execute_in_sessions
    )
    assert (
        _resolve_sandbox(
            AgentSpec(
                name="A",
                description="B",
                system_tools=SystemToolsAgentOverride(execute_in_sessions=False),
            ),
            global_config,
        )
        is None
    )


def test_apply_mcp_filter() -> None:
    assert apply_mcp_filter(["a", "b"], False) == ([], True)
    assert apply_mcp_filter(["a", "b"], None) == (["a", "b"], False)
    assert apply_mcp_filter(["a", "b"], McpFilter(exclude=["b"])) == (["a"], False)


def test_apply_skills_filter() -> None:
    assert apply_skills_filter(["a", "b"], False) == ([], True)
    assert apply_skills_filter(["a", "b"], None) == (["a", "b"], False)
    assert apply_skills_filter(["a", "b"], SkillsFilter(exclude=["b"])) == (["a"], False)


def test_apply_tools_filter() -> None:
    global_filter = ToolsFilter(exclude=["a"], custom_only=False)
    assert apply_tools_filter(False, global_filter) == (ToolsFilter(), True)
    effective, disabled = apply_tools_filter(
        ToolsFilter(exclude=["b"], custom_only=True),
        global_filter,
    )
    assert disabled is False
    assert effective.exclude == ["a", "b"]
    assert effective.custom_only is True


def test_compose_end_to_end() -> None:
    global_config = GlobalConfig(
        mcp=["learn", "ado", "ghost"],
        model="global-model",
        timeout=10,
        tools=ToolsFilter(exclude=["danger"]),
        system_tools=SystemToolsConfig(
            execute_in_sessions=ExecuteInSessionsConfig(
                session_pool_management_endpoint="https://example.test"
            ),
            tools_from_connections=[ToolsFromConnectionEntry(connection_id="conn-1")],
        ),
    )
    spec = AgentSpec(
        name="Agent",
        description="desc",
        is_main=False,
        trigger=TriggerSpec(type="timer_trigger", args={"schedule": "0 0 * * * *"}),
        mcp=McpFilter(exclude=["ado"]),
        skills=SkillsFilter(exclude=["secret"]),
        tools=ToolsFilter(exclude=["foo"], custom_only=True),
        metadata={"team": "x"},
        instructions="Do work",
        source_file=str(Path(r"Q:\agent.agent.md")),
    )

    resolved = compose(
        spec,
        global_config,
        discovered_mcp_names=["learn", "ado"],
        discovered_skill_names=["secret", "public"],
    )

    assert global_config.system_tools is not None
    assert resolved.model == "global-model"
    assert resolved.timeout == 10
    assert resolved.enabled_mcp_names == ["learn"]
    assert resolved.enabled_skills_names == ["public"]
    assert resolved.tool_filter.exclude == ["danger", "foo"]
    assert resolved.tool_filter.custom_only is True
    assert resolved.sandbox_config == global_config.system_tools.execute_in_sessions
    assert resolved.connector_specs == global_config.system_tools.tools_from_connections
    assert resolved.metadata == {"team": "x"}
