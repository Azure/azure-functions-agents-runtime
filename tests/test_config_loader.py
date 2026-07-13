from __future__ import annotations

import logging
import re
import textwrap
from pathlib import Path

import pytest

from azure_functions_agents.config.loader import load_agent_specs, load_global_config


def test_load_global_config_valid(tmp_path: Path) -> None:
    (tmp_path / "agents.config.yaml").write_text(
        textwrap.dedent(
            """
            model: gpt-4o
            timeout: 12
            system_tools:
                            dynamic_sessions_code_interpreter:
                                endpoint: https://example.test
            """
        ).strip(),
        encoding="utf-8",
    )

    config = load_global_config(tmp_path)
    assert config.model == "gpt-4o"
    assert config.timeout == 12
    assert config.system_tools is not None
    assert config.system_tools.dynamic_sessions_code_interpreter is not None


def test_load_global_config_resolves_all_string_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MODEL_NAME", "gpt-4.1")
    (tmp_path / "agents.config.yaml").write_text(
        textwrap.dedent(
            """
            model: $MODEL_NAME
            timeout: 600
            """
        ).strip(),
        encoding="utf-8",
    )

    config = load_global_config(tmp_path)
    assert config.model == "gpt-4.1"
    assert config.timeout == 600


def test_load_global_config_leaves_unset_placeholders_literal(tmp_path: Path) -> None:
    (tmp_path / "agents.config.yaml").write_text("model: $MODEL_NAME\n", encoding="utf-8")

    config = load_global_config(tmp_path)
    assert config.model == "$MODEL_NAME"


def test_load_global_config_missing_returns_empty(tmp_path: Path) -> None:
    assert load_global_config(tmp_path) == load_global_config(tmp_path)
    assert load_global_config(tmp_path).model_dump() == {
        "system_tools": None,
        "model": None,
        "timeout": None,
        "tools": None,
    }


def test_load_global_config_malformed_yaml(tmp_path: Path) -> None:
    source = tmp_path / "agents.config.yaml"
    source.write_text("mcp: [oops", encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(str(source))):
        load_global_config(tmp_path)


def test_load_agent_specs_reads_files_and_substitutes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FOO", "VALUE")
    (tmp_path / "main.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Main
            description: Main agent
            ---
            Hello $FOO
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "report.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Report
            description: Report agent
            trigger:
              type: timer_trigger
              args:
                schedule: 0 0 * * * *
            substitute_variables: false
            ---
            Keep $FOO literal
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 2
    main = next(spec for spec in specs if spec.name == "Main")
    report = next(spec for spec in specs if spec.name == "Report")
    assert main.is_main is True
    assert report.is_main is False
    assert main.instructions.strip() == "Hello VALUE"
    assert report.instructions.strip() == "Keep $FOO literal"
    assert report.source_file == str((tmp_path / "report.agent.md").resolve())


def test_load_agent_specs_unknown_field_raises(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.agent.md"
    source.write_text(
        textwrap.dedent(
            """
            ---
            name: Main
            description: Main agent
            unknown_field: true
            ---
            Hello
            """
        ).lstrip(),
        encoding="utf-8",
    )
    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match=r"unknown_field"):
        load_agent_specs(tmp_path, strict=True)

    assert any(
        "frontmatter_validation_error:" in record.getMessage()
        and "field=unknown_field" in record.getMessage()
        and "reason=" in record.getMessage()
        and "schema=aka.ms/agents-front-matter-schema" in record.getMessage()
        for record in caplog.records
    )


def test_load_agent_specs_resolves_frontmatter_strings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENT_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("RESPONSE_TEMPLATE", '{"status":"ok"}')
    (tmp_path / "main.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Main
            description: Main agent
            model: $AGENT_MODEL
            response_example: $RESPONSE_TEMPLATE
            ---
            Hello
            """
        ).lstrip(),
        encoding="utf-8",
    )

    [spec] = load_agent_specs(tmp_path)
    assert spec.model == "gpt-4.1-mini"
    assert spec.response_example == '{"status":"ok"}'


def test_load_agent_specs_substitute_variables_false_skips_frontmatter_and_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENT_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("FOO", "VALUE")
    (tmp_path / "main.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Main
            description: Main agent
            model: $AGENT_MODEL
            substitute_variables: false
            ---
            Keep $FOO literal
            """
        ).lstrip(),
        encoding="utf-8",
    )

    [spec] = load_agent_specs(tmp_path)
    assert spec.model == "$AGENT_MODEL"
    assert spec.substitute_variables is False
    assert spec.instructions.strip() == "Keep $FOO literal"


def test_load_agent_specs_resolves_trigger_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TRIG_TYPE", "http_trigger")
    (tmp_path / "report.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Report
            description: Report agent
            trigger:
              type: $TRIG_TYPE
              args:
                route: report
                methods: ["POST"]
            ---
            body
            """
        ).lstrip(),
        encoding="utf-8",
    )

    [spec] = load_agent_specs(tmp_path)
    assert spec.trigger is not None
    assert spec.trigger.type == "http_trigger"


def test_load_agent_specs_missing_name_raises(tmp_path: Path) -> None:
    source = tmp_path / "main.agent.md"
    source.write_text(
        textwrap.dedent(
            """
            ---
            description: Main agent
            ---
            Hello
            """
        ).lstrip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"name"):
        load_agent_specs(tmp_path, strict=True)


def test_load_global_config_empty_file_returns_empty(tmp_path: Path) -> None:
    """Defensive: an empty agents.config.yaml (only comments / whitespace) is a valid edge
    case and yields an empty GlobalConfig — does not crash on `data is None` from yaml.safe_load."""
    (tmp_path / "agents.config.yaml").write_text("# only comments\n\n", encoding="utf-8")
    config = load_global_config(tmp_path)
    assert config.model is None


def test_load_global_config_non_mapping_root_raises(tmp_path: Path) -> None:
    """Defensive: a YAML file whose root is a list/scalar must produce a clear error."""
    source = tmp_path / "agents.config.yaml"
    source.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_global_config(tmp_path)
    message = str(exc_info.value)
    assert str(source) in message
    assert "mapping" in message
    assert "aka.ms/agents-front-matter-schema" in message


def test_load_global_config_invalid_field_type_raises(tmp_path: Path) -> None:
    """Defensive: pydantic ValidationError on a malformed field surfaces with file path + spec link."""
    source = tmp_path / "agents.config.yaml"
    source.write_text('timeout: "not-a-number"\n', encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_global_config(tmp_path)
    message = str(exc_info.value)
    assert str(source) in message
    assert "timeout" in message
    assert "aka.ms/agents-front-matter-schema" in message


def test_load_global_config_rejects_top_level_mcp_field(tmp_path: Path) -> None:
    """Top-level `mcp:` in agents.config.yaml is no longer a recognized field."""
    source = tmp_path / "agents.config.yaml"
    source.write_text("mcp:\n  - some-server\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_global_config(tmp_path)
    message = str(exc_info.value)
    assert "field `mcp`" in message
    assert "aka.ms/agents-front-matter-schema" in message


def test_load_global_config_resolves_numeric_and_bool_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defensive: resolve_env_vars_in_data passes through non-string scalars untouched."""
    monkeypatch.setenv("ENDPOINT", "https://example.test")
    (tmp_path / "agents.config.yaml").write_text(
        textwrap.dedent(
            """
            timeout: 60
            system_tools:
                            dynamic_sessions_code_interpreter:
                                endpoint: $ENDPOINT
            """
        ).strip(),
        encoding="utf-8",
    )
    config = load_global_config(tmp_path)
    assert config.timeout == 60  # numeric value passed through unchanged
    assert config.system_tools is not None
    assert config.system_tools.dynamic_sessions_code_interpreter is not None
    # Env var resolved on the string field
    assert config.system_tools.dynamic_sessions_code_interpreter.endpoint == "https://example.test"


def test_load_agent_specs_malformed_frontmatter_yaml_raises(tmp_path: Path) -> None:
    """Defensive: malformed YAML *inside* an agent file's frontmatter produces a clear error."""
    source = tmp_path / "main.agent.md"
    source.write_text(
        textwrap.dedent(
            """
            ---
            name: Main
            description: bad
            trigger: [unclosed
            ---
            body
            """
        ).lstrip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc_info:
        load_agent_specs(tmp_path, strict=True)
    message = str(exc_info.value)
    assert str(source) in message
    assert "frontmatter" in message.lower() or "yaml" in message.lower()


def test_load_agent_specs_skips_malformed_file_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    (tmp_path / "main.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Main
            description: Main agent
            ---
            Hello
            """
        ).lstrip(),
        encoding="utf-8",
    )
    bad_source = tmp_path / "broken.agent.md"
    bad_source.write_text(
        textwrap.dedent(
            """
            ---
            name: Broken
            description: bad
            trigger: [unclosed
            ---
            body
            """
        ).lstrip(),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        specs = load_agent_specs(tmp_path)

    assert len(specs) == 1
    assert specs[0].name == "Main"
    assert any(str(bad_source) in record.getMessage() for record in caplog.records)
    assert any(
        "Skipping agent during indexing" in record.getMessage() for record in caplog.records
    )


def test_load_agent_specs_strict_reraises_first_failure(tmp_path: Path) -> None:
    source = tmp_path / "broken.agent.md"
    source.write_text(
        textwrap.dedent(
            """
            ---
            name: Broken
            description: bad
            trigger: [unclosed
            ---
            body
            """
        ).lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=re.escape(str(source))):
        load_agent_specs(tmp_path, strict=True)


def test_load_agent_specs_resolves_strings_passes_through_non_string_scalars(
    tmp_path: Path,
) -> None:
    """Defensive: resolve_env_vars_in_data must passthrough numeric/bool values inside trigger.args
    without crashing (env-var resolution only applies to strings)."""
    (tmp_path / "report.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Report
            description: d
            trigger:
              type: timer_trigger
              args:
                schedule: "0 0 * * * *"
                run_on_start: true
                priority: 5
            ---
            body
            """
        ).lstrip(),
        encoding="utf-8",
    )
    [spec] = load_agent_specs(tmp_path)
    assert spec.trigger is not None
    assert spec.trigger.args["run_on_start"] is True  # bool passthrough
    assert spec.trigger.args["priority"] == 5  # int passthrough
    assert spec.trigger.args["schedule"] == "0 0 * * * *"  # str preserved


# ─────────────────────────────────────────────────────────────────────────────
# agents/ folder discovery tests (FRD-0001)
# ─────────────────────────────────────────────────────────────────────────────


def test_load_agent_specs_from_agents_folder_only(tmp_path: Path) -> None:
    """Agents can be placed exclusively in an agents/ folder."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "chat.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Chat
            description: Chat agent in agents folder
            ---
            You are a chat agent.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Chat"
    assert "agents" in specs[0].source_file


def test_load_agent_specs_hybrid_top_level_and_agents_folder(tmp_path: Path) -> None:
    """Agents from both top-level and agents/ folder are discovered."""
    # Top-level agent
    (tmp_path / "main.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Main
            description: Top-level main agent
            ---
            You are the main agent.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    # agents/ folder agent
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "helper.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Helper
            description: Helper agent in agents folder
            ---
            You are a helper agent.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 2
    names = {spec.name for spec in specs}
    assert names == {"Main", "Helper"}


def test_load_agent_specs_main_in_agents_folder_is_marked_is_main(tmp_path: Path) -> None:
    """main.agent.md in agents/ folder is correctly marked as is_main=True."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "main.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Main
            description: Main agent in agents folder
            ---
            You are the main agent.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Main"
    assert specs[0].is_main is True


def test_load_agent_specs_case_insensitive_agents_folder(tmp_path: Path) -> None:
    """Agents/ (capitalized) folder is also recognized."""
    agents_dir = tmp_path / "Agents"
    agents_dir.mkdir()
    (agents_dir / "chat.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Chat
            description: Chat agent
            ---
            You are a chat agent.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Chat"


def test_load_agent_specs_empty_agents_folder_with_top_level(tmp_path: Path) -> None:
    """Empty agents/ folder doesn't affect top-level agent discovery."""
    # Create empty agents folder
    (tmp_path / "agents").mkdir()

    # Top-level agent
    (tmp_path / "main.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Main
            description: Main agent
            ---
            Hello
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Main"


def test_load_agent_specs_no_agents_anywhere_returns_empty(tmp_path: Path) -> None:
    """No agents at top-level or in agents/ folder returns empty list."""
    # Create empty agents folder
    (tmp_path / "agents").mkdir()

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 0


def test_load_agent_specs_sorting_across_locations(tmp_path: Path) -> None:
    """Agents from both locations are sorted deterministically by path."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    # Create agents with names that would sort differently
    (tmp_path / "zebra.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Zebra
            description: Top-level zebra agent
            ---
            Z
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (agents_dir / "alpha.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Alpha
            description: Alpha agent in folder
            ---
            A
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "beta.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Beta
            description: Top-level beta agent
            ---
            B
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 3
    # Sorted by full path - agents/ folder comes before top-level alphabetically
    names = [spec.name for spec in specs]
    # agents/alpha.agent.md < beta.agent.md < zebra.agent.md (lexicographic by path)
    assert names == ["Alpha", "Beta", "Zebra"]


# bare agent.md discovery tests (flexible naming)
# ─────────────────────────────────────────────────────────────────────────────


def test_load_agent_specs_bare_agent_md_at_top_level(tmp_path: Path) -> None:
    """Bare agent.md at top-level is treated as default.agent.md internally."""
    (tmp_path / "agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Default Agent
            description: Single-agent app using bare agent.md
            ---
            You are the default agent.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Default Agent"
    # Internally normalized to default.agent.md for function naming
    assert "default.agent.md" in specs[0].source_file
    # Bare agent.md is treated as main
    assert specs[0].is_main is True


def test_load_agent_specs_bare_agent_md_uppercase(tmp_path: Path) -> None:
    """Bare AGENT.MD (uppercase) is recognized case-insensitively."""
    (tmp_path / "AGENT.MD").write_text(
        textwrap.dedent(
            """
            ---
            name: Default Agent
            description: Uppercase bare agent file
            ---
            You are the default agent.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Default Agent"
    # Internally normalized to default.agent.md
    assert "default.agent.md" in specs[0].source_file
    assert specs[0].is_main is True


def test_load_agent_specs_bare_agent_md_mixed_case(tmp_path: Path) -> None:
    """Bare Agent.md (mixed case) is recognized case-insensitively."""
    (tmp_path / "Agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Default Agent
            description: Mixed case bare agent file
            ---
            You are the default agent.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Default Agent"
    assert "default.agent.md" in specs[0].source_file
    assert specs[0].is_main is True


def test_load_agent_specs_bare_agent_md_in_agents_folder(tmp_path: Path) -> None:
    """Bare agent.md in agents/ folder is also supported."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Default Agent
            description: Single-agent in agents folder
            ---
            You are the default agent.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Default Agent"
    assert "default.agent.md" in specs[0].source_file
    assert specs[0].is_main is True


def test_load_agent_specs_bare_agent_md_with_other_agents(tmp_path: Path) -> None:
    """Bare agent.md coexists with other named agents."""
    (tmp_path / "agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Default Agent
            description: Default agent
            ---
            Default
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "helper.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Helper
            description: Helper agent
            ---
            Helper
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 2
    names = {spec.name for spec in specs}
    assert names == {"Default Agent", "Helper"}
    # agent.md should be marked as main
    default_spec = next(spec for spec in specs if spec.name == "Default Agent")
    helper_spec = next(spec for spec in specs if spec.name == "Helper")
    assert default_spec.is_main is True
    assert helper_spec.is_main is False


def test_load_agent_specs_bare_agent_md_and_main_both_marked_is_main(tmp_path: Path) -> None:
    """Both bare agent.md and main.agent.md can coexist; both are marked is_main=True."""
    (tmp_path / "agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Default Agent
            description: Default agent
            ---
            Default
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "main.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Main Agent
            description: Main agent
            ---
            Main
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 2
    # Both should be marked as main
    for spec in specs:
        assert spec.is_main is True


# CLAUDE.md discovery tests (flexible naming)
# ─────────────────────────────────────────────────────────────────────────────


def test_load_agent_specs_claude_md_at_top_level(tmp_path: Path) -> None:
    """CLAUDE.md at top-level is treated as default.agent.md internally."""
    (tmp_path / "CLAUDE.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Claude Agent
            description: Single-agent app using CLAUDE.md
            ---
            You are Claude, an AI assistant.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Claude Agent"
    # Internally normalized to default.agent.md for function naming
    assert "default.agent.md" in specs[0].source_file
    # CLAUDE.md is treated as main
    assert specs[0].is_main is True


def test_load_agent_specs_claude_md_lowercase(tmp_path: Path) -> None:
    """claude.md (lowercase) is recognized case-insensitively."""
    (tmp_path / "claude.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Claude Agent
            description: Lowercase CLAUDE file
            ---
            You are Claude, an AI assistant.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Claude Agent"
    # Internally normalized to default.agent.md
    assert "default.agent.md" in specs[0].source_file
    assert specs[0].is_main is True


def test_load_agent_specs_claude_md_mixed_case(tmp_path: Path) -> None:
    """Claude.md (mixed case) is recognized case-insensitively."""
    (tmp_path / "Claude.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Claude Agent
            description: Mixed case CLAUDE file
            ---
            You are Claude, an AI assistant.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Claude Agent"
    assert "default.agent.md" in specs[0].source_file
    assert specs[0].is_main is True


def test_load_agent_specs_claude_md_in_agents_folder(tmp_path: Path) -> None:
    """CLAUDE.md in agents/ folder is also supported."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "CLAUDE.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Claude Agent
            description: Single-agent in agents folder
            ---
            You are Claude, an AI assistant.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Claude Agent"
    assert "default.agent.md" in specs[0].source_file
    assert specs[0].is_main is True


def test_load_agent_specs_claude_md_with_other_agents(tmp_path: Path) -> None:
    """CLAUDE.md coexists with other named agents."""
    (tmp_path / "CLAUDE.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Claude Agent
            description: Claude agent
            ---
            Claude
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "helper.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Helper
            description: Helper agent
            ---
            Helper
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 2
    names = {spec.name for spec in specs}
    assert names == {"Claude Agent", "Helper"}
    # CLAUDE.md should be marked as main
    claude_spec = next(spec for spec in specs if spec.name == "Claude Agent")
    helper_spec = next(spec for spec in specs if spec.name == "Helper")
    assert claude_spec.is_main is True
    assert helper_spec.is_main is False


def test_load_agent_specs_claude_md_and_agent_md_coexist(tmp_path: Path) -> None:
    """Both CLAUDE.md and agent.md can coexist; both are marked is_main=True."""
    (tmp_path / "CLAUDE.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Claude Agent
            description: Claude agent
            ---
            Claude
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Default Agent
            description: Default agent
            ---
            Default
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 2
    # Both should be marked as main
    for spec in specs:
        assert spec.is_main is True


def test_load_agent_specs_claude_md_and_main_both_marked_is_main(tmp_path: Path) -> None:
    """CLAUDE.md and main.agent.md can coexist; both are marked is_main=True."""
    (tmp_path / "CLAUDE.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Claude Agent
            description: Claude agent
            ---
            Claude
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "main.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Main Agent
            description: Main agent
            ---
            Main
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 2
    # Both should be marked as main
    for spec in specs:
        assert spec.is_main is True


# *.claude.md discovery tests (prefix pattern)
# ─────────────────────────────────────────────────────────────────────────────


def test_load_agent_specs_claude_md_prefix_at_top_level(tmp_path: Path) -> None:
    """Files matching *.claude.md pattern are recognized and normalized to *.agent.md."""
    (tmp_path / "report.claude.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Report Agent
            description: Report generation agent
            ---
            You generate reports.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Report Agent"
    # Internally normalized to report.agent.md for function naming
    assert "report.agent.md" in specs[0].source_file
    # Prefix patterns are NOT marked as main (only bare CLAUDE.md, agent.md, main.agent.md)
    assert specs[0].is_main is False


def test_load_agent_specs_claude_md_prefix_case_insensitive(tmp_path: Path) -> None:
    """*.claude.md pattern is case-insensitive."""
    (tmp_path / "Report.Claude.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Report Agent
            description: Report generation agent
            ---
            You generate reports.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Report Agent"
    assert "Report.agent.md" in specs[0].source_file
    assert specs[0].is_main is False


def test_load_agent_specs_claude_md_prefix_in_agents_folder(tmp_path: Path) -> None:
    """*.claude.md files in agents/ folder are also supported."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "summarizer.claude.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Summarizer Agent
            description: Summarization agent
            ---
            You summarize content.
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Summarizer Agent"
    assert "summarizer.agent.md" in specs[0].source_file
    assert specs[0].is_main is False


def test_load_agent_specs_claude_md_prefix_with_other_agents(tmp_path: Path) -> None:
    """*.claude.md files coexist with *.agent.md files."""
    (tmp_path / "report.claude.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Report Agent
            description: Report agent
            ---
            Report
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "chat.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Chat Agent
            description: Chat agent
            ---
            Chat
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 2
    names = {spec.name for spec in specs}
    assert names == {"Report Agent", "Chat Agent"}
    # Both are regular agents, not main
    for spec in specs:
        assert spec.is_main is False


def test_load_agent_specs_claude_md_prefix_with_bare_claude_md(tmp_path: Path) -> None:
    """*.claude.md prefix can coexist with bare CLAUDE.md."""
    (tmp_path / "CLAUDE.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Main Claude Agent
            description: Main agent
            ---
            Main
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "report.claude.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Report Agent
            description: Report agent
            ---
            Report
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 2
    names = {spec.name for spec in specs}
    assert names == {"Main Claude Agent", "Report Agent"}
    # Only bare CLAUDE.md is main
    main_claude = next(spec for spec in specs if spec.name == "Main Claude Agent")
    report = next(spec for spec in specs if spec.name == "Report Agent")
    assert main_claude.is_main is True
    assert report.is_main is False


def test_load_agent_specs_multiple_claude_md_prefix_files(tmp_path: Path) -> None:
    """Multiple *.claude.md files can coexist."""
    (tmp_path / "report.claude.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Report Agent
            description: Report agent
            ---
            Report
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "summarizer.claude.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Summarizer Agent
            description: Summarizer agent
            ---
            Summarize
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "analyzer.claude.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Analyzer Agent
            description: Analyzer agent
            ---
            Analyze
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 3
    names = {spec.name for spec in specs}
    assert names == {"Report Agent", "Summarizer Agent", "Analyzer Agent"}
    # None are main agents
    for spec in specs:
        assert spec.is_main is False


# --- Case-insensitive suffix matching tests (*.AGENT.md, *.CLAUDE.md) ---


def test_load_agent_specs_uppercase_agent_md_suffix(tmp_path: Path) -> None:
    """Files with uppercase .AGENT.md suffix are discovered (case-insensitive)."""
    (tmp_path / "report.AGENT.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Report Agent
            description: Report agent
            ---
            Report
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Report Agent"
    assert "report.agent.md" in str(specs[0].source_file).lower()
    assert specs[0].is_main is False


def test_load_agent_specs_uppercase_claude_md_suffix(tmp_path: Path) -> None:
    """Files with uppercase .CLAUDE.md suffix are discovered (case-insensitive)."""
    (tmp_path / "summarizer.CLAUDE.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Summarizer Agent
            description: Summarizer agent
            ---
            Summarize
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Summarizer Agent"
    assert "summarizer.agent.md" in str(specs[0].source_file).lower()
    assert specs[0].is_main is False


def test_load_agent_specs_mixed_case_agent_md_suffix(tmp_path: Path) -> None:
    """Files with mixed case .Agent.MD suffix are discovered (case-insensitive)."""
    (tmp_path / "data.Agent.MD").write_text(
        textwrap.dedent(
            """
            ---
            name: Data Agent
            description: Data agent
            ---
            Data
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Data Agent"
    assert "data.agent.md" in str(specs[0].source_file).lower()
    assert specs[0].is_main is False


def test_load_agent_specs_mixed_case_claude_md_suffix_in_agents_folder(tmp_path: Path) -> None:
    """Files with mixed case .Claude.MD suffix in agents/ folder are discovered."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "info.Claude.MD").write_text(
        textwrap.dedent(
            """
            ---
            name: Info Agent
            description: Info agent
            ---
            Info
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "Info Agent"
    assert "info.agent.md" in str(specs[0].source_file).lower()
    assert specs[0].is_main is False


def test_load_agent_specs_case_insensitive_suffix_with_lowercase(tmp_path: Path) -> None:
    """Uppercase and lowercase suffix variants coexist (finds both)."""
    (tmp_path / "report.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Report Agent
            description: Report agent
            ---
            Report
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "summary.AGENT.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Summary Agent
            description: Summary agent
            ---
            Summarize
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 2
    names = {spec.name for spec in specs}
    assert names == {"Report Agent", "Summary Agent"}
    for spec in specs:
        assert spec.is_main is False


def test_load_agent_specs_multiple_case_variants_together(tmp_path: Path) -> None:
    """Multiple case variants (*.agent.md, *.AGENT.md, *.claude.md, *.CLAUDE.md) coexist."""
    (tmp_path / "alpha.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Alpha Agent
            description: Alpha agent
            ---
            Alpha
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "beta.AGENT.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Beta Agent
            description: Beta agent
            ---
            Beta
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "gamma.claude.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Gamma Agent
            description: Gamma agent
            ---
            Gamma
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "delta.CLAUDE.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Delta Agent
            description: Delta agent
            ---
            Delta
            """
        ).lstrip(),
        encoding="utf-8",
    )

    specs = load_agent_specs(tmp_path)
    assert len(specs) == 4
    names = {spec.name for spec in specs}
    assert names == {"Alpha Agent", "Beta Agent", "Gamma Agent", "Delta Agent"}
    for spec in specs:
        assert spec.is_main is False

