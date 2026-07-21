"""Tests for the harness-agent execution path in runner.py."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from azure_functions_agents import runner
from azure_functions_agents.config.schema import HarnessAgentConfig

# ---------------------------------------------------------------------------
# Minimal fake Agent
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, response_text: str = "hello") -> None:
        self._response_text = response_text

    async def run(self, _prompt: str, *, session: Any, options: Any = None) -> Any:
        return SimpleNamespace(text=self._response_text, messages=[])


# ---------------------------------------------------------------------------
# Tests: _build_harness_agent_session falls back when import unavailable
# ---------------------------------------------------------------------------


def test_build_harness_agent_session_falls_back_on_import_error(monkeypatch: Any) -> None:
    """When create_harness_agent is missing, harness helper delegates to plain builder."""
    plain_called: list[dict[str, Any]] = []

    async def fake_plain_builder(**kwargs: Any) -> tuple[_FakeAgent, object, str]:
        plain_called.append(kwargs)
        return _FakeAgent(), object(), "fallback-session"

    import builtins

    real_import = builtins.__import__

    def _patched_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "agent_framework" and args and args[2] and "create_harness_agent" in (args[2] or ()):
            raise ImportError("create_harness_agent not available")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _patched_import)
    monkeypatch.setattr(runner, "_build_agent_session_history", fake_plain_builder)

    asyncio.run(
        runner._build_harness_agent_session(
            instructions="do stuff",
            session_id=None,
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
            harness_config=HarnessAgentConfig(),
        )
    )

    assert len(plain_called) == 1, "plain builder should have been called once as fallback"


# ---------------------------------------------------------------------------
# Tests: run_agent dispatches to harness builder when harness_config is set
# ---------------------------------------------------------------------------


def test_run_agent_uses_harness_builder_when_config_set(monkeypatch: Any) -> None:
    """run_agent calls _build_harness_agent_session when harness_config is not None."""
    harness_called: list[dict[str, Any]] = []
    plain_called: list[dict[str, Any]] = []

    async def fake_harness_builder(**kwargs: Any) -> tuple[_FakeAgent, object, str]:
        harness_called.append(kwargs)
        return _FakeAgent("harness response"), object(), "harness-session"

    async def fake_plain_builder(**kwargs: Any) -> tuple[_FakeAgent, object, str]:
        plain_called.append(kwargs)
        return _FakeAgent("plain response"), object(), "plain-session"

    monkeypatch.setattr(runner, "_build_harness_agent_session", fake_harness_builder)
    monkeypatch.setattr(runner, "_build_agent_session_history", fake_plain_builder)

    result = asyncio.run(
        runner.run_agent("hello", harness_config=HarnessAgentConfig())
    )

    assert len(harness_called) == 1
    assert len(plain_called) == 0
    assert result.content == "harness response"
    assert result.session_id == "harness-session"


def test_run_agent_uses_plain_builder_when_config_is_none(monkeypatch: Any) -> None:
    """run_agent calls _build_agent_session_history when harness_config is None (default)."""
    harness_called: list[dict[str, Any]] = []
    plain_called: list[dict[str, Any]] = []

    async def fake_harness_builder(**kwargs: Any) -> tuple[_FakeAgent, object, str]:
        harness_called.append(kwargs)
        return _FakeAgent(), object(), "harness-session"

    async def fake_plain_builder(**kwargs: Any) -> tuple[_FakeAgent, object, str]:
        plain_called.append(kwargs)
        return _FakeAgent("plain response"), object(), "plain-session"

    monkeypatch.setattr(runner, "_build_harness_agent_session", fake_harness_builder)
    monkeypatch.setattr(runner, "_build_agent_session_history", fake_plain_builder)

    result = asyncio.run(runner.run_agent("hello"))

    assert len(plain_called) == 1
    assert len(harness_called) == 0
    assert result.session_id == "plain-session"


def test_run_agent_stream_uses_harness_builder_when_config_set(monkeypatch: Any) -> None:
    """run_agent_stream calls _build_harness_agent_session when harness_config is not None."""
    harness_called: list[dict[str, Any]] = []

    async def fake_harness_builder(**kwargs: Any) -> tuple[_FakeAgent, object, str]:
        harness_called.append(kwargs)

        class _StreamingAgent(_FakeAgent):
            async def run(self, _p: str, *, session: Any, options: Any = None) -> Any:  # type: ignore[override]
                return SimpleNamespace(text="streamed", messages=[])

        return _StreamingAgent(), object(), "stream-harness-session"

    monkeypatch.setattr(runner, "_build_harness_agent_session", fake_harness_builder)

    async def collect() -> list[str]:
        return [chunk async for chunk in runner.run_agent_stream("hi", harness_config=HarnessAgentConfig())]

    asyncio.run(collect())
    assert len(harness_called) == 1
    assert harness_called[0]["harness_config"] == HarnessAgentConfig()


def test_run_agent_passes_harness_config_fields_to_builder(monkeypatch: Any) -> None:
    """run_agent forwards harness_config with its fields to _build_harness_agent_session."""
    captured: list[dict[str, Any]] = []
    cfg = HarnessAgentConfig(max_context_window_tokens=200_000, max_output_tokens=16_000)

    async def fake_harness_builder(**kwargs: Any) -> tuple[_FakeAgent, object, str]:
        captured.append(kwargs)
        return _FakeAgent(), object(), "s"

    monkeypatch.setattr(runner, "_build_harness_agent_session", fake_harness_builder)

    asyncio.run(runner.run_agent("prompt", harness_config=cfg))

    assert captured[0]["harness_config"] is cfg
