from __future__ import annotations

import pytest

from azure_functions_agents.discovery.mcp import MCPDiscoveryResult, MCPServerDefinition
from azure_functions_agents.egress.credentials import (
    McpIdentityConfigurationError,
    compile_mcp_headers,
    compile_model_key_headers,
    compile_static_header,
    validate_mcp_identity_requirements,
)
from azure_functions_agents.egress.policy import (
    CONTROL_PLANE_DENY_HOSTS,
    MAX_EGRESS_POLICY_RULES,
    build_header_transform_rule,
    compile_egress_policy,
    derive_destination_hosts,
    validate_egress_rule_order,
)
from azure_functions_agents.transport.transport_models import (
    SandboxEgressHostRule,
    SandboxEgressPolicy,
    SandboxEgressRule,
    SandboxEgressRuleAction,
    SandboxEgressRuleMatch,
    SandboxProvisioningError,
)


def test_compiler_derives_destinations_and_denies_control_plane_hosts() -> None:
    policy = compile_egress_policy(
        web_request_allowed_hosts=["api.example.com"],
        mcp_urls=["https://mcp.example.com/api"],
        model_endpoint="https://models.example.com",
        telemetry_endpoint="https://telemetry.example.com/v2",
    )

    assert policy.default_action == "Deny"
    assert policy.traffic_inspection == "Full"
    assert tuple(rule.host for rule in policy.host_rules[:2]) == CONTROL_PLANE_DENY_HOSTS
    assert all(rule.action == "Deny" for rule in policy.host_rules[:2])
    assert {
        rule.host for rule in policy.host_rules if rule.action == "Allow"
    } == {
        "api.example.com",
        "mcp.example.com",
        "models.example.com",
        "telemetry.example.com",
    }


def test_unspecified_web_hosts_use_a_broad_allow_after_control_plane_denies() -> None:
    policy = compile_egress_policy(web_request_allowed_hosts=None)

    assert policy.host_rules[-1].host == "*"
    assert policy.host_rules[-1].action == "Allow"


def test_transform_rule_is_narrow_and_precedes_broader_rules() -> None:
    headers = compile_mcp_headers({"Authorization": "$" + "TOKEN"})
    transform = build_header_transform_rule(
        name="mcp-auth",
        url="https://mcp.example.com/v1",
        headers=headers,
    )
    allow = SandboxEgressRule.create(
        name="allow-mcp",
        match=SandboxEgressRuleMatch.create(host="mcp.example.com"),
        action=SandboxEgressRuleAction.create(type="Allow"),
    )

    policy = compile_egress_policy(
        web_request_allowed_hosts=[],
        rules=(transform, allow),
    )

    assert policy.rules[0].name == "mcp-auth"
    assert policy.rules[0].action.headers[0].operation == "Set"
    assert policy.rules[0].action.headers[0].value == "$" + "TOKEN"


def test_broad_allow_shadowing_narrow_deny_fails_closed() -> None:
    allow = SandboxEgressRule.create(
        name="allow-all",
        match=SandboxEgressRuleMatch.create(host="*"),
        action=SandboxEgressRuleAction.create(type="Allow"),
    )
    deny = SandboxEgressRule.create(
        name="deny-control",
        match=SandboxEgressRuleMatch.create(host="management.azure.com", path="/"),
        action=SandboxEgressRuleAction.create(type="Deny"),
    )

    with pytest.raises(SandboxProvisioningError, match="shadow"):
        validate_egress_rule_order((allow, deny))


def test_emitted_equal_specificity_rules_keep_deny_before_allow() -> None:
    deny = SandboxEgressRule.create(
        name="z-deny",
        match=SandboxEgressRuleMatch.create(host="service.example.com", path="/v1"),
        action=SandboxEgressRuleAction.create(type="Deny"),
    )
    allow = SandboxEgressRule.create(
        name="a-allow",
        match=SandboxEgressRuleMatch.create(host="service.example.com", path="/v1"),
        action=SandboxEgressRuleAction.create(type="Allow"),
    )

    policy = compile_egress_policy(
        web_request_allowed_hosts=[],
        rules=(deny, allow),
    )

    assert [rule.name for rule in policy.rules] == ["z-deny", "a-allow"]


def test_broad_host_allow_shadowing_deny_fails_closed() -> None:
    with pytest.raises(SandboxProvisioningError, match="shadow"):
        SandboxEgressPolicy.create(
            host_rules=(
                SandboxEgressHostRule.create(host="*", action="Allow"),
                SandboxEgressHostRule.create(host="management.azure.com", action="Deny"),
            )
        )


def test_compiler_accepts_the_supported_total_rule_limit() -> None:
    rules = tuple(
        SandboxEgressRule.create(
            name=f"allow-{index}",
            match=SandboxEgressRuleMatch.create(host=f"service-{index}.example.com"),
            action=SandboxEgressRuleAction.create(type="Allow"),
        )
        for index in range(MAX_EGRESS_POLICY_RULES - len(CONTROL_PLANE_DENY_HOSTS))
    )

    policy = compile_egress_policy(web_request_allowed_hosts=[], rules=rules)

    assert len(policy.host_rules) + len(policy.rules) == MAX_EGRESS_POLICY_RULES


def test_compiler_rejects_rule_count_above_the_supported_limit() -> None:
    rules = tuple(
        SandboxEgressRule.create(
            name=f"allow-{index}",
            match=SandboxEgressRuleMatch.create(host=f"service-{index}.example.com"),
            action=SandboxEgressRuleAction.create(type="Allow"),
        )
        for index in range(MAX_EGRESS_POLICY_RULES - len(CONTROL_PLANE_DENY_HOSTS) + 1)
    )

    with pytest.raises(SandboxProvisioningError, match="rule limit"):
        compile_egress_policy(web_request_allowed_hosts=[], rules=rules)


def test_secret_reference_requires_a_non_empty_value_template() -> None:
    with pytest.raises(SandboxProvisioningError, match=r"contain \{value\}"):
        compile_mcp_headers(
            {
                "Authorization": {
                    "secretRef": {"secret": "remote-token", "key": "TOKEN", "format": "Bearer"}
                }
            }
        )

    template = "Bearer " + "{" + "value}"
    header = compile_mcp_headers(
        {
            "Authorization": {
                "secretRef": {
                    "secret": "remote-token",
                    "key": "TOKEN",
                    "format": template,
                }
            }
        }
    )[0]
    assert header.secret_ref is not None
    assert header.secret_ref.secret_id == "remote-token"
    assert header.operation == "Set"


def test_static_headers_are_not_classified_by_name() -> None:
    header = compile_static_header("X-Api-Key", "already-resolved")

    assert header.value == "already-resolved"
    assert header.operation == "Set"


def test_header_repr_redacts_static_values_and_secret_references() -> None:
    static = compile_static_header("Authorization", "sentinel-secret-value")
    referenced = compile_mcp_headers(
        {
            "Authorization": {
                "secretRef": {
                    "secret": "credential-store",
                    "key": "sentinel-secret-key",
                    "format": "Bearer " + "{" + "value}",
                }
            }
        }
    )[0]

    assert "sentinel-secret-value" not in repr(static)
    assert "sentinel-secret-key" not in repr(referenced)
    assert "credential-store" not in repr(referenced)


def test_model_keys_compile_to_static_proxy_headers() -> None:
    headers = compile_model_key_headers(
        {
            "AZURE_OPENAI_API_KEY": "azure-key",
            "OPENAI_API_KEY": "openai-key",
        }
    )

    assert [(header.name, header.value) for header in headers] == [
        ("api-key", "azure-key"),
        ("Authorization", "Bearer " + "openai-key"),
    ]


def test_model_headers_require_an_endpoint_and_become_a_transform_rule() -> None:
    headers = compile_model_key_headers({"AZURE_OPENAI_API_KEY": "azure-key"})

    with pytest.raises(SandboxProvisioningError, match="model endpoint"):
        compile_egress_policy(web_request_allowed_hosts=[], model_headers=headers)

    policy = compile_egress_policy(
        web_request_allowed_hosts=[],
        model_endpoint="https://models.example.com/openai",
        model_headers=headers,
    )
    assert policy.rules[0].name == "model-auth"
    assert policy.rules[0].action.headers[0].value == "azure-key"


def test_authenticated_mcp_requires_a_known_group_identity() -> None:
    servers = {"remote": {"auth": {"scope": "https://service/.default"}}}

    with pytest.raises(McpIdentityConfigurationError, match="requires"):
        validate_mcp_identity_requirements(servers, ())

    with pytest.raises(McpIdentityConfigurationError, match="select"):
        validate_mcp_identity_requirements(servers, ("first", "second"))

    validate_mcp_identity_requirements(servers, ("only",))


def test_authenticated_mcp_client_id_must_belong_to_the_group() -> None:
    servers = {
        "remote": {
            "auth": {
                "scope": "https://service/.default",
                "client_id": "second",
            }
        }
    }

    with pytest.raises(McpIdentityConfigurationError, match="not available"):
        validate_mcp_identity_requirements(servers, ("first",))

    validate_mcp_identity_requirements(servers, ("first", "SECOND"))


def test_authenticated_discovery_definitions_are_not_silently_skipped() -> None:
    discovery = MCPDiscoveryResult(
        servers={},
        failed_loads=[],
        definitions={
            "remote": MCPServerDefinition.create(
                "remote",
                {
                    "url": "https://mcp.example.com",
                    "auth": {"scope": "https://service/.default"},
                },
            )
        },
    )

    with pytest.raises(McpIdentityConfigurationError, match="requires"):
        validate_mcp_identity_requirements(discovery.definitions, ())

    validate_mcp_identity_requirements(discovery.definitions, ("group-identity",))


@pytest.mark.parametrize("inspection", ["Partial", "None", "Legacy"])
def test_only_full_inspection_is_accepted(inspection: str) -> None:
    with pytest.raises(SandboxProvisioningError, match="must be Full"):
        SandboxEgressPolicy.create(traffic_inspection=inspection)  # type: ignore[arg-type]


def test_destination_derivation_rejects_non_http_urls() -> None:
    with pytest.raises(SandboxProvisioningError, match="HTTP URL"):
        derive_destination_hosts(
            web_request_allowed_hosts=[],
            mcp_urls=["stdio://local"],
        )
