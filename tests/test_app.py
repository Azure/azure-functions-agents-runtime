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


def test_create_function_app_pairs_builtin_slugs_with_auto_suffixed_function_names(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _write_agent(
        tmp_path,
        "daily-report.agent.md",
        """
        name: Daily Report Dash
        description: Desc
        builtin_endpoints:
            debug_chat_ui: true
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
        builtin_endpoints:
            debug_chat_ui: true
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
        "agent_daily_report_builtin_chat_page",
        "agent_daily_report_builtin_chat",
        "agent_daily_report_builtin_chatstream",
        "daily_report_2",
        "agent_daily_report_2_builtin_chat_page",
        "agent_daily_report_2_builtin_chat",
        "agent_daily_report_2_builtin_chatstream",
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


def test_create_function_app_allows_endpoint_agent_without_trigger(
    tmp_path: Path,
) -> None:
    _write_agent(
        tmp_path,
        "main.agent.md",
        """
        name: Main Chat
        description: Desc
        builtin_endpoints:
            debug_chat_ui: true
            mcp: true
        """,
    )

    app = create_function_app(tmp_path)
    functions = app.get_functions()

    assert _function_names(functions) == [
        "agent_main_builtin_chat_page",
        "agent_main_builtin_chat",
        "agent_main_builtin_chatstream",
        "agent_main_builtin_mcp",
    ]
    assert _http_routes(functions) == [
        "agents/main/",
        "agents/main/chat",
        "agents/main/chatstream",
    ]


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


class TestStructuredIndexingLog:
    """Tests for the structured JSON indexing log emitted by create_function_app."""

    def test_emits_agent_runtime_indexed_log(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """Test that create_function_app emits a structured indexing log."""
        import json

        _write_agent(
            tmp_path,
            "main.agent.md",
            """
            name: Main
            description: Main agent
            builtin_endpoints:
                debug_chat_ui: true
            """,
        )

        with caplog.at_level(logging.INFO):
            create_function_app(tmp_path)

        # Find the indexing log message
        indexing_logs = [r for r in caplog.records if "agent_runtime_indexed" in r.message]
        assert len(indexing_logs) == 1

        # Parse the JSON from the log message
        log_message = indexing_logs[0].message
        # Extract JSON portion after the colon
        json_start = log_message.index("{")
        log_json = json.loads(log_message[json_start:])

        assert log_json["event"] == "agent_runtime_indexed"
        assert log_json["agent_count"] == 1
        assert len(log_json["agents"]) == 1
        assert log_json["agents"][0]["source_file"] == "main.agent.md"
        assert "discovered_capabilities" in log_json

    def test_indexing_log_includes_trigger_type(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """Test that the indexing log includes trigger type for each agent."""
        import json

        _write_agent(
            tmp_path,
            "timer-agent.agent.md",
            """
            name: Timer Agent
            description: Timer triggered agent
            trigger:
                type: timer_trigger
                args:
                    schedule: "0 0 * * * *"
            """,
        )

        with caplog.at_level(logging.INFO):
            create_function_app(tmp_path)

        indexing_logs = [r for r in caplog.records if "agent_runtime_indexed" in r.message]
        log_message = indexing_logs[0].message
        json_start = log_message.index("{")
        log_json = json.loads(log_message[json_start:])

        assert log_json["agents"][0]["trigger_type"] == "timer_trigger"

    def test_indexing_log_includes_builtin_endpoints(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """Test that the indexing log includes builtin_endpoints for agents."""
        import json

        _write_agent(
            tmp_path,
            "chat-agent.agent.md",
            """
            name: Chat Agent
            description: Chat agent with endpoints
            builtin_endpoints:
                debug_chat_ui: true
                chat_api: true
                mcp: true
            """,
        )

        with caplog.at_level(logging.INFO):
            create_function_app(tmp_path)

        indexing_logs = [r for r in caplog.records if "agent_runtime_indexed" in r.message]
        log_message = indexing_logs[0].message
        json_start = log_message.index("{")
        log_json = json.loads(log_message[json_start:])

        endpoints = log_json["agents"][0]["builtin_endpoints"]
        assert "debug_chat_ui" in endpoints
        assert "chat_api" in endpoints
        assert "mcp" in endpoints

    def test_indexing_log_counts_multiple_agents(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """Test that the indexing log correctly counts multiple agents."""
        import json

        _write_agent(
            tmp_path,
            "agent1.agent.md",
            """
            name: Agent One
            description: First agent
            builtin_endpoints:
                debug_chat_ui: true
            """,
        )
        _write_agent(
            tmp_path,
            "agent2.agent.md",
            """
            name: Agent Two
            description: Second agent
            trigger:
                type: timer_trigger
                args:
                    schedule: "0 0 * * * *"
            """,
        )

        with caplog.at_level(logging.INFO):
            create_function_app(tmp_path)

        indexing_logs = [r for r in caplog.records if "agent_runtime_indexed" in r.message]
        log_message = indexing_logs[0].message
        json_start = log_message.index("{")
        log_json = json.loads(log_message[json_start:])

        assert log_json["agent_count"] == 2
        assert len(log_json["agents"]) == 2
        source_files = {a["source_file"] for a in log_json["agents"]}
        assert source_files == {"agent1.agent.md", "agent2.agent.md"}

    def test_indexing_log_includes_discovered_capabilities(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """Test that the indexing log includes discovered capabilities counts."""
        import json

        _write_agent(
            tmp_path,
            "main.agent.md",
            """
            name: Main
            description: Main agent
            builtin_endpoints:
                debug_chat_ui: true
            """,
        )

        with caplog.at_level(logging.INFO):
            create_function_app(tmp_path)

        indexing_logs = [r for r in caplog.records if "agent_runtime_indexed" in r.message]
        log_message = indexing_logs[0].message
        json_start = log_message.index("{")
        log_json = json.loads(log_message[json_start:])

        capabilities = log_json["discovered_capabilities"]
        assert "mcp_servers" in capabilities
        assert "skills" in capabilities
        assert "user_tools" in capabilities
        assert isinstance(capabilities["mcp_servers"], int)
        assert isinstance(capabilities["skills"], int)
        assert isinstance(capabilities["user_tools"], int)

    def test_indexing_log_includes_discovered_capability_names(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """Test that the indexing log includes concrete names for discovered capabilities."""
        import json

        _write_agent(
            tmp_path,
            "main.agent.md",
            """
            name: Main
            description: Main agent
            builtin_endpoints:
                debug_chat_ui: true
            """,
        )

        with caplog.at_level(logging.INFO):
            create_function_app(tmp_path)

        indexing_logs = [r for r in caplog.records if "agent_runtime_indexed" in r.message]
        log_message = indexing_logs[0].message
        json_start = log_message.index("{")
        log_json = json.loads(log_message[json_start:])

        discovered_names = log_json["discovered_capability_names"]
        assert set(discovered_names.keys()) == {"mcp_servers", "skills", "user_tools"}
        assert isinstance(discovered_names["mcp_servers"], list)
        assert isinstance(discovered_names["skills"], list)
        assert isinstance(discovered_names["user_tools"], list)

    def test_indexing_log_includes_per_agent_registered_capabilities(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """Test that each agent entry includes concrete registered capability names."""
        import json

        _write_agent(
            tmp_path,
            "main.agent.md",
            """
            name: Main
            description: Main agent
            builtin_endpoints:
                debug_chat_ui: true
            """,
        )

        with caplog.at_level(logging.INFO):
            create_function_app(tmp_path)

        indexing_logs = [r for r in caplog.records if "agent_runtime_indexed" in r.message]
        log_message = indexing_logs[0].message
        json_start = log_message.index("{")
        log_json = json.loads(log_message[json_start:])

        agent_entry = log_json["agents"][0]
        registered = agent_entry["registered_capabilities"]
        assert set(registered.keys()) == {"mcp_servers", "skills", "user_tools"}
        assert isinstance(registered["mcp_servers"], list)
        assert isinstance(registered["skills"], list)
        assert isinstance(registered["user_tools"], list)

    def test_emits_agent_capabilities_registered_log(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        _write_agent(
            tmp_path,
            "main.agent.md",
            """
            name: Main
            description: Main agent
            builtin_endpoints:
                debug_chat_ui: true
            """,
        )

        with caplog.at_level(logging.INFO):
            create_function_app(tmp_path)

        assert any(
            "agent_capabilities_registered" in record.getMessage() for record in caplog.records
        )

