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
            mcp:
              - learn
            agent-configuration:
              provider: foundry
              endpoint: https://foundry.example.test
              temperature: 0.4
              timeout: 30
            model: gpt-4o
            timeout: 12
            system_tools:
              execute_in_sessions:
                session_pool_management_endpoint: https://example.test
              tools_from_connections:
                - connection_id: conn-1
            """
        ).strip(),
        encoding="utf-8",
    )

    config = load_global_config(tmp_path)
    assert config.model == "gpt-4o"
    assert config.agent_configuration is not None
    assert config.agent_configuration.provider == "foundry"
    assert config.agent_configuration.endpoint == "https://foundry.example.test"
    assert config.agent_configuration.temperature == 0.4
    assert config.agent_configuration.timeout == 30
    assert config.timeout == 12
    assert config.system_tools is not None
    assert config.system_tools.tools_from_connections[0].connection_id == "conn-1"


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
        "agent_configuration": None,
        "endpoint": None,
        "model": None,
        "temperature": None,
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


def test_load_agent_specs_unknown_field_raises(tmp_path: Path) -> None:
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
    with pytest.raises(ValueError, match=r"unknown_field"):
        load_agent_specs(tmp_path, strict=True)


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


def test_load_agent_specs_accepts_endpoint_and_agent_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://foundry.example.test")
    (tmp_path / "main.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Main
            description: Main agent
            endpoint: $PROJECT_ENDPOINT
            agent-configuration:
              provider: foundry
              model: gpt-4.1
              temperature: 0.2
              timeout: 45
            ---
            Hello
            """
        ).lstrip(),
        encoding="utf-8",
    )

    [spec] = load_agent_specs(tmp_path)
    assert spec.endpoint == "https://foundry.example.test"
    assert spec.agent_configuration is not None
    assert spec.agent_configuration.provider == "foundry"
    assert spec.agent_configuration.model == "gpt-4.1"
    assert spec.agent_configuration.temperature == 0.2
    assert spec.agent_configuration.timeout == 45


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
    assert "docs/front-matter-spec.md" in message


def test_load_global_config_invalid_field_type_raises(tmp_path: Path) -> None:
    """Defensive: pydantic ValidationError on a malformed field surfaces with file path + spec link."""
    source = tmp_path / "agents.config.yaml"
    source.write_text('timeout: "not-a-number"\n', encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_global_config(tmp_path)
    message = str(exc_info.value)
    assert str(source) in message
    assert "timeout" in message
    assert "docs/front-matter-spec.md" in message


def test_load_global_config_rejects_top_level_mcp_field(tmp_path: Path) -> None:
    """Top-level `mcp:` in agents.config.yaml is no longer a recognized field."""
    source = tmp_path / "agents.config.yaml"
    source.write_text("mcp:\n  - some-server\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_global_config(tmp_path)
    message = str(exc_info.value)
    assert "field `mcp`" in message
    assert "docs/front-matter-spec.md" in message


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
              execute_in_sessions:
                session_pool_management_endpoint: $ENDPOINT
            """
        ).strip(),
        encoding="utf-8",
    )
    config = load_global_config(tmp_path)
    assert config.timeout == 60  # numeric value passed through unchanged
    assert config.system_tools is not None
    assert config.system_tools.execute_in_sessions is not None
    # Env var resolved on the string field
    assert (
        config.system_tools.execute_in_sessions.session_pool_management_endpoint
        == "https://example.test"
    )


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
    assert any("Skipping malformed agent file" in record.getMessage() for record in caplog.records)


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
