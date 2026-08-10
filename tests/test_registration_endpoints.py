from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_type_hints

import azure.functions as func
import pytest
from azurefunctions.extensions.http.fastapi import Response

from azure_functions_agents._session_id import SESSION_ID_PATTERN
from azure_functions_agents._slug import _function_name_from_source
from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    EndpointAuthConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.controller.http import ControllerResponse
from azure_functions_agents.controller.readiness import (
    SessionActivationNotFoundError,
    SessionRuntimeBinding,
    StateStoreBinding,
)
from azure_functions_agents.execution.backend import RunContext, RunEvent, RunHandle, RunStatus
from azure_functions_agents.execution.binding import AgentBinding
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.endpoints import (
    _MAX_HISTORY_REPLAY_MESSAGES,
    _SAFE_SESSION_ID_PATTERN,
    _extract_mcp_session_id,
    _run_agent_stream,
    _run_builtin_agent,
    _run_builtin_agent_stream,
    _start_aca_stream,
    register_builtin_endpoints,
    register_sandbox_management_endpoints,
)
from azure_functions_agents.runner import _SESSION_ID_PATTERN
from azure_functions_agents.session_state import AppIdentity
from azure_functions_agents.transport.transport_models import SandboxProvisioningLabels
from tests.doubles.fake_session_runtime import (
    DEFAULT_GROUP_RESOURCE_ID,
    FakeSandboxSessionHandle,
    FakeSandboxSessionProvider,
    FakeSessionStateStore,
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


def _runtime(tmp_path: Path) -> SessionRuntimeBinding:
    provider = FakeSandboxSessionProvider(FakeSandboxSessionHandle())
    store = FakeSessionStateStore()

    async def provider_factory() -> FakeSandboxSessionProvider:
        return provider

    async def state_store_factory() -> StateStoreBinding:
        return StateStoreBinding.create(
            store=store,
            state_store_fingerprint="s1-" + ("a" * 52),
        )

    return SessionRuntimeBinding.create(
        app_identity=AppIdentity.create(
            subscription_id="11111111-2222-3333-4444-555555555555",
            site_name="agent-app",
        ),
        sandbox_group_resource_id=DEFAULT_GROUP_RESOURCE_ID,
        script_root=tmp_path,
        provider_factory=provider_factory,
        state_store_factory=state_store_factory,
    )


def test_sandbox_management_routes_share_one_submission_auth_policy(tmp_path: Path) -> None:
    app = FakeFunctionApp()
    auth = EndpointAuthConfig(mode="function")

    register_sandbox_management_endpoints(
        app,  # type: ignore[arg-type]
        slug="test-agent",
        auth=auth,
        session_runtime=_runtime(tmp_path),
        binding=AgentBinding(agent_name="test-agent"),
    )

    assert {(route["route"], tuple(route["methods"])) for route in app.routes} == {
        ("agents/test-agent/sessions/{session_id}/runs/{run_id}", ("GET",)),
        ("agents/test-agent/sessions/{session_id}/runs/{run_id}/result", ("GET",)),
        ("agents/test-agent/sessions/{session_id}/runs/{run_id}/events", ("GET",)),
        ("agents/test-agent/sessions/{session_id}/runs/{run_id}/cancel", ("POST",)),
    }
    assert {route["auth_level"] for route in app.routes} == {func.AuthLevel.FUNCTION}
    events_route = next(route for route in app.routes if route["route"].endswith("/events"))
    assert get_type_hints(events_route["handler"])["return"] is Response


@pytest.mark.parametrize(
    ("source_file", "expected_slug"),
    [
        ("agent.md", "main"),
        ("summarizer.claude.md", "summarizer"),
    ],
)
def test_flexible_name_slug_drives_sandbox_management_routes(
    tmp_path: Path,
    source_file: str,
    expected_slug: str,
) -> None:
    slug = _function_name_from_source(source_file, "Display Name")
    app = FakeFunctionApp()

    register_sandbox_management_endpoints(
        app,  # type: ignore[arg-type]
        slug=slug,
        auth=EndpointAuthConfig(mode="function"),
        session_runtime=_runtime(tmp_path),
        binding=AgentBinding(agent_name=slug),
    )

    assert slug == expected_slug
    assert {route["route"] for route in app.routes} == {
        f"agents/{expected_slug}/sessions/{{session_id}}/runs/{{run_id}}",
        f"agents/{expected_slug}/sessions/{{session_id}}/runs/{{run_id}}/result",
        f"agents/{expected_slug}/sessions/{{session_id}}/runs/{{run_id}}/events",
        f"agents/{expected_slug}/sessions/{{session_id}}/runs/{{run_id}}/cancel",
    }


@pytest.mark.asyncio
async def test_sandbox_management_status_uses_the_registered_output_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    binding = AgentBinding(agent_name="test-agent", output_validator=lambda _: None)
    captured: dict[str, Any] = {}

    def create_backend(**kwargs: Any) -> object:
        captured["binding"] = kwargs["binding"]
        return object()

    async def status_response(*_args: Any, **_kwargs: Any) -> ControllerResponse:
        return ControllerResponse(status_code=200, body={"status": "failed"})

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        create_backend,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.read_status",
        status_response,
    )
    register_sandbox_management_endpoints(
        app,  # type: ignore[arg-type]
        slug="test-agent",
        auth=EndpointAuthConfig(mode="function"),
        session_runtime=_runtime(tmp_path),
        binding=binding,
    )
    route = next(route for route in app.routes if route["route"].endswith("{run_id}"))
    request = DummyRequest({})
    request.path_params = {"session_id": "session-1", "run_id": "run-1"}

    response = await route["handler"](request)

    assert response.status_code == 200
    assert captured["binding"] is binding


@pytest.mark.asyncio
async def test_sandbox_events_preflight_returns_non_streaming_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    calls: list[str] = []

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        lambda **_kwargs: object(),
    )

    async def missing_status(*_args: Any, **_kwargs: Any) -> ControllerResponse:
        calls.append("status")
        return ControllerResponse(status_code=404, body={"error": "run_not_found"})

    def unexpected_stream(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("SSE must not start before a successful preflight")

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.read_status",
        missing_status,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.render_events",
        unexpected_stream,
    )
    register_sandbox_management_endpoints(
        app,  # type: ignore[arg-type]
        slug="test-agent",
        auth=EndpointAuthConfig(mode="function"),
        session_runtime=_runtime(tmp_path),
        binding=AgentBinding(agent_name="test-agent"),
    )
    route = next(route for route in app.routes if route["route"].endswith("/events"))
    request = DummyRequest({})
    request.path_params = {"session_id": "missing-session", "run_id": "missing-run"}

    response = await route["handler"](request)

    assert response.status_code == 404
    assert calls == ["status"]


@pytest.mark.asyncio
async def test_sandbox_events_preflight_returns_the_gone_status_without_sse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        lambda **_kwargs: object(),
    )

    async def gone_status(*_args: Any, **_kwargs: Any) -> ControllerResponse:
        return ControllerResponse(
            status_code=200,
            body={
                "status": "abandoned",
                "error": {"code": "session_tombstoned"},
            },
        )

    def unexpected_stream(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("gone sessions must not start SSE")

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.read_status",
        gone_status,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.render_events",
        unexpected_stream,
    )
    register_sandbox_management_endpoints(
        app,  # type: ignore[arg-type]
        slug="test-agent",
        auth=EndpointAuthConfig(mode="function"),
        session_runtime=_runtime(tmp_path),
        binding=AgentBinding(agent_name="test-agent"),
    )
    route = next(route for route in app.routes if route["route"].endswith("/events"))
    request = DummyRequest({})
    request.path_params = {"session_id": "gone-session", "run_id": "gone-run"}

    response = await route["handler"](request)

    assert response.status_code == 200
    assert json.loads(response.body)["error"]["code"] == "session_tombstoned"


@pytest.mark.asyncio
async def test_sandbox_events_preflight_touches_once_before_starting_sse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    touches = 0
    rendered: dict[str, Any] = {}

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        lambda **_kwargs: object(),
    )

    async def touch(*_args: Any, **_kwargs: Any) -> None:
        nonlocal touches
        touches += 1

    async def ready_status(*_args: Any, **_kwargs: Any) -> ControllerResponse:
        return ControllerResponse(status_code=200, body={"status": "running"})

    async def stream() -> Any:
        if False:
            yield ""

    def render(*args: Any, **kwargs: Any) -> Any:
        rendered["args"] = args
        rendered["kwargs"] = kwargs
        return stream()

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.touch_session_activity",
        touch,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.read_status",
        ready_status,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.render_events",
        render,
    )
    register_sandbox_management_endpoints(
        app,  # type: ignore[arg-type]
        slug="test-agent",
        auth=EndpointAuthConfig(mode="function"),
        session_runtime=_runtime(tmp_path),
        binding=AgentBinding(agent_name="test-agent"),
    )
    route = next(route for route in app.routes if route["route"].endswith("/events"))
    request = DummyRequest({}, headers={"Last-Event-ID": "3"})
    request.path_params = {"session_id": "session-1", "run_id": "run-1"}

    response = await route["handler"](request)

    assert response.status_code == 200
    assert touches == 1
    assert rendered["kwargs"]["after_sequence"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_id", "run_id"),
    [
        ("", "run-1"),
        ("session-1", ""),
        ("s" * 129, "run-1"),
        ("session-1", "r" * 129),
        ("bad/session", "run-1"),
        ("session-1", "bad/run"),
    ],
)
async def test_sandbox_management_routes_reject_invalid_path_identifiers(
    session_id: str,
    run_id: str,
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    register_sandbox_management_endpoints(
        app,  # type: ignore[arg-type]
        slug="test-agent",
        auth=EndpointAuthConfig(mode="function"),
        session_runtime=_runtime(tmp_path),
        binding=AgentBinding(agent_name="test-agent"),
    )

    for route in app.routes:
        request = DummyRequest({})
        request.path_params = {"session_id": session_id, "run_id": run_id}
        response = await route["handler"](request)
        assert response.status_code == 400
        assert json.loads(response.body) == {
            "error": "invalid session or run identifier"
        }


@pytest.mark.asyncio
async def test_sandbox_management_routes_accept_canonical_boundary_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    session_id = "s" * 128
    run_id = "r" * 128
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        lambda **_kwargs: object(),
    )

    async def status(*_args: Any, **_kwargs: Any) -> ControllerResponse:
        return ControllerResponse(status_code=200, body={"status": "running"})

    async def result(*_args: Any, **_kwargs: Any) -> ControllerResponse:
        return ControllerResponse(status_code=200, body={"status": "running"})

    async def cancel(*_args: Any, **_kwargs: Any) -> ControllerResponse:
        return ControllerResponse(status_code=200, body={"status": "canceled"})

    async def stream() -> Any:
        if False:
            yield ""

    async def touch(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.read_status",
        status,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.read_result",
        result,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.cancel_controller_run",
        cancel,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.render_events",
        lambda *_args, **_kwargs: stream(),
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.touch_session_activity",
        touch,
    )
    register_sandbox_management_endpoints(
        app,  # type: ignore[arg-type]
        slug="test-agent",
        auth=EndpointAuthConfig(mode="function"),
        session_runtime=_runtime(tmp_path),
        binding=AgentBinding(agent_name="test-agent"),
    )

    for route in app.routes:
        request = DummyRequest({})
        request.path_params = {"session_id": session_id, "run_id": run_id}
        response = await route["handler"](request)
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_builtin_stream_honors_respond_async(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_create_execution_backend(*args: Any, **kwargs: Any) -> object:
        del args
        captured.update(kwargs)
        return object()

    async def fake_submit_run(*args: Any, **kwargs: Any) -> ControllerResponse:
        del args
        captured["respond_async"] = kwargs["respond_async"]
        return ControllerResponse(
            status_code=202,
            body={"session_id": "session-1", "run_id": "run-1", "status": "accepted"},
        )

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        fake_create_execution_backend,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.submit_run",
        fake_submit_run,
    )

    response = await _start_aca_stream(
        prompt="hello",
        resolved=_resolved_agent(
            name="Test Agent",
            is_main=True,
            builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
            response_schema={"type": "object", "required": ["answer"]},
        ),
        session_id=None,
        idempotency_key=None,
        session_runtime=_runtime(tmp_path),
        owner=None,
        respond_async=True,
        after_sequence=0,
    )

    assert response.status_code == 202
    assert captured["respond_async"] is True
    assert callable(captured["binding"].output_validator)


@pytest.mark.asyncio
async def test_builtin_aca_helpers_thread_the_resolved_output_validator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[dict[str, Any]] = []
    resolved = _resolved_agent(
        name="Test Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(mcp=True),
        response_schema={"type": "object", "required": ["answer"]},
    )

    async def run_agent(*_args: Any, **kwargs: Any) -> object:
        captured.append(kwargs)
        return object()

    def run_agent_stream(*_args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)

        async def stream() -> Any:
            if False:
                yield ""

        return stream()

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_agent",
        run_agent,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_agent_stream",
        run_agent_stream,
    )

    await _run_builtin_agent(
        "hello",
        resolved=resolved,
        capabilities=AgentCapabilities(),
        session_id="session-1",
        session_runtime=_runtime(tmp_path),
        owner=None,
    )
    _run_builtin_agent_stream(
        "hello",
        resolved=resolved,
        capabilities=AgentCapabilities(),
        session_id="session-1",
        session_runtime=_runtime(tmp_path),
        owner=None,
    )

    assert len(captured) == 2
    assert all(callable(kwargs["output_validator"]) for kwargs in captured)


@pytest.mark.asyncio
async def test_builtin_mcp_aca_path_threads_the_resolved_output_validator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    captured: dict[str, Any] = {}
    resolved = _resolved_agent(
        name="Test Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(mcp=True),
        response_schema={"type": "object", "required": ["answer"]},
    )

    async def run_agent(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            session_id="session-1",
            content='{"answer":"ok"}',
            tool_calls=[],
            delegate_error_count=0,
        )

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_agent",
        run_agent,
    )
    register_builtin_endpoints(
        app,  # type: ignore[arg-type]
        resolved,
        AgentCapabilities(),
        session_runtime=_runtime(tmp_path),
    )
    route = next(route for route in app.routes if route.get("mcp_tool_trigger"))

    response = await route["handler"](
        json.dumps(
            {
                "arguments": {"prompt": "hello"},
                "sessionId": "session-1",
            }
        )
    )

    assert json.loads(response)["session_id"] == "session-1"
    assert callable(captured["output_validator"])


def test_builtin_chat_sync_uses_controller_session_header(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    async def fake_submit_run(*_args: Any, **_kwargs: Any) -> ControllerResponse:
        return ControllerResponse(
            status_code=200,
            body={"response": "answer", "tool_calls": []},
            headers={"x-ms-session-id": "new-session"},
        )

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.submit_run",
        fake_submit_run,
    )
    register_builtin_endpoints(
        app,
        _resolved_agent(
            name="Test Agent",
            is_main=False,
            builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
            source_file=tmp_path / "test.agent.md",
            response_schema={"type": "object", "required": ["answer"]},
        ),
        AgentCapabilities(),
        session_runtime=_runtime(tmp_path),
    )
    route = next(route for route in app.routes if route["route"].endswith("/chat"))

    response = asyncio.run(route["handler"](DummyRequest({"prompt": "hello"})))

    assert response.status_code == 200
    assert response.headers["x-ms-session-id"] == "new-session"
    assert json.loads(response.body) == {
        "session_id": "new-session",
        "response": "answer",
        "tool_calls": [],
    }
    assert callable(captured["binding"].output_validator)


def test_builtin_sandbox_http_maps_unknown_session_through_real_controller_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class MissingSessionBackend:
        async def start_run(self, _request: Any) -> None:
            raise SessionActivationNotFoundError("Session was not found for this owner.")

    app = FakeFunctionApp()
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        lambda **_kwargs: MissingSessionBackend(),
    )
    register_builtin_endpoints(
        app,
        _resolved_agent(
            name="Test Agent",
            is_main=False,
            builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
            source_file=tmp_path / "test.agent.md",
        ),
        AgentCapabilities(),
        session_runtime=_runtime(tmp_path),
    )
    route = next(route for route in app.routes if route["route"].endswith("/chat"))

    response = asyncio.run(
        route["handler"](
            DummyRequest(
                {"prompt": "hello"},
                headers={"x-ms-session-id": "unknown-session"},
            )
        )
    )

    assert response.status_code == 404
    assert json.loads(response.body) == {"error": "session_not_found"}
    assert "owner" not in response.body.decode("utf-8")


def test_run_agent_stream_adopts_terminal_state_after_normal_exhaustion(
    monkeypatch: Any,
) -> None:
    class StreamingBackend:
        def __init__(self) -> None:
            self.get_run_contexts: list[RunContext] = []

        async def start_run(self, _request: Any) -> RunHandle:
            return RunHandle(
                run_id="run-1",
                session_id="session-1",
                state="accepted",
                created_at=datetime.now(UTC),
            )

        def read_events(self, _context: RunContext, after_sequence: int) -> Any:
            del after_sequence

            async def events() -> Any:
                yield RunEvent(
                    sequence=1,
                    type="done",
                    data={"content": "answer"},
                    timestamp=datetime.now(UTC),
                )

            return events()

        async def get_run(self, context: RunContext) -> RunStatus:
            self.get_run_contexts.append(context)
            return RunStatus(
                run_id=context.run_id,
                session_id=context.session_id,
                state="succeeded",
                last_sequence=1,
                result_available=False,
            )

    backend = StreamingBackend()
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        lambda **_kwargs: backend,
    )

    async def consume() -> list[str]:
        stream = _run_agent_stream(prompt="hello", agent_name="main")
        return [event async for event in stream]

    events = asyncio.run(consume())

    assert events == ['data: {"type": "done", "content": "answer"}\n\n']
    assert backend.get_run_contexts == [RunContext(run_id="run-1", session_id="session-1")]


def test_run_agent_stream_does_not_adopt_after_client_disconnect(monkeypatch: Any) -> None:
    class StreamingBackend:
        def __init__(self) -> None:
            self.get_run_calls = 0

        async def start_run(self, _request: Any) -> RunHandle:
            return RunHandle(
                run_id="run-1",
                session_id="session-1",
                state="accepted",
                created_at=datetime.now(UTC),
            )

        def read_events(self, _context: RunContext, after_sequence: int) -> Any:
            del after_sequence

            async def events() -> Any:
                yield RunEvent(
                    sequence=1,
                    type="delta",
                    data={"content": "partial"},
                    timestamp=datetime.now(UTC),
                )
                await asyncio.Event().wait()

            return events()

        async def get_run(self, _context: RunContext) -> RunStatus:
            self.get_run_calls += 1
            raise AssertionError("A disconnected stream must not adopt the run.")

    backend = StreamingBackend()
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        lambda **_kwargs: backend,
    )

    async def disconnect() -> str:
        stream = _run_agent_stream(prompt="hello", agent_name="main")
        first_event = await anext(stream)
        await stream.aclose()
        return first_event

    event = asyncio.run(disconnect())

    assert event == 'data: {"type": "delta", "content": "partial"}\n\n'
    assert backend.get_run_calls == 0


def _resolved_agent(
    *,
    name: str,
    is_main: bool,
    builtin_endpoints: BuiltinEndpointsConfig,
    source_file: str | Path | None = None,
    input_schema: dict[str, Any] | None = None,
    response_schema: dict[str, Any] | None = None,
    # Deliberately distinct from `name` above (S1): identity/telemetry call
    # sites must key off `slug`, never the mutable display `name` (FRD 0007
    # §4.3, "Display `name` is never an identity"). Defaulted so existing
    # callers of this factory are unaffected; note route paths (e.g.
    # `agents/daily_report_a/`) are derived from `source_file`/`name` via
    # `_function_name_from_source`, not this `slug` field, so changing its
    # default here does not affect any route-path assertions.
    slug: str = "resolved-agent-slug",
) -> ResolvedAgent:
    source = source_file or Path(__file__).resolve()
    return ResolvedAgent(
        name=name,
        slug=slug,
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
        response_schema=response_schema,
        response_example=None,
        metadata={},
        source_file=str(source) if source is not None else None,
    )


def _response_text(response: func.HttpResponse) -> str:
    body = response.body
    return body.decode("utf-8") if isinstance(body, bytes) else str(body)


class _CapturedSpan:
    """Fake ``RuntimeSpan`` for asserting on built-in endpoints' own ``agent.run`` span (B3).

    Mirrors ``test_web_request.py``'s ``_CapturedSpan``. The built-in chat/MCP
    endpoints call ``run_agent``/``run_agent_stream`` directly rather than
    going through ``_handlers.py``'s trigger-registered handlers, so they now
    open their *own* ``agent.run {name}`` span (see the comment above
    ``start_span`` in ``handle_chat``/``handle_mcp_agent_chat``) instead of
    relying on a caller to have opened one.
    """

    def __init__(self, attributes: dict[str, Any]) -> None:
        self.attributes: dict[str, Any] = dict(attributes)
        self.errors: list[tuple[str, str]] = []
        self.exceptions: list[BaseException] = []
        self.content: dict[str, str] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        if value is not None:
            self.attributes[key] = value

    def set_content(self, key: str, value: str) -> None:
        self.content[key] = value

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        pass

    def set_error(self, message: str, *, fault_domain: str) -> None:
        self.errors.append((message, fault_domain))

    def record_exception(self, exc: BaseException, *, fault_domain: str | None = None) -> None:
        self.exceptions.append(exc)
        self.errors.append((str(exc), fault_domain or "unknown"))


def _install_start_span_capture(monkeypatch: Any) -> list[_CapturedSpan]:
    spans: list[_CapturedSpan] = []

    @contextlib.contextmanager
    def _fake_start_span(
        name: str,
        *,
        fault_domain: str | None = None,
        lifecycle_stage: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[_CapturedSpan]:
        span = _CapturedSpan(attributes or {})
        spans.append(span)
        yield span

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.start_span", _fake_start_span
    )
    return spans


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
        "agents/daily_report_a/history",
        "agents/daily_report_b/",
        "agents/daily_report_b/chat",
        "agents/daily_report_b/chatstream",
        "agents/daily_report_b/history",
    ]


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
    # S1: the coordinator/direct-role agent must be identified by
    # `resolved.slug` for telemetry, not the mutable display `name` (FRD
    # 0007 §4.3) -- matches round 2's B2 fix for delegated specialists.
    assert calls["run_agent"]["agent_name"] == resolved.slug
    assert calls["run_agent"]["agent_name"] != resolved.name


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
    # S1: same contract as the non-streaming builtin-agent test above.
    assert calls["run_agent_stream"]["agent_name"] == resolved.slug
    assert calls["run_agent_stream"]["agent_name"] != resolved.name


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
        "agents/secondary_agent/history",
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
        "agents/secondary_agent/history",
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
        "agents/main/history",
    ]
    assert [route["function_name"] for route in app.routes] == [
        "agent_main_builtin_chat_page",
        "agent_main_builtin_chat",
        "agent_main_builtin_chatstream",
        "agent_main_builtin_history",
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


def test_handle_chat_reports_delegate_error_count_on_span(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """B3 (built-in chat endpoint): a recoverable delegate failure must land on this endpoint's own span.

    ``handle_chat`` calls ``run_agent`` directly rather than going through
    ``_handlers.py``'s trigger-registered handlers, so nothing upstream
    applies ``_set_run_result_attributes``/``af.agent.tool_error_count`` for
    it. This asserts the endpoint now opens its own span and folds
    ``AgentResult.delegate_error_count`` into that span's
    ``af.agent.tool_error_count``, exactly like `_handlers.py` does for
    trigger-registered agents.
    """
    app = FakeFunctionApp()
    source_file = tmp_path / "secondary_agent.agent.md"
    source_file.write_text("---\nname: Secondary Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Secondary Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True),
        source_file=source_file,
    )
    spans = _install_start_span_capture(monkeypatch)

    async def fake_run_builtin_agent(prompt: str, **kwargs: Any) -> Any:
        return SimpleNamespace(
            session_id="session-123",
            content="ok, but the billing specialist failed",
            tool_calls=[{"name": "delegate_billing", "result": "ok"}],
            delegate_error_count=2,
        )

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
    [span] = spans
    assert span.attributes["af.agent.outcome"] == "success"
    # `_tool_error_count`'s JSON-envelope heuristic finds nothing wrong with
    # the one recorded tool call (`result: "ok"`) — the whole count of 2
    # comes from `delegate_error_count`, proving it is the piece that was
    # previously dropped on this surface.
    assert span.attributes["af.agent.tool_error_count"] == 2
    # S1: this endpoint's own span must identify the coordinator agent by
    # `resolved.slug`, not the mutable display `name` (FRD 0007 §4.3).
    assert span.attributes["af.agent.name"] == resolved.slug
    assert span.attributes["af.agent.name"] != resolved.name


def test_handle_mcp_agent_chat_reports_delegate_error_count_on_span(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """B3 (built-in MCP endpoint): same contract as the chat endpoint, for the MCP surface."""
    app = FakeFunctionApp()
    source_file = tmp_path / "test_agent.agent.md"
    source_file.write_text("---\nname: Test Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Test Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(mcp=True),
        source_file=source_file,
    )
    spans = _install_start_span_capture(monkeypatch)

    async def fake_run_builtin_agent(prompt: str, **kwargs: Any) -> Any:
        return SimpleNamespace(
            session_id="session-456",
            content="ok, but the shipping specialist failed",
            tool_calls=[],
            delegate_error_count=1,
        )

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_builtin_agent",
        fake_run_builtin_agent,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities(), workflows_enabled=False)
    mcp_routes = [route for route in app.routes if route.get("mcp_tool_trigger")]
    assert len(mcp_routes) == 1
    mcp_handler = mcp_routes[0]["handler"]

    result = asyncio.run(
        mcp_handler(json.dumps({"arguments": {"prompt": "hello"}, "sessionId": "session-456"}))
    )

    assert json.loads(result) == {
        "session_id": "session-456",
        "response": "ok, but the shipping specialist failed",
        "tool_calls": [],
    }
    [span] = spans
    assert span.attributes["af.agent.outcome"] == "success"
    assert span.attributes["af.agent.tool_error_count"] == 1
    # S1: same contract as the chat endpoint test above, for the MCP surface.
    assert span.attributes["af.agent.name"] == resolved.slug
    assert span.attributes["af.agent.name"] != resolved.name


def test_handle_mcp_agent_chat_refreshes_span_session_id_when_caller_omits_it(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """N1: when the caller supplies no explicit session id, the span's
    ``af.agent.session_id`` attribute must reflect the id the runner actually
    resolved/generated for the turn (``result.session_id``), not be left
    unset from the pre-call ``None``.
    """
    app = FakeFunctionApp()
    source_file = tmp_path / "test_agent.agent.md"
    source_file.write_text("---\nname: Test Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Test Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(mcp=True),
        source_file=source_file,
    )
    spans = _install_start_span_capture(monkeypatch)

    async def fake_run_builtin_agent(prompt: str, **kwargs: Any) -> Any:
        assert kwargs["session_id"] is None  # caller omitted it
        return SimpleNamespace(
            session_id="runner-generated-session-id",
            content="ok",
            tool_calls=[],
            delegate_error_count=0,
        )

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_builtin_agent",
        fake_run_builtin_agent,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities(), workflows_enabled=False)
    mcp_routes = [route for route in app.routes if route.get("mcp_tool_trigger")]
    assert len(mcp_routes) == 1
    mcp_handler = mcp_routes[0]["handler"]

    # No "sessionId" key at all -- the caller-omitted case.
    result = asyncio.run(mcp_handler(json.dumps({"arguments": {"prompt": "hello"}})))

    assert json.loads(result)["session_id"] == "runner-generated-session-id"
    [span] = spans
    assert span.attributes["af.agent.session_id"] == "runner-generated-session-id"


def test_extract_mcp_session_id_passes_through_safe_ids() -> None:
    """A caller-supplied id that already matches the safe pattern is kept as-is."""
    assert _extract_mcp_session_id({"sessionId": "session-456"}) == "session-456"
    assert _extract_mcp_session_id({"sessionId": "  trimmed.me_1  "}) == "trimmed.me_1"
    assert _extract_mcp_session_id({"sessionId": "a" * 63}) == "a" * 63


def test_extract_mcp_session_id_hashes_runner_safe_ids_that_exceed_label_limits() -> None:
    raw = "a" * 64

    sanitized = _extract_mcp_session_id({"sessionId": raw})

    assert sanitized is not None
    assert sanitized.startswith("mcp-")
    assert len(sanitized) == 56
    assert _SAFE_SESSION_ID_PATTERN.fullmatch(sanitized)
    assert sanitized == _extract_mcp_session_id({"sessionId": raw})
    assert sanitized != _extract_mcp_session_id({"sessionId": raw + "a"})
    assert SandboxProvisioningLabels.create(
        owner_hash_version="o1",
        owner_kind="function_app",
        owner_hash="o1-" + ("a" * 52),
        app_hash="a1-" + ("b" * 52),
        session_id=sanitized,
    ).session_id == sanitized
    assert _extract_mcp_session_id({"sessionId": ".runner-safe"}) != ".runner-safe"


def test_extract_mcp_session_id_returns_none_when_absent_or_blank() -> None:
    assert _extract_mcp_session_id({}) is None
    assert _extract_mcp_session_id({"sessionId": "   "}) is None
    assert _extract_mcp_session_id({"sessionId": 123}) is None


def test_safe_session_id_pattern_matches_runner_pattern() -> None:
    """The endpoint layer and the runner both validate session ids against the
    same rule. They now share a single source of truth
    (``_session_id.SESSION_ID_PATTERN``) rather than duplicating the regex, so
    pass-through session ids validated here remain valid in the runner. Assert
    both reference that shared object to catch any future re-duplication.
    """
    assert _SAFE_SESSION_ID_PATTERN is SESSION_ID_PATTERN
    assert _SESSION_ID_PATTERN is SESSION_ID_PATTERN



def test_extract_mcp_session_id_sanitizes_transport_ids() -> None:
    """The MCP extension mints transport ids the runner would reject (invalid
    characters or over the 128-char cap). They must be mapped deterministically
    into the runner's safe session-id space so the run doesn't fail validation.
    """
    # Streamable-HTTP style value with characters outside [A-Za-z0-9._-].
    raw = "urn:mcp:session:9f8b/6a2c+d4==@conn"
    sanitized = _extract_mcp_session_id({"sessionId": raw})
    assert sanitized is not None
    assert _SESSION_ID_PATTERN.match(sanitized), sanitized
    # Deterministic: same transport id -> same agent session (continuity).
    assert sanitized == _extract_mcp_session_id({"sessionId": raw})
    # Different transport id -> different agent session.
    assert sanitized != _extract_mcp_session_id({"sessionId": raw + "-other"})
    assert len(sanitized) <= 63
    assert SandboxProvisioningLabels.create(
        owner_hash_version="o1",
        owner_kind="function_app",
        owner_hash="o1-" + ("a" * 52),
        app_hash="a1-" + ("b" * 52),
        session_id=sanitized,
    ).session_id == sanitized

    # Over the 128-char length cap is also brought back into range.
    long_id = "a" * 200
    long_sanitized = _extract_mcp_session_id({"sessionId": long_id})
    assert long_sanitized is not None
    assert _SESSION_ID_PATTERN.match(long_sanitized), long_sanitized



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
    assert get_type_hints(stream_handler)["return"] is Response
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
    assert get_type_hints(stream_handler)["return"] is Response
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


def _chat_api_agent(tmp_path: Path, auth: EndpointAuthConfig) -> ResolvedAgent:
    source_file = tmp_path / "test_agent.agent.md"
    source_file.write_text("---\nname: Test Agent\n---\n", encoding="utf-8")
    return _resolved_agent(
        name="Test Agent",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True, http_auth=auth),
        source_file=source_file,
    )


class _FakeHistoryProvider:
    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages
        self.skip_excluded = False
        self.captured_session_id: str | None = None

    async def get_messages(self, session_id: str, **kwargs: Any) -> list[Any]:
        self.captured_session_id = session_id
        return self._messages


def _history_route(app: FakeFunctionApp) -> dict[str, Any]:
    return next(route for route in app.routes if route["route"] == "agents/test_agent/history")


def test_history_endpoint_registered_under_chat_api(tmp_path: Path) -> None:
    app = FakeFunctionApp()
    register_builtin_endpoints(
        app, _chat_api_agent(tmp_path, EndpointAuthConfig()), AgentCapabilities()
    )

    route = _history_route(app)

    assert route["methods"] == ["GET"]
    assert route["function_name"] == "agent_test_agent_builtin_history"
    assert route["auth_level"] == func.AuthLevel.FUNCTION


def test_history_endpoint_returns_empty_without_session_header(
    monkeypatch: Any, tmp_path: Path
) -> None:
    build_calls = {"count": 0}

    def fake_build(**kwargs: Any) -> Any:
        build_calls["count"] += 1
        return None

    monkeypatch.setattr(
        "azure_functions_agents._blob_history.build_blob_provider_from_environment",
        fake_build,
    )
    app = FakeFunctionApp()
    register_builtin_endpoints(
        app, _chat_api_agent(tmp_path, EndpointAuthConfig()), AgentCapabilities()
    )

    response = asyncio.run(_history_route(app)["handler"](DummyRequest({}, headers={})))

    assert response.status_code == 200
    assert json.loads(_response_text(response)) == {"messages": [], "truncated": False}
    # No session id: must short-circuit before touching storage.
    assert build_calls["count"] == 0


def test_history_endpoint_rejects_invalid_session_id(tmp_path: Path) -> None:
    app = FakeFunctionApp()
    register_builtin_endpoints(
        app, _chat_api_agent(tmp_path, EndpointAuthConfig()), AgentCapabilities()
    )

    request = DummyRequest({}, headers={"x-ms-session-id": "bad id!"})
    response = asyncio.run(_history_route(app)["handler"](request))

    assert response.status_code == 400
    assert json.loads(_response_text(response)) == {"error": "invalid session id"}


def test_history_endpoint_returns_empty_when_storage_unconfigured(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "azure_functions_agents._blob_history.build_blob_provider_from_environment",
        lambda **kwargs: None,
    )
    app = FakeFunctionApp()
    register_builtin_endpoints(
        app, _chat_api_agent(tmp_path, EndpointAuthConfig()), AgentCapabilities()
    )

    request = DummyRequest({}, headers={"x-ms-session-id": "abc123"})
    response = asyncio.run(_history_route(app)["handler"](request))

    assert response.status_code == 200
    assert json.loads(_response_text(response)) == {"messages": [], "truncated": False}


def test_history_endpoint_filters_to_user_and_assistant_text(
    monkeypatch: Any, tmp_path: Path
) -> None:
    provider = _FakeHistoryProvider(
        [
            SimpleNamespace(role="user", text="hi"),
            SimpleNamespace(role="assistant", text="hello"),
            SimpleNamespace(role="tool", text="tool output"),
            SimpleNamespace(role="assistant", text=""),
            SimpleNamespace(role="user", text="bye"),
        ]
    )
    monkeypatch.setattr(
        "azure_functions_agents._blob_history.build_blob_provider_from_environment",
        lambda **kwargs: provider,
    )
    app = FakeFunctionApp()
    register_builtin_endpoints(
        app, _chat_api_agent(tmp_path, EndpointAuthConfig()), AgentCapabilities()
    )

    request = DummyRequest({}, headers={"x-ms-session-id": "abc123"})
    response = asyncio.run(_history_route(app)["handler"](request))

    assert response.status_code == 200
    assert json.loads(_response_text(response)) == {
        "messages": [
            {"role": "user", "text": "hi"},
            {"role": "assistant", "text": "hello"},
            {"role": "user", "text": "bye"},
        ],
        "truncated": False,
    }
    # The endpoint asks for a clean transcript and forwards the exact session id.
    assert provider.skip_excluded is True
    assert provider.captured_session_id == "abc123"


def test_history_endpoint_caps_to_latest_messages(
    monkeypatch: Any, tmp_path: Path
) -> None:
    message_count = _MAX_HISTORY_REPLAY_MESSAGES + 3
    provider = _FakeHistoryProvider(
        [
            SimpleNamespace(
                role="user" if index % 2 == 0 else "assistant",
                text=f"message-{index:03d}",
            )
            for index in range(message_count)
        ]
    )
    monkeypatch.setattr(
        "azure_functions_agents._blob_history.build_blob_provider_from_environment",
        lambda **kwargs: provider,
    )
    app = FakeFunctionApp()
    register_builtin_endpoints(
        app, _chat_api_agent(tmp_path, EndpointAuthConfig()), AgentCapabilities()
    )

    request = DummyRequest({}, headers={"x-ms-session-id": "abc123"})
    response = asyncio.run(_history_route(app)["handler"](request))

    assert response.status_code == 200
    payload = json.loads(_response_text(response))
    assert payload["truncated"] is True
    assert len(payload["messages"]) == _MAX_HISTORY_REPLAY_MESSAGES
    assert payload["messages"][0]["text"] == "message-003"
    assert payload["messages"][-1]["text"] == f"message-{message_count - 1:03d}"


def test_history_endpoint_returns_500_on_provider_error(
    monkeypatch: Any, tmp_path: Path
) -> None:
    class _BoomProvider:
        skip_excluded = False

        async def get_messages(self, session_id: str, **kwargs: Any) -> list[Any]:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "azure_functions_agents._blob_history.build_blob_provider_from_environment",
        lambda **kwargs: _BoomProvider(),
    )
    app = FakeFunctionApp()
    register_builtin_endpoints(
        app, _chat_api_agent(tmp_path, EndpointAuthConfig()), AgentCapabilities()
    )

    request = DummyRequest({}, headers={"x-ms-session-id": "abc123"})
    response = asyncio.run(_history_route(app)["handler"](request))

    assert response.status_code == 500
    assert json.loads(_response_text(response)) == {"error": "failed to load history"}


def test_chat_routes_default_to_function_auth_level(tmp_path: Path) -> None:
    app = FakeFunctionApp()
    resolved = _chat_api_agent(tmp_path, EndpointAuthConfig())

    register_builtin_endpoints(app, resolved, AgentCapabilities())

    for name in ("agents/test_agent/chat", "agents/test_agent/chatstream"):
        route = next(route for route in app.routes if route["route"] == name)
        assert route["auth_level"] == func.AuthLevel.FUNCTION


def test_chat_routes_admin_and_anonymous_auth_levels(tmp_path: Path) -> None:
    for mode, expected in (
        ("admin", func.AuthLevel.ADMIN),
        ("anonymous", func.AuthLevel.ANONYMOUS),
        ("entra", func.AuthLevel.ANONYMOUS),
    ):
        app = FakeFunctionApp()
        resolved = _chat_api_agent(tmp_path, EndpointAuthConfig(mode=mode))  # type: ignore[arg-type]
        register_builtin_endpoints(app, resolved, AgentCapabilities())
        route = next(route for route in app.routes if route["route"] == "agents/test_agent/chat")
        assert route["auth_level"] == expected


def test_entra_chat_without_identity_is_unauthorized(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")
    app = FakeFunctionApp()
    resolved = _chat_api_agent(tmp_path, EndpointAuthConfig(mode="entra"))

    register_builtin_endpoints(app, resolved, AgentCapabilities())
    chat_route = next(route for route in app.routes if route["route"] == "agents/test_agent/chat")

    response = asyncio.run(chat_route["handler"](DummyRequest({"prompt": "hello"})))

    assert response.status_code == 401


def test_entra_chatstream_without_identity_emits_sse_error(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")
    app = FakeFunctionApp()
    resolved = _chat_api_agent(tmp_path, EndpointAuthConfig(mode="entra"))

    register_builtin_endpoints(app, resolved, AgentCapabilities())
    stream_route = next(
        route for route in app.routes if route["route"] == "agents/test_agent/chatstream"
    )

    response = asyncio.run(stream_route["handler"](DummyRequest({"prompt": "hi"})))

    assert response.status_code == 401


def test_entra_chat_with_easy_auth_principal_proceeds(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import base64
    import json as _json

    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")
    app = FakeFunctionApp()
    resolved = _chat_api_agent(tmp_path, EndpointAuthConfig(mode="entra"))

    async def fake_run_builtin_agent(prompt: str, **kwargs: Any) -> Any:
        return SimpleNamespace(session_id="s-1", content="ok", tool_calls=[])

    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints._run_builtin_agent",
        fake_run_builtin_agent,
    )

    register_builtin_endpoints(app, resolved, AgentCapabilities())
    chat_route = next(route for route in app.routes if route["route"] == "agents/test_agent/chat")

    principal = base64.b64encode(
        _json.dumps(
            {"auth_typ": "aad", "claims": [{"typ": "tid", "val": "t-1"}]}
        ).encode("utf-8")
    ).decode("ascii")
    request = DummyRequest({"prompt": "hello"}, headers={"x-ms-client-principal": principal})
    response = asyncio.run(chat_route["handler"](request))

    assert response.status_code == 200


def test_entra_chat_without_easy_auth_rejects_principal(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import base64
    import json as _json

    # Easy Auth is NOT enforced, so a client-supplied principal header must be
    # rejected rather than trusted (authentication-bypass guard).
    monkeypatch.delenv("WEBSITE_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH", raising=False)
    app = FakeFunctionApp()
    resolved = _chat_api_agent(tmp_path, EndpointAuthConfig(mode="entra"))

    register_builtin_endpoints(app, resolved, AgentCapabilities())
    chat_route = next(route for route in app.routes if route["route"] == "agents/test_agent/chat")

    principal = base64.b64encode(
        _json.dumps(
            {"auth_typ": "aad", "claims": [{"typ": "tid", "val": "t-1"}]}
        ).encode("utf-8")
    ).decode("ascii")
    request = DummyRequest({"prompt": "hello"}, headers={"x-ms-client-principal": principal})
    response = asyncio.run(chat_route["handler"](request))

    assert response.status_code == 401

    app = FakeFunctionApp()
    resolved = _chat_api_agent(tmp_path, EndpointAuthConfig())

    register_builtin_endpoints(app, resolved, AgentCapabilities(), workflows_enabled=True)

    for name in ("agents/test_agent/workflows", "agents/test_agent/workflow-status"):
        route = next(route for route in app.routes if route["route"] == name)
        assert route["auth_level"] == func.AuthLevel.FUNCTION


def test_workflow_endpoints_apply_configured_auth_level(tmp_path: Path) -> None:
    for mode, expected in (
        ("admin", func.AuthLevel.ADMIN),
        ("anonymous", func.AuthLevel.ANONYMOUS),
        ("entra", func.AuthLevel.ANONYMOUS),
    ):
        app = FakeFunctionApp()
        resolved = _chat_api_agent(tmp_path, EndpointAuthConfig(mode=mode))  # type: ignore[arg-type]
        register_builtin_endpoints(app, resolved, AgentCapabilities(), workflows_enabled=True)
        for name in ("agents/test_agent/workflows", "agents/test_agent/workflow-status"):
            route = next(route for route in app.routes if route["route"] == name)
            assert route["auth_level"] == expected


def test_entra_workflow_endpoints_without_identity_are_unauthorized(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")
    app = FakeFunctionApp()
    resolved = _chat_api_agent(tmp_path, EndpointAuthConfig(mode="entra"))

    register_builtin_endpoints(app, resolved, AgentCapabilities(), workflows_enabled=True)

    for name in ("agents/test_agent/workflows", "agents/test_agent/workflow-status"):
        route = next(route for route in app.routes if route["route"] == name)
        response = asyncio.run(route["handler"](DummyRequest({}), client=object()))
        assert response.status_code == 401
