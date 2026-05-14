from __future__ import annotations

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
    assert config.timeout == 12
    assert config.mcp == ["learn"]
    assert config.system_tools is not None
    assert config.system_tools.tools_from_connections[0].connection_id == "conn-1"


def test_load_global_config_missing_returns_empty(tmp_path: Path) -> None:
    assert load_global_config(tmp_path) == load_global_config(tmp_path)
    assert load_global_config(tmp_path).model_dump() == {
        "mcp": [],
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
        load_agent_specs(tmp_path)


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
        load_agent_specs(tmp_path)
