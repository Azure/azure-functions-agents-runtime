from __future__ import annotations

import logging
from pathlib import Path

import pytest

from azure_functions_agents.client_manager.providers import AzureOpenAIConfig, OpenAIConfig
from azure_functions_agents.config.merge import (
    _resolve_debug,
    _resolve_sandbox,
    apply_mcp_filter,
    apply_skills_filter,
    apply_tools_filter,
    compose,
)
from azure_functions_agents.config.schema import (
    AgentConfiguration,
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
from azure_functions_agents.config.validation import validate_resolved_agent


def _openai_agent_configuration(**overrides: object) -> AgentConfiguration:
    payload: dict[str, object] = {
        "provider": "openai",
        "timeout": 900,
        "temperature": 0.4,
        "top_p": 0.9,
        "max_tokens": 512,
        "openai": {
            "model": "gpt-4o",
            "base_url": "https://openai.example.test",
            "organization": "global-org",
        },
    }
    payload.update(overrides)
    return AgentConfiguration.model_validate(payload)


def _azure_agent_configuration(**overrides: object) -> AgentConfiguration:
    payload: dict[str, object] = {
        "provider": "azure_openai",
        "timeout": 120,
        "azure_openai": {
            "model": "gpt-4o-mini",
            "azure_endpoint": "https://azure-openai.example.test",
            "api_version": "2024-10-21",
        },
    }
    payload.update(overrides)
    return AgentConfiguration.model_validate(payload)


def _global_config(**overrides: object) -> GlobalConfig:
    payload: dict[str, object] = {
        "agent_configuration": _openai_agent_configuration(),
    }
    payload.update(overrides)
    return GlobalConfig.model_validate(payload)


def test_compose_uses_agent_only_agent_configuration() -> None:
    resolved = compose(
        AgentSpec(
            name="Agent",
            description="desc",
            is_main=True,
            agent_configuration=_openai_agent_configuration(timeout=60),
        ),
        GlobalConfig(),
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )

    assert resolved.agent_configuration.provider == "openai"
    assert resolved.agent_configuration.timeout == 60
    assert resolved.agent_configuration.openai == OpenAIConfig(
        model="gpt-4o",
        base_url="https://openai.example.test",
        organization="global-org",
    )


def test_compose_uses_global_only_agent_configuration() -> None:
    global_config = _global_config()

    resolved = compose(
        AgentSpec(name="Agent", description="desc", is_main=True),
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )

    assert resolved.agent_configuration == global_config.agent_configuration


def test_compose_agent_universal_knobs_override_global_values() -> None:
    resolved = compose(
        AgentSpec(
            name="Agent",
            description="desc",
            is_main=True,
            agent_configuration=_openai_agent_configuration(
                timeout=60,
                temperature=0.1,
                top_p=0.8,
                max_tokens=128,
                openai={"model": "gpt-4.1"},
            ),
        ),
        _global_config(),
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )

    assert resolved.agent_configuration.timeout == 60
    assert resolved.agent_configuration.temperature == 0.1
    assert resolved.agent_configuration.top_p == 0.8
    assert resolved.agent_configuration.max_tokens == 128
    assert resolved.agent_configuration.openai == OpenAIConfig(
        model="gpt-4.1",
        base_url="https://openai.example.test",
        organization="global-org",
    )


def test_compose_shallow_merges_same_provider_sub_block_per_key() -> None:
    global_config = _global_config(
        agent_configuration=_openai_agent_configuration(
            openai={
                "model": "gpt-4o",
                "base_url": "https://global.example.test",
                "organization": "global-org",
                "project": "global-project",
            }
        )
    )
    spec = AgentSpec(
        name="Agent",
        description="desc",
        is_main=True,
        agent_configuration=_openai_agent_configuration(
            timeout=30,
            openai={
                "model": "gpt-4.1",
                "base_url": "https://agent.example.test",
                "region": "westus3",
            },
        ),
    )

    resolved = compose(spec, global_config, discovered_mcp_names=[], discovered_skill_names=[])

    assert resolved.agent_configuration.timeout == 30
    assert resolved.agent_configuration.openai == OpenAIConfig(
        model="gpt-4.1",
        base_url="https://agent.example.test",
        organization="global-org",
        project="global-project",
        region="westus3",
    )


def test_compose_cross_provider_override_drops_global_provider_block(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        resolved = compose(
            AgentSpec(
                name="Agent",
                description="desc",
                is_main=True,
                agent_configuration=_azure_agent_configuration(
                    azure_openai={
                        "model": "gpt-4.1",
                        "azure_endpoint": "https://agent-azure.example.test",
                        "api_version": "2024-10-21",
                        "audience": "agents",
                    }
                ),
            ),
            _global_config(),
            discovered_mcp_names=[],
            discovered_skill_names=[],
        )

    assert resolved.agent_configuration.provider == "azure_openai"
    assert resolved.agent_configuration.openai is None
    assert resolved.agent_configuration.azure_openai == AzureOpenAIConfig(
        model="gpt-4.1",
        azure_endpoint="https://agent-azure.example.test",
        api_version="2024-10-21",
        audience="agents",
    )
    assert "dropping the global provider sub-block during merge" in caplog.text


def test_compose_requires_agent_configuration_anywhere() -> None:
    with pytest.raises(ValueError, match="must declare agent_configuration either at the global level"):
        compose(
            AgentSpec(name="Agent", description="desc", is_main=True),
            GlobalConfig(),
            discovered_mcp_names=[],
            discovered_skill_names=[],
        )


def test_removed_non_secret_env_vars_do_not_influence_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAF_MODEL", "env-model")
    monkeypatch.setenv("AGENT_TIMEOUT", "33")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env-openai.example.test")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://env-azure.example.test")

    resolved = compose(
        AgentSpec(name="Agent", description="desc", is_main=True),
        _global_config(
            agent_configuration=_openai_agent_configuration(
                timeout=15,
                openai={
                    "model": "config-model",
                    "base_url": "https://config-openai.example.test",
                },
            )
        ),
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )

    assert resolved.agent_configuration.timeout == 15
    assert resolved.agent_configuration.openai == OpenAIConfig(
        model="config-model",
        base_url="https://config-openai.example.test",
    )


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
    global_config = _global_config(
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
    global_config = _global_config(
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
    assert resolved.agent_configuration == global_config.agent_configuration
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
        _global_config(),
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )

    assert resolved.metadata["logger"] is False


def test_compose_preserves_substitute_variables_flag() -> None:
    resolved = compose(
        AgentSpec(name="Agent", description="desc", substitute_variables=False, is_main=True),
        _global_config(),
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )

    assert resolved.substitute_variables is False


def test_compose_defers_warning_only_validation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = AgentSpec(
        name="Agent",
        description="desc",
        is_main=True,
        skills=SkillsFilter(exclude=["missing-skill"]),
        tools=ToolsFilter(exclude=["bash"]),
    )

    with caplog.at_level(logging.WARNING):
        resolved = compose(
            spec,
            _global_config(),
            discovered_mcp_names=[],
            discovered_skill_names=["known-skill"],
        )

    assert caplog.records == []

    with caplog.at_level(logging.WARNING):
        validate_resolved_agent(
            resolved,
            discovered_mcp_names=[],
            discovered_skills=["known-skill"],
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("skills.exclude" in message for message in messages)
    assert any("tools.exclude" in message for message in messages)


def test_resolve_debug_explicit_false() -> None:
    spec = AgentSpec(name="Main", description="d", debug=False, is_main=True)
    debug = _resolve_debug(spec)
    assert debug.chat is False
    assert debug.http is False
    assert debug.mcp is False


def test_resolve_sandbox_no_global_returns_none() -> None:
    spec = AgentSpec(name="A", description="d")
    assert _resolve_sandbox(spec, _global_config(system_tools=None)) is None


def test_resolve_connectors_no_global_returns_empty() -> None:
    from azure_functions_agents.config.merge import _resolve_connectors

    assert _resolve_connectors(_global_config(system_tools=None)) == []


def test_apply_tools_filter_inherits_global_when_agent_unset() -> None:
    global_filter = ToolsFilter(exclude=["bash"], custom_only=False)
    effective, disabled = apply_tools_filter(None, global_filter)
    assert disabled is False
    assert effective.exclude == ["bash"]
    assert effective.custom_only is False
    effective.exclude.append("new")
    assert global_filter.exclude == ["bash"]


def test_apply_tools_filter_true_inherits_global() -> None:
    global_filter = ToolsFilter(exclude=["bash"])
    effective, disabled = apply_tools_filter(True, global_filter)
    assert disabled is False
    assert effective.exclude == ["bash"]


def test_apply_tools_filter_no_global_no_agent_returns_empty_filter() -> None:
    effective, disabled = apply_tools_filter(None, None)
    assert disabled is False
    assert effective.exclude == []
    assert effective.custom_only is False


def test_compose_enables_all_discovered_mcp_when_no_per_agent_filter() -> None:
    resolved = compose(
        AgentSpec(name="Agent", description="desc", is_main=True),
        _global_config(),
        discovered_mcp_names=["a", "b"],
        discovered_skill_names=[],
    )

    assert resolved.enabled_mcp_names == ["a", "b"]


def test_compose_disables_mcp_when_agent_sets_mcp_false() -> None:
    resolved = compose(
        AgentSpec(name="Agent", description="desc", is_main=True, mcp=False),
        _global_config(),
        discovered_mcp_names=["a", "b"],
        discovered_skill_names=[],
    )

    assert resolved.enabled_mcp_names == []
    assert resolved.mcp_disabled is True
