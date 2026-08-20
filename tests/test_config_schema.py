from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from azure_functions_agents.config.schema import (
    AgentSpec,
    BuiltinEndpointsConfig,
    CompactionConfig,
    DynamicSessionsCodeInterpreterConfig,
    GlobalConfig,
    McpFilter,
    ResolvedAgent,
    SubagentRef,
    SystemToolsConfig,
    ToolsFilter,
    TriggerSpec,
    WorkflowConfig,
    WorkflowSubagentRef,
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
    value: bool | BuiltinEndpointsConfig | None,
) -> None:
    spec = AgentSpec(name="X", description="Y", builtin_endpoints=value)
    assert spec.builtin_endpoints == value


def test_builtin_endpoints_debug_chat_ui_enables_chat_api() -> None:
    config = BuiltinEndpointsConfig(debug_chat_ui=True)
    assert config.debug_chat_ui is True
    assert config.chat_api is True


def test_builtin_endpoints_auth_defaults_to_function() -> None:
    config = BuiltinEndpointsConfig(chat_api=True)
    assert config.http_auth.mode == "function"
    assert config.http_auth.entra is None


def test_builtin_endpoints_auth_string_shorthand() -> None:
    config = BuiltinEndpointsConfig.model_validate({"chat_api": True, "http_auth": "entra"})
    assert config.http_auth.mode == "entra"


def test_builtin_endpoints_auth_full_object() -> None:
    config = BuiltinEndpointsConfig.model_validate(
        {
            "chat_api": True,
            "http_auth": {
                "mode": "entra",
                "entra": {
                    "tenant_id": "t-1",
                    "allowed_audiences": ["api://app"],
                    "allowed_client_ids": ["caller"],
                },
            },
        }
    )
    assert config.http_auth.mode == "entra"
    assert config.http_auth.entra is not None
    assert config.http_auth.entra.tenant_id == "t-1"
    assert config.http_auth.entra.allowed_audiences == ["api://app"]


def test_builtin_endpoints_auth_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        BuiltinEndpointsConfig.model_validate({"chat_api": True, "http_auth": "basic"})


def test_builtin_endpoints_auth_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        BuiltinEndpointsConfig.model_validate(
            {"chat_api": True, "http_auth": {"mode": "entra", "bogus": 1}}
        )


@pytest.mark.parametrize(
    "value",
    [False, None, McpFilter(exclude=["x"])],
)
def test_agent_spec_mcp_variants(value: bool | McpFilter | None) -> None:
    spec = AgentSpec(name="X", description="Y", mcp=value)
    assert spec.mcp == value


@pytest.mark.parametrize(
    "value",
    [False, None, ToolsFilter(exclude=["x"])],
)
def test_agent_spec_tools_variants(value: bool | ToolsFilter | None) -> None:
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
# CompactionConfig
# ---------------------------------------------------------------------------


def test_compaction_config_with_valid_limits() -> None:
    config = CompactionConfig(max_context_window_tokens=128_000, max_output_tokens=4_096)
    assert config.max_context_window_tokens == 128_000
    assert config.max_output_tokens == 4_096


def test_compaction_config_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        CompactionConfig.model_validate(
            {
                "max_context_window_tokens": 128_000,
                "max_output_tokens": 4_096,
                "unknown_field": True,
            }
        )


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"max_context_window_tokens": 128_000},
        {"max_output_tokens": 4_096},
        {"max_context_window_tokens": 0, "max_output_tokens": 4_096},
        {"max_context_window_tokens": 4_096, "max_output_tokens": 4_096},
        {"max_context_window_tokens": 4_096, "max_output_tokens": 8_192},
    ],
)
def test_compaction_config_rejects_invalid_limits(value: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        CompactionConfig.model_validate(value)


@pytest.mark.parametrize(
    "value",
    [False, None, CompactionConfig(max_context_window_tokens=8_192, max_output_tokens=1_024)],
)
def test_agent_spec_compaction_variants(value: bool | CompactionConfig | None) -> None:
    spec = AgentSpec(name="X", description="Y", compaction=value)
    assert spec.compaction == value


@pytest.mark.parametrize(
    "value",
    [None, CompactionConfig(max_context_window_tokens=8_192, max_output_tokens=1_024)],
)
def test_global_config_compaction_variants(value: CompactionConfig | None) -> None:
    config = GlobalConfig(compaction=value)
    assert config.compaction == value


@pytest.mark.parametrize("model", [AgentSpec, GlobalConfig])
@pytest.mark.parametrize("value", [True, False])
def test_removed_harness_config_is_forbidden(model: type[AgentSpec] | type[GlobalConfig], value: bool) -> None:
    payload = {"harness": value}
    if model is AgentSpec:
        payload.update(name="X", description="Y")
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_resolved_agent_compaction_config_defaults_none() -> None:
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
    assert resolved.compaction_config is None


def test_global_config_auth_defaults_to_none() -> None:
    assert GlobalConfig().http_auth is None


def test_global_config_auth_string_shorthand() -> None:
    config = GlobalConfig.model_validate({"http_auth": "entra"})
    assert config.http_auth is not None
    assert config.http_auth.mode == "entra"


def test_global_config_auth_full_object() -> None:
    config = GlobalConfig.model_validate(
        {"http_auth": {"mode": "entra", "entra": {"tenant_id": "t-1"}}}
    )
    assert config.http_auth is not None
    assert config.http_auth.mode == "entra"
    assert config.http_auth.entra is not None
    assert config.http_auth.entra.tenant_id == "t-1"


def test_global_config_auth_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        GlobalConfig.model_validate({"http_auth": "basic"})


def test_system_tools_config_parses() -> None:
    payload: dict[str, Any] = {
        "dynamic_sessions_code_interpreter": {"endpoint": "https://example.test"},
    }
    config = SystemToolsConfig.model_validate(payload)
    assert config.dynamic_sessions_code_interpreter == DynamicSessionsCodeInterpreterConfig(
        endpoint="https://example.test"
    )


def test_subagent_ref_object_form_parses() -> None:
    ref = SubagentRef.model_validate({"agent": "billing-specialist"})
    assert ref.agent == "billing-specialist"
    assert ref.when is None


def test_subagent_ref_object_form_with_when_parses() -> None:
    ref = SubagentRef.model_validate(
        {"agent": "billing-specialist", "when": "Route billing questions here."}
    )
    assert ref.agent == "billing-specialist"
    assert ref.when == "Route billing questions here."


@pytest.mark.parametrize("ref_type", [SubagentRef, WorkflowSubagentRef])
def test_subagent_ref_normalizes_blank_when_to_none(
    ref_type: type[SubagentRef] | type[WorkflowSubagentRef],
) -> None:
    ref = ref_type.model_validate({"agent": "billing-specialist", "when": "   "})

    assert ref.when is None


@pytest.mark.parametrize("ref_type", [SubagentRef, WorkflowSubagentRef])
def test_subagent_ref_is_immutable(
    ref_type: type[SubagentRef] | type[WorkflowSubagentRef],
) -> None:
    ref = ref_type(agent="billing-specialist")

    with pytest.raises(ValidationError, match="Instance is frozen"):
        ref.agent = "shipping-specialist"


def test_subagent_ref_rejects_empty_agent() -> None:
    with pytest.raises(ValidationError):
        SubagentRef(agent="   ")


@pytest.mark.parametrize("forbidden_field", ["id", "tool_name"])
def test_subagent_ref_extra_forbidden(forbidden_field: str) -> None:
    """No `id` or `tool_name` override field exists — identity is the slug only (FRD 0007 §5 Decision #16)."""
    with pytest.raises(ValidationError):
        SubagentRef.model_validate({"agent": "billing-specialist", forbidden_field: "x"})


def test_agent_spec_subagents_object_form_parses() -> None:
    spec = AgentSpec.model_validate(
        {
            "name": "Coordinator",
            "description": "desc",
            "subagents": [{"agent": "billing-specialist", "when": "Billing questions."}],
        }
    )
    assert spec.subagents == [
        SubagentRef(agent="billing-specialist", when="Billing questions.")
    ]


def test_agent_spec_subagents_rejects_string_shorthand() -> None:
    """String shorthand (`subagents: [billing-specialist]`) is rejected — object form only."""
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                "name": "Coordinator",
                "description": "desc",
                "subagents": ["billing-specialist"],
            }
        )


def test_agent_spec_subagents_defaults_to_none() -> None:
    spec = AgentSpec(name="X", description="Y")
    assert spec.subagents is None


def test_workflow_subagent_ref_parses_optional_routing_hint() -> None:
    ref = WorkflowSubagentRef.model_validate(
        {"agent": "pr_status_analyst", "when": "Analyze one pull request."}
    )

    assert ref.agent == "pr_status_analyst"
    assert ref.when == "Analyze one pull request."


def test_workflow_subagent_ref_rejects_empty_agent() -> None:
    with pytest.raises(ValidationError, match="agent must be non-empty"):
        WorkflowSubagentRef(agent=" ")


def test_workflow_subagent_ref_forbids_chat_grant_fields() -> None:
    with pytest.raises(ValidationError):
        WorkflowSubagentRef.model_validate(
            {"agent": "pr_status_analyst", "tool_name": "delegate_pr_status"}
        )


def test_agent_spec_workflows_parses_typed_subagent_grant() -> None:
    spec = AgentSpec.model_validate(
        {
            "name": "Coordinator",
            "description": "Coordinates PR reporting.",
            "workflows": {
                "enabled": True,
                "exclude": ["private_tool"],
                "subagents": [
                    {
                        "agent": "pr_status_analyst",
                        "when": "Analyze one pull request.",
                    }
                ],
            },
        }
    )

    assert spec.workflows == WorkflowConfig(
        enabled=True,
        exclude=("private_tool",),
        subagents=(
            WorkflowSubagentRef(
                agent="pr_status_analyst",
                when="Analyze one pull request.",
            ),
        ),
    )


def test_agent_spec_workflows_omission_is_deny_by_default() -> None:
    spec = AgentSpec(name="Coordinator", description="Coordinates work.")

    assert spec.workflows is None


@pytest.mark.parametrize(
    "workflows",
    [
        {"enabled": True, "unknown": True},
        {"enabled": True, "subagents": ["pr_status_analyst"]},
        {"enabled": True, "subagents": [{"agent": ""}]},
    ],
)
def test_agent_spec_workflows_rejects_invalid_shape(workflows: object) -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                "name": "Coordinator",
                "description": "Coordinates work.",
                "workflows": workflows,
            }
        )
