from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from typing import Any

import azure.functions as func
import pytest

from azure_functions_agents.config.loader import load_agent_specs
from azure_functions_agents.config.merge import compose
from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    GlobalConfig,
    ResolvedAgent,
    ToolsFilter,
    TriggerSpec,
)
from azure_functions_agents.registration._naming import (
    _function_name_from_source,
    _safe_function_name,
)
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.triggers import (
    allocate_unique_function_name,
    register_agent,
)


class FakeFunctionApp:
    def __init__(self, *, function_name_error: Exception | None = None) -> None:
        self.function_names: list[str] = []
        self.trigger_calls: list[tuple[str, dict[str, Any]]] = []
        self.function_name_error = function_name_error

    def function_name(self, *, name: str) -> Any:
        if self.function_name_error is not None:
            raise self.function_name_error

        def decorator(handler: Any) -> Any:
            self.function_names.append(name)
            return handler

        return decorator

    def timer_trigger(self, **kwargs: Any) -> Any:
        def decorator(handler: Any) -> Any:
            self.trigger_calls.append(("timer_trigger", kwargs))
            return handler

        return decorator

    def connector_trigger(self, **kwargs: Any) -> Any:
        def decorator(handler: Any) -> Any:
            self.trigger_calls.append(("connector_trigger", kwargs))
            return handler

        return decorator

    def generic_trigger(self, **kwargs: Any) -> Any:
        def decorator(handler: Any) -> Any:
            self.trigger_calls.append(("generic_trigger", kwargs))
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


def _resolved_agent(*, trigger: TriggerSpec, is_main: bool = False) -> ResolvedAgent:
    return ResolvedAgent(
        name="Daily Report",
        description="desc",
        trigger=trigger,
        instructions="Run the timer workflow.",
        is_main=is_main,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=__file__,
    )


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


def test_allocate_unique_function_name_fails_fast_on_collision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Duplicate slugs fail fast instead of silently auto-suffixing (FRD 0007 Decision #17)."""
    registered_names = {"daily_report"}

    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match="Function name collision"):
        allocate_unique_function_name(
            "/path/daily-report.agent.md",
            "Daily Report",
            registered_names,
        )

    assert registered_names == {"daily_report"}
    assert "Function name collision" in caplog.text
    assert "/path/daily-report.agent.md" in caplog.text
    assert "'daily_report'" in caplog.text


def test_allocate_unique_function_name_no_warning_for_unique_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registered_names: set[str] = set()

    with caplog.at_level(logging.WARNING):
        function_name = allocate_unique_function_name(
            "/path/daily-report.agent.md",
            "Daily Report",
            registered_names,
        )

    assert function_name == "daily_report"
    assert registered_names == {"daily_report"}
    assert caplog.records == []


def test_register_agent_missing_source_file_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(type="timer_trigger", args={"schedule": "0 0 * * * *"})
    )
    resolved = resolved.model_copy(update={"source_file": None})
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    with caplog.at_level("WARNING"):
        register_agent(app, resolved, AgentCapabilities())

    assert app.function_names == [_safe_function_name("Daily Report")]
    assert "missing source_file" in caplog.text


def test_register_agent_fails_fast_on_duplicate_function_names_with_registry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Same-slug collisions fail fast instead of auto-suffixing (FRD 0007 Decision #17)."""
    _write_timer_agent(tmp_path, "daily-report.agent.md", "Daily Report Dash")
    _write_timer_agent(tmp_path, "daily_report.agent.md", "Daily Report Underscore")
    resolved_agents = _resolve_agents(tmp_path)
    app = FakeFunctionApp()
    registered_names: set[str] = set()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match="Function name collision"):
        for resolved in resolved_agents:
            register_agent(app, resolved, AgentCapabilities(), registered_names=registered_names)

    assert app.function_names == ["daily_report"]
    assert registered_names == {"daily_report"}
    assert "Function name collision" in caplog.text
    assert "daily_report.agent.md" in caplog.text


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


def test_register_agent_propagates_builtin_trigger_registration_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(type="timer_trigger", args={"schedule": "0 0 * * * *"})
    )
    app = FakeFunctionApp(function_name_error=ValueError("builtin registration failed"))
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    with pytest.raises(ValueError, match="builtin registration failed"):
        register_agent(app, resolved, AgentCapabilities())


def test_register_agent_propagates_http_trigger_registration_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(
            type="http_trigger",
            args={"route": "reports", "methods": ["POST"]},
        )
    )
    app = FakeFunctionApp(function_name_error=ValueError("http registration failed"))
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_http_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    with pytest.raises(ValueError, match="http registration failed"):
        register_agent(app, resolved, AgentCapabilities())


def test_register_agent_raises_when_http_trigger_missing_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(type="http_trigger", args={"methods": ["POST"]})
    )
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_http_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    with pytest.raises(ValueError, match="route"):
        register_agent(app, resolved, AgentCapabilities())


def test_register_agent_raises_on_invalid_auth_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(
            type="http_trigger",
            args={"route": "reports", "methods": ["POST"], "auth_level": "adminn"},
        )
    )
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_http_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    with pytest.raises(
        ValueError, match=r"admin, anonymous, function|anonymous, function, admin"
    ):
        register_agent(app, resolved, AgentCapabilities())


@pytest.mark.parametrize(
    ("auth_level", "expected"),
    [
        ("anonymous", func.AuthLevel.ANONYMOUS),
        ("function", func.AuthLevel.FUNCTION),
        ("admin", func.AuthLevel.ADMIN),
    ],
)
def test_register_agent_accepts_valid_auth_levels(
    monkeypatch: pytest.MonkeyPatch,
    auth_level: str,
    expected: func.AuthLevel,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(
            type="http_trigger",
            args={"route": f"{auth_level}-reports", "auth_level": auth_level},
        )
    )
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_http_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    register_agent(app, resolved, AgentCapabilities())

    assert app.trigger_calls == [
        (
            "route",
            {
                "route": f"{auth_level}-reports",
                "methods": ["POST"],
                "auth_level": expected,
            },
        )
    ]


@pytest.mark.parametrize(
    ("auth", "expected"),
    [
        ("anonymous", func.AuthLevel.ANONYMOUS),
        ("function", func.AuthLevel.FUNCTION),
        ("admin", func.AuthLevel.ADMIN),
        ("entra", func.AuthLevel.ANONYMOUS),
    ],
)
def test_register_agent_accepts_nested_auth_string_shorthand(
    monkeypatch: pytest.MonkeyPatch,
    auth: str,
    expected: func.AuthLevel,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(
            type="http_trigger",
            args={"route": f"{auth}-reports", "http_auth": auth},
        )
    )
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_http_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    register_agent(app, resolved, AgentCapabilities())

    assert app.trigger_calls == [
        (
            "route",
            {
                "route": f"{auth}-reports",
                "methods": ["POST"],
                "auth_level": expected,
            },
        )
    ]


def test_register_agent_accepts_nested_auth_object_with_entra_allowlists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _capture(resolved: Any, capabilities: Any, catalog: Any = None, *, auth: Any) -> Any:
        captured["auth"] = auth
        return _stub_handler

    resolved = _resolved_agent(
        trigger=TriggerSpec(
            type="http_trigger",
            args={
                "route": "secured",
                "http_auth": {"mode": "entra", "entra": {"tenant_id": "t-1"}},
            },
        )
    )
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_http_agent_handler",
        _capture,
    )

    register_agent(app, resolved, AgentCapabilities())

    assert captured["auth"].mode == "entra"
    assert captured["auth"].entra is not None
    assert captured["auth"].entra.tenant_id == "t-1"
    assert app.trigger_calls == [
        (
            "route",
            {"route": "secured", "methods": ["POST"], "auth_level": func.AuthLevel.ANONYMOUS},
        )
    ]


def test_register_agent_nested_auth_wins_over_flat_auth_level(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(
            type="http_trigger",
            args={"route": "secured", "http_auth": "entra", "auth_level": "function"},
        )
    )
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_http_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    with caplog.at_level(logging.WARNING):
        register_agent(app, resolved, AgentCapabilities())

    assert "'auth_level' is deprecated and ignored" in caplog.text
    assert app.trigger_calls == [
        (
            "route",
            {"route": "secured", "methods": ["POST"], "auth_level": func.AuthLevel.ANONYMOUS},
        )
    ]


def test_register_agent_flat_auth_level_logs_deprecation_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(
            type="http_trigger",
            args={"route": "reports", "auth_level": "admin"},
        )
    )
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_http_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    with caplog.at_level(logging.WARNING):
        register_agent(app, resolved, AgentCapabilities())

    assert "'auth_level' is deprecated" in caplog.text
    assert app.trigger_calls == [
        (
            "route",
            {"route": "reports", "methods": ["POST"], "auth_level": func.AuthLevel.ADMIN},
        )
    ]


def test_register_agent_raises_on_invalid_nested_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(
            type="http_trigger",
            args={"route": "reports", "http_auth": {"mode": "nope"}},
        )
    )
    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_http_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    with pytest.raises(ValueError, match="invalid http_trigger 'http_auth'"):
        register_agent(app, resolved, AgentCapabilities())


def test_register_agent_propagates_connector_trigger_registration_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(type="connector_trigger", args={"connection": "example"})
    )
    app = FakeFunctionApp(function_name_error=ValueError("connector registration failed"))
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    with pytest.raises(ValueError, match="connector registration failed"):
        register_agent(app, resolved, AgentCapabilities())


def test_register_agent_dispatches_connector_trigger_to_builtin_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(type="connector_trigger", args={"connection": "example"})
    )
    app = FakeFunctionApp()
    capabilities = AgentCapabilities()
    builtin_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers._register_builtin_agent",
        lambda *args: builtin_calls.append(args),
    )

    register_agent(app, resolved, capabilities)

    assert builtin_calls == [
        (
            app,
            resolved,
            capabilities,
            _function_name_from_source(resolved.source_file, resolved.name),
            {"connection": "example"},
            "connector_trigger",
            None,
        )
    ]


def test_register_agent_falls_back_to_generic_connector_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AppWithoutNativeConnectorTrigger:
        def __init__(self) -> None:
            self.trigger_calls: list[tuple[str, dict[str, Any]]] = []
            self.function_names: list[str] = []

        def generic_trigger(self, **kwargs: Any) -> Any:
            def decorator(handler: Any) -> Any:
                self.trigger_calls.append(("generic_trigger", kwargs))
                return handler

            return decorator

        def function_name(self, *, name: str) -> Any:
            def decorator(handler: Any) -> Any:
                self.function_names.append(name)
                return handler

            return decorator

    resolved = _resolved_agent(
        trigger=TriggerSpec(type="connector_trigger", args={"connection_name": "office365"})
    )
    app = AppWithoutNativeConnectorTrigger()
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers.make_agent_handler",
        lambda *args, **kwargs: _stub_handler,
    )

    register_agent(app, resolved, AgentCapabilities())  # type: ignore[arg-type]

    assert app.trigger_calls == [
        (
            "generic_trigger",
            {
                "connection_name": "office365",
                "type": "connectorTrigger",
                "arg_name": "trigger_data",
            },
        )
    ]
    assert app.function_names == [_function_name_from_source(resolved.source_file, resolved.name)]


def test_register_agent_registers_non_http_trigger_on_main_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(type="timer_trigger", args={"schedule": "0 0 * * * *"}),
        is_main=True,
    )
    app = FakeFunctionApp()
    capabilities = AgentCapabilities()
    builtin_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers._register_builtin_agent",
        lambda *args: builtin_calls.append(args),
    )

    register_agent(app, resolved, capabilities)

    assert builtin_calls == [
        (
            app,
            resolved,
            capabilities,
            _function_name_from_source(resolved.source_file, resolved.name),
            {"schedule": "0 0 * * * *"},
            "timer_trigger",
            None,
        )
    ]


def test_register_agent_registers_http_trigger_on_main_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(type="http_trigger", args={"route": "reports"}),
        is_main=True,
    )
    app = FakeFunctionApp()
    http_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers._register_http_agent",
        lambda *args: http_calls.append(args),
    )

    capabilities = AgentCapabilities()

    register_agent(app, resolved, capabilities)

    assert http_calls == [
        (
            app,
            resolved,
            capabilities,
            _function_name_from_source(resolved.source_file, resolved.name),
            {"route": "reports"},
            None,
        )
    ]
    assert app.trigger_calls == []


def test_register_agent_dispatches_non_connector_trigger_types_to_builtin_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved_agent(
        trigger=TriggerSpec(type="queue_trigger", args={"queue_name": "reports"})
    )
    app = FakeFunctionApp()
    capabilities = AgentCapabilities()
    builtin_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "azure_functions_agents.registration.triggers._register_builtin_agent",
        lambda *args: builtin_calls.append(args),
    )

    register_agent(app, resolved, capabilities)

    assert builtin_calls == [
        (
            app,
            resolved,
            capabilities,
            _function_name_from_source(resolved.source_file, resolved.name),
            {"queue_name": "reports"},
            "queue_trigger",
            None,
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
