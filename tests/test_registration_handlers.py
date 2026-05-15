from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from azure_functions_agents.config.schema import DebugConfig, ResolvedAgent, ToolsFilter
from azure_functions_agents.registration._handlers import make_http_agent_handler
from azure_functions_agents.registration.capabilities import AgentCapabilities


class DummyRequest:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def json(self) -> Any:
        return self._payload

    async def body(self) -> bytes:
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")


def _resolved_agent(*, response_schema: dict[str, Any] | None) -> ResolvedAgent:
    source = Path(__file__).resolve()
    return ResolvedAgent(
        name="Report",
        description="desc",
        trigger=None,
        instructions="Return JSON",
        is_main=True,
        debug=DebugConfig(),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        connector_specs=[],
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
