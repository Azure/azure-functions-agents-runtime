from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import azure.functions as func
import pytest
from azurefunctions.extensions.http.fastapi import Request

from azure_functions_agents._session_id import SESSION_ID_PATTERN
from azure_functions_agents.config import (
    A2AConfig,
    BuiltinEndpointsConfig,
    EndpointAuthConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.registration import a2a as a2a_registration
from azure_functions_agents.registration.a2a import (
    _MAX_PARTS,
    _MAX_REQUEST_BYTES,
    _MAX_RESPONSE_TEXT_BYTES,
    _MAX_TEXT_PART_BYTES,
    _MAX_TOTAL_TEXT_BYTES,
    _runner_session_id,
    register_a2a_endpoints,
)
from azure_functions_agents.registration.capabilities import AgentCapabilities


class _FakeFunctionApp:
    def __init__(self) -> None:
        self.routes: list[dict[str, Any]] = []
        self.durable_clients: dict[Any, str] = {}

    def route(self, **kwargs: Any) -> Any:
        def decorator(handler: Any) -> Any:
            route = {"handler": handler, **kwargs}
            if handler in self.durable_clients:
                route["durable_client_input"] = self.durable_clients[handler]
            self.routes.append(route)
            return handler

        return decorator

    def function_name(self, *, name: str) -> Any:
        def decorator(handler: Any) -> Any:
            next(route for route in self.routes if route["handler"] is handler)[
                "function_name"
            ] = name
            return handler

        return decorator

    def durable_client_input(self, *, client_name: str) -> Any:
        def decorator(handler: Any) -> Any:
            self.durable_clients[handler] = client_name
            return handler

        return decorator


def _resolved_agent(
    *,
    auth: EndpointAuthConfig | None = None,
) -> ResolvedAgent:
    return ResolvedAgent(
        name="Incident Triage Specialist",
        slug="incident-triage",
        description="Turns production symptoms into a focused incident brief.",
        trigger=None,
        instructions="Triage the incident.",
        is_main=True,
        builtin_endpoints=BuiltinEndpointsConfig(
            a2a=A2AConfig(
                url="http://localhost:7071/api/agents/incident-triage/a2a"
            ),
            http_auth=auth or EndpointAuthConfig(mode="function"),
        ),
        model=None,
        timeout=30,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        source_file=str(Path("incident-triage.agent.md")),
    )


def _request(
    payload: Any = None,
    *,
    body: bytes | None = None,
    method: str = "POST",
    path: str = "/agents/incident-triage/a2a",
    version: str | None = "1.0",
    headers: dict[str, str] | None = None,
) -> Request:
    request_body = (
        body
        if body is not None
        else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    request_headers = {"content-type": "application/json", **(headers or {})}
    if version is not None:
        request_headers["a2a-version"] = version
    encoded_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in request_headers.items()
    ]
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {
            "type": "http.request",
            "body": request_body,
            "more_body": False,
        }

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": encoded_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("localhost", 7071),
            "state": {},
        },
        receive,
    )


def _message_request(
    *,
    request_id: str | int = "request-42",
    context_id: str | None = "incident-7",
    parts: list[dict[str, Any]] | None = None,
    return_immediately: bool = False,
    task_id: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "messageId": "message-1",
        "role": "ROLE_USER",
        "parts": parts or [{"text": "API latency doubled after the latest deployment."}],
    }
    if context_id is not None:
        message["contextId"] = context_id
    if task_id is not None:
        message["taskId"] = task_id
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": {
            "message": message,
            "configuration": {
                "acceptedOutputModes": ["text/plain"],
                "returnImmediately": return_immediately,
            },
        },
    }


def _route(app: _FakeFunctionApp, suffix: str) -> dict[str, Any]:
    return next(route for route in app.routes if route["route"].endswith(suffix))


def _response_json(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


@pytest.fixture
def registered_a2a(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeFunctionApp, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def fake_run(prompt: str, **kwargs: Any) -> Any:
        calls.append({"prompt": prompt, **kwargs})
        return SimpleNamespace(
            session_id=kwargs["session_id"],
            content="Rollback is the safest first action; preserve logs before retrying.",
            tool_calls=[{"name": "private_tool", "arguments": {"secret": "hidden"}}],
            delegate_error_count=0,
        )

    monkeypatch.setattr(a2a_registration, "_run_builtin_agent", fake_run)
    app = _FakeFunctionApp()
    register_a2a_endpoints(
        app,  # type: ignore[arg-type]
        _resolved_agent(),
        AgentCapabilities(),
        slug="incident-triage",
    )
    return app, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("return_immediately", [False, True])
async def test_send_message_returns_correlated_direct_message(
    registered_a2a: tuple[_FakeFunctionApp, list[dict[str, Any]]],
    return_immediately: bool,
) -> None:
    app, calls = registered_a2a

    response = await _route(app, "/a2a")["handler"](
        _request(_message_request(return_immediately=return_immediately))
    )

    assert response.status_code == 200
    body = _response_json(response)
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == "request-42"
    message = body["result"]["message"]
    assert message["role"] == "ROLE_AGENT"
    assert message["contextId"] == "incident-7"
    assert message["messageId"]
    assert message["parts"] == [
        {"text": "Rollback is the safest first action; preserve logs before retrying."}
    ]
    assert "task" not in body["result"]
    assert "metadata" not in message
    assert "private_tool" not in response.body.decode()
    assert calls[0]["prompt"] == "API latency doubled after the latest deployment."
    assert calls[0]["durable_client"] is None
    assert SESSION_ID_PATTERN.fullmatch(calls[0]["session_id"])


@pytest.mark.asyncio
async def test_in_flight_limit_rejects_without_queueing_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = 0
    all_entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_run(prompt: str, **kwargs: Any) -> Any:
        nonlocal entered
        del prompt
        entered += 1
        if entered == a2a_registration._MAX_IN_FLIGHT:
            all_entered.set()
        await release.wait()
        return SimpleNamespace(
            session_id=kwargs["session_id"],
            content="Triage complete.",
            tool_calls=[],
            delegate_error_count=0,
        )

    monkeypatch.setattr(a2a_registration, "_run_builtin_agent", blocked_run)
    app = _FakeFunctionApp()
    register_a2a_endpoints(
        app,  # type: ignore[arg-type]
        _resolved_agent(),
        AgentCapabilities(),
        slug="incident-triage",
    )
    handler = _route(app, "/a2a")["handler"]
    active = [
        asyncio.create_task(
            handler(
                _request(
                    _message_request(
                        request_id=f"active-{index}",
                        context_id=f"context-{index}",
                    )
                )
            )
        )
        for index in range(a2a_registration._MAX_IN_FLIGHT)
    ]
    await asyncio.wait_for(all_entered.wait(), timeout=5)

    rejected = await handler(
        _request(_message_request(request_id="over-capacity", context_id="overflow"))
    )

    rejected_body = _response_json(rejected)
    assert rejected_body["id"] == "over-capacity"
    assert rejected_body["error"]["code"] == -32004
    assert entered == a2a_registration._MAX_IN_FLIGHT

    release.set()
    completed = await asyncio.gather(*active)
    assert all(response.status_code == 200 for response in completed)

    recovered = await handler(
        _request(_message_request(request_id="recovered", context_id="after-release"))
    )
    assert _response_json(recovered)["id"] == "recovered"
    assert entered == a2a_registration._MAX_IN_FLIGHT + 1


@pytest.mark.asyncio
async def test_send_message_combines_text_parts_through_maf_conversion(
    registered_a2a: tuple[_FakeFunctionApp, list[dict[str, Any]]],
) -> None:
    app, calls = registered_a2a
    payload = _message_request(
        parts=[
            {"text": "Service: checkout-api."},
            {"text": "Symptom: elevated 503 responses."},
        ]
    )

    response = await _route(app, "/a2a")["handler"](_request(payload))

    assert response.status_code == 200
    assert calls[0]["prompt"] == "Service: checkout-api. Symptom: elevated 503 responses."


@pytest.mark.asyncio
async def test_send_message_generates_and_echoes_context_id(
    registered_a2a: tuple[_FakeFunctionApp, list[dict[str, Any]]],
) -> None:
    app, calls = registered_a2a

    response = await _route(app, "/a2a")["handler"](
        _request(_message_request(context_id=None))
    )

    message = _response_json(response)["result"]["message"]
    assert message["contextId"]
    assert calls[0]["session_id"] == _runner_session_id(
        "functions-app",
        "incident-triage",
        message["contextId"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "0.3", "1.1", "2.0"])
async def test_send_message_rejects_every_non_exact_wire_version(
    registered_a2a: tuple[_FakeFunctionApp, list[dict[str, Any]]],
    version: str | None,
) -> None:
    app, calls = registered_a2a

    response = await _route(app, "/a2a")["handler"](
        _request(_message_request(request_id=99), version=version)
    )

    body = _response_json(response)
    assert response.status_code == 200
    assert body["id"] == 99
    assert body["error"]["code"] == -32009
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "GetTask",
        "ListTasks",
        "CancelTask",
        "SendStreamingMessage",
        "SubscribeToTask",
        "CreateTaskPushNotificationConfig",
        "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig",
        "GetExtendedAgentCard",
    ],
)
async def test_simple_profile_rejects_task_and_streaming_methods(
    registered_a2a: tuple[_FakeFunctionApp, list[dict[str, Any]]],
    method: str,
) -> None:
    app, calls = registered_a2a
    payload = {
        "jsonrpc": "2.0",
        "id": f"id-{method}",
        "method": method,
        "params": {},
    }

    response = await _route(app, "/a2a")["handler"](_request(payload))

    body = _response_json(response)
    assert body["id"] == f"id-{method}"
    assert body["error"]["code"] == -32004
    assert calls == []


@pytest.mark.asyncio
async def test_simple_profile_rejects_task_continuation(
    registered_a2a: tuple[_FakeFunctionApp, list[dict[str, Any]]],
) -> None:
    app, calls = registered_a2a

    response = await _route(app, "/a2a")["handler"](
        _request(_message_request(task_id="task-1"))
    )

    body = _response_json(response)
    assert body["id"] == "request-42"
    assert body["error"]["code"] == -32004
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parts",
    [
        [{"url": "https://example.test/incident.txt"}],
        [{"text": "x" * (_MAX_TEXT_PART_BYTES + 1)}],
        [{"text": "x"}] * (_MAX_PARTS + 1),
        [{"text": "x" * 22_000}] * 3,
    ],
)
async def test_simple_profile_rejects_unsupported_or_oversized_parts(
    registered_a2a: tuple[_FakeFunctionApp, list[dict[str, Any]]],
    parts: list[dict[str, Any]],
) -> None:
    app, calls = registered_a2a

    response = await _route(app, "/a2a")["handler"](
        _request(_message_request(parts=parts))
    )

    body = _response_json(response)
    assert body["id"] == "request-42"
    assert body["error"]["code"] == -32602
    assert calls == []
    assert _MAX_TOTAL_TEXT_BYTES < 22_000 * 3


@pytest.mark.asyncio
async def test_preflight_uses_http_errors_before_dispatch(
    registered_a2a: tuple[_FakeFunctionApp, list[dict[str, Any]]],
) -> None:
    app, calls = registered_a2a
    handler = _route(app, "/a2a")["handler"]

    malformed = await handler(_request(body=b"{not-json"))
    oversized = await handler(_request(body=b" " * (_MAX_REQUEST_BYTES + 1)))

    assert malformed.status_code == 400
    assert oversized.status_code == 413
    assert calls == []


@pytest.mark.asyncio
async def test_sdk_shapes_batch_alias_and_unknown_method_errors(
    registered_a2a: tuple[_FakeFunctionApp, list[dict[str, Any]]],
) -> None:
    app, calls = registered_a2a
    handler = _route(app, "/a2a")["handler"]

    batch = await handler(_request([_message_request()]))
    alias = await handler(
        _request(
            {
                "jsonrpc": "2.0",
                "id": "alias",
                "method": "message/send",
                "params": {},
            }
        )
    )
    unknown = await handler(
        _request(
            {
                "jsonrpc": "2.0",
                "id": "unknown",
                "method": "DoMagic",
                "params": {},
            }
        )
    )

    assert _response_json(batch)["error"]["code"] == -32600
    assert _response_json(alias)["error"]["code"] == -32601
    assert _response_json(unknown)["error"]["code"] == -32601
    assert calls == []


@pytest.mark.asyncio
async def test_oversized_agent_response_is_correlated_and_sanitized(
    registered_a2a: tuple[_FakeFunctionApp, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = registered_a2a
    internal_marker = "must-not-leak"

    async def oversized_run(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return SimpleNamespace(
            session_id="session",
            content="x" * (_MAX_RESPONSE_TEXT_BYTES + 1),
            tool_calls=[{"result": internal_marker}],
            delegate_error_count=0,
        )

    monkeypatch.setattr(a2a_registration, "_run_builtin_agent", oversized_run)
    response = await _route(app, "/a2a")["handler"](_request(_message_request()))

    body = _response_json(response)
    assert body["id"] == "request-42"
    assert body["error"]["code"] == -32006
    assert internal_marker not in response.body.decode()


@pytest.mark.asyncio
async def test_agent_card_uses_configured_url_and_declares_simple_capabilities(
    registered_a2a: tuple[_FakeFunctionApp, list[dict[str, Any]]],
) -> None:
    app, _ = registered_a2a

    response = await _route(app, "agent-card.json")["handler"](
        _request(method="GET", path="/agents/incident-triage/.well-known/agent-card.json")
    )

    assert response.status_code == 200
    card = _response_json(response)
    assert card["supportedInterfaces"] == [
        {
            "url": "http://localhost:7071/api/agents/incident-triage/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
            "tenant": "",
        }
    ]
    assert card["capabilities"]["streaming"] is False
    assert card["capabilities"]["pushNotifications"] is False
    assert card["defaultInputModes"] == ["text/plain"]
    assert card["defaultOutputModes"] == ["text/plain"]
    assert card["skills"][0]["id"] == "incident-triage"
    assert card["securitySchemes"]["function_key"]["apiKeySecurityScheme"][
        "name"
    ] == "x-functions-key"
    assert card["securityRequirements"] == [
        {"schemes": {"function_key": {"list": []}}}
    ]


def test_registration_uses_scoped_auth_and_optional_durable_binding() -> None:
    non_workflow_app = _FakeFunctionApp()
    register_a2a_endpoints(
        non_workflow_app,  # type: ignore[arg-type]
        _resolved_agent(),
        AgentCapabilities(),
        slug="incident-triage",
    )
    workflow_app = _FakeFunctionApp()
    register_a2a_endpoints(
        workflow_app,  # type: ignore[arg-type]
        _resolved_agent(),
        AgentCapabilities(),
        slug="incident-triage",
        workflows_enabled=True,
    )

    assert [route["route"] for route in non_workflow_app.routes] == [
        "agents/incident-triage/.well-known/agent-card.json",
        "agents/incident-triage/a2a",
    ]
    assert all(
        route["auth_level"] is func.AuthLevel.FUNCTION
        for route in non_workflow_app.routes
    )
    assert "durable_client_input" not in _route(non_workflow_app, "/a2a")
    assert _route(workflow_app, "/a2a")["durable_client_input"] == "client"


def test_runner_session_id_is_safe_and_scoped() -> None:
    session_id = _runner_session_id("functions-app", "incident-triage", "../unsafe")

    assert SESSION_ID_PATTERN.fullmatch(session_id)
    assert session_id != _runner_session_id("functions-app", "other-agent", "../unsafe")
    assert session_id != _runner_session_id("entra:tenant:caller", "incident-triage", "../unsafe")
