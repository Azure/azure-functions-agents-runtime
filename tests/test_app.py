from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from typing import Any

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


def _function_names(functions: list[Any]) -> list[str]:
    return [function.get_function_name() for function in functions]


def _http_routes(functions: list[Any]) -> list[str]:
    routes: list[str] = []
    for function in functions:
        for binding in function.get_bindings():
            route = getattr(binding, "route", None)
            if route is not None:
                routes.append(route)
    return routes


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

    functions = app.get_functions()

    assert _function_names(functions) == ["daily_report", "daily_report_2"]
    assert "Function name collision" in caplog.text
    assert "daily_report.agent.md" in caplog.text


def test_create_function_app_pairs_debug_slugs_with_auto_suffixed_function_names(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _write_agent(
        tmp_path,
        "daily-report.agent.md",
        """
        name: Daily Report Dash
        description: Desc
        debug:
          chat: true
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
        debug:
          chat: true
        trigger:
          type: timer_trigger
          args:
            schedule: "0 5 * * * *"
        """,
    )

    with caplog.at_level(logging.WARNING):
        app = create_function_app(tmp_path)

    functions = app.get_functions()

    assert _function_names(functions) == [
        "daily_report",
        "agent_daily_report_debug_chat_page",
        "agent_daily_report_debug_chat",
        "agent_daily_report_debug_chatstream",
        "daily_report_2",
        "agent_daily_report_2_debug_chat_page",
        "agent_daily_report_2_debug_chat",
        "agent_daily_report_2_debug_chatstream",
    ]
    assert _http_routes(functions) == [
        "agents/daily_report/",
        "agents/daily_report/chat",
        "agents/daily_report/chatstream",
        "agents/daily_report_2/",
        "agents/daily_report_2/chat",
        "agents/daily_report_2/chatstream",
    ]
    assert "Function name collision" in caplog.text


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

    assert _function_names(app.get_functions()) == ["good"]
