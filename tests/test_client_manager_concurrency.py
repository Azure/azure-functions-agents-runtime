from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework.openai import OpenAIChatClient
from openai import AsyncOpenAI

from azure_functions_agents.client_manager import MAFClientManager

_MARKERS = ("alpha", "beta")


def _response(marker: str) -> dict[str, Any]:
    return {
        "id": f"response-{marker}",
        "created_at": 0,
        "model": "test-model",
        "object": "response",
        "output": [
            {
                "id": f"message-{marker}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": f"reply-{marker}",
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "status": "completed",
    }


def _stream_events(marker: str) -> list[dict[str, Any]]:
    response = _response(marker)
    return [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "sequence_number": 0,
            "item": {
                "id": f"function-{marker}",
                "type": "function_call",
                "call_id": f"call-{marker}",
                "name": f"tool_{marker}",
                "arguments": "",
                "status": "in_progress",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": f"function-{marker}",
            "output_index": 0,
            "sequence_number": 1,
            "delta": json.dumps({"marker": marker}),
        },
        {
            "type": "response.output_text.delta",
            "content_index": 0,
            "delta": f"reply-{marker}",
            "item_id": f"message-{marker}",
            "logprobs": [],
            "output_index": 1,
            "sequence_number": 2,
        },
        {
            "type": "response.completed",
            "sequence_number": 3,
            "response": response,
        },
    ]


class _DeterministicResponsesTransport:
    def __init__(self) -> None:
        self.requests: dict[str, list[str]] = {marker: [] for marker in _MARKERS}
        self.active_requests = 0
        self.max_active_requests = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        marker = next(marker for marker in _MARKERS if marker in body)
        self.requests[marker].append(body)
        self.active_requests += 1
        self.max_active_requests = max(self.max_active_requests, self.active_requests)
        try:
            await asyncio.sleep(0.01 if marker == "alpha" else 0)
            payload = json.loads(body)
            if payload.get("stream"):
                content = "".join(
                    f"data: {json.dumps(event)}\n\n" for event in _stream_events(marker)
                )
                content += "data: [DONE]\n\n"
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=content,
                )
            return httpx.Response(200, json=_response(marker))
        finally:
            self.active_requests -= 1


class _FakeProjectClient:
    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client
        self.close = AsyncMock()

    def get_openai_client(self, **_kwargs: Any) -> AsyncOpenAI:
        return self._client


def _build_chat_client(
    provider: str,
    transport: _DeterministicResponsesTransport,
) -> tuple[OpenAIChatClient | FoundryChatClient, _FakeProjectClient | None]:
    async_http_client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    openai_client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=async_http_client,
    )
    function_invocation_configuration = {"enabled": False}
    if provider == "foundry":
        project_client = _FakeProjectClient(openai_client)
        return (
            FoundryChatClient(
                project_client=project_client,  # type: ignore[arg-type]
                model="test-model",
                function_invocation_configuration=function_invocation_configuration,
            ),
            project_client,
        )
    return (
        OpenAIChatClient(
            model="test-model",
            async_client=openai_client,
            function_invocation_configuration=function_invocation_configuration,
        ),
        None,
    )


async def _consume_stream(
    agent: Agent[Any],
    marker: str,
    session_id: str,
) -> tuple[str, set[str], str]:
    session = agent.create_session(session_id=session_id)
    stream = agent.run(marker, stream=True, session=session)
    text_parts: list[str] = []
    call_ids: set[str] = set()
    async for update in stream:
        for content in update.contents:
            if content.type == "text":
                text_parts.append(content.text)
            elif content.type == "function_call":
                call_ids.add(content.call_id)
    return "".join(text_parts), call_ids, session.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "foundry"])
async def test_cached_maf_client_isolates_two_concurrent_streaming_runs(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider)
    if provider == "foundry":
        monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    transport = _DeterministicResponsesTransport()
    chat_client, project_client = _build_chat_client(provider, transport)
    manager = MAFClientManager()
    builder = "_build_foundry" if provider == "foundry" else "_build_openai"

    try:
        with patch.object(MAFClientManager, builder, return_value=chat_client):
            shared, _ = manager.build_chat_client_with_target("test-model")
            reused, _ = manager.build_chat_client_with_target("test-model")
            first_agent = Agent(shared)
            second_agent = Agent(reused)
            first, second = await asyncio.gather(
                _consume_stream(first_agent, "alpha", "session-alpha"),
                _consume_stream(second_agent, "beta", "session-beta"),
            )
    finally:
        await manager.close()

    assert shared is reused is chat_client
    assert first == ("reply-alpha", {"call-alpha"}, "session-alpha")
    assert second == ("reply-beta", {"call-beta"}, "session-beta")
    assert all("beta" not in body for body in transport.requests["alpha"])
    assert all("alpha" not in body for body in transport.requests["beta"])
    assert transport.max_active_requests == 2
    if project_client is not None:
        project_client.close.assert_awaited_once_with()


async def _run_two_non_streaming_turns(
    agent: Agent[Any],
    marker: str,
    session_id: str,
) -> tuple[str, str, str]:
    session = agent.create_session(session_id=session_id)
    first = await agent.run(marker, session=session)
    second = await agent.run(f"{marker}-followup", session=session)
    return first.text, second.text, session.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "foundry"])
async def test_cached_maf_client_isolates_two_concurrent_non_streaming_runs(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider)
    if provider == "foundry":
        monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    transport = _DeterministicResponsesTransport()
    chat_client, project_client = _build_chat_client(provider, transport)
    manager = MAFClientManager()
    builder = "_build_foundry" if provider == "foundry" else "_build_openai"

    try:
        with patch.object(MAFClientManager, builder, return_value=chat_client):
            shared, _ = manager.build_chat_client_with_target("test-model")
            first_agent = Agent(shared)
            second_agent = Agent(shared)
            first, second = await asyncio.gather(
                _run_two_non_streaming_turns(first_agent, "alpha", "session-alpha"),
                _run_two_non_streaming_turns(second_agent, "beta", "session-beta"),
            )
    finally:
        await manager.close()

    assert first == ("reply-alpha", "reply-alpha", "session-alpha")
    assert second == ("reply-beta", "reply-beta", "session-beta")
    assert len(transport.requests["alpha"]) == 2
    assert len(transport.requests["beta"]) == 2
    assert json.loads(transport.requests["alpha"][1])["previous_response_id"] == "response-alpha"
    assert json.loads(transport.requests["beta"][1])["previous_response_id"] == "response-beta"
    assert all("beta" not in body for body in transport.requests["alpha"])
    assert all("alpha" not in body for body in transport.requests["beta"])
    assert transport.max_active_requests == 2
    if project_client is not None:
        project_client.close.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "foundry"])
async def test_cached_maf_client_isolates_mixed_streaming_and_non_streaming_runs(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider)
    if provider == "foundry":
        monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    transport = _DeterministicResponsesTransport()
    chat_client, project_client = _build_chat_client(provider, transport)
    manager = MAFClientManager()
    builder = "_build_foundry" if provider == "foundry" else "_build_openai"

    try:
        with patch.object(MAFClientManager, builder, return_value=chat_client):
            shared, _ = manager.build_chat_client_with_target("test-model")
            first_agent = Agent(shared)
            second_agent = Agent(shared)
            non_streaming, streaming = await asyncio.gather(
                first_agent.run(
                    "alpha",
                    session=first_agent.create_session(session_id="session-alpha"),
                ),
                _consume_stream(second_agent, "beta", "session-beta"),
            )
    finally:
        await manager.close()

    assert non_streaming.text == "reply-alpha"
    assert streaming == ("reply-beta", {"call-beta"}, "session-beta")
    assert all("beta" not in body for body in transport.requests["alpha"])
    assert all("alpha" not in body for body in transport.requests["beta"])
    assert transport.max_active_requests == 2
    await manager.close()
    if project_client is not None:
        project_client.close.assert_awaited_once_with()
