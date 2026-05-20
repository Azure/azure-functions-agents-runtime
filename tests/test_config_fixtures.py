"""Integration-style tests that exercise loader behavior against on-disk fixtures.

Each fixture under ``tests/fixtures/config_scenarios/`` represents a realistic
combination of ``agents.config.yaml`` and ``*.agent.md`` files. These tests load
them through the public API (``load_global_config``/``load_agent_specs``) and
assert the parsed configuration matches what the fixtures advertise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_framework import MCPStdioTool

import azure_functions_agents.discovery.mcp as mcp_discovery
from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.config.schema import (
    DebugConfig,
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

    assert global_config.model is None
    assert global_config.timeout is None
    assert global_config.system_tools is None
    assert global_config.tools is None

    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "Minimal Assistant"
    assert spec.is_main is True
    assert spec.trigger is None
    assert spec.debug is None
    assert spec.model is None
    assert "helpful assistant" in spec.instructions
    assert spec.substitute_variables is True


# ---------------------------------------------------------------------------
# 02 — global defaults inherited by a bare main agent
# ---------------------------------------------------------------------------


def test_global_defaults_inherited() -> None:
    fixture = FIXTURES_ROOT / "02_global_defaults"

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)

    assert global_config.model == "gpt-4o"
    assert global_config.timeout == 900
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
    assert spec.model is None  # inherits global model at resolve time
    assert spec.timeout is None


# ---------------------------------------------------------------------------
# 03 — env substitution across global and agent files
# ---------------------------------------------------------------------------


def test_env_substitution_resolves_known_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = FIXTURES_ROOT / "03_env_substitution"

    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    monkeypatch.setenv("ACA_SESSION_POOL_ENDPOINT", "https://pool.contoso.test")
    monkeypatch.setenv("SUBSCRIPTION_ID", "sub-123")
    monkeypatch.setenv("TO_EMAIL", "alerts@contoso.test")
    # Intentionally NOT setting AGENT_MODEL_OVERRIDE so it stays literal.

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)

    assert global_config.model == "gpt-4o-mini"
    assert global_config.timeout == 600
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
    # Unset env var should remain literal.
    assert spec.model == "$AGENT_MODEL_OVERRIDE"
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
    assert spec.model == "$AGENT_MODEL"
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

    assert global_config.model == "gpt-4o"
    assert global_config.timeout == 300

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

    assert global_config.model == "gpt-4o"
    assert global_config.timeout == 900
    assert global_config.tools is not None
    assert isinstance(global_config.tools, ToolsFilter)
    assert global_config.tools.exclude == ["bash", "execute_shell"]
    assert global_config.tools.custom_only is False

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
    assert selective.tools.custom_only is True


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

    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    monkeypatch.setenv("ACA_SESSION_POOL_ENDPOINT", "https://pool.example.test")
    monkeypatch.setenv("GITHUB_CONNECTION_ID", "conn-gh")
    monkeypatch.setenv("SERVICENOW_CONNECTION_ID", "conn-sn")
    monkeypatch.setenv("REGION", "westus3")
    monkeypatch.setenv("TENANT", "tenant-xyz")

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)

    assert global_config.model == "gpt-4o"
    assert global_config.timeout == 1200
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
# 11 — .vscode/mcp.json env-var substitution
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

    assert set(servers) == {"github", "filesystem"}

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

    filesystem = servers["filesystem"]
    assert isinstance(filesystem, MCPStdioTool)
    assert filesystem.command == "/usr/local/bin/node"
    assert filesystem.args == [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/srv/workspace",
    ]
    # allowed_tools is None when the original config used the "*" wildcard.
    assert filesystem.allowed_tools is None
    # Resolved values flow through, unresolved placeholders stay literal.
    assert filesystem.env == {
        "LOG_LEVEL": "debug",
        "API_KEY": "$UNSET_API_KEY",
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
    assert spec.model == "gpt-4o-mini"
    assert spec.metadata is not None
    assert spec.metadata["literal_dollar"] == "$API_TOKEN"
    assert spec.metadata["literal_percent"] == "%TENANT_ID%"
    assert spec.metadata["mixed"] == "team-platform-uses-$API_TOKEN-and-%TENANT_ID%"

    body = spec.instructions
    assert "Render literal examples: $API_TOKEN and %TENANT_ID%." in body
    assert "Still resolve normal placeholders: model gpt-4o-mini, contact ops@contoso.test." in body
    assert "leaked-token" not in body
    assert "tenant-live" not in body
