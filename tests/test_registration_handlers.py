from __future__ import annotations

import asyncio
import base64
import json
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    DynamicSessionsCodeInterpreterConfig,
    EndpointAuthConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.registration._handlers import (
    _tool_error_count,
    _total_tool_error_count,
    build_sandbox_tools_for_session,
    make_agent_handler,
    make_http_agent_handler,
)
from azure_functions_agents.registration.capabilities import AgentCapabilities


class DummyRequest:
    def __init__(self, payload: Any, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    async def json(self) -> Any:
        return self._payload

    async def body(self) -> bytes:
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")


class RecordingSpan:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any] | None]] = []

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_content(self, key: str, value: str) -> None:
        return None

    def set_error(self, message: str, *, fault_domain: str) -> None:
        return None

    def record_exception(self, exc: BaseException, *, fault_domain: str | None = None) -> None:
        return None

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append((name, attributes))


def _install_recording_span(monkeypatch: Any) -> RecordingSpan:
    span = RecordingSpan()

    @contextmanager
    def _fake_start_span(*args: Any, **kwargs: Any) -> Any:
        yield span

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers.start_span",
        _fake_start_span,
    )
    return span


def _resolved_agent(
    *,
    response_schema: dict[str, Any] | None,
    input_schema: dict[str, Any] | None = None,
    sandbox_config: DynamicSessionsCodeInterpreterConfig | None = None,
    tools_disabled: bool = False,
    # Deliberately distinct from `name` below (S1): identity/telemetry call
    # sites must key off `slug`, never the mutable display `name` (FRD 0007
    # §4.3, "Display `name` is never an identity"). Defaulted so existing
    # callers of this factory are unaffected.
    slug: str = "resolved-agent-slug",
) -> ResolvedAgent:
    source = Path(__file__).resolve()
    return ResolvedAgent(
        name="Report",
        slug=slug,
        description="desc",
        trigger=None,
        instructions="Return JSON",
        is_main=True,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        tools_disabled=tools_disabled,
        sandbox_config=sandbox_config,
        input_schema=input_schema,
        response_schema=response_schema,
        response_example=None,
        metadata={},
        source_file=str(source),
    )


def test_http_handler_response_schema_valid_output_passes(monkeypatch: Any) -> None:
    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(content='{"message":"ok"}', session_id="session-123")

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_http_agent_handler(
        _resolved_agent(
            response_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            }
        ),
        AgentCapabilities(),
    )

    response = asyncio.run(handler(DummyRequest({"hello": "world"})))

    assert response.status_code == 200
    assert json.loads(response.body) == {"message": "ok"}


def test_http_handler_response_schema_invalid_output_returns_500(monkeypatch: Any) -> None:
    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(content='{"message":123}', session_id="session-123")

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_http_agent_handler(
        _resolved_agent(
            response_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            }
        ),
        AgentCapabilities(),
    )

    response = asyncio.run(handler(DummyRequest({"hello": "world"})))

    assert response.status_code == 500
    assert json.loads(response.body) == {
        "error": "Agent response validation failed",
        "details": "123 is not of type 'string'",
    }


def test_http_handler_records_input_validation_failed_event(monkeypatch: Any) -> None:
    span = _install_recording_span(monkeypatch)

    handler = make_http_agent_handler(
        _resolved_agent(
            response_schema=None,
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        ),
        AgentCapabilities(),
    )

    response = asyncio.run(handler(DummyRequest({"message": 123})))

    assert response.status_code == 400
    assert span.events == [
        (
            "af.input.validation_failed",
            {"af.fault_domain": "app", "af.http.status_code": 400},
        )
    ]


def test_http_handler_records_invalid_json_event(monkeypatch: Any) -> None:
    span = _install_recording_span(monkeypatch)

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(content="not-json", session_id="session-123", tool_calls=[])

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_http_agent_handler(
        _resolved_agent(
            response_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            }
        ),
        AgentCapabilities(),
    )

    response = asyncio.run(handler(DummyRequest({"hello": "world"})))

    assert response.status_code == 500
    assert span.events == [
        ("af.agent.invoke.completed", None),
        ("af.response.invalid_json", {"af.fault_domain": "app"}),
    ]


def test_http_handler_records_response_schema_validation_failed_event(monkeypatch: Any) -> None:
    span = _install_recording_span(monkeypatch)

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(content='{"message":123}', session_id="session-123", tool_calls=[])

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_http_agent_handler(
        _resolved_agent(
            response_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            }
        ),
        AgentCapabilities(),
    )

    response = asyncio.run(handler(DummyRequest({"hello": "world"})))

    assert response.status_code == 500
    assert span.events == [
        ("af.agent.invoke.completed", None),
        ("af.response.schema_validation_failed", {"af.fault_domain": "app"}),
    ]


def test_build_sandbox_tools_skips_disabled_tools(monkeypatch: Any) -> None:
    create_calls: list[tuple[dict[str, Any], str]] = []

    def fake_create_sandbox_tools(config: dict[str, Any], *, fallback_session_id: str) -> list[str]:
        create_calls.append((config, fallback_session_id))
        return ["execute_python"]

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers.import_module",
        lambda name: SimpleNamespace(create_sandbox_tools=fake_create_sandbox_tools),
    )

    disabled = build_sandbox_tools_for_session(
        _resolved_agent(
            response_schema=None,
            sandbox_config=DynamicSessionsCodeInterpreterConfig(endpoint="https://sandbox.example"),
            tools_disabled=True,
        ),
        "session-123",
    )
    enabled = build_sandbox_tools_for_session(
        _resolved_agent(
            response_schema=None,
            sandbox_config=DynamicSessionsCodeInterpreterConfig(endpoint="https://sandbox.example"),
        ),
        "session-456",
    )

    assert disabled is None
    assert enabled == ["execute_python"]
    assert create_calls == [
        (
            {"endpoint": "https://sandbox.example", "client_id": None},
            "session-456",
        )
    ]


def test_build_sandbox_tools_generates_unique_guid_when_session_missing(monkeypatch: Any) -> None:
    create_calls: list[str] = []

    def fake_create_sandbox_tools(config: dict[str, Any], *, fallback_session_id: str) -> list[str]:
        create_calls.append(fallback_session_id)
        return [fallback_session_id]

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers.import_module",
        lambda name: SimpleNamespace(create_sandbox_tools=fake_create_sandbox_tools),
    )

    resolved = _resolved_agent(
        response_schema=None,
        sandbox_config=DynamicSessionsCodeInterpreterConfig(endpoint="https://sandbox.example"),
    )

    first = build_sandbox_tools_for_session(resolved, None)
    second = build_sandbox_tools_for_session(resolved, None)

    assert first == [create_calls[0]]
    assert second == [create_calls[1]]
    assert len(create_calls) == 2
    assert re.fullmatch(r"[0-9a-f]{32}", create_calls[0])
    assert re.fullmatch(r"[0-9a-f]{32}", create_calls[1])
    assert create_calls[0] != create_calls[1]
    assert "default" not in create_calls


def test_http_handler_uses_case_insensitive_session_header(monkeypatch: Any) -> None:
    sandbox_session_ids: list[str | None] = []
    run_kwargs: dict[str, Any] = {}

    def fake_build_sandbox_tools(resolved: ResolvedAgent, session_id: str | None) -> list[str]:
        sandbox_session_ids.append(session_id)
        return [f"sandbox:{session_id}"]

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        run_kwargs.update(kwargs)
        return SimpleNamespace(content="plain text", session_id=kwargs["session_id"])

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers.build_sandbox_tools_for_session",
        fake_build_sandbox_tools,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_http_agent_handler(_resolved_agent(response_schema=None), AgentCapabilities())

    response = asyncio.run(
        handler(DummyRequest({"hello": "world"}, headers={"X-MS-SESSION-ID": "client-session"}))
    )

    assert response.status_code == 200
    assert response.headers["x-ms-session-id"] == "client-session"
    assert sandbox_session_ids == ["client-session"]
    assert run_kwargs["session_id"] == "client-session"
    assert run_kwargs["sandbox_tools"] == ["sandbox:client-session"]


def test_http_handler_generates_session_id_once_per_request(monkeypatch: Any) -> None:
    sandbox_session_ids: list[str | None] = []
    run_kwargs: dict[str, Any] = {}

    def fake_build_sandbox_tools(resolved: ResolvedAgent, session_id: str | None) -> list[str]:
        sandbox_session_ids.append(session_id)
        return [f"sandbox:{session_id}"]

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        run_kwargs.update(kwargs)
        return SimpleNamespace(content="plain text", session_id=kwargs["session_id"])

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers.build_sandbox_tools_for_session",
        fake_build_sandbox_tools,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers.uuid.uuid4",
        lambda: SimpleNamespace(hex="generated-session"),
    )

    handler = make_http_agent_handler(_resolved_agent(response_schema=None), AgentCapabilities())

    response = asyncio.run(handler(DummyRequest({"hello": "world"})))

    assert response.status_code == 200
    assert response.headers["x-ms-session-id"] == "generated-session"
    assert sandbox_session_ids == ["generated-session"]
    assert run_kwargs["session_id"] == "generated-session"
    assert run_kwargs["sandbox_tools"] == ["sandbox:generated-session"]


def test_http_handler_passes_instructions_only_as_system_message(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        captured["prompt"] = args[0]
        captured["instructions"] = kwargs["instructions"]
        return SimpleNamespace(content="plain text", session_id="session-123")

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_http_agent_handler(_resolved_agent(response_schema=None), AgentCapabilities())

    response = asyncio.run(handler(DummyRequest({"hello": "world"})))

    assert response.status_code == 200
    assert captured["instructions"] == "Return JSON"
    assert "Return JSON" not in captured["prompt"]
    assert 'HTTP request data:\n```json\n{"hello": "world"}\n```' in captured["prompt"]


def test_http_handler_records_invoke_completed_event(monkeypatch: Any) -> None:
    span = _install_recording_span(monkeypatch)

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(content="plain text", session_id="session-123", tool_calls=[])

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_http_agent_handler(_resolved_agent(response_schema=None), AgentCapabilities())

    response = asyncio.run(handler(DummyRequest({"hello": "world"})))

    assert response.status_code == 200
    assert span.events == [("af.agent.invoke.completed", None)]


def test_non_http_handler_generates_fresh_session_id_per_invocation(monkeypatch: Any) -> None:
    sandbox_session_ids: list[str | None] = []
    run_session_ids: list[str | None] = []
    generated_ids = iter(["session-one", "session-two"])

    def fake_build_sandbox_tools(resolved: ResolvedAgent, session_id: str | None) -> list[str]:
        sandbox_session_ids.append(session_id)
        return [f"sandbox:{session_id}"]

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        run_session_ids.append(kwargs["session_id"])
        return SimpleNamespace(
            content="ok",
            session_id=kwargs["session_id"],
            tool_calls=[],
        )

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers.build_sandbox_tools_for_session",
        fake_build_sandbox_tools,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers.uuid.uuid4",
        lambda: SimpleNamespace(hex=next(generated_ids)),
    )

    handler = make_agent_handler(
        _resolved_agent(response_schema=None), "queue_trigger", AgentCapabilities()
    )

    asyncio.run(handler({"message": 1}))
    asyncio.run(handler({"message": 2}))

    assert sandbox_session_ids == ["session-one", "session-two"]
    assert run_session_ids == ["session-one", "session-two"]


def test_non_http_handler_passes_instructions_only_as_system_message(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        captured["prompt"] = args[0]
        captured["instructions"] = kwargs["instructions"]
        return SimpleNamespace(
            content="ok",
            session_id=kwargs["session_id"],
            tool_calls=[],
        )

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_agent_handler(
        _resolved_agent(response_schema=None), "queue_trigger", AgentCapabilities()
    )

    asyncio.run(handler({"message": "hello"}))

    assert captured["instructions"] == "Return JSON"
    assert "Return JSON" not in captured["prompt"]
    assert captured["prompt"] == (
        'Triggered by: queue_trigger\n\nTrigger data:\n```json\n{"message": "hello"}\n```'
    )


def test_non_http_handler_records_invoke_completed_event(monkeypatch: Any) -> None:
    span = _install_recording_span(monkeypatch)

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            content="ok",
            session_id=kwargs["session_id"],
            tool_calls=[],
        )

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_agent_handler(
        _resolved_agent(response_schema=None), "queue_trigger", AgentCapabilities()
    )

    asyncio.run(handler({"message": "hello"}))

    assert span.events == [("af.agent.invoke.completed", None)]


def test_non_http_handler_reraises_agent_failures(monkeypatch: Any) -> None:
    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("agent failed")

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_agent_handler(
        _resolved_agent(response_schema=None), "queue_trigger", AgentCapabilities()
    )

    try:
        asyncio.run(handler({"message": "boom"}))
    except RuntimeError as exc:
        assert str(exc) == "agent failed"
    else:
        raise AssertionError("Expected RuntimeError to be re-raised")


def test_total_tool_error_count_combines_heuristic_and_delegate_errors() -> None:
    # `_looks_like_tool_error`'s JSON `{error}`/stderr heuristic recognizes the
    # sandbox-style tool_calls entry below as one failure; a specialist's
    # sanitized free-text delegate failure is invisible to that heuristic
    # (FRD 0007 §4.12), so `AgentResult.delegate_error_count` must be added
    # on top rather than relying on the heuristic to catch it too.
    result = SimpleNamespace(
        tool_calls=[
            {"name": "run_code", "result": json.dumps({"error": "boom"})},
            {"name": "run_code", "result": json.dumps({"stdout": "ok"})},
        ],
        delegate_error_count=2,
    )

    assert _total_tool_error_count(result) == 1 + 2


def test_total_tool_error_count_does_not_misclassify_sanitized_delegate_text() -> None:
    # A delegate failure's sanitized message ("The 'x' specialist could not
    # complete this task ...") is plain text, not the sandbox JSON envelope —
    # `_looks_like_tool_error` must NOT flag it, proving the two accounting
    # paths are additive rather than double-counting or mis-classifying.
    sanitized_message = (
        "The 'billing' specialist could not complete this task. "
        "Consider trying again, rephrasing the request, or proceeding without it."
    )
    result = SimpleNamespace(
        tool_calls=[{"name": "delegate_billing", "result": sanitized_message}],
        delegate_error_count=1,
    )

    assert _tool_error_count(result.tool_calls) == 0
    assert _total_tool_error_count(result) == 1


def test_total_tool_error_count_handles_missing_fields_gracefully() -> None:
    assert _total_tool_error_count(SimpleNamespace()) == 0
    assert _total_tool_error_count(None) == 0


def test_non_http_handler_passes_resolved_slug_not_display_name_as_agent_name(
    monkeypatch: Any,
) -> None:
    """S1: the coordinator/direct-role agent must be identified by `resolved.slug`.

    Round 2's B2 fix already made *delegated* specialists use `resolved.slug`
    for telemetry identity rather than the mutable display `name` (FRD 0007
    §4.3, "Display `name` is never an identity"). This asserts the direct/
    coordinator role gets the same treatment on the non-HTTP trigger path.
    """
    captured: dict[str, Any] = {}

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        captured["agent_name"] = kwargs.get("agent_name")
        return SimpleNamespace(content="ok", session_id=kwargs["session_id"], tool_calls=[])

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    resolved = _resolved_agent(response_schema=None, slug="report-slug")
    handler = make_agent_handler(resolved, "queue_trigger", AgentCapabilities())

    asyncio.run(handler({"message": "hello"}))

    assert captured["agent_name"] == "report-slug"
    assert captured["agent_name"] != resolved.name


def test_http_handler_passes_resolved_slug_not_display_name_as_agent_name(
    monkeypatch: Any,
) -> None:
    """S1: same contract as the non-HTTP handler test above, for the HTTP trigger path."""
    captured: dict[str, Any] = {}

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        captured["agent_name"] = kwargs.get("agent_name")
        return SimpleNamespace(content="ok", session_id="session-123")

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    resolved = _resolved_agent(response_schema=None, slug="report-slug")
    handler = make_http_agent_handler(resolved, AgentCapabilities())

    asyncio.run(handler(DummyRequest({"hello": "world"})))

    assert captured["agent_name"] == "report-slug"
    assert captured["agent_name"] != resolved.name


def _principal_header(claims: list[dict[str, str]], *, auth_typ: str = "aad") -> str:
    payload = json.dumps({"auth_typ": auth_typ, "claims": claims})
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def test_http_handler_entra_without_easy_auth_returns_401(monkeypatch: Any) -> None:
    monkeypatch.delenv("WEBSITE_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH", raising=False)

    called = False

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        return SimpleNamespace(content="ok", session_id="s")

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_http_agent_handler(
        _resolved_agent(response_schema=None),
        AgentCapabilities(),
        auth=EndpointAuthConfig(mode="entra"),
    )

    response = asyncio.run(
        handler(
            DummyRequest(
                {"hello": "world"},
                headers={"x-ms-client-principal": _principal_header([{"typ": "tid", "val": "t"}])},
            )
        )
    )

    assert response.status_code == 401
    assert called is False


def test_http_handler_entra_without_principal_returns_401(monkeypatch: Any) -> None:
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")

    handler = make_http_agent_handler(
        _resolved_agent(response_schema=None),
        AgentCapabilities(),
        auth=EndpointAuthConfig(mode="entra"),
    )

    response = asyncio.run(handler(DummyRequest({"hello": "world"})))

    assert response.status_code == 401


def test_http_handler_entra_with_valid_principal_proceeds(monkeypatch: Any) -> None:
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(content="plain text", session_id="session-123")

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_http_agent_handler(
        _resolved_agent(response_schema=None),
        AgentCapabilities(),
        auth=EndpointAuthConfig(mode="entra"),
    )

    response = asyncio.run(
        handler(
            DummyRequest(
                {"hello": "world"},
                headers={"x-ms-client-principal": _principal_header([{"typ": "tid", "val": "t"}])},
            )
        )
    )

    assert response.status_code == 200


def test_http_handler_default_auth_does_not_gate_requests(monkeypatch: Any) -> None:
    # No Easy Auth env, no principal header: a non-entra (default) handler must
    # still serve the request -- key enforcement is handled by the route AuthLevel.
    monkeypatch.delenv("WEBSITE_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH", raising=False)

    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(content="plain text", session_id="session-123")

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_http_agent_handler(_resolved_agent(response_schema=None), AgentCapabilities())

    response = asyncio.run(handler(DummyRequest({"hello": "world"})))

    assert response.status_code == 200
