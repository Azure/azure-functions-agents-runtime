from __future__ import annotations

import asyncio
import json
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from azure_functions_agents import runner
from azure_functions_agents.discovery.tools import clear_tool_discovery_cache, discover_user_tools


class _Content:
    def __init__(self, type: str, **kwargs: Any) -> None:
        self.type = type
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Update:
    def __init__(self, contents: list[_Content]) -> None:
        self.contents = contents


class _Agent:
    def run(
        self,
        _prompt: str,
        *,
        stream: bool,
        session: object,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[_Update]:
        assert stream is True
        assert session is not None
        assert options is None
        return self._updates()

    async def _updates(self) -> AsyncIterator[_Update]:
        yield _Update(
            [_Content("function_call", call_id="call_1", name="azure_rest", arguments='{"')]
        )
        yield _Update(
            [_Content("function_call", call_id="call_1", name="azure_rest", arguments="path")]
        )
        yield _Update(
            [_Content("function_call", call_id="call_1", name="azure_rest", arguments='":"/x"}')]
        )
        yield _Update([_Content("function_result", call_id="call_1", result="ok")])


def _events_from_sse(chunks: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for chunk in chunks:
        assert chunk.startswith("data: ")
        events.append(json.loads(chunk.removeprefix("data: ").strip()))
    return events


def test_run_agent_stream_coalesces_tool_argument_chunks(monkeypatch: Any) -> None:
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY", raising=False)

    async def fake_build_agent_session_history(
        **_kwargs: Any,
    ) -> tuple[_Agent, object, str, None]:
        return _Agent(), object(), "test-session", None

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build_agent_session_history)

    async def collect() -> list[str]:
        return [chunk async for chunk in runner.run_agent_stream("prompt")]

    events = _events_from_sse(asyncio.run(collect()))
    tool_starts = [event for event in events if event["type"] == "tool_start"]
    tool_ends = [event for event in events if event["type"] == "tool_end"]

    assert tool_starts == [
        {
            "type": "tool_start",
            "tool_call_id": "call_1",
            "tool_name": "azure_rest",
            "arguments": '{"path":"/x"}',
        }
    ]
    assert tool_ends == [
        {
            "type": "tool_end",
            "tool_call_id": "call_1",
            "tool_name": None,
            "result": "ok",
        }
    ]


def test_build_chat_options_from_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", "medium")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY", "detailed")

    assert runner._build_chat_options_from_environment() == {
        "reasoning": {
            "effort": "medium",
            "summary": "detailed",
        }
    }


def test_build_chat_options_omits_reasoning_when_unset(monkeypatch: Any) -> None:
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY", raising=False)

    assert runner._build_chat_options_from_environment() is None


def test_build_chat_options_allows_partial_reasoning_configuration(monkeypatch: Any) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", "low")
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY", raising=False)

    assert runner._build_chat_options_from_environment() == {
        "reasoning": {
            "effort": "low",
        }
    }


def test_discover_user_tools_flattens_single_basemodel_parameter(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "resource_tool.py").write_text(
        textwrap.dedent(
            """
            from pydantic import BaseModel

            class LookupParams(BaseModel):
                path: str

            async def lookup(params: LookupParams) -> str:
                return params.path
            """
        ),
        encoding="utf-8",
    )

    clear_tool_discovery_cache()
    try:
        result = discover_user_tools(tmp_path)
        discovered = result.tools
        assert len(discovered) == 1

        tool = discovered[0]
        parameters = tool.parameters()

        assert "path" in parameters["properties"]
        assert "params" not in parameters["properties"]
        assert (
            asyncio.run(tool.invoke(arguments={"path": "/subscriptions/1"}, skip_parsing=True))
            == "/subscriptions/1"
        )
    finally:
        clear_tool_discovery_cache()
