from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import azure.functions as func
import pytest

from azure_functions_agents.config.loader import load_agent_specs
from azure_functions_agents.config.merge import compose
from azure_functions_agents.config.schema import (
    DebugConfig,
    GlobalConfig,
    ResolvedAgent,
    ToolsFilter,
    TriggerSpec,
)
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.triggers import (
    _function_name_from_source,
    _safe_function_name,
    register_agent,
)


class FakeFunctionApp:
    def __init__(self) -> None:
        self.function_names: list[str] = []
        self.trigger_calls: list[tuple[str, dict[str, Any]]] = []

    def function_name(self, *, name: str) -> Any:
        def decorator(handler: Any) -> Any:
            self.function_names.append(name)
            return handler

        return decorator

    def timer_trigger(self, **kwargs: Any) -> Any:
        def decorator(handler: Any) -> Any:
            self.trigger_calls.append(("timer_trigger", kwargs))
            return handler

        return decorator

    def route(self, **kwargs: Any) -> Any:
        def decorator(handler: Any) -> Any:
            self.trigger_calls.append(("route", kwargs))
            return handler

        return decorator


def _write_timer_agent(tmp_path: Path, filename: str, display_name: str) -> None:
    (tmp_path / filename).write_text(
        textwrap.dedent(
            f"""
            ---
            name: "{display_name}"
            description: Test agent
            trigger:
              type: timer_trigger
              args:
                schedule: "0 0 * * * *"
            ---
            Run the timer workflow.
            """
        ).lstrip(),
        encoding="utf-8",
    )


def _resolve_agents(tmp_path: Path) -> list[ResolvedAgent]:
    return [
        compose(spec, GlobalConfig(), discovered_mcp_names=[], discovered_skill_names=[])
        for spec in load_agent_specs(tmp_path)
    ]


def _stub_handler(*args: Any, **kwargs: Any) -> Any:
    return None


def test_register_agent_uses_source_filename_for_function_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_timer_agent(tmp_path, "simple.agent.md", "Simple Agent")
    [resolved] = _resolve_agents(tmp_path)
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    register_agent(app, resolved, AgentCapabilities())

    assert app.function_names == ["simple"]


def test_register_agent_sanitizes_source_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_timer_agent(tmp_path, "daily-report.agent.md", "Daily Azure Report")
    [resolved] = _resolve_agents(tmp_path)
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    register_agent(app, resolved, AgentCapabilities())

    assert app.function_names == [_safe_function_name("daily-report")]


def test_register_agent_avoids_name_collisions_from_display_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_timer_agent(tmp_path, "report-a.agent.md", "Daily Report")
    _write_timer_agent(tmp_path, "report-b.agent.md", "Daily Report")
    resolved_agents = _resolve_agents(tmp_path)
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    for resolved in resolved_agents:
        register_agent(app, resolved, AgentCapabilities())

    assert app.function_names == [
        _safe_function_name("report-a"),
        _safe_function_name("report-b"),
    ]
    assert app.function_names[0] != app.function_names[1]


def test_loaded_agent_keeps_display_name_in_metadata(tmp_path: Path) -> None:
    _write_timer_agent(tmp_path, "simple.agent.md", "Simple Agent")

    [spec] = load_agent_specs(tmp_path)
    [resolved] = _resolve_agents(tmp_path)

    assert spec.name == "Simple Agent"
    assert resolved.name == "Simple Agent"


def test_function_name_from_source_falls_back_to_display_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    display_name = "Daily Report"

    with caplog.at_level("WARNING"):
        function_name = _function_name_from_source(None, display_name)

    assert function_name == _safe_function_name(display_name)
    assert "missing source_file" in caplog.text


def test_register_agent_missing_source_file_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolved = ResolvedAgent(
        name="Daily Report",
        description="desc",
        trigger=TriggerSpec(type="timer_trigger", args={"schedule": "0 0 * * * *"}),
        instructions="Run the timer workflow.",
        is_main=False,
        debug=DebugConfig(),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        connector_specs=[],
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=None,
    )
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    with caplog.at_level("WARNING"):
        register_agent(app, resolved, AgentCapabilities())

    assert app.function_names == [_safe_function_name("Daily Report")]
    assert "missing source_file" in caplog.text


def test_register_agent_keeps_literal_trigger_args_when_substitution_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "literal.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Literal Route
            description: Test agent
            substitute_variables: false
            trigger:
              type: http_trigger
              args:
                route: "${ROUTE_SEGMENT}"
                methods: ["POST"]
            ---
            Keep ${ROUTE_SEGMENT} literal.
            """
        ).lstrip(),
        encoding="utf-8",
    )
    [resolved] = _resolve_agents(tmp_path)
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_http_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    register_agent(app, resolved, AgentCapabilities())

    assert resolved.substitute_variables is False
    assert app.trigger_calls == [
        (
            "route",
            {
                "route": "${ROUTE_SEGMENT}",
                "methods": ["POST"],
                "auth_level": func.AuthLevel.FUNCTION,
            },
        )
    ]


def test_register_agent_does_not_double_substitute_trigger_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ROUTE", "$OTHER")
    monkeypatch.setenv("OTHER", "actual-route")
    (tmp_path / "nested.agent.md").write_text(
        textwrap.dedent(
            """
            ---
            name: Nested Route
            description: Test agent
            trigger:
              type: http_trigger
              args:
                route: "$ROUTE"
                methods: ["POST"]
            ---
            Use the resolved route.
            """
        ).lstrip(),
        encoding="utf-8",
    )
    [resolved] = _resolve_agents(tmp_path)
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_http_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    register_agent(app, resolved, AgentCapabilities())

    assert resolved.trigger is not None
    assert resolved.trigger.args["route"] == "$OTHER"
    assert app.trigger_calls == [
        (
            "route",
            {
                "route": "$OTHER",
                "methods": ["POST"],
                "auth_level": func.AuthLevel.FUNCTION,
            },
        )
    ]
