"""Integration-style tests that exercise loader behavior against on-disk fixtures.

Each fixture under ``tests/fixtures/config_scenarios/`` represents a realistic
combination of ``agents.config.yaml`` and ``*.agent.md`` files. These tests load
them through the public API (``load_global_config``/``load_agent_specs``) and
assert the parsed configuration matches what the fixtures advertise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import azure_functions_agents.discovery.mcp as mcp_discovery
from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.config.merge import compose
from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    McpFilter,
    SkillsFilter,
    SubagentRef,
    ToolsFilter,
)
from azure_functions_agents.config.validation import (
    validate_resolved_agent,
    validate_subagent_references,
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
        load_tools: bool = True,
        load_prompts: bool = True,
        header_provider: object = None,
        http_client: object = None,
        **_: object,
    ) -> None:
        self.name = name
        self.url = url
        self.allowed_tools = allowed_tools
        self.load_tools = load_tools
        self.load_prompts = load_prompts
        self.header_provider = header_provider
        self.http_client = http_client


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
    assert spec.builtin_endpoints is None
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
    assert global_config.system_tools.dynamic_sessions_code_interpreter is not None
    assert global_config.system_tools.dynamic_sessions_code_interpreter.endpoint == (
        "https://pool.example.test"
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
    assert global_config.system_tools.dynamic_sessions_code_interpreter is not None
    assert global_config.system_tools.dynamic_sessions_code_interpreter.endpoint == (
        "https://pool.contoso.test"
    )

    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "Azure Reporter"
    # description in frontmatter mixes both %VAR% and $VAR styles.
    assert spec.description == ("Reports on subscription sub-123 and emails alerts@contoso.test.")
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

    by_name = _specs_by_name(specs)
    assert set(by_name) == {"Locked Down", "Selective Filters"}

    locked = by_name["Locked Down"]
    assert locked.tools is False
    assert locked.skills is False
    assert locked.mcp is False
    assert locked.system_tools is not None
    assert locked.system_tools.dynamic_sessions_code_interpreter is False
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
# 07 — built-in endpoint variants (none/true/false/object)
# ---------------------------------------------------------------------------


def test_builtin_endpoint_variants() -> None:
    fixture = FIXTURES_ROOT / "07_builtin_endpoints"

    specs = load_agent_specs(fixture, strict=True)
    by_name = _specs_by_name(specs)
    assert set(by_name) == {
        "Builtin Main",
        "Builtin Shorthand On",
        "Builtin Shorthand Off",
        "Builtin Mixed",
    }

    main = by_name["Builtin Main"]
    assert main.is_main is True
    assert main.builtin_endpoints is None

    on = by_name["Builtin Shorthand On"]
    assert on.builtin_endpoints is True
    assert on.trigger is not None and on.trigger.args["route"] == "builtin-on"

    off = by_name["Builtin Shorthand Off"]
    assert off.builtin_endpoints is False

    mixed = by_name["Builtin Mixed"]
    assert isinstance(mixed.builtin_endpoints, BuiltinEndpointsConfig)
    assert mixed.builtin_endpoints.debug_chat_ui is True
    assert mixed.builtin_endpoints.chat_api is True
    assert mixed.builtin_endpoints.mcp is False


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
        spec.response_schema["properties"]["findings"]["items"]["properties"]["severity"]["type"]
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
# 11 — mcp.json env-var substitution
# ---------------------------------------------------------------------------


def test_mcp_json_env_substitution(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = FIXTURES_ROOT / "11_mcp_json_substitution"

    monkeypatch.setenv("GITHUB_MCP_TOKEN", "ghp_live_token")
    monkeypatch.setenv("TENANT_NAME", "contoso")
    monkeypatch.setenv("NODE_BIN", "/usr/local/bin/node")
    monkeypatch.setenv("WORKSPACE_ROOT", "/srv/workspace")
    monkeypatch.setenv("MCP_LOG_LEVEL", "debug")
    monkeypatch.setattr(mcp_discovery, "MCPStreamableHTTPTool", _CapturedMCPStreamableHTTPTool)
    # Intentionally leave UNSET_API_KEY unset to confirm it stays literal.

    # The agent file itself should also load cleanly through the loader.
    specs = load_agent_specs(fixture, strict=True)
    assert len(specs) == 1
    assert specs[0].name == "MCP Consumer"
    assert specs[0].is_main is True

    clear_mcp_cache()
    try:
        result = discover_mcp_servers(fixture)
        servers = result.servers
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


# ---------------------------------------------------------------------------
# 13 — agents/ folder discovery (FRD-0001)
# ---------------------------------------------------------------------------


def test_agents_folder_hybrid_discovery() -> None:
    """Agents from both top-level and agents/ folder are discovered."""
    fixture = FIXTURES_ROOT / "13_agents_folder"

    specs = load_agent_specs(fixture, strict=True)
    by_name = _specs_by_name(specs)

    # Expect 3 agents: 1 top-level (main) + 2 in agents/ folder
    assert len(specs) == 3
    assert set(by_name) == {"Main Agent", "Chat Agent", "Report Agent"}

    # Top-level main.agent.md is marked is_main
    main = by_name["Main Agent"]
    assert main.is_main is True
    # Verify main is NOT in the agents/ subdirectory (parent folder is not "agents")
    assert main.source_file.lower().replace("\\", "/").split("/")[-2] != "agents"

    # Agents in folder have correct source_file paths (parent folder IS "agents")
    chat = by_name["Chat Agent"]
    assert chat.source_file.lower().replace("\\", "/").split("/")[-2] == "agents"
    assert chat.trigger is not None
    assert chat.trigger.type == "http_trigger"

    report = by_name["Report Agent"]
    assert report.source_file.lower().replace("\\", "/").split("/")[-2] == "agents"
    assert report.trigger is not None
    assert report.trigger.type == "timer_trigger"


# ---------------------------------------------------------------------------
# 14 — web_request system tool (default-on, per-agent opt-out)
# ---------------------------------------------------------------------------


def test_web_request_fixture() -> None:
    """Global web_request config is honored by default; an agent can opt out."""
    fixture = FIXTURES_ROOT / "14_web_request"

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)
    by_name = _specs_by_name(specs)

    assert global_config.system_tools is not None
    global_web_request = global_config.system_tools.web_request
    assert global_web_request is not None
    assert global_web_request is not False
    assert global_web_request.allowed_hosts == ["api.example.test"]
    assert global_web_request.timeout_seconds == 10
    assert global_web_request.max_response_bytes == 1000000
    assert global_web_request.max_request_bytes == 200000

    default_spec = by_name["Default Web Agent"]
    resolved_default = compose(default_spec, global_config)
    assert resolved_default.web_request_config is not None
    assert resolved_default.web_request_config.allowed_hosts == ["api.example.test"]

    opted_out_spec = by_name["Opted Out Agent"]
    assert opted_out_spec.system_tools is not None
    assert opted_out_spec.system_tools.web_request is False
    resolved_opted_out = compose(opted_out_spec, global_config)
    assert resolved_opted_out.web_request_config is None


# ---------------------------------------------------------------------------
# 15 — multi-agent delegation: coordinator + specialists via `subagents:`
# (FRD 0006). One specialist (billing) is independently runnable; the other
# (shipping) is endpoint-less and reachable only as an internal specialist.
# ---------------------------------------------------------------------------


def test_multi_agent_delegation_fixture() -> None:
    fixture = FIXTURES_ROOT / "15_multi_agent_delegation"

    global_config = load_global_config(fixture)
    specs = load_agent_specs(fixture, strict=True)
    by_name = _specs_by_name(specs)

    assert len(specs) == 3
    assert set(by_name) == {"Support Coordinator", "Billing Specialist", "Shipping Specialist"}

    coordinator_spec = by_name["Support Coordinator"]
    assert coordinator_spec.subagents == [
        SubagentRef(agent="billing", when="Route billing, invoicing, and payment questions here."),
        SubagentRef(agent="shipping"),
    ]

    coordinator = compose(coordinator_spec, global_config)
    billing = compose(by_name["Billing Specialist"], global_config)
    shipping = compose(by_name["Shipping Specialist"], global_config)

    # Identity slugs are derived from the file stem, independent of display name.
    assert coordinator.slug == "coordinator"
    assert billing.slug == "billing"
    assert shipping.slug == "shipping"
    assert coordinator.subagents == [
        SubagentRef(agent="billing", when="Route billing, invoicing, and payment questions here."),
        SubagentRef(agent="shipping"),
    ]

    known_slugs = {coordinator.slug, billing.slug, shipping.slug}

    # Every subagents: reference on the coordinator resolves to a known,
    # non-self, non-duplicate slug.
    validate_subagent_references(coordinator, known_slugs=known_slugs)
    # Specialists themselves declare no subagents, so this is a no-op for them.
    validate_subagent_references(billing, known_slugs=known_slugs)
    validate_subagent_references(shipping, known_slugs=known_slugs)

    # Coordinator and billing are independently runnable (builtin_endpoints.chat_api).
    validate_resolved_agent(coordinator, discovered_mcp_names=[], discovered_skills=[])
    validate_resolved_agent(billing, discovered_mcp_names=[], discovered_skills=[])

    # Shipping has no trigger and no builtin_endpoints: on its own this is an
    # error, but once it is known to be referenced as a subagent the
    # requirement relaxes (FRD 0006 Decision #18).
    with pytest.raises(ValueError, match="field `trigger`"):
        validate_resolved_agent(shipping, discovered_mcp_names=[], discovered_skills=[])
    validate_resolved_agent(
        shipping,
        discovered_mcp_names=[],
        discovered_skills=[],
        is_referenced_as_subagent=True,
    )
