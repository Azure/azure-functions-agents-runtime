from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from azure_functions_agents.config.schema import (
    AgentSpec,
    BuiltinEndpointsConfig,
    DynamicSessionsCodeInterpreterConfig,
    GlobalConfig,
    HarnessAgentConfig,
    McpFilter,
    ResolvedAgent,
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


# ---------------------------------------------------------------------------
# HarnessAgentConfig
# ---------------------------------------------------------------------------


def test_harness_agent_config_defaults() -> None:
    config = HarnessAgentConfig()
    assert config.max_context_window_tokens is None
    assert config.max_output_tokens is None
    assert config.disable_file_memory is False


def test_harness_agent_config_with_fields() -> None:
    config = HarnessAgentConfig(max_context_window_tokens=128_000, max_output_tokens=4_096)
    assert config.max_context_window_tokens == 128_000
    assert config.max_output_tokens == 4_096


def test_harness_agent_config_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        HarnessAgentConfig.model_validate({"unknown_field": True})


@pytest.mark.parametrize("value", [True, False, None, HarnessAgentConfig(max_context_window_tokens=8192)])
def test_agent_spec_harness_variants(value: bool | None | HarnessAgentConfig) -> None:
    spec = AgentSpec(name="X", description="Y", harness=value)
    assert spec.harness == value


@pytest.mark.parametrize("value", [True, False, None, HarnessAgentConfig()])
def test_global_config_harness_variants(value: bool | None | HarnessAgentConfig) -> None:
    config = GlobalConfig(harness=value)
    assert config.harness == value


def test_resolved_agent_harness_config_defaults_none() -> None:
    resolved = ResolvedAgent(
        name="X",
        description="desc",
        trigger=None,
        instructions="",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model=None,
        timeout=900.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
    )
    assert resolved.harness_config is None



def test_system_tools_config_parses() -> None:
    payload: dict[str, Any] = {
        "dynamic_sessions_code_interpreter": {"endpoint": "https://example.test"},
    }
    config = SystemToolsConfig.model_validate(payload)
    assert config.dynamic_sessions_code_interpreter == DynamicSessionsCodeInterpreterConfig(
        endpoint="https://example.test"
    )
