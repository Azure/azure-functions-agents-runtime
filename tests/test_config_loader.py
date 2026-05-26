from __future__ import annotations

import logging
import re
import textwrap
from pathlib import Path

import pytest

from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.config.merge import compose
from azure_functions_agents.config.validation import validate_resolved_agent


def _write_agent(tmp_path: Path, frontmatter: str, body: str = "Hello") -> Path:
    source = tmp_path / "main.agent.md"
    normalized_frontmatter = textwrap.dedent(frontmatter).strip()
    normalized_body = textwrap.dedent(body).strip()
    source.write_text(
        f"---\n{normalized_frontmatter}\n---\n{normalized_body}\n",
        encoding="utf-8",
    )
    return source


def test_load_global_config_valid(tmp_path: Path) -> None:
    (tmp_path / "agents.config.yaml").write_text(
        textwrap.dedent(
            """
            agent_configuration:
              provider: openai
              model: gpt-4o
              timeout: 30
              temperature: 0.4
              top_p: 0.9
              max_tokens: 256
              openai:
                base_url: https://openai.example.test
                organization: contoso
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

    assert config.agent_configuration is not None
    assert config.agent_configuration.provider == "openai"
    assert config.agent_configuration.model == "gpt-4o"
    assert config.agent_configuration.timeout == 30
    assert config.agent_configuration.temperature == 0.4
    assert config.agent_configuration.top_p == 0.9
    assert config.agent_configuration.max_tokens == 256
    assert config.agent_configuration.openai is not None
    assert config.agent_configuration.openai.base_url == "https://openai.example.test"
    assert config.agent_configuration.openai.model_dump()["organization"] == "contoso"
    assert config.system_tools is not None
    assert config.system_tools.tools_from_connections[0].connection_id == "conn-1"


@pytest.mark.parametrize(
    ("provider", "model", "provider_block", "extra_field", "expected_extra"),
    [
        (
            "openai",
            "gpt-4o",
            """
            openai:
              base_url: https://openai.example.test
              organization: contoso
            """,
            "organization",
            "contoso",
        ),
        (
            "azure_openai",
            "gpt-4o-mini",
            """
            azure_openai:
              azure_endpoint: https://azure-openai.example.test
              api_version: "2024-10-21"
              audience: agents
            """,
            "audience",
            "agents",
        ),
        (
            "foundry",
            "gpt-4.1",
            """
            foundry:
              project_endpoint: https://foundry.example.test
              audience: agents
            """,
            "audience",
            "agents",
        ),
    ],
)
def test_load_global_config_parses_provider_sub_blocks_and_extras(
    tmp_path: Path,
    provider: str,
    model: str,
    provider_block: str,
    extra_field: str,
    expected_extra: str,
) -> None:
    (tmp_path / "agents.config.yaml").write_text(
        (
            "agent_configuration:\n"
            f"  provider: {provider}\n"
            f"  model: {model}\n"
            f"{textwrap.indent(textwrap.dedent(provider_block).strip(), '  ')}\n"
        ),
        encoding="utf-8",
    )

    config = load_global_config(tmp_path)

    assert config.agent_configuration is not None
    assert config.agent_configuration.model == model
    provider_config = config.agent_configuration.provider_config
    assert provider_config.model_dump()[extra_field] == expected_extra


def test_load_global_config_resolves_api_key_env_var_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret")
    (tmp_path / "agents.config.yaml").write_text(
        textwrap.dedent(
            """
            agent_configuration:
              provider: openai
              model: gpt-4.1
              openai:
                api_key: $OPENAI_API_KEY
            """
        ).strip(),
        encoding="utf-8",
    )

    config = load_global_config(tmp_path)

    assert config.agent_configuration is not None
    assert config.agent_configuration.openai is not None
    assert config.agent_configuration.openai.api_key == "super-secret"


def test_load_global_config_leaves_unset_placeholders_literal(tmp_path: Path) -> None:
    (tmp_path / "agents.config.yaml").write_text(
        textwrap.dedent(
            """
            agent_configuration:
              provider: openai
              model: $MODEL_NAME
              openai: {}
            """
        ).strip(),
        encoding="utf-8",
    )

    config = load_global_config(tmp_path)

    assert config.agent_configuration is not None
    assert config.agent_configuration.model == "$MODEL_NAME"


def test_load_global_config_rejects_missing_azure_endpoint_at_load_time(
    tmp_path: Path,
) -> None:
    source = tmp_path / "agents.config.yaml"
    source.write_text(
        textwrap.dedent(
            """
            agent_configuration:
              provider: azure_openai
              model: gpt-4o
              azure_openai:
                api_version: "2024-10-21"
            """
        ).strip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"agent_configuration\.azure_openai\.azure_endpoint must be set",
    ):
        load_global_config(tmp_path)


def test_load_global_config_rejects_missing_project_endpoint_for_foundry_at_load_time(
    tmp_path: Path,
) -> None:
    source = tmp_path / "agents.config.yaml"
    source.write_text(
        textwrap.dedent(
            """
            agent_configuration:
              provider: foundry
              model: gpt-4o
              foundry: {}
            """
        ).strip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"agent_configuration\.foundry\.project_endpoint must be set",
    ):
        load_global_config(tmp_path)


def test_load_global_config_missing_returns_empty(tmp_path: Path) -> None:
    assert load_global_config(tmp_path) == load_global_config(tmp_path)
    assert load_global_config(tmp_path).model_dump() == {
        "system_tools": None,
        "agent_configuration": None,
        "tools": None,
    }


def test_load_global_config_malformed_yaml(tmp_path: Path) -> None:
    source = tmp_path / "agents.config.yaml"
    source.write_text("agent_configuration: [oops", encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(str(source))):
        load_global_config(tmp_path)


def test_load_agent_specs_reads_files_and_substitutes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FOO", "VALUE")
    _write_agent(
        tmp_path,
        """
        name: Main
        description: Main agent
        """,
        "Hello $FOO",
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
    _write_agent(
        tmp_path,
        """
        name: Main
        description: Main agent
        unknown_field: true
        """,
    )

    with pytest.raises(ValueError, match=r"unknown_field"):
        load_agent_specs(tmp_path, strict=True)


def test_load_agent_specs_resolves_frontmatter_strings_in_agent_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENT_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("RESPONSE_TEMPLATE", '{"status":"ok"}')
    _write_agent(
        tmp_path,
        """
        name: Main
        description: Main agent
        agent_configuration:
          provider: openai
          model: $AGENT_MODEL
          openai: {}
        response_example: $RESPONSE_TEMPLATE
        """,
    )

    [spec] = load_agent_specs(tmp_path)
    assert spec.agent_configuration is not None
    assert spec.agent_configuration["model"] == "gpt-4.1-mini"
    assert spec.response_example == '{"status":"ok"}'


def test_load_agent_specs_parses_provider_sub_block_and_extras(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        """
        name: Main
        description: Main agent
        agent_configuration:
          provider: azure_openai
          model: gpt-4.1
          timeout: 45
          azure_openai:
            azure_endpoint: https://azure-openai.example.test
            api_version: "2024-10-21"
            audience: agents
        """,
    )

    [spec] = load_agent_specs(tmp_path)

    assert spec.agent_configuration is not None
    assert spec.agent_configuration["provider"] == "azure_openai"
    assert spec.agent_configuration["model"] == "gpt-4.1"
    assert spec.agent_configuration["timeout"] == 45
    assert (
        spec.agent_configuration["azure_openai"]["azure_endpoint"]
        == "https://azure-openai.example.test"
    )
    assert spec.agent_configuration["azure_openai"]["audience"] == "agents"


def test_partial_agent_configuration_parses_successfully(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        """
        name: Main
        description: Main agent
        agent_configuration:
          azure_openai:
            azure_endpoint: https://azure-openai.example.test
        """,
    )

    [spec] = load_agent_specs(tmp_path, strict=True)

    assert spec.agent_configuration == {
        "azure_openai": {"azure_endpoint": "https://azure-openai.example.test"}
    }


def test_post_merge_validation_surfaces_clear_errors_for_provider_mismatch(
    tmp_path: Path,
) -> None:
    (tmp_path / "agents.config.yaml").write_text(
        textwrap.dedent(
            """
            agent_configuration:
              provider: openai
              model: gpt-4o
              openai: {}
            """
        ).strip(),
        encoding="utf-8",
    )
    _write_agent(
        tmp_path,
        """
        name: Main
        description: Main agent
        agent_configuration:
          provider: openai
          model: gpt-4o-mini
          azure_openai:
            azure_endpoint: https://azure-openai.example.test
            api_version: "2024-10-21"
        """,
    )

    global_config = load_global_config(tmp_path)
    [spec] = load_agent_specs(tmp_path, strict=True)

    with pytest.raises(ValueError, match="unrelated provider sub-block"):
        compose(spec, global_config, discovered_mcp_names=[], discovered_skill_names=[])


def test_post_merge_validation_surfaces_missing_required_fields(tmp_path: Path) -> None:
    (tmp_path / "agents.config.yaml").write_text(
        textwrap.dedent(
            """
            agent_configuration:
              provider: azure_openai
              model: gpt-4o
              azure_openai:
                azure_endpoint: https://azure-openai.example.test
                api_version: "2024-10-21"
            """
        ).strip(),
        encoding="utf-8",
    )
    _write_agent(
        tmp_path,
        """
        name: Main
        description: Main agent
        agent_configuration:
          azure_openai:
            azure_endpoint:
        """,
    )

    global_config = load_global_config(tmp_path)
    [spec] = load_agent_specs(tmp_path, strict=True)
    resolved = compose(spec, global_config, discovered_mcp_names=[], discovered_skill_names=[])

    with pytest.raises(
        ValueError,
        match=r"agent_configuration\.azure_openai\.azure_endpoint must be set",
    ):
        validate_resolved_agent(resolved, discovered_mcp_names=[], discovered_skills=[])


def test_load_agent_specs_rejects_hyphenated_agent_configuration_key(tmp_path: Path) -> None:
    source = _write_agent(
        tmp_path,
        """
        name: Main
        description: Main agent
        agent-configuration:
          provider: openai
          model: gpt-4o
          openai: {}
        """,
    )

    with pytest.raises(ValueError, match=re.escape(str(source))):
        load_agent_specs(tmp_path, strict=True)


def test_load_global_config_rejects_hyphenated_agent_configuration_key(tmp_path: Path) -> None:
    source = tmp_path / "agents.config.yaml"
    source.write_text(
        textwrap.dedent(
            """
            agent-configuration:
              provider: openai
              model: gpt-4o
              openai: {}
            """
        ).strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=re.escape(str(source))):
        load_global_config(tmp_path)


@pytest.mark.parametrize("field_name", ["model", "endpoint", "temperature", "timeout"])
def test_load_global_config_rejects_legacy_top_level_fields(
    tmp_path: Path,
    field_name: str,
) -> None:
    source = tmp_path / "agents.config.yaml"
    value = '"legacy"' if field_name in {"model", "endpoint"} else "1"
    source.write_text(f"{field_name}: {value}\n", encoding="utf-8")

    with pytest.raises(ValueError, match=rf"field `{field_name}`"):
        load_global_config(tmp_path)


@pytest.mark.parametrize("field_name", ["model", "endpoint", "temperature", "timeout"])
def test_load_agent_specs_rejects_legacy_top_level_fields(
    tmp_path: Path,
    field_name: str,
) -> None:
    value = '"legacy"' if field_name in {"model", "endpoint"} else "1"
    _write_agent(
        tmp_path,
        f"""
        name: Main
        description: Main agent
        {field_name}: {value}
        """,
    )

    with pytest.raises(ValueError, match=rf"field `{field_name}`"):
        load_agent_specs(tmp_path, strict=True)


def test_load_agent_specs_substitute_variables_false_skips_frontmatter_and_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENT_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "VALUE")
    monkeypatch.setenv("FOO", "VALUE")
    _write_agent(
        tmp_path,
        """
        name: Main
        description: Main agent
        agent_configuration:
          provider: openai
          model: $AGENT_MODEL
          openai:
            api_key: $OPENAI_API_KEY
        substitute_variables: false
        """,
        "Keep $FOO literal",
    )

    [spec] = load_agent_specs(tmp_path)
    assert spec.agent_configuration is not None
    assert spec.agent_configuration["model"] == "$AGENT_MODEL"
    assert spec.agent_configuration["openai"]["api_key"] == "$OPENAI_API_KEY"
    assert spec.substitute_variables is False
    assert spec.instructions.strip() == "Keep $FOO literal"


def test_load_agent_specs_resolves_trigger_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TRIG_TYPE", "http_trigger")
    _write_agent(
        tmp_path,
        """
        name: Report
        description: Report agent
        trigger:
          type: $TRIG_TYPE
          args:
            route: report
            methods: ["POST"]
        """,
        "body",
    )

    [spec] = load_agent_specs(tmp_path)
    assert spec.trigger is not None
    assert spec.trigger.type == "http_trigger"


def test_load_agent_specs_missing_name_raises(tmp_path: Path) -> None:
    source = _write_agent(
        tmp_path,
        """
        description: Main agent
        """,
    )
    with pytest.raises(ValueError, match=re.escape(str(source))):
        load_agent_specs(tmp_path, strict=True)


def test_load_global_config_empty_file_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "agents.config.yaml").write_text("# only comments\n\n", encoding="utf-8")
    config = load_global_config(tmp_path)
    assert config.agent_configuration is None


def test_load_global_config_non_mapping_root_raises(tmp_path: Path) -> None:
    source = tmp_path / "agents.config.yaml"
    source.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_global_config(tmp_path)
    message = str(exc_info.value)
    assert str(source) in message
    assert "mapping" in message
    assert "docs/front-matter-spec.md" in message


def test_load_global_config_invalid_field_type_raises(tmp_path: Path) -> None:
    source = tmp_path / "agents.config.yaml"
    source.write_text(
        textwrap.dedent(
            """
            agent_configuration:
              provider: openai
              model: gpt-4o
              timeout: "not-a-number"
              openai: {}
            """
        ).strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc_info:
        load_global_config(tmp_path)
    message = str(exc_info.value)
    assert str(source) in message
    assert "timeout" in message
    assert "docs/front-matter-spec.md" in message


def test_load_global_config_rejects_top_level_mcp_field(tmp_path: Path) -> None:
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
    monkeypatch.setenv("ENDPOINT", "https://example.test")
    (tmp_path / "agents.config.yaml").write_text(
        textwrap.dedent(
            """
            agent_configuration:
              provider: openai
              model: gpt-4o
              timeout: 60
              openai: {}
            system_tools:
              execute_in_sessions:
                session_pool_management_endpoint: $ENDPOINT
            """
        ).strip(),
        encoding="utf-8",
    )
    config = load_global_config(tmp_path)
    assert config.agent_configuration is not None
    assert config.agent_configuration.timeout == 60
    assert config.system_tools is not None
    assert config.system_tools.execute_in_sessions is not None
    assert (
        config.system_tools.execute_in_sessions.session_pool_management_endpoint
        == "https://example.test"
    )


def test_load_agent_specs_malformed_frontmatter_yaml_raises(tmp_path: Path) -> None:
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
    _write_agent(
        tmp_path,
        """
        name: Main
        description: Main agent
        """,
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
    assert spec.trigger.args["run_on_start"] is True
    assert spec.trigger.args["priority"] == 5
    assert spec.trigger.args["schedule"] == "0 0 * * * *"
