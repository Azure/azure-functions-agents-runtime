from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import azure.functions as func
import pytest

from azure_functions_agents.app import create_function_app


def _write_agent(
    tmp_path: Path,
    filename: str,
    frontmatter: str,
    body: str = "Assist the user.",
) -> None:
    cleaned_frontmatter = textwrap.dedent(frontmatter).strip()
    cleaned_body = textwrap.dedent(body).strip()
    (tmp_path / filename).write_text(
        f"---\n{cleaned_frontmatter}\n---\n{cleaned_body}\n",
        encoding="utf-8",
    )


def _function_names(app: func.FunctionApp) -> list[str]:
    return [function.get_function_name() for function in app.get_functions()]


def test_create_function_app_auto_suffixes_duplicate_function_names(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _write_agent(
        tmp_path,
        "daily-report.agent.md",
        """
        name: Daily Report Dash
        description: Desc
        trigger:
          type: timer_trigger
          args:
            schedule: "0 0 * * * *"
        """,
    )
    _write_agent(
        tmp_path,
        "daily_report.agent.md",
        """
        name: Daily Report Underscore
        description: Desc
        trigger:
          type: timer_trigger
          args:
            schedule: "0 5 * * * *"
        """,
    )

    with caplog.at_level(logging.WARNING):
        app = create_function_app(tmp_path)

    assert _function_names(app) == ["daily_report", "daily_report_2"]
    assert "Function name collision" in caplog.text
    assert "daily_report.agent.md" in caplog.text


def test_create_function_app_raises_on_missing_http_route(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "missing-route.agent.md",
        """
        name: Missing Route
        description: Desc
        trigger:
          type: http_trigger
          args:
            methods: ["POST"]
        """,
    )

    with pytest.raises(ValueError, match="http_trigger requires 'route'"):
        create_function_app(tmp_path)


def test_create_function_app_raises_on_invalid_auth_level(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "invalid-auth.agent.md",
        """
        name: Invalid Auth
        description: Desc
        trigger:
          type: http_trigger
          args:
            route: reports
            auth_level: adminn
        """,
    )

    with pytest.raises(ValueError, match="invalid auth_level 'adminn'"):
        create_function_app(tmp_path)


def test_create_function_app_skips_malformed_yaml_but_registers_valid(tmp_path: Path) -> None:
    (tmp_path / "bad.agent.md").write_text("---\nname: [\n---\n", encoding="utf-8")
    _write_agent(
        tmp_path,
        "good.agent.md",
        """
        name: Good
        description: Desc
        trigger:
          type: timer_trigger
          args:
            schedule: "0 0 * * * *"
        """,
    )

    app = create_function_app(tmp_path)

    assert _function_names(app) == ["good"]
