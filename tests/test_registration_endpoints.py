from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import azure.functions as func
import pytest

from azure_functions_agents.config.schema import DebugConfig, ResolvedAgent, ToolsFilter
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.endpoints import (
    _run_debug_agent,
    _run_debug_agent_stream,
    register_debug_endpoints,
)


class FakeFunctionApp:
    def __init__(self) -> None:
        self.routes: list[dict[str, Any]] = []

    def route(self, **kwargs: Any) -> Any:
        def decorator(handler: Any) -> Any:
            self.routes.append({"handler": handler, **kwargs})
            return handler

        return decorator

    def function_name(self, *, name: str) -> Any:
        def decorator(handler: Any) -> Any:
            for route in self.routes:
                if route["handler"] is handler:
                    route["function_name"] = name
                    break
            return handler

        return decorator


class DummyRequest:
    def __init__(self, payload: Any, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    async def json(self) -> Any:
        return self._payload


def _resolved_agent(
    *,
    name: str,
    is_main: bool,
    debug: DebugConfig,
    source_file: str | Path | None = None,
    input_schema: dict[str, Any] | None = None,
) -> ResolvedAgent:
    source = source_file or Path(__file__).resolve()
    return ResolvedAgent(
        name=name,
        description="desc",
        trigger=None,
        instructions="Assist the user.",
        is_main=is_main,
        debug=debug,
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        connector_specs=[],
        input_schema=input_schema,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=str(source) if source is not None else None,
    )


def _response_text(response: func.HttpResponse) -> str:
    body = response.body
    return body.decode("utf-8") if isinstance(body, bytes) else str(body)


def test_register_debug_endpoints_serves_agent_aware_chat_ui_for_non_main_agent(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_file = tmp_path / "secondary_agent.agent.md"
    source_file.write_text("---\nname: Secondary Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Secondary Agent",
        is_main=False,
        debug=DebugConfig(chat=True),
        source_file=source_file,
    )

    register_debug_endpoints(app, resolved, AgentCapabilities())

    chat_page_route = app.routes[0]
    assert chat_page_route["route"] == "agents/secondary_agent/"
    assert chat_page_route["methods"] == ["GET"]
    assert chat_page_route["auth_level"] == func.AuthLevel.ANONYMOUS

    response = chat_page_route["handler"](SimpleNamespace(path_params={}))

    assert response.status_code == 200
    html = _response_text(response)
    # Confirm the chat UI uses the prefix-preserving regex so non-main pages also
    # work when served behind Azure Functions' default `api` route prefix or
    # any reverse-proxy prefix (matches the trailing `/agents/{slug}` segment).
    assert r'pathname.match(/^(.*)\/agents\/([^/]+)\/?$/)' in html
    # Fallback when no /agents/{slug} segment matches preserves the page prefix
    # while still resolving `/` to `/agent`.
    assert 'window.location.pathname === "/"' in html
    assert 'window.location.pathname.endsWith("/")' in html


def test_register_debug_endpoints_uses_filename_slug_for_duplicate_display_names(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_a = tmp_path / "daily_report_a.agent.md"
    source_b = tmp_path / "daily_report_b.agent.md"
    source_a.write_text("---\nname: Daily Report\n---\n", encoding="utf-8")
    source_b.write_text("---\nname: Daily Report\n---\n", encoding="utf-8")

    register_debug_endpoints(
        app,
        _resolved_agent(
            name="Daily Report",
            is_main=False,
            debug=DebugConfig(chat=True),
            source_file=source_a,
        ),
        AgentCapabilities(),
    )
    register_debug_endpoints(
        app,
        _resolved_agent(
            name="Daily Report",
            is_main=False,
            debug=DebugConfig(chat=True),
            source_file=source_b,
        ),
        AgentCapabilities(),
    )

    assert [route["route"] for route in app.routes] == [
        "agents/daily_report_a/",
        "agents/daily_report_a/chat",
        "agents/daily_report_a/chatstream",
        "agents/daily_report_b/",
        "agents/daily_report_b/chat",
        "agents/daily_report_b/chatstream",
    ]


def test_register_debug_endpoints_reports_sanitized_slug_collisions(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_a = tmp_path / "daily-report.agent.md"
    source_b = tmp_path / "daily_report.agent.md"
    source_a.write_text("---\nname: Daily Report Dash\n---\n", encoding="utf-8")
    source_b.write_text("---\nname: Daily Report Underscore\n---\n", encoding="utf-8")

    register_debug_endpoints(
        app,
        _resolved_agent(
            name="Daily Report Dash",
            is_main=False,
            debug=DebugConfig(chat=True),
            source_file=source_a,
        ),
        AgentCapabilities(),
    )

    with pytest.raises(ValueError, match="sanitize to the same debug slug 'daily_report'") as exc_info:
        register_debug_endpoints(
            app,
            _resolved_agent(
                name="Daily Report Underscore",
                is_main=False,
                debug=DebugConfig(chat=True),
                source_file=source_b,
            ),
            AgentCapabilities(),
        )

    assert "Rename one so the sanitized slug is unique" in str(exc_info.value)


def test_run_debug_agent_generates_session_id_before_building_sandbox_tools(
    monkeypatch: Any,
) -> None:
    resolved = _resolved_agent(name="Secondary Agent", is_main=False, debug=DebugConfig(chat=True))
    calls: dict[str, Any] = {}

    class FakeUuid:
        hex = "generated-session-id"

    def fake_build_sandbox_tools_for_session(
        build_resolved: ResolvedAgent, session_id: str | None
    ) -> list[str]:
        calls["sandbox"] = (build_resolved, session_id)
        return ["sandbox-tool"]

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        calls["run_agent"] = kwargs
        return SimpleNamespace(session_id=kwargs["session_id"], content="ok", tool_calls=[])

    monkeypatch.setattr("azure_functions_agents.registration.endpoints.uuid.uuid4", lambda: FakeUuid())
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.build_sandbox_tools_for_session",
        fake_build_sandbox_tools_for_session,
    )
    monkeypatch.setattr("azure_functions_agents.registration.endpoints._run_agent", fake_run_agent)

    result = asyncio.run(
        _run_debug_agent(
            "hello",
            resolved=resolved,
            capabilities=AgentCapabilities(),
            session_id=None,
        )
    )

    assert calls["sandbox"] == (resolved, "generated-session-id")
    assert calls["run_agent"]["session_id"] == "generated-session-id"
    assert calls["run_agent"]["sandbox_tools"] == ["sandbox-tool"]
    assert result.session_id == "generated-session-id"


def test_run_debug_agent_stream_generates_session_id_before_building_sandbox_tools(
    monkeypatch: Any,
) -> None:
    resolved = _resolved_agent(name="Secondary Agent", is_main=False, debug=DebugConfig(chat=True))
    calls: dict[str, Any] = {}

    class FakeUuid:
        hex = "generated-stream-session-id"

    def fake_build_sandbox_tools_for_session(
        build_resolved: ResolvedAgent, session_id: str | None
    ) -> list[str]:
        calls["sandbox"] = (build_resolved, session_id)
        return ["sandbox-tool"]

    def fake_run_agent_stream(*args: Any, **kwargs: Any) -> str:
        calls["run_agent_stream"] = kwargs
        return "stream"

    monkeypatch.setattr("azure_functions_agents.registration.endpoints.uuid.uuid4", lambda: FakeUuid())
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.build_sandbox_tools_for_session",
        fake_build_sandbox_tools_for_session,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_agent_stream",
        fake_run_agent_stream,
    )

    result = _run_debug_agent_stream(
        "hello",
        resolved=resolved,
        capabilities=AgentCapabilities(),
        session_id=None,
    )

    assert calls["sandbox"] == (resolved, "generated-stream-session-id")
    assert calls["run_agent_stream"]["session_id"] == "generated-stream-session-id"
    assert calls["run_agent_stream"]["sandbox_tools"] == ["sandbox-tool"]
    assert result == "stream"


def test_register_debug_endpoints_chat_also_registers_http_routes_for_non_main_agent(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_file = tmp_path / "secondary_agent.agent.md"
    source_file.write_text("---\nname: Secondary Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Secondary Agent",
        is_main=False,
        debug=DebugConfig(chat=True),
        source_file=source_file,
    )

    register_debug_endpoints(app, resolved, AgentCapabilities())

    assert [route["route"] for route in app.routes] == [
        "agents/secondary_agent/",
        "agents/secondary_agent/chat",
        "agents/secondary_agent/chatstream",
    ]


def test_register_debug_endpoints_chat_and_http_do_not_double_register_routes(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_file = tmp_path / "secondary_agent.agent.md"
    source_file.write_text("---\nname: Secondary Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Secondary Agent",
        is_main=False,
        debug=DebugConfig(chat=True, http=True),
        source_file=source_file,
    )

    register_debug_endpoints(app, resolved, AgentCapabilities())

    assert [route["route"] for route in app.routes] == [
        "agents/secondary_agent/",
        "agents/secondary_agent/chat",
        "agents/secondary_agent/chatstream",
    ]


def test_debug_chat_endpoint_skips_input_schema_validation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    app = FakeFunctionApp()
    source_file = tmp_path / "secondary_agent.agent.md"
    source_file.write_text("---\nname: Secondary Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Secondary Agent",
        is_main=False,
        debug=DebugConfig(chat=True),
        source_file=source_file,
        input_schema={"type": "object", "required": ["subscription_id"]},
    )
    run_calls: dict[str, Any] = {}

    async def fake_run_debug_agent(prompt: str, **kwargs: Any) -> Any:
        run_calls["prompt"] = prompt
        run_calls["kwargs"] = kwargs
        return SimpleNamespace(session_id="session-123", content="ok", tool_calls=[])

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_debug_agent",
        fake_run_debug_agent,
    )

    register_debug_endpoints(app, resolved, AgentCapabilities())
    chat_route = next(route for route in app.routes if route["route"] == "agents/secondary_agent/chat")

    response = asyncio.run(chat_route["handler"](DummyRequest({"prompt": "hello"})))

    assert response.status_code == 200
    assert json.loads(_response_text(response)) == {
        "session_id": "session-123",
        "response": "ok",
        "tool_calls": [],
    }
    assert run_calls["prompt"] == "hello"


def test_debug_chat_stream_endpoint_skips_input_schema_validation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    app = FakeFunctionApp()
    source_file = tmp_path / "secondary_agent.agent.md"
    source_file.write_text("---\nname: Secondary Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Secondary Agent",
        is_main=False,
        debug=DebugConfig(chat=True),
        source_file=source_file,
        input_schema={"type": "object", "required": ["subscription_id"]},
    )
    run_calls: dict[str, Any] = {}

    async def fake_stream() -> Any:
        yield "data: hello\n\n"

    def fake_run_debug_agent_stream(prompt: str, **kwargs: Any) -> Any:
        run_calls["prompt"] = prompt
        run_calls["kwargs"] = kwargs
        return fake_stream()

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_debug_agent_stream",
        fake_run_debug_agent_stream,
    )

    register_debug_endpoints(app, resolved, AgentCapabilities())
    stream_route = next(
        route for route in app.routes if route["route"] == "agents/secondary_agent/chatstream"
    )

    response = asyncio.run(stream_route["handler"](DummyRequest({"prompt": "hello"})))

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert run_calls["prompt"] == "hello"
