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


def test_compose_copies_logger_into_metadata() -> None:
    resolved = compose(
        AgentSpec(name="Agent", description="desc", logger=False, is_main=True),
        GlobalConfig(),
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )

    assert resolved.metadata["logger"] is False


def test_compose_preserves_substitute_variables_flag() -> None:
    resolved = compose(
        AgentSpec(name="Agent", description="desc", substitute_variables=False, is_main=True),
        GlobalConfig(),
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )

    assert resolved.substitute_variables is False


def test_resolve_debug_explicit_false() -> None:
    """Defensive: explicit debug: false returns an all-disabled DebugConfig (overrides the
    is_main default-true behavior)."""
    spec = AgentSpec(name="Main", description="d", debug=False, is_main=True)
    debug = _resolve_debug(spec)
    assert debug.chat is False
    assert debug.http is False
    assert debug.mcp is False


def test_resolve_timeout_garbage_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: a non-numeric AGENT_TIMEOUT env var must NOT crash; falls through to the
    framework default."""
    monkeypatch.setenv("AGENT_TIMEOUT", "not-a-number")
    spec = AgentSpec(name="A", description="d")
    global_config = GlobalConfig()
    assert _resolve_timeout(spec, global_config) == DEFAULT_TIMEOUT


def test_resolve_sandbox_no_global_returns_none() -> None:
    """Defensive: when the global config has no system_tools block, sandbox is None."""
    spec = AgentSpec(name="A", description="d")
    assert _resolve_sandbox(spec, GlobalConfig()) is None


def test_resolve_connectors_no_global_returns_empty() -> None:
    """Defensive: when the global config has no system_tools block, connectors list is empty."""
    from azure_functions_agents.config.merge import _resolve_connectors

    assert _resolve_connectors(GlobalConfig()) == []


def test_apply_tools_filter_inherits_global_when_agent_unset() -> None:
    """Defensive: when an agent doesn't specify tools, it inherits the global filter as-is."""
    global_filter = ToolsFilter(exclude=["bash"], custom_only=False)
    effective, disabled = apply_tools_filter(None, global_filter)
    assert disabled is False
    assert effective.exclude == ["bash"]
    assert effective.custom_only is False
    # Returned object is a deep copy — mutating it must not affect the caller's filter
    effective.exclude.append("new")
    assert global_filter.exclude == ["bash"]


def test_apply_tools_filter_true_inherits_global() -> None:
    """`tools: true` shorthand also inherits the global filter."""
    global_filter = ToolsFilter(exclude=["bash"])
    effective, disabled = apply_tools_filter(True, global_filter)
    assert disabled is False
    assert effective.exclude == ["bash"]


def test_apply_tools_filter_no_global_no_agent_returns_empty_filter() -> None:
    """Defensive: when neither agent nor global declares a tools filter, an empty (allow-all)
    filter is returned."""
    effective, disabled = apply_tools_filter(None, None)
    assert disabled is False
    assert effective.exclude == []
    assert effective.custom_only is False
