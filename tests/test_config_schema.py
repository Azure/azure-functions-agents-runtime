from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from azure_functions_agents.config.schema import (
    AgentConfiguration,
    AgentSpec,
    DebugConfig,
    GlobalConfig,
    McpFilter,
    SystemToolsConfig,
    ToolsFilter,
    TriggerSpec,
)


def test_agent_spec_constructs() -> None:
    spec = AgentSpec(name="X", description="Y")
    assert spec.name == "X"


def test_agent_spec_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({"name": "X", "description": "Y", "extra_field": 1})


@pytest.mark.parametrize(
    "value",
    [True, False, None, DebugConfig(http=True)],
)
def test_agent_spec_debug_variants(value: bool | None | DebugConfig) -> None:
    spec = AgentSpec(name="X", description="Y", debug=value)
    assert spec.debug == value


@pytest.mark.parametrize(
    "value",
    [False, None, McpFilter(exclude=["x"])],
)
def test_agent_spec_mcp_variants(value: bool | None | McpFilter) -> None:
    spec = AgentSpec(name="X", description="Y", mcp=value)
    assert spec.mcp == value


@pytest.mark.parametrize(
    "value",
    [False, None, ToolsFilter(exclude=["x"])],
)
def test_agent_spec_tools_variants(value: bool | None | ToolsFilter) -> None:
    spec = AgentSpec(name="X", description="Y", tools=value)
    assert spec.tools == value


def test_agent_spec_accepts_logger_field() -> None:
    spec = AgentSpec.model_validate({"name": "X", "description": "Y", "logger": True})
    assert spec.logger is True


def test_trigger_spec_validates() -> None:
    trigger = TriggerSpec(type="timer_trigger", args={"schedule": "0 0 * * * *"})
    assert trigger.type == "timer_trigger"


def test_trigger_spec_rejects_empty_type() -> None:
    with pytest.raises(ValidationError):
        TriggerSpec(type="")


def test_global_config_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        GlobalConfig.model_validate({"extra_field": 1})


def test_system_tools_config_parses() -> None:
    payload: dict[str, Any] = {
        "execute_in_sessions": {"session_pool_management_endpoint": "https://example.test"},
        "tools_from_connections": [{"connection_id": "conn-1", "prefix": "o365"}],
    }
    config = SystemToolsConfig.model_validate(payload)
    assert config.execute_in_sessions is not None
    assert config.tools_from_connections[0].connection_id == "conn-1"


def test_agent_configuration_accepts_top_level_model_only() -> None:
    config = AgentConfiguration.model_validate(
        {
            "provider": "openai",
            "model": "gpt-4o",
            "openai": {},
        }
    )

    assert config.model == "gpt-4o"
    assert config.openai is not None
    assert config.openai.model is None


@pytest.mark.parametrize(
    ("provider", "provider_block"),
    [
        ("openai", {"model": "gpt-4o"}),
        (
            "azure_openai",
            {
                "model": "gpt-4o",
                "azure_endpoint": "https://azure-openai.example.test",
                "api_version": "2024-10-21",
            },
        ),
        (
            "foundry",
            {
                "model": "gpt-4o",
                "project_endpoint": "https://foundry.example.test",
            },
        ),
    ],
)
def test_agent_configuration_accepts_subblock_model_only(
    provider: str, provider_block: dict[str, str]
) -> None:
    config = AgentConfiguration.model_validate(
        {
            "provider": provider,
            provider: provider_block,
        }
    )

    assert config.provider == provider
    assert config.model is None
    assert config.provider_config.model == "gpt-4o"


def test_agent_configuration_accepts_both_models_set() -> None:
    config = AgentConfiguration.model_validate(
        {
            "provider": "openai",
            "model": "gpt-4o",
            "openai": {"model": "gpt-4o-mini"},
        }
    )

    assert config.model == "gpt-4o"
    assert config.openai is not None
    assert config.openai.model == "gpt-4o-mini"


def test_agent_configuration_rejects_when_no_model_anywhere() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AgentConfiguration.model_validate(
            {
                "provider": "openai",
                "openai": {},
            }
        )

    message = str(exc_info.value)
    assert "agent_configuration.model" in message
    assert "agent_configuration.openai.model" in message


def test_empty_string_model_normalized_to_none_top_level() -> None:
    config = AgentConfiguration.model_validate(
        {
            "provider": "openai",
            "model": "",
            "openai": {"model": "gpt-4o"},
        }
    )

    assert config.model is None
    assert config.openai is not None
    assert config.openai.model == "gpt-4o"


def test_empty_string_model_normalized_to_none_subblock() -> None:
    config = AgentConfiguration.model_validate(
        {
            "provider": "openai",
            "model": "gpt-4o",
            "openai": {"model": ""},
        }
    )

    assert config.model == "gpt-4o"
    assert config.openai is not None
    assert config.openai.model is None
    assert "model" not in config.openai.model_dump(exclude_none=True)


def test_all_empty_strings_for_model_fails_validation() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AgentConfiguration.model_validate(
            {
                "provider": "openai",
                "model": "   ",
                "openai": {"model": ""},
            }
        )

    message = str(exc_info.value)
    assert "agent_configuration.model" in message
    assert "agent_configuration.openai.model" in message


def test_agent_spec_accepts_dict_for_agent_configuration() -> None:
    spec = AgentSpec(name="X", description="Y", agent_configuration={"model": "x"})

    assert spec.agent_configuration == {"model": "x"}
    assert isinstance(spec.agent_configuration, dict)


def test_agent_configuration_rejects_unknown_provider() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AgentConfiguration.model_validate(
            {
                "provider": "bogus",
                "openai": {"model": "gpt-4o"},
            }
        )

    message = str(exc_info.value)
    assert "Unknown provider" in message
    assert "'bogus'" in message


def test_agent_configuration_rejects_multiple_provider_sub_blocks() -> None:
    with pytest.raises(
        ValidationError,
        match="Only the sub-block matching the declared provider is permitted",
    ):
        AgentConfiguration.model_validate(
            {
                "provider": "openai",
                "openai": {"model": "gpt-4o"},
                "azure_openai": {
                    "model": "gpt-4o",
                    "azure_endpoint": "https://azure-openai.example.test",
                    "api_version": "2024-10-21",
                },
            }
        )


def test_agent_configuration_rejects_mismatched_provider_sub_block() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AgentConfiguration.model_validate(
            {
                "provider": "openai",
                "azure_openai": {
                    "model": "gpt-4o",
                    "azure_endpoint": "https://azure-openai.example.test",
                    "api_version": "2024-10-21",
                },
            }
        )

    message = str(exc_info.value)
    assert "agent_configuration.openai must be provided" in message
