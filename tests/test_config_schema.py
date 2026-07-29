from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from azure_functions_agents.config.schema import (
    AcaSandboxConfig,
    AgentSpec,
    BuiltinEndpointsConfig,
    DynamicSessionsCodeInterpreterConfig,
    GlobalConfig,
    McpFilter,
    RetentionConfig,
    SessionRuntimeConfig,
    SubagentRef,
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


# --- session_runtime / aca_sandbox / retention (FRD 0008, P2) --------------


def test_global_config_session_runtime_defaults_to_none() -> None:
    """Absence of the block is the default and means the default in-process backend."""
    config = GlobalConfig()
    assert config.session_runtime is None


def test_session_runtime_config_defaults() -> None:
    config = SessionRuntimeConfig()
    assert config.harness == "maf"
    assert config.aca_sandbox is None


def test_session_runtime_config_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        SessionRuntimeConfig.model_validate({"extra_field": 1})


def test_aca_sandbox_config_parses() -> None:
    config = AcaSandboxConfig.model_validate(
        {"sandbox_group_resource_id": "/subscriptions/.../sandboxGroups/my-group"}
    )
    assert config.sandbox_group_resource_id == "/subscriptions/.../sandboxGroups/my-group"
    assert config.retention is None


def test_aca_sandbox_config_rejects_empty_sandbox_group_resource_id() -> None:
    with pytest.raises(ValidationError):
        AcaSandboxConfig(sandbox_group_resource_id="   ")


def test_aca_sandbox_config_rejects_missing_sandbox_group_resource_id() -> None:
    """FRD Row 5 remains fully enforced -- ``sandbox_group_resource_id`` is a
    required Pydantic field on ``AcaSandboxConfig`` independent of the
    presence-based backend selection removed by Decision #84, so an
    ``aca_sandbox`` block that omits it entirely (not just a blank string)
    must still fail at the schema level."""
    with pytest.raises(ValidationError, match="sandbox_group_resource_id"):
        AcaSandboxConfig.model_validate({})


def test_aca_sandbox_config_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        AcaSandboxConfig.model_validate(
            {"sandbox_group_resource_id": "x", "extra_field": 1}
        )


def test_retention_config_parses() -> None:
    retention = RetentionConfig.model_validate({"auto_suspend_idle": 300, "reclaim_idle": 3600})
    assert retention.auto_suspend_idle == 300
    assert retention.reclaim_idle == 3600


def test_retention_config_requires_both_fields() -> None:
    """Both fields are required together -- `reclaim_idle` is only meaningful
    relative to `auto_suspend_idle`, so a partial block is rejected rather
    than silently paired with an implicit default for the missing field."""
    with pytest.raises(ValidationError):
        RetentionConfig.model_validate({"auto_suspend_idle": 300})
    with pytest.raises(ValidationError):
        RetentionConfig.model_validate({"reclaim_idle": 3600})


def test_retention_config_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        RetentionConfig.model_validate(
            {"auto_suspend_idle": 300, "reclaim_idle": 3600, "extra_field": 1}
        )


def test_aca_sandbox_config_retention_nests_inside_aca_sandbox() -> None:
    """`retention` is a field on `AcaSandboxConfig`, not a sibling under
    `session_runtime` -- the presence of `aca_sandbox` alone selects the
    backend, so `retention` only makes sense nested inside it."""
    config = AcaSandboxConfig.model_validate(
        {
            "sandbox_group_resource_id": "/subscriptions/.../sandboxGroups/my-group",
            "retention": {"auto_suspend_idle": 300, "reclaim_idle": 3600},
        }
    )
    assert config.retention is not None
    assert config.retention.reclaim_idle == 3600


def test_global_config_session_runtime_aca_sandbox_parses() -> None:
    config = GlobalConfig.model_validate(
        {
            "session_runtime": {
                "harness": "maf",
                "aca_sandbox": {
                    "sandbox_group_resource_id": (
                        "/subscriptions/sub-1/resourceGroups/rg-1/providers/"
                        "Microsoft.App/sandboxGroups/my-group"
                    ),
                    "retention": {"auto_suspend_idle": 300, "reclaim_idle": 3600},
                },
            }
        }
    )
    assert config.session_runtime is not None
    assert config.session_runtime.aca_sandbox is not None
    assert config.session_runtime.aca_sandbox.retention is not None
    assert config.session_runtime.aca_sandbox.retention.reclaim_idle == 3600


def test_global_config_session_runtime_absent_aca_sandbox_means_default_backend() -> None:
    """No `provider` field exists -- omitting `aca_sandbox` entirely selects
    the default (in-process) backend, it is not an error."""
    config = GlobalConfig.model_validate({"session_runtime": {"harness": "maf"}})
    assert config.session_runtime is not None
    assert config.session_runtime.aca_sandbox is None


def test_session_runtime_config_rejects_provider_field() -> None:
    """`provider` was removed entirely (Decision #84) -- presence of the
    `aca_sandbox` block is now the sole backend discriminant, so an
    author-supplied `provider` key is rejected the same as any other unknown
    field (`extra="forbid"`)."""
    with pytest.raises(ValidationError):
        SessionRuntimeConfig.model_validate({"provider": "aca_sandbox"})


def test_session_runtime_config_rejects_retention_as_sibling_of_aca_sandbox() -> None:
    """`retention` moved onto `AcaSandboxConfig` (Decision #84) -- authoring
    it as a sibling of `aca_sandbox` under `session_runtime` (its pre-move
    location) is rejected, not silently ignored."""
    with pytest.raises(ValidationError):
        SessionRuntimeConfig.model_validate(
            {
                "aca_sandbox": {"sandbox_group_resource_id": "/subscriptions/.../x"},
                "retention": {"auto_suspend_idle": 300, "reclaim_idle": 3600},
            }
        )


@pytest.mark.parametrize(
    "dropped_field",
    ["max_run_seconds", "region", "disk", "content_package"],
)
def test_session_runtime_rejects_dropped_field(dropped_field: str) -> None:
    """FRD 0008 explicitly removed these fields during consolidation; a config
    still authoring one must fail loudly, not be silently ignored."""
    with pytest.raises(ValidationError, match="no longer supported"):
        SessionRuntimeConfig.model_validate({dropped_field: "anything"})


@pytest.mark.parametrize(
    "dropped_field",
    ["max_run_seconds", "region", "disk", "content_package"],
)
def test_aca_sandbox_config_rejects_dropped_field(dropped_field: str) -> None:
    with pytest.raises(ValidationError, match="no longer supported"):
        AcaSandboxConfig.model_validate(
            {"sandbox_group_resource_id": "x", dropped_field: "anything"}
        )


def test_session_runtime_dropped_field_error_names_scope_and_field() -> None:
    with pytest.raises(ValidationError, match=r"session_runtime.*`region`"):
        SessionRuntimeConfig.model_validate({"region": "eastus"})


def test_aca_sandbox_dropped_field_error_names_scope_and_field() -> None:
    with pytest.raises(ValidationError, match=r"session_runtime\.aca_sandbox.*`disk`"):
        AcaSandboxConfig.model_validate({"sandbox_group_resource_id": "x", "disk": "10Gi"})


def test_session_runtime_rejects_multiple_dropped_fields_at_once() -> None:
    with pytest.raises(ValidationError, match=r"`max_run_seconds`, `region`"):
        SessionRuntimeConfig.model_validate({"max_run_seconds": 60, "region": "eastus"})
