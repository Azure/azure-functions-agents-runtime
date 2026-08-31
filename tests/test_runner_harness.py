"""Tests for the harness-agent execution path in runner.py."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, ClassVar

from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    HistoryProvider,
    Message,
    SkillsProvider,
)

from azure_functions_agents import runner
from azure_functions_agents.client_manager import InferenceTarget
from azure_functions_agents.config.schema import (
    AgentConfiguration,
    AgentFrameworkCompactionConfig,
    AgentFrameworkConfiguration,
)

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


def test_build_agent_session_forces_provider_managed_history(
    monkeypatch: Any,
) -> None:
    """Fresh request-scoped sessions must reload history from the configured provider."""
    captured: list[dict[str, Any]] = []

    def fake_create_harness_agent(_client: Any, **kwargs: Any) -> _FakeAgent:
        captured.append(kwargs)
        return _FakeAgent()

    import agent_framework

    monkeypatch.setattr(
        agent_framework,
        "create_harness_agent",
        fake_create_harness_agent,
        raising=False,
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
            agent_name=None,
            web_request_tools=None,
            agent_configuration=AgentConfiguration(),
        )
    )

    assert captured[0]["default_options"] == {"store": False}


def test_build_agent_session_forwards_system_instructions(monkeypatch: Any) -> None:
    """Markdown and runtime instructions are forwarded without MAF harness guidance."""
    captured: list[dict[str, Any]] = []

    def fake_create_harness_agent(_client: Any, **kwargs: Any) -> _FakeAgent:
        captured.append(kwargs)
        return _FakeAgent()

    import agent_framework

    monkeypatch.setattr(
        agent_framework,
        "create_harness_agent",
        fake_create_harness_agent,
        raising=False,
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
            agent_configuration=AgentConfiguration(),
        )
    )

    assert captured[0]["agent_instructions"] == (
        "Markdown system prompt. Runtime system addendum."
    )
    assert captured[0]["harness_instructions"] == ""
    assert captured[0]["tools"] == []
    assert captured[0]["disable_todo"] is True
    assert captured[0]["disable_mode"] is True
    assert captured[0]["disable_file_memory"] is True
    assert captured[0]["disable_web_search"] is True
    assert captured[0]["disable_tool_auto_approval"] is True
    assert captured[0]["default_options"] == {"store": False}


def test_build_role_agent_auto_approves_read_only_skill_tools(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    captured: list[dict[str, Any]] = []

    def fake_create_harness_agent(_client: Any, **kwargs: Any) -> _FakeAgent:
        captured.append(kwargs)
        return _FakeAgent()

    import agent_framework

    monkeypatch.setattr(
        agent_framework,
        "create_harness_agent",
        fake_create_harness_agent,
    )

    runner._build_role_agent(
        object(),
        agent_instructions=None,
        tools=[],
        skill_paths=[tmp_path],
        agent_name="skills-agent",
        history_provider=None,
        agent_configuration=AgentConfiguration(),
    )

    provider = captured[0]["skills_provider"]
    assert isinstance(provider, SkillsProvider)
    assert provider._disable_load_skill_approval is True
    assert provider._disable_read_skill_resource_approval is True
    assert provider._disable_run_skill_script_approval is False
    assert "skills_paths" not in captured[0]


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

    import agent_framework

    monkeypatch.setattr(
        agent_framework,
        "create_harness_agent",
        fake_create_harness_agent,
        raising=False,
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
            agent_configuration=AgentConfiguration(),
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
            "agent_configuration": AgentConfiguration(),
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
            "agent_configuration": AgentConfiguration(
                max_output_tokens=100,
                agent_framework=AgentFrameworkConfiguration(
                    compaction=AgentFrameworkCompactionConfig(max_context_window_tokens=500)
                ),
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
# Tests: public runners always dispatch to the harness session builder
# ---------------------------------------------------------------------------


def test_run_agent_uses_session_builder_with_configuration(monkeypatch: Any) -> None:
    session_calls: list[dict[str, Any]] = []
    subagents = [SimpleNamespace(agent="billing")]
    catalog = object()

    async def fake_session_builder(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        session_calls.append(kwargs)
        return (
            _FakeAgent("harness response"),
            object(),
            "harness-session",
            None,
            InferenceTarget(),
        )

    monkeypatch.setattr(runner, "_build_agent_session", fake_session_builder)

    result = asyncio.run(
        runner.run_agent(
            "hello",
            agent_configuration=AgentConfiguration(),
            subagents=subagents,
            catalog=catalog,
        )
    )

    assert len(session_calls) == 1
    assert session_calls[0]["subagents"] is subagents
    assert session_calls[0]["catalog"] is catalog
    assert isinstance(session_calls[0]["coordinator_deadline"], float)
    assert result.content == "harness response"
    assert result.session_id == "harness-session"


def test_run_agent_uses_session_builder_with_default_configuration(monkeypatch: Any) -> None:
    session_calls: list[dict[str, Any]] = []

    async def fake_session_builder(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        session_calls.append(kwargs)
        return _FakeAgent("response"), object(), "session", None, InferenceTarget()

    monkeypatch.setattr(runner, "_build_agent_session", fake_session_builder)

    result = asyncio.run(runner.run_agent("hello"))

    assert len(session_calls) == 1
    assert session_calls[0]["agent_configuration"] is None
    assert result.session_id == "session"


def test_run_agent_stream_uses_session_builder_with_configuration(monkeypatch: Any) -> None:
    session_calls: list[dict[str, Any]] = []
    subagents = [SimpleNamespace(agent="billing")]
    catalog = object()

    async def fake_session_builder(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        session_calls.append(kwargs)

        class _StreamingAgent(_FakeAgent):
            async def run(self, _p: str, *, session: Any, options: Any = None) -> Any:  # type: ignore[override]
                return SimpleNamespace(text="streamed", messages=[])

        return _StreamingAgent(), object(), "stream-harness-session", None, InferenceTarget()

    monkeypatch.setattr(runner, "_build_agent_session", fake_session_builder)

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in runner.run_agent_stream(
                "hi",
                agent_configuration=AgentConfiguration(),
                subagents=subagents,
                catalog=catalog,
            )
        ]

    asyncio.run(collect())
    assert len(session_calls) == 1
    assert session_calls[0]["agent_configuration"] == AgentConfiguration()
    assert session_calls[0]["subagents"] is subagents
    assert session_calls[0]["catalog"] is catalog
    assert isinstance(session_calls[0]["coordinator_deadline"], float)


def test_run_agent_passes_agent_configuration_to_builder(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    config = AgentConfiguration(
        max_output_tokens=16_000,
        agent_framework=AgentFrameworkConfiguration(
            compaction=AgentFrameworkCompactionConfig(max_context_window_tokens=200_000)
        ),
    )

    async def fake_harness_builder(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        captured.append(kwargs)
        return _FakeAgent(), object(), "s", None, InferenceTarget()

    monkeypatch.setattr(runner, "_build_agent_session", fake_harness_builder)

    asyncio.run(runner.run_agent("prompt", agent_configuration=config))

    assert captured[0]["agent_configuration"] is config
