"""Tests for the harness-agent execution path in runner.py."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    HistoryProvider,
    Message,
)

from azure_functions_agents import runner
from azure_functions_agents.client_manager import InferenceTarget
from azure_functions_agents.config.schema import CompactionConfig

# ---------------------------------------------------------------------------
# Minimal fake Agent
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, response_text: str = "hello") -> None:
        self._response_text = response_text

    async def run(self, _prompt: str, *, session: Any, options: Any = None) -> Any:
        return SimpleNamespace(text=self._response_text, messages=[])


class _RecordingStoringChatClient(ChatMiddlewareLayer, BaseChatClient):
    STORES_BY_DEFAULT: ClassVar[bool] = True

    def __init__(self, response_text: str = "response") -> None:
        super().__init__()
        self.calls: list[list[str]] = []
        self.response_text = response_text

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        assert not stream
        self.calls.append([message.text for message in messages])

        async def get_response() -> ChatResponse[Any]:
            return ChatResponse(messages=[Message("assistant", [self.response_text])])

        return get_response()


class _SharedHistoryProvider(HistoryProvider):
    def __init__(self, messages: list[Message]) -> None:
        super().__init__(source_id="shared_history")
        self.messages = messages

    async def get_messages(
        self,
        session_id: str | None,
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Message]:
        return list(self.messages)

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.messages.extend(messages)


# ---------------------------------------------------------------------------
# Tests: universal harness construction
# ---------------------------------------------------------------------------


def test_runner_import_fails_without_create_harness_agent() -> None:
    """An incompatible MAF installation fails during runtime import, without fallback."""
    project_root = Path(__file__).resolve().parents[1]
    script = """
import sys
import types

import agent_framework as installed

incompatible = types.ModuleType("agent_framework")
incompatible.__dict__.update(
    {
        name: value
        for name, value in vars(installed).items()
        if name not in {"create_harness_agent", "__getattr__"}
    }
)

def resolve_export(name):
    if name == "create_harness_agent":
        raise AttributeError(name)
    return getattr(installed, name)

incompatible.__getattr__ = resolve_export
sys.modules["agent_framework"] = incompatible
for name in list(sys.modules):
    if name.startswith("azure_functions_agents"):
        del sys.modules[name]

try:
    import azure_functions_agents.runner
except RuntimeError as exc:
    assert "agent-framework-core==1.13.0" in str(exc)
    assert "create_harness_agent" in str(exc)
else:
    raise AssertionError("runner import unexpectedly accepted incompatible MAF")
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(project_root / "src"), environment.get("PYTHONPATH")))
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_build_agent_session_forces_provider_managed_history(
    monkeypatch: Any,
) -> None:
    """Fresh request-scoped sessions must reload history from the configured provider."""
    captured: list[dict[str, Any]] = []

    def fake_create_harness_agent(_client: Any, **kwargs: Any) -> _FakeAgent:
        captured.append(kwargs)
        return _FakeAgent()

    monkeypatch.setattr(
        runner,
        "create_harness_agent",
        fake_create_harness_agent,
    )
    monkeypatch.setattr(
        runner.get_client_manager(),
        "build_chat_client_with_target",
        lambda _model: (object(), InferenceTarget()),
    )
    monkeypatch.setattr(runner, "_build_history_provider", lambda: object())

    asyncio.run(
        runner._build_agent_session(
            instructions="do stuff",
            session_id="shared-session",
            tools=[],
            mcp_tools=[],
            skill_paths=None,
            model=None,
            sandbox_tools=None,
            system_addendum=None,
            workflow_enabled=False,
            workflow_durable_client=None,
            agent_name="support-agent",
            web_request_tools=None,
            compaction_config=None,
        )
    )

    assert captured[0]["default_options"] == {"store": False}
    assert captured[0]["name"] == "support-agent"
    assert captured[0]["tools"] == []
    assert captured[0]["max_context_window_tokens"] is None
    assert captured[0]["max_output_tokens"] is None
    assert captured[0]["harness_instructions"] == ""
    assert captured[0]["disable_todo"] is True
    assert captured[0]["disable_mode"] is True
    assert captured[0]["disable_file_memory"] is True
    assert captured[0]["disable_web_search"] is True
    assert captured[0]["disable_tool_auto_approval"] is True


def test_build_agent_session_forwards_system_instructions(monkeypatch: Any) -> None:
    """Markdown and runtime instructions are forwarded without MAF harness guidance."""
    captured: list[dict[str, Any]] = []

    def fake_create_harness_agent(_client: Any, **kwargs: Any) -> _FakeAgent:
        captured.append(kwargs)
        return _FakeAgent()

    monkeypatch.setattr(
        runner,
        "create_harness_agent",
        fake_create_harness_agent,
    )
    monkeypatch.setattr(
        runner.get_client_manager(),
        "build_chat_client_with_target",
        lambda _model: (object(), InferenceTarget()),
    )
    monkeypatch.setattr(runner, "_build_history_provider", lambda: object())

    asyncio.run(
        runner._build_agent_session(
            instructions="Markdown system prompt.",
            session_id="instruction-session",
            tools=[],
            mcp_tools=[],
            skill_paths=None,
            model=None,
            sandbox_tools=None,
            system_addendum=" Runtime system addendum.",
            workflow_enabled=False,
            workflow_durable_client=None,
            agent_name=None,
            web_request_tools=None,
            compaction_config=None,
        )
    )

    assert captured[0]["agent_instructions"] == (
        "Markdown system prompt. Runtime system addendum."
    )
    assert captured[0]["harness_instructions"] == ""
    assert captured[0]["disable_todo"] is True


def test_build_agent_session_appends_subagent_tools(monkeypatch: Any) -> None:
    """Harness agents receive all shared tools and return their delegation error tracker."""
    captured_agent_options: list[dict[str, Any]] = []
    captured_delegate_options: list[tuple[Any, Any, float]] = []
    local_tool = SimpleNamespace(name="local_tool")
    mcp_tool = SimpleNamespace(name="mcp_tool")
    sandbox_tool = SimpleNamespace(name="sandbox_tool")
    web_request_tool = SimpleNamespace(name="web_request_tool")
    delegate_tool = SimpleNamespace(name="delegate_billing")
    delegate_tracker = runner._DelegateErrorTracker()
    subagents = [SimpleNamespace(agent="billing")]
    catalog = object()

    def fake_create_harness_agent(_client: Any, **kwargs: Any) -> _FakeAgent:
        captured_agent_options.append(kwargs)
        return _FakeAgent()

    async def fake_build_subagent_tools(
        received_subagents: Any,
        received_catalog: Any,
        *,
        coordinator_deadline: float,
    ) -> tuple[list[Any], runner._DelegateErrorTracker]:
        captured_delegate_options.append(
            (received_subagents, received_catalog, coordinator_deadline)
        )
        return [delegate_tool], delegate_tracker

    monkeypatch.setattr(
        runner,
        "create_harness_agent",
        fake_create_harness_agent,
    )
    monkeypatch.setattr(
        runner.get_client_manager(),
        "build_chat_client_with_target",
        lambda _model: (object(), InferenceTarget()),
    )
    monkeypatch.setattr(runner, "_build_history_provider", lambda: object())
    monkeypatch.setattr(runner, "build_subagent_tools", fake_build_subagent_tools)

    _, _, _, returned_tracker, _ = asyncio.run(
        runner._build_agent_session(
            instructions="coordinate specialists",
            session_id="shared-session",
            tools=[local_tool],
            mcp_tools=[mcp_tool],
            skill_paths=None,
            model=None,
            sandbox_tools=[sandbox_tool],
            system_addendum=None,
            workflow_enabled=False,
            workflow_durable_client=None,
            agent_name="coordinator",
            web_request_tools=[web_request_tool],
            compaction_config=None,
            subagents=subagents,
            catalog=catalog,
            coordinator_deadline=123.0,
        )
    )

    assert captured_delegate_options == [(subagents, catalog, 123.0)]
    assert [tool.name for tool in captured_agent_options[0]["tools"]] == [
        "local_tool",
        "sandbox_tool",
        "web_request_tool",
        "mcp_tool",
        "delegate_billing",
    ]
    assert returned_tracker is delegate_tracker


def test_fresh_harness_agents_reload_history_for_same_session(monkeypatch: Any) -> None:
    """Turn two receives turn-one history even though both agents and sessions are fresh."""
    client = _RecordingStoringChatClient()
    stored_messages: list[Message] = []

    monkeypatch.setattr(
        runner.get_client_manager(),
        "build_chat_client_with_target",
        lambda _model: (client, InferenceTarget()),
    )
    monkeypatch.setattr(
        runner,
        "_build_history_provider",
        lambda: _SharedHistoryProvider(stored_messages),
    )

    async def run_two_turns() -> None:
        common = {
            "instructions": "",
            "session_id": "shared-session",
            "tools": [],
            "mcp_tools": [],
            "skill_paths": None,
            "model": None,
            "sandbox_tools": None,
            "system_addendum": None,
            "workflow_enabled": False,
            "workflow_durable_client": None,
            "agent_name": None,
            "web_request_tools": None,
            "compaction_config": None,
        }
        first_agent, first_session, _, _, _ = await runner._build_agent_session(**common)
        await first_agent.run("Use the Premium plan.", session=first_session)
        assert [message.text for message in stored_messages] == [
            "Use the Premium plan.",
            "response",
        ]

        second_agent, second_session, _, _, _ = await runner._build_agent_session(**common)
        await second_agent.run("Which plan did I choose?", session=second_session)

    asyncio.run(run_two_turns())

    assert client.calls == [
        ["Use the Premium plan."],
        ["Use the Premium plan.", "response", "Which plan did I choose?"],
    ]


def test_harness_compacts_model_context_without_rewriting_stored_history(
    monkeypatch: Any,
) -> None:
    """Compaction trims model context while provider storage retains the full conversation."""
    response_text = "prior response detail " * 80
    first_prompt = "first-turn context " * 80
    second_prompt = "current-turn question " * 80
    client = _RecordingStoringChatClient(response_text=response_text)
    stored_messages: list[Message] = []

    monkeypatch.setattr(
        runner.get_client_manager(),
        "build_chat_client_with_target",
        lambda _model: (client, InferenceTarget()),
    )
    monkeypatch.setattr(
        runner,
        "_build_history_provider",
        lambda: _SharedHistoryProvider(stored_messages),
    )

    async def run_two_turns() -> None:
        common = {
            "instructions": "",
            "session_id": "compacted-session",
            "tools": [],
            "mcp_tools": [],
            "skill_paths": None,
            "model": None,
            "sandbox_tools": None,
            "system_addendum": None,
            "workflow_enabled": False,
            "workflow_durable_client": None,
            "agent_name": None,
            "web_request_tools": None,
            "compaction_config": CompactionConfig(
                max_context_window_tokens=500,
                max_output_tokens=100,
            ),
        }
        first_agent, first_session, _, _, _ = await runner._build_agent_session(**common)
        await first_agent.run(first_prompt, session=first_session)

        second_agent, second_session, _, _, _ = await runner._build_agent_session(**common)
        await second_agent.run(second_prompt, session=second_session)

    asyncio.run(run_two_turns())

    assert client.calls[0] == [first_prompt]
    assert client.calls[1], "expected a second model call"
    assert client.calls[1][-1] == second_prompt
    assert first_prompt not in client.calls[1], (
        "expected prior history to be compacted (no full first prompt in the second call)"
    )
    assert [message.text for message in stored_messages] == [
        first_prompt,
        response_text,
        second_prompt,
        response_text,
    ], "expected provider storage to retain the full, uncompacted conversation"


# ---------------------------------------------------------------------------
# Tests: public execution always uses the universal builder
# ---------------------------------------------------------------------------


def test_run_agent_uses_universal_builder_without_compaction(monkeypatch: Any) -> None:
    """run_agent uses the universal builder when compaction is not configured."""
    captured: list[dict[str, Any]] = []
    subagents = [SimpleNamespace(agent="billing")]
    catalog = object()

    async def fake_builder(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        captured.append(kwargs)
        return (
            _FakeAgent("response"),
            object(),
            "session",
            None,
            InferenceTarget(),
        )

    monkeypatch.setattr(runner, "_build_agent_session", fake_builder)

    result = asyncio.run(
        runner.run_agent(
            "hello",
            subagents=subagents,
            catalog=catalog,
        )
    )

    assert len(captured) == 1
    assert captured[0]["compaction_config"] is None
    assert captured[0]["subagents"] is subagents
    assert captured[0]["catalog"] is catalog
    assert isinstance(captured[0]["coordinator_deadline"], float)
    assert result.content == "response"
    assert result.session_id == "session"


def test_run_agent_stream_forwards_compaction(monkeypatch: Any) -> None:
    """run_agent_stream forwards compaction to the universal builder."""
    captured: list[dict[str, Any]] = []
    subagents = [SimpleNamespace(agent="billing")]
    catalog = object()
    config = CompactionConfig(max_context_window_tokens=64_000, max_output_tokens=4_000)

    async def fake_builder(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        captured.append(kwargs)

        class _StreamingAgent(_FakeAgent):
            async def run(self, _p: str, *, session: Any, options: Any = None) -> Any:  # type: ignore[override]
                return SimpleNamespace(text="streamed", messages=[])

        return _StreamingAgent(), object(), "stream-session", None, InferenceTarget()

    monkeypatch.setattr(runner, "_build_agent_session", fake_builder)

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in runner.run_agent_stream(
                "hi",
                compaction_config=config,
                subagents=subagents,
                catalog=catalog,
            )
        ]

    asyncio.run(collect())
    assert len(captured) == 1
    assert captured[0]["compaction_config"] is config
    assert captured[0]["subagents"] is subagents
    assert captured[0]["catalog"] is catalog
    assert isinstance(captured[0]["coordinator_deadline"], float)


def test_run_agent_passes_compaction_config_to_builder(monkeypatch: Any) -> None:
    """run_agent forwards the resolved compaction configuration unchanged."""
    captured: list[dict[str, Any]] = []
    config = CompactionConfig(max_context_window_tokens=200_000, max_output_tokens=16_000)

    async def fake_builder(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        captured.append(kwargs)
        return _FakeAgent(), object(), "s", None, InferenceTarget()

    monkeypatch.setattr(runner, "_build_agent_session", fake_builder)

    asyncio.run(runner.run_agent("prompt", compaction_config=config))

    assert captured[0]["compaction_config"] is config


def test_removed_harness_config_python_keywords_are_rejected() -> None:
    """The old constructor-selection keyword has no compatibility alias."""
    with pytest.raises(TypeError, match="harness_config"):
        runner.run_agent("prompt", harness_config=True)  # type: ignore[call-arg]

    with pytest.raises(TypeError, match="harness_config"):
        runner.run_agent_stream("prompt", harness_config=True)  # type: ignore[call-arg]
