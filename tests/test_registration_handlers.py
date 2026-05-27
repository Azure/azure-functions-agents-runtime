from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from azure_functions_agents.config.schema import (
    AgentConfiguration,
    DebugConfig,
    ExecuteInSessionsConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.registration._handlers import (
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


def _resolved_agent(
    *,
    response_schema: dict[str, Any] | None,
    sandbox_config: ExecuteInSessionsConfig | None = None,
    tools_disabled: bool = False,
) -> ResolvedAgent:
    source = Path(__file__).resolve()
    return ResolvedAgent(
        name="Report",
        description="desc",
        trigger=None,
        instructions="Return JSON",
        is_main=True,
        debug=DebugConfig(),
        agent_configuration=AgentConfiguration.model_validate(
            {
                "provider": "openai",
                "model": "gpt-4o",
                "openai": {},
            }
        ),
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        tools_disabled=tools_disabled,
        sandbox_config=sandbox_config,
        input_schema=None,
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
            sandbox_config=ExecuteInSessionsConfig(
                session_pool_management_endpoint="https://sandbox.example"
            ),
            tools_disabled=True,
        ),
        "session-123",
    )
    enabled = build_sandbox_tools_for_session(
        _resolved_agent(
            response_schema=None,
            sandbox_config=ExecuteInSessionsConfig(
                session_pool_management_endpoint="https://sandbox.example"
            ),
        ),
        "session-456",
    )

    assert disabled is None
    assert enabled == ["execute_python"]
    assert create_calls == [
        (
            {"session_pool_management_endpoint": "https://sandbox.example"},
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
        sandbox_config=ExecuteInSessionsConfig(
            session_pool_management_endpoint="https://sandbox.example"
        ),
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
    resolved = _resolved_agent(response_schema=None)

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

    handler = make_http_agent_handler(resolved, AgentCapabilities())

    response = asyncio.run(
        handler(DummyRequest({"hello": "world"}, headers={"X-MS-SESSION-ID": "client-session"}))
    )

    assert response.status_code == 200
    assert response.headers["x-ms-session-id"] == "client-session"
    assert sandbox_session_ids == ["client-session"]
    assert run_kwargs["session_id"] == "client-session"
    assert run_kwargs["sandbox_tools"] == ["sandbox:client-session"]
    assert run_kwargs["agent_configuration"] == resolved.agent_configuration


def test_http_handler_generates_session_id_once_per_request(monkeypatch: Any) -> None:
    sandbox_session_ids: list[str | None] = []
    run_kwargs: dict[str, Any] = {}
    resolved = _resolved_agent(response_schema=None)

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

    handler = make_http_agent_handler(resolved, AgentCapabilities())

    response = asyncio.run(handler(DummyRequest({"hello": "world"})))

    assert response.status_code == 200
    assert response.headers["x-ms-session-id"] == "generated-session"
    assert sandbox_session_ids == ["generated-session"]
    assert run_kwargs["session_id"] == "generated-session"
    assert run_kwargs["sandbox_tools"] == ["sandbox:generated-session"]
    assert run_kwargs["agent_configuration"] == resolved.agent_configuration


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

    handler = make_agent_handler(_resolved_agent(response_schema=None), "queue_trigger", AgentCapabilities())

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

    handler = make_agent_handler(_resolved_agent(response_schema=None), "queue_trigger", AgentCapabilities())

    asyncio.run(handler({"message": "hello"}))

    assert captured["instructions"] == "Return JSON"
    assert "Return JSON" not in captured["prompt"]
    assert captured["prompt"] == (
        'Triggered by: queue_trigger\n\nTrigger data:\n```json\n{"message": "hello"}\n```'
    )


def test_non_http_handler_reraises_agent_failures(monkeypatch: Any) -> None:
    async def fake_run_agent(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("agent failed")

    monkeypatch.setattr(
        "azure_functions_agents.registration._handlers._run_agent",
        fake_run_agent,
    )

    handler = make_agent_handler(_resolved_agent(response_schema=None), "queue_trigger", AgentCapabilities())

    try:
        asyncio.run(handler({"message": "boom"}))
    except RuntimeError as exc:
        assert str(exc) == "agent failed"
    else:
        raise AssertionError("Expected RuntimeError to be re-raised")
