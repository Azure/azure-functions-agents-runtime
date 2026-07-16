from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_type_hints

import azure.functions as func
import pytest

from azure_functions_agents.config.schema import BuiltinEndpointsConfig, ResolvedAgent, ToolsFilter
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.endpoints import (
    _run_builtin_agent,
    _run_builtin_agent_stream,
    register_builtin_endpoints,
)


class FakeFunctionApp:
    def __init__(self) -> None:
        self.routes: list[dict[str, Any]] = []
        self.durable_clients: dict[Any, str] = {}  # handler -> client_name mapping

    def route(self, **kwargs: Any) -> Any:
        def decorator(handler: Any) -> Any:
            route_info = {"handler": handler, **kwargs}
            # Check if this handler has a durable client input
            if handler in self.durable_clients:
                route_info["durable_client_input"] = self.durable_clients[handler]
            self.routes.append(route_info)
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

    def durable_client_input(self, *, client_name: str) -> Any:
        def decorator(handler: Any) -> Any:
            # Store the client name for this handler
            self.durable_clients[handler] = client_name
            # Also update any existing routes with this handler
            for route in self.routes:
                if route["handler"] is handler:
                    route["durable_client_input"] = client_name
                    break
            return handler

        return decorator

    def mcp_tool_trigger(self, **kwargs: Any) -> Any:
        def decorator(handler: Any) -> Any:
            route_info = {"handler": handler, "mcp_tool_trigger": True, **kwargs}
            # Check if this handler has a durable client input
            if handler in self.durable_clients:
                route_info["durable_client_input"] = self.durable_clients[handler]
            self.routes.append(route_info)
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
    builtin_endpoints: BuiltinEndpointsConfig,
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
        builtin_endpoints=builtin_endpoints,
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=input_schema,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=str(source) if source is not None else None,
    )


def _response_text(response: func.HttpResponse) -> str:
    body = response.body
    return body.decode("utf-8") if isinstance(body, bytes) else str(body)


def test_register_builtin_endpoints_serves_agent_aware_debug_chat_ui(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_file = tmp_path / "secondary_agent.agent.md"
    source_file.write_text("---\nname: Secondary Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Secondary Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True),
        source_file=source_file,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities())

    chat_page_route = app.routes[0]
    assert chat_page_route["route"] == "agents/secondary_agent/"
    assert chat_page_route["methods"] == ["GET"]
    assert chat_page_route["auth_level"] == func.AuthLevel.ANONYMOUS

    response = chat_page_route["handler"](SimpleNamespace(path_params={}))

    assert response.status_code == 200
    html = _response_text(response)
    # Confirm the chat UI uses the prefix-preserving regex so agent pages also
    # work when served behind Azure Functions' default `api` route prefix or
    # any reverse-proxy prefix (matches the trailing `/agents/{slug}` segment).
    assert r"pathname.match(/^(.*)\/agents\/([^/]+)\/?$/)" in html
    # Fallback when no /agents/{slug} segment matches points at /agents/main.
    assert 'window.location.pathname === "/"' in html
    assert 'window.location.pathname.endsWith("/")' in html
    assert 'return "/agents/main"' in html


def test_register_builtin_endpoints_uses_filename_slug_for_duplicate_display_names(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_a = tmp_path / "daily_report_a.agent.md"
    source_b = tmp_path / "daily_report_b.agent.md"
    source_a.write_text("---\nname: Daily Report\n---\n", encoding="utf-8")
    source_b.write_text("---\nname: Daily Report\n---\n", encoding="utf-8")

    register_builtin_endpoints(
        app,
        _resolved_agent(
            name="Daily Report",
            is_main=False,
            builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True),
            source_file=source_a,
        ),
        AgentCapabilities(),
    )
    register_builtin_endpoints(
        app,
        _resolved_agent(
            name="Daily Report",
            is_main=False,
            builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True),
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


def test_register_builtin_endpoints_fails_fast_on_sanitized_slug_collisions(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Same-slug collisions fail fast instead of auto-suffixing (FRD 0006 Decision #17)."""
    app = FakeFunctionApp()
    source_a = tmp_path / "daily-report.agent.md"
    source_b = tmp_path / "daily_report.agent.md"
    source_a.write_text("---\nname: Daily Report Dash\n---\n", encoding="utf-8")
    source_b.write_text("---\nname: Daily Report Underscore\n---\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        register_builtin_endpoints(
            app,
            _resolved_agent(
                name="Daily Report Dash",
                is_main=False,
                builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True),
                source_file=source_a,
            ),
            AgentCapabilities(),
        )
        with pytest.raises(ValueError, match="Built-in endpoint slug collision"):
            register_builtin_endpoints(
                app,
                _resolved_agent(
                    name="Daily Report Underscore",
                    is_main=False,
                    builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True),
                    source_file=source_b,
                ),
                AgentCapabilities(),
            )

    assert [route["route"] for route in app.routes] == [
        "agents/daily_report/",
        "agents/daily_report/chat",
        "agents/daily_report/chatstream",
    ]
    assert "Built-in endpoint slug collision" in caplog.text
    assert "'daily_report.agent.md' would register at '/agents/daily_report/'" in caplog.text


def test_run_builtin_agent_generates_session_id_before_building_sandbox_tools(
    monkeypatch: Any,
) -> None:
    resolved = _resolved_agent(name="Secondary Agent", is_main=False, builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True))
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

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.uuid.uuid4", lambda: FakeUuid()
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.build_sandbox_tools_for_session",
        fake_build_sandbox_tools_for_session,
    )
    monkeypatch.setattr("azure_functions_agents.registration.endpoints._run_agent", fake_run_agent)

    result = asyncio.run(
        _run_builtin_agent(
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


def test_run_builtin_agent_stream_generates_session_id_before_building_sandbox_tools(
    monkeypatch: Any,
) -> None:
    resolved = _resolved_agent(name="Secondary Agent", is_main=False, builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True))
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

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.uuid.uuid4", lambda: FakeUuid()
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.build_sandbox_tools_for_session",
        fake_build_sandbox_tools_for_session,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_agent_stream",
        fake_run_agent_stream,
    )

    result = _run_builtin_agent_stream(
        "hello",
        resolved=resolved,
        capabilities=AgentCapabilities(),
        session_id=None,
    )

    assert calls["sandbox"] == (resolved, "generated-stream-session-id")
    assert calls["run_agent_stream"]["session_id"] == "generated-stream-session-id"
    assert calls["run_agent_stream"]["sandbox_tools"] == ["sandbox-tool"]
    assert result == "stream"


def test_register_builtin_endpoints_chat_also_registers_http_routes_for_non_main_agent(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_file = tmp_path / "secondary_agent.agent.md"
    source_file.write_text("---\nname: Secondary Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Secondary Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True),
        source_file=source_file,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities())

    assert [route["route"] for route in app.routes] == [
        "agents/secondary_agent/",
        "agents/secondary_agent/chat",
        "agents/secondary_agent/chatstream",
    ]


def test_register_builtin_endpoints_chat_and_http_do_not_double_register_routes(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_file = tmp_path / "secondary_agent.agent.md"
    source_file.write_text("---\nname: Secondary Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Secondary Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True, chat_api=True),
        source_file=source_file,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities())

    assert [route["route"] for route in app.routes] == [
        "agents/secondary_agent/",
        "agents/secondary_agent/chat",
        "agents/secondary_agent/chatstream",
    ]


def test_register_builtin_endpoints_main_agent_uses_regular_agent_routes(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_file = tmp_path / "main.agent.md"
    source_file.write_text("---\nname: Main Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Main Agent",
        is_main=True,
        builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True),
        source_file=source_file,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities())

    assert [route["route"] for route in app.routes] == [
        "agents/main/",
        "agents/main/chat",
        "agents/main/chatstream",
    ]
    assert [route["function_name"] for route in app.routes] == [
        "agent_main_builtin_chat_page",
        "agent_main_builtin_chat",
        "agent_main_builtin_chatstream",
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
        builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True),
        source_file=source_file,
        input_schema={"type": "object", "required": ["subscription_id"]},
    )
    run_calls: dict[str, Any] = {}

    async def fake_run_builtin_agent(prompt: str, **kwargs: Any) -> Any:
        run_calls["prompt"] = prompt
        run_calls["kwargs"] = kwargs
        return SimpleNamespace(session_id="session-123", content="ok", tool_calls=[])

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_builtin_agent",
        fake_run_builtin_agent,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities())
    chat_route = next(
        route for route in app.routes if route["route"] == "agents/secondary_agent/chat"
    )

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
        builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True),
        source_file=source_file,
        input_schema={"type": "object", "required": ["subscription_id"]},
    )
    run_calls: dict[str, Any] = {}

    async def fake_stream() -> Any:
        yield "data: hello\n\n"

    def fake_run_builtin_agent_stream(prompt: str, **kwargs: Any) -> Any:
        run_calls["prompt"] = prompt
        run_calls["kwargs"] = kwargs
        return fake_stream()

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_builtin_agent_stream",
        fake_run_builtin_agent_stream,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities())
    stream_route = next(
        route for route in app.routes if route["route"] == "agents/secondary_agent/chatstream"
    )

    response = asyncio.run(stream_route["handler"](DummyRequest({"prompt": "hello"})))

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert run_calls["prompt"] == "hello"


def test_register_builtin_endpoints_without_workflows_has_no_client_parameter(
    tmp_path: Path,
) -> None:
    """When workflows_enabled=False, chat endpoints should not have a client parameter."""
    import inspect

    app = FakeFunctionApp()
    source_file = tmp_path / "test_agent.agent.md"
    source_file.write_text("---\nname: Test Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Test Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
        source_file=source_file,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities(), workflows_enabled=False)

    # Check chat endpoint
    chat_route = next(route for route in app.routes if route["route"] == "agents/test_agent/chat")
    chat_handler = chat_route["handler"]
    chat_params = inspect.signature(chat_handler).parameters
    assert "client" not in chat_params, "Chat handler should not have 'client' parameter when workflows disabled"
    assert "__signature__" not in chat_handler.__dict__, "Chat handler should expose its natural signature"
    assert "durable_client_input" not in chat_route, "Chat handler should not have durable_client_input decorator"

    # Check chatstream endpoint
    stream_route = next(route for route in app.routes if route["route"] == "agents/test_agent/chatstream")
    stream_handler = stream_route["handler"]
    stream_params = inspect.signature(stream_handler).parameters
    assert "client" not in stream_params, "Stream handler should not have 'client' parameter when workflows disabled"
    assert "__signature__" not in stream_handler.__dict__, "Stream handler should expose its natural signature"
    assert "durable_client_input" not in stream_route, "Stream handler should not have durable_client_input decorator"


def test_register_builtin_endpoints_with_workflows_has_client_parameter(
    tmp_path: Path,
) -> None:
    """When workflows_enabled=True, chat endpoints should have a client parameter with durable_client_input."""
    import inspect

    app = FakeFunctionApp()
    source_file = tmp_path / "test_agent.agent.md"
    source_file.write_text("---\nname: Test Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Test Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
        source_file=source_file,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities(), workflows_enabled=True)

    # Check chat endpoint
    chat_route = next(route for route in app.routes if route["route"] == "agents/test_agent/chat")
    chat_handler = chat_route["handler"]
    chat_params = inspect.signature(chat_handler).parameters
    assert "client" in chat_params, "Chat handler should have 'client' parameter when workflows enabled"
    assert chat_params["client"].default is inspect.Parameter.empty
    assert get_type_hints(chat_handler)["client"] is str
    assert "durable_client_input" in chat_route, "Chat handler should have durable_client_input decorator"
    assert chat_route["durable_client_input"] == "client", "Durable client input should be named 'client'"

    # Check chatstream endpoint
    stream_route = next(route for route in app.routes if route["route"] == "agents/test_agent/chatstream")
    stream_handler = stream_route["handler"]
    stream_params = inspect.signature(stream_handler).parameters
    assert "client" in stream_params, "Stream handler should have 'client' parameter when workflows enabled"
    assert stream_params["client"].default is inspect.Parameter.empty
    assert get_type_hints(stream_handler)["client"] is str
    assert "durable_client_input" in stream_route, "Stream handler should have durable_client_input decorator"
    assert stream_route["durable_client_input"] == "client", "Durable client input should be named 'client'"


def test_register_builtin_endpoints_mcp_without_workflows_has_no_client_parameter(
    tmp_path: Path,
) -> None:
    """When workflows_enabled=False, MCP endpoint should not have a client parameter."""
    import inspect

    app = FakeFunctionApp()
    source_file = tmp_path / "test_agent.agent.md"
    source_file.write_text("---\nname: Test Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Test Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(mcp=True),
        source_file=source_file,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities(), workflows_enabled=False)

    # Find the MCP route (it uses mcp_tool_trigger decorator)
    mcp_routes = [route for route in app.routes if route.get("mcp_tool_trigger")]
    assert len(mcp_routes) == 1, "Should have exactly one MCP route"
    mcp_handler = mcp_routes[0]["handler"]
    mcp_params = inspect.signature(mcp_handler).parameters
    assert "client" not in mcp_params, "MCP handler should not have 'client' parameter when workflows disabled"
    assert "__signature__" not in mcp_handler.__dict__, "MCP handler should expose its natural signature"
    assert "durable_client_input" not in mcp_routes[0], "MCP handler should not have durable_client_input decorator"


def test_register_builtin_endpoints_mcp_with_workflows_has_client_parameter(
    tmp_path: Path,
) -> None:
    """When workflows_enabled=True, MCP endpoint should have a client parameter with durable_client_input."""
    import inspect

    app = FakeFunctionApp()
    source_file = tmp_path / "test_agent.agent.md"
    source_file.write_text("---\nname: Test Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Test Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(mcp=True),
        source_file=source_file,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities(), workflows_enabled=True)

    # Find the MCP route (it uses mcp_tool_trigger decorator)
    mcp_routes = [route for route in app.routes if route.get("mcp_tool_trigger")]
    assert len(mcp_routes) == 1, "Should have exactly one MCP route"
    mcp_handler = mcp_routes[0]["handler"]
    mcp_params = inspect.signature(mcp_handler).parameters
    assert "client" in mcp_params, "MCP handler should have 'client' parameter when workflows enabled"
    assert mcp_params["client"].default is inspect.Parameter.empty
    assert get_type_hints(mcp_handler)["client"] is str
    assert "durable_client_input" in mcp_routes[0], "MCP handler should have durable_client_input decorator"
    assert mcp_routes[0]["durable_client_input"] == "client", "Durable client input should be named 'client'"


def test_workflows_enabled_passes_client_to_run_builtin_agent(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """When workflows enabled, the client parameter should be passed to _run_builtin_agent."""
    app = FakeFunctionApp()
    source_file = tmp_path / "test_agent.agent.md"
    source_file.write_text("---\nname: Test Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Test Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
        source_file=source_file,
    )
    run_calls: dict[str, Any] = {}

    async def fake_run_builtin_agent(prompt: str, **kwargs: Any) -> Any:
        run_calls["kwargs"] = kwargs
        return SimpleNamespace(session_id="session-123", content="ok", tool_calls=[])

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_builtin_agent",
        fake_run_builtin_agent,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities(), workflows_enabled=True)
    chat_route = next(route for route in app.routes if route["route"] == "agents/test_agent/chat")

    # Call with a mock durable client
    mock_client = SimpleNamespace(name="mock_durable_client")
    response = asyncio.run(chat_route["handler"](DummyRequest({"prompt": "hello"}), client=mock_client))

    assert response.status_code == 200
    assert run_calls["kwargs"]["workflows_enabled"] is True
    assert run_calls["kwargs"]["durable_client"] is mock_client


def test_workflows_disabled_does_not_pass_client_to_run_builtin_agent(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """When workflows disabled, durable_client should be None in _run_builtin_agent."""
    app = FakeFunctionApp()
    source_file = tmp_path / "test_agent.agent.md"
    source_file.write_text("---\nname: Test Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Test Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
        source_file=source_file,
    )
    run_calls: dict[str, Any] = {}

    async def fake_run_builtin_agent(prompt: str, **kwargs: Any) -> Any:
        run_calls["kwargs"] = kwargs
        return SimpleNamespace(session_id="session-123", content="ok", tool_calls=[])

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_builtin_agent",
        fake_run_builtin_agent,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities(), workflows_enabled=False)
    chat_route = next(route for route in app.routes if route["route"] == "agents/test_agent/chat")

    response = asyncio.run(chat_route["handler"](DummyRequest({"prompt": "hello"})))

    assert response.status_code == 200
    assert run_calls["kwargs"]["workflows_enabled"] is False
    assert run_calls["kwargs"]["durable_client"] is None
