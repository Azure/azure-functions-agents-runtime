from __future__ import annotations

import asyncio
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from azure_functions_agents import runner
from azure_functions_agents.config.schema import AgentConfiguration


def _openai_agent_configuration(**overrides: object) -> AgentConfiguration:
    payload: dict[str, object] = {
        "provider": "openai",
        "timeout": 15,
        "temperature": 0.2,
        "max_tokens": 256,
        "openai": {"model": "gpt-4o"},
    }
    payload.update(overrides)
    return AgentConfiguration.model_validate(payload)


def test_build_agent_session_history_builds_chat_options_from_universal_knobs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeChatOptions:
        def __init__(self, **kwargs: Any) -> None:
            captured["chat_options_kwargs"] = kwargs

    class FakeAgentSession:
        def __init__(self, session_id: str | None = None) -> None:
            self.session_id = session_id or "generated-session-id"

    class FakeFileHistoryProvider:
        def __init__(self, storage_path: Path) -> None:
            captured["history_path"] = storage_path

    class FakeAgent:
        def __init__(self, chat_client: object, **kwargs: Any) -> None:
            captured["chat_client"] = chat_client
            captured["agent_kwargs"] = kwargs

    fake_agent_framework = ModuleType("agent_framework")
    fake_agent_framework.Agent = FakeAgent
    fake_agent_framework.AgentSession = FakeAgentSession
    fake_agent_framework.ChatOptions = FakeChatOptions
    fake_agent_framework.FileHistoryProvider = FakeFileHistoryProvider

    monkeypatch.setitem(__import__("sys").modules, "agent_framework", fake_agent_framework)
    monkeypatch.setattr(
        runner,
        "build_chat_client",
        lambda cfg: captured.setdefault("client_cfg", cfg) or "chat-client",
    )
    monkeypatch.setattr(runner, "resolve_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "get_app_root", lambda: tmp_path)

    cfg = _openai_agent_configuration(top_p=None)

    asyncio.run(
        runner._build_agent_session_history(
            instructions="Help the user.",
            agent_configuration=cfg,
            session_id="session-123",
            tools=[],
            mcp_tools=[],
            skill_paths=None,
            use_connector_tools=False,
            sandbox_tools=None,
        )
    )

    assert captured["client_cfg"] is cfg
    assert captured["chat_options_kwargs"] == {
        "temperature": 0.2,
        "max_tokens": 256,
    }
    assert "top_p" not in captured["chat_options_kwargs"]
    assert "timeout" not in captured["chat_options_kwargs"]
    assert captured["agent_kwargs"]["default_options"].__class__ is FakeChatOptions


def test_run_agent_passes_agent_configuration_to_builder(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    cfg = _openai_agent_configuration()

    class FakeAgent:
        async def run(self, prompt: str, *, session: object) -> object:
            return SimpleNamespace(text="ok", messages=[])

    async def fake_build_agent_session_history(**kwargs: Any) -> tuple[object, object, str]:
        captured.update(kwargs)
        return FakeAgent(), object(), "session-123"

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build_agent_session_history)

    result = asyncio.run(
        runner.run_agent(
            "hello",
            instructions="Help the user.",
            agent_configuration=cfg,
            tools=[],
            mcp_tools=[],
            use_connector_tools=False,
        )
    )

    assert captured["agent_configuration"] is cfg
    assert result.session_id == "session-123"
    assert result.content == "ok"
