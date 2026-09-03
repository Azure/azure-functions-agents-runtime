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
    """No `id` or `tool_name` override field exists — identity is the slug only."""
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


# --- session_runtime / aca_sandbox / retention --------------------------------


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
        {
            "sandbox_group_resource_id": "/subscriptions/.../sandboxGroups/my-group",
            "region": " WestUS2 ",
        }
    )
    assert config.sandbox_group_resource_id == "/subscriptions/.../sandboxGroups/my-group"
    assert config.region == "westus2"
    assert config.retention is None


def test_aca_sandbox_config_rejects_empty_sandbox_group_resource_id() -> None:
    with pytest.raises(ValidationError):
        AcaSandboxConfig(sandbox_group_resource_id="   ", region="westus2")


def test_aca_sandbox_config_rejects_missing_sandbox_group_resource_id() -> None:
    """FRD Row 5 remains fully enforced -- ``sandbox_group_resource_id`` is a
    required Pydantic field on ``AcaSandboxConfig`` independent of backend
    selection, so an ``aca_sandbox`` block that omits it entirely (not just a
    blank string) must still fail at the schema level."""
    with pytest.raises(ValidationError, match="sandbox_group_resource_id"):
        AcaSandboxConfig.model_validate({"region": "westus2"})


def test_aca_sandbox_config_rejects_missing_or_invalid_region() -> None:
    resource_id = "/subscriptions/.../sandboxGroups/my-group"
    with pytest.raises(ValidationError, match="region"):
        AcaSandboxConfig.model_validate({"sandbox_group_resource_id": resource_id})
    for region in ("", "   ", "west us 2", "west-us-2", "wéstus2"):
        with pytest.raises(ValidationError, match="region"):
            AcaSandboxConfig.model_validate(
                {
                    "sandbox_group_resource_id": resource_id,
                    "region": region,
                }
            )


def test_aca_sandbox_config_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        AcaSandboxConfig.model_validate(
            {
                "sandbox_group_resource_id": "x",
                "region": "westus2",
                "extra_field": 1,
            }
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
            "region": "westus2",
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
                    "region": "westus2",
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


def test_session_runtime_config_rejects_explicit_null_aca_sandbox() -> None:
    """A bare `aca_sandbox:` key (present in the mapping, explicit `None`) is
    NOT the same as the key being omitted. `aca_sandbox: AcaSandboxConfig |
    None` means Pydantic matches an explicit `None` directly against the
    union's `None` arm without ever attempting to construct
    `AcaSandboxConfig` -- so that model's own required-field validation
    (`sandbox_group_resource_id`) never runs, and the config would otherwise
    silently select the in-process default instead of failing startup
    (fail-open, not fail-closed)."""
    with pytest.raises(
        ValidationError, match=r"aca_sandbox.*must not be explicitly `null`"
    ):
        SessionRuntimeConfig.model_validate({"aca_sandbox": None})


def test_session_runtime_config_omitted_aca_sandbox_still_defaults_to_none() -> None:
    """The explicit-null guard above must not affect the key being omitted
    entirely -- that must keep defaulting to `None` (in-process backend)
    with no error, exactly as before the guard was added."""
    config = SessionRuntimeConfig.model_validate({})
    assert config.aca_sandbox is None


def test_global_config_session_runtime_rejects_explicit_null_aca_sandbox() -> None:
    """Same guard, exercised through the full `GlobalConfig` -- proves the
    fix closes the bug end-to-end and not just on the isolated sub-model."""
    with pytest.raises(ValidationError, match=r"aca_sandbox.*must not be explicitly `null`"):
        GlobalConfig.model_validate({"session_runtime": {"aca_sandbox": None}})


def test_session_runtime_config_rejects_provider_field() -> None:
    """`provider` was removed entirely -- presence of the `aca_sandbox` block
    is now the sole backend discriminant, so an author-supplied `provider` key
    is rejected the same as any other unknown field (`extra="forbid"`)."""
    with pytest.raises(ValidationError):
        SessionRuntimeConfig.model_validate({"provider": "aca_sandbox"})


def test_session_runtime_config_rejects_retention_as_sibling_of_aca_sandbox() -> None:
    """`retention` belongs on `AcaSandboxConfig` -- authoring it as a sibling
    of `aca_sandbox` under `session_runtime` is rejected, not silently
    ignored."""
    with pytest.raises(ValidationError):
        SessionRuntimeConfig.model_validate(
            {
                "aca_sandbox": {
                    "sandbox_group_resource_id": "/subscriptions/.../x",
                    "region": "westus2",
                },
                "retention": {"auto_suspend_idle": 300, "reclaim_idle": 3600},
            }
        )


@pytest.mark.parametrize(
    "dropped_field",
    ["max_run_seconds", "region", "disk", "content_package"],
)
def test_session_runtime_rejects_dropped_field(dropped_field: str) -> None:
    """These names were considered and rejected during FRD 0008 design and
    never shipped as real fields, so Pydantic's own `extra="forbid"` policy
    rejects them like any other unknown field -- no dedicated check needed."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SessionRuntimeConfig.model_validate({dropped_field: "anything"})


@pytest.mark.parametrize(
    "dropped_field",
    ["max_run_seconds", "disk", "content_package"],
)
def test_aca_sandbox_config_rejects_dropped_field(dropped_field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AcaSandboxConfig.model_validate(
            {
                "sandbox_group_resource_id": "x",
                "region": "westus2",
                dropped_field: "anything",
            }
        )


def test_session_runtime_dropped_field_error_names_scope_and_field() -> None:
    """No custom message is involved, but Pydantic's structured error still
    identifies exactly which field was rejected."""
    with pytest.raises(ValidationError) as exc_info:
        SessionRuntimeConfig.model_validate({"region": "eastus"})
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["type"] == "extra_forbidden"
    assert errors[0]["loc"] == ("region",)


def test_aca_sandbox_dropped_field_error_names_scope_and_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AcaSandboxConfig.model_validate(
            {
                "sandbox_group_resource_id": "x",
                "region": "westus2",
                "disk": "10Gi",
            }
        )
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["type"] == "extra_forbidden"
    assert errors[0]["loc"] == ("disk",)


def test_session_runtime_rejects_multiple_dropped_fields_at_once() -> None:
    """Pydantic reports each extra field as its own error entry rather than
    a single combined message."""
    with pytest.raises(ValidationError) as exc_info:
        SessionRuntimeConfig.model_validate({"max_run_seconds": 60, "region": "eastus"})
    locs = {error["loc"] for error in exc_info.value.errors()}
    assert locs == {("max_run_seconds",), ("region",)}
    assert all(error["type"] == "extra_forbidden" for error in exc_info.value.errors())
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
