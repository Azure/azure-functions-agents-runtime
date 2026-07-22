from __future__ import annotations

import logging
from pathlib import Path

import pytest

from azure_functions_agents.config.merge import (
    DEFAULT_TIMEOUT,
    _resolve_builtin_endpoints,
    _resolve_harness,
    _resolve_model,
    _resolve_sandbox,
    _resolve_timeout,
    _resolve_web_request,
    apply_mcp_filter,
    apply_skills_filter,
    apply_tools_filter,
    compose,
)
from azure_functions_agents.config.schema import (
    AgentSpec,
    BuiltinEndpointsConfig,
    DynamicSessionsCodeInterpreterConfig,
    EndpointAuthConfig,
    GlobalConfig,
    HarnessAgentConfig,
    McpFilter,
    SkillsFilter,
    SubagentRef,
    SystemToolsAgentOverride,
    SystemToolsConfig,
    ToolsFilter,
    TriggerSpec,
    WebRequestConfig,
)
from azure_functions_agents.config.validation import validate_resolved_agent


def test_resolve_model_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "env-model")
    global_config = GlobalConfig(model="global-model")
    assert (
        _resolve_model(AgentSpec(name="A", description="B", model="agent-model"), global_config)
        == "agent-model"
    )
    assert _resolve_model(AgentSpec(name="A", description="B"), global_config) == "global-model"
    assert _resolve_model(AgentSpec(name="A", description="B"), GlobalConfig()) == "env-model"
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_MODEL", raising=False)
    assert _resolve_model(AgentSpec(name="A", description="B"), GlobalConfig()) is None


def test_resolve_timeout_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS", "33")
    global_config = GlobalConfig(timeout=22)
    assert _resolve_timeout(AgentSpec(name="A", description="B", timeout=11), global_config) == 11
    assert _resolve_timeout(AgentSpec(name="A", description="B"), global_config) == 22
    assert _resolve_timeout(AgentSpec(name="A", description="B"), GlobalConfig()) == 33
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS", raising=False)
    assert _resolve_timeout(AgentSpec(name="A", description="B"), GlobalConfig()) == DEFAULT_TIMEOUT


def test_resolve_builtin_endpoints() -> None:
    empty = GlobalConfig()
    assert _resolve_builtin_endpoints(
        AgentSpec(name="A", description="B", is_main=True), empty
    ) == BuiltinEndpointsConfig()
    assert _resolve_builtin_endpoints(
        AgentSpec(name="A", description="B", is_main=False), empty
    ) == BuiltinEndpointsConfig()
    assert _resolve_builtin_endpoints(
        AgentSpec(name="A", description="B", builtin_endpoints=True), empty
    ) == BuiltinEndpointsConfig(debug_chat_ui=True, chat_api=True, mcp=True)
    assert _resolve_builtin_endpoints(
        AgentSpec(name="A", description="B", builtin_endpoints=BuiltinEndpointsConfig(chat_api=True)),
        empty,
    ) == BuiltinEndpointsConfig(chat_api=True)


def test_resolve_builtin_endpoints_shorthand_is_not_main_special_cased() -> None:
    assert _resolve_builtin_endpoints(
        AgentSpec(name="A", description="B", builtin_endpoints=True, is_main=True), GlobalConfig()
    ) == BuiltinEndpointsConfig(debug_chat_ui=True, chat_api=True, mcp=True)


def test_app_wide_auth_is_inherited_by_agents() -> None:
    """A top-level agents.config.yaml `http_auth` becomes each agent's default."""
    global_config = GlobalConfig(http_auth=EndpointAuthConfig(mode="entra"))
    resolved = _resolve_builtin_endpoints(
        AgentSpec(name="A", description="B", builtin_endpoints=BuiltinEndpointsConfig(chat_api=True)),
        global_config,
    )
    assert resolved.http_auth.mode == "entra"


def test_app_wide_auth_inherited_for_shorthand_builtin_endpoints() -> None:
    """`builtin_endpoints: true` still inherits the app-wide auth default."""
    global_config = GlobalConfig(http_auth=EndpointAuthConfig(mode="anonymous"))
    resolved = _resolve_builtin_endpoints(
        AgentSpec(name="A", description="B", builtin_endpoints=True), global_config
    )
    assert resolved.http_auth.mode == "anonymous"


def test_app_wide_auth_shorthand_string_is_coerced() -> None:
    """A bare-string `http_auth: entra` at the global level is coerced and inherited."""
    global_config = GlobalConfig.model_validate({"http_auth": "admin"})
    resolved = _resolve_builtin_endpoints(
        AgentSpec(name="A", description="B", builtin_endpoints=True), global_config
    )
    assert resolved.http_auth.mode == "admin"


def test_per_agent_auth_overrides_app_wide_default() -> None:
    """An explicit per-agent auth wins over the app-wide default, even if weaker."""
    global_config = GlobalConfig(http_auth=EndpointAuthConfig(mode="entra"))
    spec = AgentSpec.model_validate(
        {
            "name": "A",
            "description": "B",
            "builtin_endpoints": {"chat_api": True, "http_auth": "function"},
        }
    )
    resolved = _resolve_builtin_endpoints(spec, global_config)
    assert resolved.http_auth.mode == "function"


def test_no_app_wide_auth_keeps_default_function() -> None:
    """Without a global auth, agents keep the built-in `function` default."""
    resolved = _resolve_builtin_endpoints(
        AgentSpec(name="A", description="B", builtin_endpoints=True), GlobalConfig()
    )
    assert resolved.http_auth.mode == "function"


def test_resolve_sandbox() -> None:
    global_config = GlobalConfig(
        system_tools=SystemToolsConfig(
            dynamic_sessions_code_interpreter=DynamicSessionsCodeInterpreterConfig(
                endpoint="https://example.test"
            )
        )
    )
    assert global_config.system_tools is not None
    assert (
        _resolve_sandbox(AgentSpec(name="A", description="B"), global_config)
        == global_config.system_tools.dynamic_sessions_code_interpreter
    )
    assert (
        _resolve_sandbox(
            AgentSpec(
                name="A",
                description="B",
                system_tools=SystemToolsAgentOverride(dynamic_sessions_code_interpreter=False),
            ),
            global_config,
        )
        is None
    )


def test_apply_mcp_filter() -> None:
    assert apply_mcp_filter(["a", "b"], False) == ([], True)
    assert apply_mcp_filter(["a", "b"], None) == (["a", "b"], False)
    assert apply_mcp_filter(["a", "b"], McpFilter(exclude=["b"])) == (["a"], False)


def test_apply_skills_filter() -> None:
    assert apply_skills_filter(["a", "b"], False) == ([], True)
    assert apply_skills_filter(["a", "b"], None) == (["a", "b"], False)
    assert apply_skills_filter(["a", "b"], SkillsFilter(exclude=["b"])) == (["a"], False)


def test_apply_tools_filter() -> None:
    global_filter = ToolsFilter(exclude=["a"])
    assert apply_tools_filter(False, global_filter) == (ToolsFilter(), True)
    effective, disabled = apply_tools_filter(
        ToolsFilter(exclude=["b"]),
        global_filter,
    )
    assert disabled is False
    assert effective.exclude == ["a", "b"]


def test_compose_end_to_end() -> None:
    global_config = GlobalConfig(
        model="global-model",
        timeout=10,
        tools=ToolsFilter(exclude=["danger"]),
        system_tools=SystemToolsConfig(
            dynamic_sessions_code_interpreter=DynamicSessionsCodeInterpreterConfig(
                endpoint="https://example.test"
            ),
        ),
    )
    spec = AgentSpec(
        name="Agent",
        description="desc",
        is_main=False,
        trigger=TriggerSpec(type="timer_trigger", args={"schedule": "0 0 * * * *"}),
        mcp=McpFilter(exclude=["ado"]),
        skills=SkillsFilter(exclude=["secret"]),
        tools=ToolsFilter(exclude=["foo"]),
        metadata={"team": "x"},
        instructions="Do work",
        source_file=str(Path(r"Q:\agent.agent.md")),
    )

    resolved = compose(
        spec,
        global_config,
        discovered_mcp_names=["learn", "ado"],
        discovered_skill_names=["secret", "public"],
    )

    assert global_config.system_tools is not None
    assert resolved.model == "global-model"
    assert resolved.timeout == 10
    assert resolved.enabled_mcp_names == ["learn"]
    assert resolved.enabled_skills_names == ["public"]
    assert resolved.tool_filter.exclude == ["danger", "foo"]
    assert resolved.sandbox_config == global_config.system_tools.dynamic_sessions_code_interpreter
    assert resolved.metadata == {"team": "x"}


def test_compose_copies_logger_into_metadata() -> None:
    resolved = compose(
        AgentSpec(name="Agent", description="desc", logger=False, is_main=True),
        GlobalConfig(),
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )

    assert resolved.metadata["logger"] is False


def test_compose_preserves_substitute_variables_flag() -> None:
    resolved = compose(
        AgentSpec(name="Agent", description="desc", substitute_variables=False, is_main=True),
        GlobalConfig(),
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )

    assert resolved.substitute_variables is False


def test_compose_defers_warning_only_validation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = AgentSpec(
        name="Agent",
        description="desc",
        is_main=True,
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
        skills=SkillsFilter(exclude=["missing-skill"]),
        tools=ToolsFilter(exclude=["bash"]),
    )

    with caplog.at_level(logging.WARNING):
        resolved = compose(
            spec,
            GlobalConfig(),
            discovered_mcp_names=[],
            discovered_skill_names=["known-skill"],
        )

    assert caplog.records == []

    with caplog.at_level(logging.WARNING):
        validate_resolved_agent(
            resolved,
            discovered_mcp_names=[],
            discovered_skills=["known-skill"],
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("skills.exclude" in message for message in messages)
    assert any("tools.exclude" in message for message in messages)


def test_resolve_builtin_endpoints_explicit_false() -> None:
    """Defensive: explicit builtin_endpoints: false returns an all-disabled BuiltinEndpointsConfig
    (keeps built-in endpoints disabled even for main.agent.md)."""
    spec = AgentSpec(name="Main", description="d", builtin_endpoints=False, is_main=True)
    debug = _resolve_builtin_endpoints(spec, GlobalConfig())
    assert debug.debug_chat_ui is False
    assert debug.chat_api is False
    assert debug.mcp is False


def test_resolve_timeout_garbage_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: a non-numeric timeout env var must NOT crash; falls through to the
    framework default."""
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS", "not-a-number")
    spec = AgentSpec(name="A", description="d")
    global_config = GlobalConfig()
    assert _resolve_timeout(spec, global_config) == DEFAULT_TIMEOUT


def test_resolve_sandbox_no_global_returns_none() -> None:
    """Defensive: when the global config has no system_tools block, sandbox is None."""
    spec = AgentSpec(name="A", description="d")
    assert _resolve_sandbox(spec, GlobalConfig()) is None


def test_resolve_web_request_default_on_absent() -> None:
    """Default-on: no system_tools block anywhere -> enabled with default config."""
    spec = AgentSpec(name="A", description="B")
    assert _resolve_web_request(spec, GlobalConfig()) == WebRequestConfig()


def test_resolve_web_request_default_on_global_none() -> None:
    """Default-on: global system_tools present but web_request unset (None) -> still enabled."""
    spec = AgentSpec(name="A", description="B")
    global_config = GlobalConfig(system_tools=SystemToolsConfig())
    assert _resolve_web_request(spec, global_config) == WebRequestConfig()


def test_resolve_web_request_default_on_global_true() -> None:
    """Default-on: global web_request explicitly True -> enabled with default config."""
    spec = AgentSpec(name="A", description="B")
    global_config = GlobalConfig(system_tools=SystemToolsConfig(web_request=True))
    assert _resolve_web_request(spec, global_config) == WebRequestConfig()


def test_resolve_web_request_global_false_disables_app_wide() -> None:
    """global system_tools.web_request: false disables it for every agent."""
    spec = AgentSpec(name="A", description="B")
    global_config = GlobalConfig(system_tools=SystemToolsConfig(web_request=False))
    assert _resolve_web_request(spec, global_config) is None


def test_resolve_web_request_per_agent_false_opts_out() -> None:
    """Per-agent `web_request: false` opts out even when globally enabled."""
    spec = AgentSpec(
        name="A", description="B", system_tools=SystemToolsAgentOverride(web_request=False)
    )
    assert _resolve_web_request(spec, GlobalConfig()) is None


def test_resolve_web_request_per_agent_true_or_absent_inherits_global() -> None:
    """Per-agent `web_request: true`/absent inherits whatever the global config resolves to."""
    global_config = GlobalConfig(
        system_tools=SystemToolsConfig(web_request=WebRequestConfig(require_https=False))
    )
    spec_absent = AgentSpec(name="A", description="B")
    spec_true = AgentSpec(
        name="A", description="B", system_tools=SystemToolsAgentOverride(web_request=True)
    )
    assert _resolve_web_request(spec_absent, global_config) == WebRequestConfig(
        require_https=False
    )
    assert _resolve_web_request(spec_true, global_config) == WebRequestConfig(require_https=False)


def test_compose_derives_slug_from_source_file_stem() -> None:
    """Identity slug = sanitized file stem, same derivation as function/endpoint names (FRD 0007 §4.2)."""
    spec = AgentSpec(
        name="Billing Specialist",
        description="d",
        source_file=str(Path("agents") / "billing-specialist.agent.md"),
    )
    resolved = compose(spec, GlobalConfig(), discovered_mcp_names=[], discovered_skill_names=[])
    assert resolved.slug == "billing_specialist"


def test_compose_slug_matches_function_name_derivation() -> None:
    """The slug must equal exactly what `_naming.py`'s function-name allocator would compute
    for the same source file — this equivalence is load-bearing for FRD 0007 Decision #17."""
    from azure_functions_agents._slug import _function_name_from_source

    spec = AgentSpec(
        name="Weird Name!!",
        description="d",
        source_file=str(Path(r"C:\agents\my-cool.agent.md")),
    )
    resolved = compose(spec, GlobalConfig(), discovered_mcp_names=[], discovered_skill_names=[])
    assert resolved.slug == _function_name_from_source(resolved.source_file, resolved.name)


def test_compose_slug_missing_source_file_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Directly-constructed AgentSpecs (common in tests) may omit source_file; compose() must
    silently fall back rather than warn (validation-time concerns belong elsewhere)."""
    spec = AgentSpec(name="No Source File", description="d")
    with caplog.at_level(logging.WARNING):
        resolved = compose(spec, GlobalConfig(), discovered_mcp_names=[], discovered_skill_names=[])
    assert resolved.slug == "No_Source_File"
    assert caplog.records == []


def test_compose_normalizes_subagents() -> None:
    spec = AgentSpec(
        name="Coordinator",
        description="d",
        subagents=[
            SubagentRef(agent="billing-specialist", when="Billing questions."),
            SubagentRef(agent="shipping-specialist"),
        ],
    )
    resolved = compose(spec, GlobalConfig(), discovered_mcp_names=[], discovered_skill_names=[])
    assert resolved.subagents == [
        SubagentRef(agent="billing-specialist", when="Billing questions."),
        SubagentRef(agent="shipping-specialist"),
    ]


def test_compose_subagents_defaults_to_empty_list() -> None:
    spec = AgentSpec(name="Coordinator", description="d")
    resolved = compose(spec, GlobalConfig(), discovered_mcp_names=[], discovered_skill_names=[])
    assert resolved.subagents == []


def test_compose_normalized_subagents_are_independent_copies() -> None:
    """`compose()` must copy SubagentRef entries, not alias the spec's own list/objects."""
    ref = SubagentRef(agent="billing-specialist")
    spec = AgentSpec(name="Coordinator", description="d", subagents=[ref])
    resolved = compose(spec, GlobalConfig(), discovered_mcp_names=[], discovered_skill_names=[])
    assert resolved.subagents[0] == ref
    assert resolved.subagents[0] is not ref
    assert resolved.subagents is not spec.subagents


def test_resolve_web_request_global_object_is_used_verbatim() -> None:
    """A configured global WebRequestConfig object is returned as-is (not defaulted)."""
    configured = WebRequestConfig(
        allowed_hosts=["api.example.com"],
        timeout_seconds=5,
        max_response_bytes=1000,
        max_request_bytes=500,
    )
    global_config = GlobalConfig(system_tools=SystemToolsConfig(web_request=configured))
    spec = AgentSpec(name="A", description="B")
    assert _resolve_web_request(spec, global_config) is configured


def test_apply_tools_filter_inherits_global_when_agent_unset() -> None:
    """Defensive: when an agent doesn't specify tools, it inherits the global filter as-is."""
    global_filter = ToolsFilter(exclude=["bash"])
    effective, disabled = apply_tools_filter(None, global_filter)
    assert disabled is False
    assert effective.exclude == ["bash"]
    # Returned object is a deep copy — mutating it must not affect the caller's filter
    effective.exclude.append("new")
    assert global_filter.exclude == ["bash"]


def test_apply_tools_filter_true_inherits_global() -> None:
    """`tools: true` shorthand also inherits the global filter."""
    global_filter = ToolsFilter(exclude=["bash"])
    effective, disabled = apply_tools_filter(True, global_filter)
    assert disabled is False
    assert effective.exclude == ["bash"]


def test_apply_tools_filter_no_global_no_agent_returns_empty_filter() -> None:
    """Defensive: when neither agent nor global declares a tools filter, an empty (allow-all)
    filter is returned."""
    effective, disabled = apply_tools_filter(None, None)
    assert disabled is False
    assert effective.exclude == []


# ---------------------------------------------------------------------------
# _resolve_harness
# ---------------------------------------------------------------------------


def test_resolve_harness_default_is_none() -> None:
    """No harness config anywhere → plain Agent path (None)."""
    spec = AgentSpec(name="A", description="B")
    assert _resolve_harness(spec, GlobalConfig()) is None


def test_resolve_harness_global_true_enables_defaults() -> None:
    """global harness: true → HarnessAgentConfig with defaults."""
    spec = AgentSpec(name="A", description="B")
    result = _resolve_harness(spec, GlobalConfig(harness=True))
    assert result == HarnessAgentConfig()


def test_resolve_harness_global_object_preserved() -> None:
    """global harness object fields are returned as-is."""
    cfg = HarnessAgentConfig(max_context_window_tokens=128_000, max_output_tokens=4_096)
    result = _resolve_harness(AgentSpec(name="A", description="B"), GlobalConfig(harness=cfg))
    assert result is cfg


def test_resolve_harness_per_agent_true_overrides_missing_global() -> None:
    """per-agent harness: true enables harness even when global is silent."""
    spec = AgentSpec(name="A", description="B", harness=True)
    result = _resolve_harness(spec, GlobalConfig())
    assert result == HarnessAgentConfig()


def test_resolve_harness_per_agent_object_overrides_global_true() -> None:
    """per-agent harness object takes precedence over global true."""
    agent_cfg = HarnessAgentConfig(max_context_window_tokens=64_000)
    spec = AgentSpec(name="A", description="B", harness=agent_cfg)
    result = _resolve_harness(spec, GlobalConfig(harness=True))
    assert result is agent_cfg


def test_resolve_harness_per_agent_false_opts_out_of_global_true() -> None:
    """per-agent harness: false opts out even when global is enabled."""
    spec = AgentSpec(name="A", description="B", harness=False)
    assert _resolve_harness(spec, GlobalConfig(harness=True)) is None


def test_resolve_harness_global_false_disables_app_wide() -> None:
    """global harness: false disables it for every agent that doesn't explicitly opt in."""
    spec = AgentSpec(name="A", description="B")
    assert _resolve_harness(spec, GlobalConfig(harness=False)) is None


def test_compose_wires_harness_config() -> None:
    """compose() propagates harness_config from _resolve_harness."""
    spec = AgentSpec(name="A", description="desc", harness=True)
    resolved = compose(spec, GlobalConfig())
    assert resolved.harness_config == HarnessAgentConfig()


def test_compose_harness_config_none_by_default() -> None:
    """compose() leaves harness_config as None when harness is not configured."""
    spec = AgentSpec(name="A", description="desc")
    resolved = compose(spec, GlobalConfig())
    assert resolved.harness_config is None


def test_compose_enables_all_discovered_mcp_when_no_per_agent_filter() -> None:
    resolved = compose(
        AgentSpec(name="Agent", description="desc", is_main=True),
        GlobalConfig(),
        discovered_mcp_names=["a", "b"],
        discovered_skill_names=[],
    )

    assert resolved.enabled_mcp_names == ["a", "b"]


def test_compose_disables_mcp_when_agent_sets_mcp_false() -> None:
    resolved = compose(
        AgentSpec(name="Agent", description="desc", is_main=True, mcp=False),
        GlobalConfig(),
        discovered_mcp_names=["a", "b"],
        discovered_skill_names=[],
    )

    assert resolved.enabled_mcp_names == []
    assert resolved.mcp_disabled is True


def test_compose_excludes_specific_mcp_servers() -> None:
    resolved = compose(
        AgentSpec(name="Agent", description="desc", is_main=True, mcp=McpFilter(exclude=["a"])),
        GlobalConfig(),
        discovered_mcp_names=["a", "b", "c"],
        discovered_skill_names=[],
    )

    assert resolved.enabled_mcp_names == ["b", "c"]
    assert resolved.mcp_exclude_names == ["a"]
