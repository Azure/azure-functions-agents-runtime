from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from azure_functions_agents.config.schema import (
    AgentSpec,
    BuiltinEndpointsConfig,
    DynamicSessionsCodeInterpreterConfig,
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
    [True, False, None, BuiltinEndpointsConfig(chat_api=True)],
)
def test_agent_spec_builtin_endpoints_variants(
    value: bool | None | BuiltinEndpointsConfig,
) -> None:
    spec = AgentSpec(name="X", description="Y", builtin_endpoints=value)
    assert spec.builtin_endpoints == value


def test_builtin_endpoints_debug_chat_ui_enables_chat_api() -> None:
    config = BuiltinEndpointsConfig(debug_chat_ui=True)
    assert config.debug_chat_ui is True
    assert config.chat_api is True


def test_builtin_endpoints_auth_defaults_to_function() -> None:
    config = BuiltinEndpointsConfig(chat_api=True)
    assert config.auth.mode == "function"
    assert config.auth.entra is None


def test_builtin_endpoints_auth_string_shorthand() -> None:
    config = BuiltinEndpointsConfig.model_validate({"chat_api": True, "auth": "entra"})
    assert config.auth.mode == "entra"


def test_builtin_endpoints_auth_full_object() -> None:
    config = BuiltinEndpointsConfig.model_validate(
        {
            "chat_api": True,
            "auth": {
                "mode": "entra",
                "entra": {
                    "tenant_id": "t-1",
                    "allowed_audiences": ["api://app"],
                    "allowed_client_ids": ["caller"],
                },
            },
        }
    )
    assert config.auth.mode == "entra"
    assert config.auth.entra is not None
    assert config.auth.entra.tenant_id == "t-1"
    assert config.auth.entra.allowed_audiences == ["api://app"]


def test_builtin_endpoints_auth_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        BuiltinEndpointsConfig.model_validate({"chat_api": True, "auth": "basic"})


def test_builtin_endpoints_auth_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        BuiltinEndpointsConfig.model_validate(
            {"chat_api": True, "auth": {"mode": "entra", "bogus": 1}}
        )


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


def test_global_config_auth_defaults_to_none() -> None:
    assert GlobalConfig().auth is None


def test_global_config_auth_string_shorthand() -> None:
    config = GlobalConfig.model_validate({"auth": "entra"})
    assert config.auth is not None
    assert config.auth.mode == "entra"


def test_global_config_auth_full_object() -> None:
    config = GlobalConfig.model_validate(
        {"auth": {"mode": "entra", "entra": {"tenant_id": "t-1"}}}
    )
    assert config.auth is not None
    assert config.auth.mode == "entra"
    assert config.auth.entra is not None
    assert config.auth.entra.tenant_id == "t-1"


def test_global_config_auth_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        GlobalConfig.model_validate({"auth": "basic"})


def test_system_tools_config_parses() -> None:
    payload: dict[str, Any] = {
        "dynamic_sessions_code_interpreter": {"endpoint": "https://example.test"},
    }
    config = SystemToolsConfig.model_validate(payload)
    assert config.dynamic_sessions_code_interpreter == DynamicSessionsCodeInterpreterConfig(
        endpoint="https://example.test"
    )
