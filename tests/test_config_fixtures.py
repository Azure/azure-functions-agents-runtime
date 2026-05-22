"""Integration-style tests that exercise loader behavior against on-disk fixtures.

Each fixture under ``tests/fixtures/config_scenarios/`` represents a realistic
combination of ``agents.config.yaml`` and ``*.agent.md`` files. These tests load
them through the public API (``load_global_config``/``load_agent_specs``) and
assert the parsed configuration matches what the fixtures advertise.
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

import azure_functions_agents.discovery.mcp as mcp_discovery
from azure_functions_agents.client_manager.providers import (
    AzureOpenAIConfig,
    FoundryConfig,
    OpenAIConfig,
)
from azure_functions_agents.config.loader import (
    _load_agent_spec,
    load_agent_specs,
    load_global_config,
)
from azure_functions_agents.config.merge import compose
from azure_functions_agents.config.schema import (
    DebugConfig,
    GlobalConfig,
    McpFilter,
    SkillsFilter,
    ToolsFilter,
)
from azure_functions_agents.discovery.mcp import clear_mcp_cache, discover_mcp_servers

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "config_scenarios"


class _CapturedMCPStreamableHTTPTool:
    def __init__(
        self,
        name: str,
        url: str,
        *,
        allowed_tools: list[str] | None = None,
        header_provider: object = None,
        **_: object,
    ) -> None:
        self.name = name
        self.url = url
        self.allowed_tools = allowed_tools
        self.header_provider = header_provider


def _specs_by_name(specs):
    return {spec.name: spec for spec in specs}


# ---------------------------------------------------------------------------
# 01 — minimal main-only agent with no global config
# ---------------------------------------------------------------------------


def test_minimal_main_only_agent() -> None:
    fixture = FIXTURES_ROOT / "01_minimal"

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)

    assert global_config.agent_configuration is None
    assert global_config.system_tools is None
    assert global_config.tools is None

    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "Minimal Assistant"
    assert spec.is_main is True
    assert spec.trigger is None
    assert spec.debug is None
    assert spec.agent_configuration is None
    assert "helpful assistant" in spec.instructions
    assert spec.substitute_variables is True


# ---------------------------------------------------------------------------
# 02 — global defaults inherited by a bare main agent
# ---------------------------------------------------------------------------


def test_global_defaults_inherited() -> None:
    fixture = FIXTURES_ROOT / "02_global_defaults"

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)

    assert global_config.agent_configuration is not None
    assert global_config.agent_configuration.provider == "openai"
    assert global_config.agent_configuration.timeout == 900
    assert global_config.agent_configuration.openai is not None
    assert global_config.agent_configuration.openai.model == "gpt-4o"
    assert global_config.system_tools is not None
    assert global_config.system_tools.execute_in_sessions is not None
    assert (
        global_config.system_tools.execute_in_sessions.session_pool_management_endpoint
        == "https://pool.example.test"
    )

    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "Default Assistant"
    assert spec.is_main is True
    assert spec.agent_configuration is None  # inherits global config at resolve time


# ---------------------------------------------------------------------------
# 03 — env substitution across global and agent files
# ---------------------------------------------------------------------------


def test_env_substitution_resolves_known_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = FIXTURES_ROOT / "03_env_substitution"

    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("ACA_SESSION_POOL_ENDPOINT", "https://pool.contoso.test")
    monkeypatch.setenv("SUBSCRIPTION_ID", "sub-123")
    monkeypatch.setenv("TO_EMAIL", "alerts@contoso.test")

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)

    assert global_config.agent_configuration is not None
    assert global_config.agent_configuration.provider == "openai"
    assert global_config.agent_configuration.timeout == 600
    assert global_config.agent_configuration.openai is not None
    assert global_config.agent_configuration.openai.model == "gpt-4o-mini"
    assert global_config.agent_configuration.openai.api_key == "secret-key"
    assert global_config.system_tools is not None
    assert global_config.system_tools.execute_in_sessions is not None
    assert (
        global_config.system_tools.execute_in_sessions.session_pool_management_endpoint
        == "https://pool.contoso.test"
    )

    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "Azure Reporter"
    # description in frontmatter mixes both %VAR% and $VAR styles.
    assert spec.description == (
        "Reports on subscription sub-123 and emails alerts@contoso.test."
    )
    assert spec.agent_configuration is None
    assert spec.trigger is not None
    assert spec.trigger.type == "timer_trigger"
    assert spec.trigger.args["schedule"] == "0 0 7 * * *"
    assert spec.trigger.args["run_on_start"] is True
    # Body should reflect both substitution syntaxes.
    assert "sub-123" in spec.instructions
    assert "alerts@contoso.test" in spec.instructions


# ---------------------------------------------------------------------------
# 04 — substitute_variables=false keeps every placeholder literal
# ---------------------------------------------------------------------------


def test_substitute_variables_false_preserves_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FIXTURES_ROOT / "04_substitute_variables_false"

    # Set these to prove they are deliberately ignored when opt-out is on.
    monkeypatch.setenv("AGENT_MODEL", "gpt-4o")
    monkeypatch.setenv("RESPONSE_TEMPLATE", "ignored")
    monkeypatch.setenv("TO_EMAIL", "ignored@example.test")
    monkeypatch.setenv("REPORT_FORMAT", "ignored")

    specs = load_agent_specs(fixture, strict=True)

    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "Literal Agent"
    assert spec.substitute_variables is False
    # Model retained the literal placeholder.
    assert spec.agent_configuration is not None
    assert spec.agent_configuration["openai"]["model"] == "$AGENT_MODEL"
    assert spec.response_example == "$RESPONSE_TEMPLATE"
    assert spec.trigger is not None
    assert spec.trigger.args["route"] == "literal"
    assert spec.trigger.args["methods"] == ["POST"]
    assert spec.trigger.args["auth_level"] == "function"
    # Body placeholders must remain literal.
    assert "$TO_EMAIL" in spec.instructions
    assert "%REPORT_FORMAT%" in spec.instructions
    assert "ignored" not in spec.instructions


# ---------------------------------------------------------------------------
# 05 — multiple trigger types coexist alongside a main agent
# ---------------------------------------------------------------------------


def test_multi_trigger_fixture() -> None:
    fixture = FIXTURES_ROOT / "05_multi_trigger"

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)

    assert global_config.agent_configuration is not None
    assert global_config.agent_configuration.openai is not None
    assert global_config.agent_configuration.openai.model == "gpt-4o"
    assert global_config.agent_configuration.timeout == 300

    by_name = _specs_by_name(specs)
    assert set(by_name) == {
        "Main Chat",
        "Nightly Report",
        "Resource Summary",
        "Queue Worker",
        "Blob Watcher",
    }

    main = by_name["Main Chat"]
    assert main.is_main is True
    assert main.trigger is None

    nightly = by_name["Nightly Report"]
    assert nightly.trigger is not None
    assert nightly.trigger.type == "timer_trigger"
    assert nightly.trigger.args["schedule"] == "0 0 7 * * *"
    assert nightly.trigger.args["run_on_start"] is False
    assert nightly.is_main is False

    resource = by_name["Resource Summary"]
    assert resource.trigger is not None
    assert resource.trigger.type == "http_trigger"
    assert resource.trigger.args["route"] == "resource-summary"
    assert resource.trigger.args["methods"] == ["POST"]
    assert resource.trigger.args["auth_level"] == "function"

    queue = by_name["Queue Worker"]
    assert queue.trigger is not None
    assert queue.trigger.type == "queue_trigger"
    assert queue.trigger.args["name"] == "work-items"
    assert queue.trigger.args["connection"] == "AzureWebJobsStorage"

    blob = by_name["Blob Watcher"]
    assert blob.trigger is not None
    assert blob.trigger.type == "blob_trigger"
    assert blob.trigger.args["path"] == "uploads/{name}.txt"
    assert blob.trigger.args["connection"] == "AzureWebJobsStorage"


# ---------------------------------------------------------------------------
# 06 — capability filtering (false-disable plus exclude lists)
# ---------------------------------------------------------------------------


def test_capability_filtering_fixture() -> None:
    fixture = FIXTURES_ROOT / "06_capability_filtering"

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)

    assert global_config.agent_configuration is not None
    assert global_config.agent_configuration.openai is not None
    assert global_config.agent_configuration.openai.model == "gpt-4o"
    assert global_config.agent_configuration.timeout == 900
    assert global_config.tools is not None
    assert isinstance(global_config.tools, ToolsFilter)
    assert global_config.tools.exclude == ["bash", "execute_shell"]

    by_name = _specs_by_name(specs)
    assert set(by_name) == {"Locked Down", "Selective Filters"}

    locked = by_name["Locked Down"]
    assert locked.tools is False
    assert locked.skills is False
    assert locked.mcp is False
    assert locked.system_tools is not None
    assert locked.system_tools.execute_in_sessions is False
    assert locked.trigger is not None
    assert locked.trigger.args["route"] == "locked-down"

    selective = by_name["Selective Filters"]
    assert isinstance(selective.mcp, McpFilter)
    assert selective.mcp.exclude == ["experimental-server", "custom-api"]
    assert isinstance(selective.skills, SkillsFilter)
    assert selective.skills.exclude == ["compliance-checker", "security-review"]
    assert isinstance(selective.tools, ToolsFilter)
    assert selective.tools.exclude == ["web_fetch"]


# ---------------------------------------------------------------------------
# 07 — debug surface variants (none/true/false/object)
# ---------------------------------------------------------------------------


def test_debug_endpoint_variants() -> None:
    fixture = FIXTURES_ROOT / "07_debug_endpoints"

    specs = load_agent_specs(fixture, strict=True)
    by_name = _specs_by_name(specs)
    assert set(by_name) == {
        "Debug Main",
        "Debug Shorthand On",
        "Debug Shorthand Off",
        "Debug Mixed",
    }

    main = by_name["Debug Main"]
    assert main.is_main is True
    assert main.debug is None  # resolver applies main-default later

    on = by_name["Debug Shorthand On"]
    assert on.debug is True
    assert on.trigger is not None and on.trigger.args["route"] == "debug-on"

    off = by_name["Debug Shorthand Off"]
    assert off.debug is False

    mixed = by_name["Debug Mixed"]
    assert isinstance(mixed.debug, DebugConfig)
    assert mixed.debug.chat is True
    assert mixed.debug.http is True
    assert mixed.debug.mcp is False


# ---------------------------------------------------------------------------
# 08 — JSON schemas, response example, and free-form metadata
# ---------------------------------------------------------------------------


def test_schemas_and_metadata_parsed() -> None:
    fixture = FIXTURES_ROOT / "08_schemas_and_metadata"

    specs = load_agent_specs(fixture, strict=True)
    assert len(specs) == 1
    spec = specs[0]

    assert spec.name == "Structured Reporter"
    assert spec.input_schema is not None
    assert spec.input_schema["type"] == "object"
    assert spec.input_schema["required"] == ["subscription_id", "report_type"]
    assert spec.input_schema["properties"]["report_type"]["enum"] == [
        "cost",
        "security",
        "inventory",
    ]

    assert spec.response_schema is not None
    assert spec.response_schema["required"] == ["status", "summary"]
    assert (
        spec.response_schema["properties"]["findings"]["items"]["properties"][
            "severity"
        ]["type"]
        == "string"
    )

    assert spec.response_example is not None
    assert '"status": "ok"' in spec.response_example
    assert spec.response_example.rstrip().endswith("}")

    assert spec.metadata is not None
    assert spec.metadata["owner"] == "platform-team"
    assert spec.metadata["tags"] == ["reporting", "azure"]
    assert spec.metadata["cost_center"] == 4242
    assert spec.metadata["enabled"] is True


# ---------------------------------------------------------------------------
# 09 — fenced code blocks in body are preserved verbatim
# ---------------------------------------------------------------------------


def test_code_block_preservation(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = FIXTURES_ROOT / "09_code_block_preservation"

    monkeypatch.setenv("DEPLOY_REGION", "westus2")
    monkeypatch.setenv("ALERT_CHANNEL", "ops-room")
    monkeypatch.setenv("ONCALL_USER", "alice")
    monkeypatch.setenv("SUBSCRIPTION_ID", "sub-abc")
    # Set the "do not touch" vars to prove the fenced code block ignored them.
    monkeypatch.setenv("AZURE_OPENAI_KEY", "leaked")
    monkeypatch.setenv("ENDPOINT", "leaked")
    monkeypatch.setenv("DO_NOT_TOUCH", "leaked")

    specs = load_agent_specs(fixture, strict=True)
    assert len(specs) == 1
    spec = specs[0]
    body = spec.instructions

    # Prose substitutions resolved.
    assert "region is westus2" in body
    assert "alert channel is ops-room" in body
    assert "contact alice for escalation" in body
    assert "subscription is sub-abc" in body

    # Fenced contents must remain literal.
    assert "export AZURE_OPENAI_KEY=$AZURE_OPENAI_KEY" in body
    assert 'echo "Region: %DEPLOY_REGION%"' in body
    assert "curl https://api.example.test/$ENDPOINT" in body
    assert "secret: $DO_NOT_TOUCH" in body
    assert "queue: %DO_NOT_TOUCH%" in body

    # "leaked" must never appear inside fenced regions — easiest check: it
    # should appear nowhere because every occurrence of those vars is fenced.
    assert "leaked" not in body


# ---------------------------------------------------------------------------
# 10 — connector tools list + partial-identifier behavior
# ---------------------------------------------------------------------------


def test_connector_tools_and_partial_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FIXTURES_ROOT / "10_connector_tools_and_partial_idents"

    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("ACA_SESSION_POOL_ENDPOINT", "https://pool.example.test")
    monkeypatch.setenv("GITHUB_CONNECTION_ID", "conn-gh")
    monkeypatch.setenv("SERVICENOW_CONNECTION_ID", "conn-sn")
    monkeypatch.setenv("REGION", "westus3")
    monkeypatch.setenv("TENANT", "tenant-xyz")

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)

    assert global_config.agent_configuration is not None
    assert global_config.agent_configuration.openai is not None
    assert global_config.agent_configuration.openai.model == "gpt-4o"
    assert global_config.agent_configuration.timeout == 1200
    assert global_config.system_tools is not None
    assert global_config.system_tools.execute_in_sessions is not None
    assert (
        global_config.system_tools.execute_in_sessions.session_pool_management_endpoint
        == "https://pool.example.test"
    )
    connectors = global_config.system_tools.tools_from_connections
    assert [c.connection_id for c in connectors] == ["conn-gh", "conn-sn"]
    assert connectors[0].prefix == "github"
    assert connectors[1].prefix is None

    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "Partial Identifier Agent"
    assert spec.metadata is not None
    # $REGION resolves, `-primary` is preserved literally.
    assert spec.metadata["primary_region"] == "westus3-primary"
    # %REGION-secondary% never matches the percent pattern (hyphen breaks the
    # identifier), so it stays exactly as written.
    assert spec.metadata["raw_label"] == "%REGION-secondary%"
    # `$TENANT.id` resolves the identifier and leaves `.id` as a suffix.
    assert spec.metadata["tenant_ref"] == "tenant-xyz.id"

    # Body shows the same partial-identifier rules.
    assert "primary region is westus3-primary" in spec.instructions
    assert "%REGION-secondary%" in spec.instructions
    assert "Tenant pointer: tenant-xyz.id" in spec.instructions


# ---------------------------------------------------------------------------
# 11 — mcp.json env-var substitution
# ---------------------------------------------------------------------------


def test_mcp_json_env_substitution(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = FIXTURES_ROOT / "11_mcp_json_substitution"

    monkeypatch.setenv("GITHUB_MCP_TOKEN", "ghp_live_token")
    monkeypatch.setenv("TENANT_NAME", "contoso")
    monkeypatch.setenv("NODE_BIN", "/usr/local/bin/node")
    monkeypatch.setenv("WORKSPACE_ROOT", "/srv/workspace")
    monkeypatch.setenv("MCP_LOG_LEVEL", "debug")
    monkeypatch.setattr(
        mcp_discovery, "MCPStreamableHTTPTool", _CapturedMCPStreamableHTTPTool
    )
    # Intentionally leave UNSET_API_KEY unset to confirm it stays literal.

    # The agent file itself should also load cleanly through the loader.
    specs = load_agent_specs(fixture, strict=True)
    assert len(specs) == 1
    assert specs[0].name == "MCP Consumer"
    assert specs[0].is_main is True

    clear_mcp_cache()
    try:
        servers = discover_mcp_servers(fixture)
    finally:
        clear_mcp_cache()

    assert set(servers) == {"github", "internal"}

    github = servers["github"]
    assert isinstance(github, _CapturedMCPStreamableHTTPTool)
    assert github.url == "https://api.githubcopilot.com/mcp/"
    assert github.allowed_tools == ["search_issues", "list_pull_requests"]
    header_provider = github.header_provider
    assert callable(header_provider)
    headers = header_provider(None)
    assert headers == {
        "Authorization": "Bearer ghp_live_token",
        "X-Tenant": "contoso",
    }

    internal = servers["internal"]
    assert isinstance(internal, _CapturedMCPStreamableHTTPTool)
    # Both $VAR and %VAR% styles are substituted in the URL.
    assert internal.url == "https://debug.internal.example.test//srv/workspace"
    # allowed_tools is None when the original config omits a "tools" entry.
    assert internal.allowed_tools is None
    internal_headers = internal.header_provider(None)
    # Resolved values flow through, unresolved placeholders stay literal.
    assert internal_headers == {
        "X-Node": "/usr/local/bin/node",
        "X-API-Key": "$UNSET_API_KEY",
    }


# ---------------------------------------------------------------------------
# 12 — escaped placeholders stay literal while substitution remains enabled
# ---------------------------------------------------------------------------


def test_escaped_placeholders_preserve_literal_sigils(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FIXTURES_ROOT / "12_escaped_placeholders"

    monkeypatch.setenv("AGENT_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("TEAM", "platform")
    monkeypatch.setenv("CONTACT_EMAIL", "ops@contoso.test")
    monkeypatch.setenv("API_TOKEN", "leaked-token")
    monkeypatch.setenv("TENANT_ID", "tenant-live")

    specs = load_agent_specs(fixture, strict=True)

    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "Escaped Literals"
    assert spec.description == "Keep $API_TOKEN and %TENANT_ID% literal for platform."
    assert spec.agent_configuration is not None
    assert spec.agent_configuration["openai"]["model"] == "gpt-4o-mini"
    assert spec.metadata is not None
    assert spec.metadata["literal_dollar"] == "$API_TOKEN"
    assert spec.metadata["literal_percent"] == "%TENANT_ID%"
    assert spec.metadata["mixed"] == "team-platform-uses-$API_TOKEN-and-%TENANT_ID%"

    body = spec.instructions
    assert "Render literal examples: $API_TOKEN and %TENANT_ID%." in body
    assert "Still resolve normal placeholders: model gpt-4o-mini, contact ops@contoso.test." in body
    assert "leaked-token" not in body
    assert "tenant-live" not in body


# ---------------------------------------------------------------------------
# 13 — agent_configuration supports all provider-specific sub-blocks
# ---------------------------------------------------------------------------


def test_agent_configuration_providers_fixture() -> None:
    fixture = FIXTURES_ROOT / "13_agent_configuration_providers"

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)

    assert global_config.agent_configuration is None

    by_name = _specs_by_name(specs)
    assert set(by_name) == {
        "Azure OpenAI Provider Agent",
        "Foundry Provider Agent",
        "OpenAI Provider Agent",
    }

    openai = compose(
        by_name["OpenAI Provider Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert openai.provider == "openai"
    assert openai.temperature == 0.2
    assert openai.top_p == 0.9
    assert openai.max_tokens == 256
    assert isinstance(openai.openai, OpenAIConfig)
    assert openai.openai.model == "gpt-4.1-mini"
    assert openai.openai.base_url == "https://api.openai.example.test/v1"

    azure_openai = compose(
        by_name["Azure OpenAI Provider Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert azure_openai.provider == "azure_openai"
    assert azure_openai.temperature == 0.4
    assert azure_openai.top_p == 0.85
    assert azure_openai.max_tokens == 512
    assert isinstance(azure_openai.azure_openai, AzureOpenAIConfig)
    assert azure_openai.azure_openai.model == "gpt-4.1"
    assert azure_openai.azure_openai.azure_endpoint == "https://azure-openai.example.test"
    assert azure_openai.azure_openai.api_version == "2024-10-21"
    assert azure_openai.azure_openai.api_key == "azure-openai-key"

    foundry = compose(
        by_name["Foundry Provider Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert foundry.provider == "foundry"
    assert isinstance(foundry.foundry, FoundryConfig)
    assert foundry.foundry.model == "gpt-4.1-nano"
    assert foundry.foundry.project_endpoint == "https://foundry.example.test/api/projects/demo"


# ---------------------------------------------------------------------------
# 14 — managed identity fields parse cleanly for Azure OpenAI and Foundry
# ---------------------------------------------------------------------------


def test_managed_identity_auth_fixture() -> None:
    fixture = FIXTURES_ROOT / "14_managed_identity_auth"

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)

    assert global_config.agent_configuration is None

    by_name = _specs_by_name(specs)
    assert set(by_name) == {
        "Azure OpenAI API Key Agent",
        "Azure OpenAI System MI Agent",
        "Azure OpenAI User Assigned MI Agent",
        "Foundry System MI Agent",
        "Foundry User Assigned MI Agent",
    }

    azure_api_key = compose(
        by_name["Azure OpenAI API Key Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert isinstance(azure_api_key.azure_openai, AzureOpenAIConfig)
    assert azure_api_key.azure_openai.api_key == "live-api-key"
    assert azure_api_key.azure_openai.managed_identity_client_id is None

    azure_user_assigned = compose(
        by_name["Azure OpenAI User Assigned MI Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert isinstance(azure_user_assigned.azure_openai, AzureOpenAIConfig)
    assert azure_user_assigned.azure_openai.api_key is None
    assert (
        azure_user_assigned.azure_openai.managed_identity_client_id
        == "11111111-1111-1111-1111-111111111111"
    )

    azure_system_default = compose(
        by_name["Azure OpenAI System MI Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert isinstance(azure_system_default.azure_openai, AzureOpenAIConfig)
    assert azure_system_default.azure_openai.api_key is None
    assert azure_system_default.azure_openai.managed_identity_client_id is None

    foundry_user_assigned = compose(
        by_name["Foundry User Assigned MI Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert isinstance(foundry_user_assigned.foundry, FoundryConfig)
    assert (
        foundry_user_assigned.foundry.managed_identity_client_id
        == "22222222-2222-2222-2222-222222222222"
    )

    foundry_system_default = compose(
        by_name["Foundry System MI Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert isinstance(foundry_system_default.foundry, FoundryConfig)
    assert foundry_system_default.foundry.managed_identity_client_id is None


# ---------------------------------------------------------------------------
# 15 — invalid provider declarations produce targeted loader errors
# ---------------------------------------------------------------------------


def test_invalid_agent_configurations_fixture() -> None:
    fixture = FIXTURES_ROOT / "15_agent_configuration_invalid"

    expected_substrings = {
        "multiple_provider_sub_blocks.agent.md": [
            "unrelated provider sub-block",
            "azure_openai",
        ],
        "azure_openai_mutual_exclusivity.agent.md": [
            "managed_identity_client_id",
            "api_key",
        ],
        "credential_extra_passthrough.agent.md": ["credential"],
        "unknown_provider.agent.md": ["Unknown provider", "cohere"],
    }

    global_config = load_global_config(fixture)
    specs = _specs_by_name(load_agent_specs(fixture, strict=True))

    assert set(specs) == {
        "Azure OpenAI Mutual Exclusivity",
        "Credential Extra Passthrough",
        "Multiple Provider Sub-blocks",
        "Unknown Provider",
    }

    for filename, substrings in expected_substrings.items():
        spec = next(
            value
            for value in specs.values()
            if value.source_file is not None and value.source_file.endswith(filename)
        )
        with pytest.raises(ValueError) as exc_info:
            compose(spec, global_config, discovered_mcp_names=[], discovered_skill_names=[])

        message = str(exc_info.value)
        for substring in substrings:
            assert substring in message


def test_post_merge_validation_errors_do_not_chain_or_log_secrets(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "VERY_SECRET_KEY_VALUE_DO_NOT_LEAK"
    fixture = FIXTURES_ROOT / "15_agent_configuration_invalid"
    source_file = fixture / "azure_openai_mutual_exclusivity.agent.md"
    secret_post = SimpleNamespace(
        metadata={
            "name": "Azure OpenAI Mutual Exclusivity",
            "description": "Invalid because api_key and managed_identity_client_id are both set.",
            "agent_configuration": {
                "provider": "azure_openai",
                "azure_openai": {
                    "model": "gpt-4.1",
                    "azure_endpoint": "https://azure-openai.example.test",
                    "api_version": "2024-10-21",
                    "api_key": secret,
                    "managed_identity_client_id": "33333333-3333-3333-3333-333333333333",
                },
            },
        },
        content="This fixture should fail validation because Azure OpenAI auth modes are mutually exclusive.\n",
    )
    monkeypatch.setattr(
        "azure_functions_agents.config.loader.frontmatter.load",
        lambda _: secret_post,
    )

    spec = _load_agent_spec(source_file)

    with pytest.raises(ValueError) as exc_info:
        compose(spec, GlobalConfig(), discovered_mcp_names=[], discovered_skill_names=[])

    exc = exc_info.value
    assert exc.__cause__ is None
    assert secret not in repr(exc)
    assert secret not in str(exc)
    if exc.__context__ is not None:
        assert secret not in repr(exc.__context__)
        assert secret not in str(exc.__context__)
    assert secret not in "".join(traceback.format_exception(exc))

    caplog.set_level(logging.ERROR)
    try:
        compose(spec, GlobalConfig(), discovered_mcp_names=[], discovered_skill_names=[])
    except ValueError:
        logging.getLogger(__name__).exception("compose failed")
    assert secret not in caplog.text


# ---------------------------------------------------------------------------
# 16 — partial overrides preserve inherited siblings after composition
# ---------------------------------------------------------------------------


def test_partial_overrides_fixture() -> None:
    fixture = FIXTURES_ROOT / "16_partial_overrides"

    global_config = load_global_config(fixture)
    specs = _specs_by_name(load_agent_specs(fixture, strict=True))

    endpoint_only = compose(
        specs["Endpoint Override Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert endpoint_only.azure_openai is not None
    assert endpoint_only.azure_openai.azure_endpoint == "https://override-azure.example.test"
    assert endpoint_only.azure_openai.api_version == "2024-10-21"
    assert endpoint_only.azure_openai.model == "gpt-4o-mini"

    top_level_model_only = compose(
        specs["Top Level Model Override Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert top_level_model_only.model == "gpt-4o-mini"
    assert top_level_model_only.azure_openai is not None
    assert top_level_model_only.azure_openai.model == "gpt-4o-mini"

    subblock_model_only = compose(
        specs["Sub-block Model Override Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert subblock_model_only.azure_openai is not None
    assert subblock_model_only.azure_openai.model == "gpt-4.1-mini"
    assert subblock_model_only.azure_openai.azure_endpoint == "https://global-azure.example.test"

    timeout_only = compose(
        specs["Timeout Override Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    ).agent_configuration
    assert timeout_only.timeout == 30
    assert timeout_only.azure_openai is not None
    assert timeout_only.azure_openai.api_version == "2024-10-21"


# ---------------------------------------------------------------------------
# 17 — explicit null semantics for unsetting inherited values
# ---------------------------------------------------------------------------


def test_unset_semantics_fixture() -> None:
    fixture = FIXTURES_ROOT / "17_unset_semantics"

    global_config = load_global_config(fixture)
    specs = _specs_by_name(load_agent_specs(fixture, strict=True))

    api_key_unset = compose(
        specs["Unset API Key Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )
    assert api_key_unset.agent_configuration.azure_openai is not None
    assert api_key_unset.agent_configuration.azure_openai.api_key is None
    assert (
        api_key_unset.agent_configuration.azure_openai.managed_identity_client_id == "cid"
    )

    top_level_model_unset = compose(
        specs["Top Level Model Unset Agent"],
        global_config,
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )
    assert top_level_model_unset.agent_configuration.model is None
    assert top_level_model_unset.agent_configuration.azure_openai is not None
    assert top_level_model_unset.agent_configuration.azure_openai.model == "gpt-4o-mini"

    with pytest.raises(
        ValueError,
        match=r"agent_configuration\.model.*agent_configuration\.azure_openai\.model",
    ):
        compose(
            specs["Both Models Unset Agent"],
            global_config,
            discovered_mcp_names=[],
            discovered_skill_names=[],
        )
