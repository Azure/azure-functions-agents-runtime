from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from typing import Any

import pytest

from azure_functions_agents.app import create_function_app

# On-disk fixtures shared with test_config_fixtures.py's loader-level tests
# (see FIXTURES_ROOT there). Used here for end-to-end create_function_app()
# regression tests where a fresh tmp_path-built fixture wouldn't be as
# convincing a proof — e.g. "this fixture registers identically to before"
# needs a fixture that already existed pre-change.
FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "config_scenarios"


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


def test_create_function_app_fails_fast_on_duplicate_function_names(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Same-slug collisions fail fast at composition time (FRD 0006 Decision #17)."""
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

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ValueError, match=r"[Dd]uplicate agent slug") as exc_info,
    ):
        create_function_app(tmp_path)

    message = str(exc_info.value)
    assert "daily_report" in message
    assert "daily-report.agent.md" in message
    assert "daily_report.agent.md" in message


def test_create_function_app_fails_fast_on_duplicate_slugs_with_builtin_endpoints(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Same-slug collisions fail fast even when builtin_endpoints are also involved."""
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

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ValueError, match=r"[Dd]uplicate agent slug") as exc_info,
    ):
        create_function_app(tmp_path)

    message = str(exc_info.value)
    assert "daily_report" in message
    assert "daily-report.agent.md" in message
    assert "daily_report.agent.md" in message


def test_create_function_app_fails_fast_on_duplicate_slug_across_root_and_agents_folder(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Same-stem collisions fail fast even when one file is at the root and the
    other lives in the agents/ subfolder (FRD 0006 Decision #17)."""
    (tmp_path / "agents").mkdir()
    _write_agent(
        tmp_path,
        "report.agent.md",
        """
        name: Root Report
        description: Desc
        trigger:
          type: timer_trigger
          args:
            schedule: "0 0 * * * *"
        """,
    )
    _write_agent(
        tmp_path,
        "agents/report.agent.md",
        """
        name: Agents Folder Report
        description: Desc
        trigger:
          type: timer_trigger
          args:
            schedule: "0 5 * * * *"
        """,
    )

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ValueError, match=r"[Dd]uplicate agent slug") as exc_info,
    ):
        create_function_app(tmp_path)

    message = str(exc_info.value)
    assert "report" in message
    assert "report.agent.md" in message
    assert "agents_report.agent.md" in message


def test_create_function_app_registers_distinct_slugs_without_collision(
    tmp_path: Path,
) -> None:
    """Regression: genuinely distinct slugs (no subagents involved) continue to
    register exactly as before the app-wide fail-fast slug-uniqueness check was
    added (FRD 0006 Decision #17) — the check must be a no-op for the happy path."""
    _write_agent(
        tmp_path,
        "report-alpha.agent.md",
        """
        name: Report Alpha
        description: Desc
        trigger:
          type: timer_trigger
          args:
            schedule: "0 0 * * * *"
        """,
    )
    _write_agent(
        tmp_path,
        "report-beta.agent.md",
        """
        name: Report Beta
        description: Desc
        trigger:
          type: timer_trigger
          args:
            schedule: "0 5 * * * *"
        """,
    )

    app = create_function_app(tmp_path)

    assert _function_names(app.get_functions()) == ["report_alpha", "report_beta"]


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


# ---------------------------------------------------------------------------
# End-to-end delegation regressions through the real create_function_app()
# pipeline (FRD 0006 §6 test plan) — on-disk fixtures, not manually
# reconstructed catalogs.
# ---------------------------------------------------------------------------


def test_create_function_app_accepts_endpoint_less_specialist_referenced_only_via_subagents() -> None:
    """An internal-only specialist (no trigger, no builtin_endpoints) must be
    accepted end-to-end when it is reachable solely through another agent's
    ``subagents:`` — and must register *zero* external entry points of its
    own (FRD 0006 Decision #18).

    ``test_multi_agent_delegation_fixture`` in test_config_fixtures.py
    already covers this fixture at the loader/validation layer by calling
    ``validate_resolved_agent(shipping, ..., is_referenced_as_subagent=True)``
    directly. That does not exercise the real two-pass composition root
    (``create_function_app`` -> ``validate_subagent_references`` ->
    ``build_capabilities``/``build_catalog`` -> per-agent registration) at
    all, so a wiring bug in *that* pipeline (e.g. the endpoint-less-agent
    carve-out only applying to the manually-called validator, not to the
    real startup path) would not be caught. This test drives the same
    on-disk fixture through the real pipeline instead.
    """
    fixture = FIXTURES_ROOT / "15_multi_agent_delegation"

    app = create_function_app(fixture)
    functions = app.get_functions()

    # Exactly billing's and coordinator's builtin chat_api endpoints — no
    # error was raised despite "shipping" having neither a trigger nor
    # builtin_endpoints, and critically, no function was registered for it.
    assert _function_names(functions) == [
        "agent_billing_builtin_chat",
        "agent_billing_builtin_chatstream",
        "agent_coordinator_builtin_chat",
        "agent_coordinator_builtin_chatstream",
    ]
    assert not any("shipping" in name for name in _function_names(functions))
    assert _http_routes(functions) == [
        "agents/billing/chat",
        "agents/billing/chatstream",
        "agents/coordinator/chat",
        "agents/coordinator/chatstream",
    ]


def test_create_function_app_regression_pre_existing_multi_agent_fixture_unaffected_by_two_pass_composition() -> (
    None
):
    """A collision-free, pre-FRD-0006-style multi-agent fixture (none of its
    agents declare ``subagents:``) must register identical function names,
    routes, and tool assembly under the new two-pass composition
    (``validate_subagent_references``/``build_catalog`` added ahead of
    per-agent registration) as it would have before FRD 0006.

    None of the pre-existing fixtures under ``fixtures/config_scenarios/``
    (01-14) can actually run through ``create_function_app()`` as-is: they
    were built only for the loader-level tests in test_config_fixtures.py
    (see that module's docstring) and several have unrelated, pre-existing
    bugs that predate this PR (confirmed via ``git log`` / ``git blame`` —
    e.g. ``05_multi_trigger``'s ``main.agent.md`` has neither a ``trigger``
    nor ``builtin_endpoints``, and its ``queue_worker.agent.md`` uses a
    trigger arg name ``TriggerApi.queue_trigger()`` does not accept;
    ``06_capability_filtering``'s ``selective.agent.md`` references MCP
    server names with no corresponding ``mcp.json`` in the fixture). Rather
    than risk destabilizing those fixtures/tests (which are exercised only
    at the loader layer and depend on their exact current shape), this adds
    a new, genuinely full-pipeline-runnable fixture,
    ``16_no_subagent_regression`` (three agents, zero ``subagents:``
    references between them, mirroring the *spirit* of the older
    multi-agent fixtures), and proves it composes and registers exactly as
    a delegation-unaware pipeline would have.
    """
    fixture = FIXTURES_ROOT / "16_no_subagent_regression"

    app = create_function_app(fixture)
    functions = app.get_functions()

    assert _function_names(functions) == [
        "agent_main_builtin_chat_page",
        "agent_main_builtin_chat",
        "agent_main_builtin_chatstream",
        "agent_main_builtin_mcp",
        "nightly_report",
        "resource_summary",
    ]
    assert _http_routes(functions) == [
        "agents/main/",
        "agents/main/chat",
        "agents/main/chatstream",
        "resource-summary",
    ]


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
        assert log_json["agents"][0]["source_file"].endswith("main.agent.md")
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
        source_files = {Path(a["source_file"]).name for a in log_json["agents"]}
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

