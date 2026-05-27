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
        "model": "gpt-4o",
        "timeout": 15,
        "temperature": 0.2,
        "max_tokens": 256,
        "openai": {},
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


async def _collect_stream(agen: Any) -> list[str]:
    out: list[str] = []
    async for chunk in agen:
        out.append(chunk)
    return out


def test_run_agent_raises_runtime_error_when_asyncio_timeout_expires(monkeypatch) -> None:
    """Non-streaming run_agent must surface a `RuntimeError` when the
    `asyncio.timeout(...)` deadline expires (regression for the
    `asyncio.wait_for` → `asyncio.timeout` refactor).
    """
    cfg = _openai_agent_configuration(timeout=1)

    class StallingAgent:
        async def run(self, prompt: str, *, session: object) -> object:
            await asyncio.sleep(5.0)
            return SimpleNamespace(text="never", messages=[])

    async def fake_build(**_kwargs: Any) -> tuple[object, object, str]:
        return StallingAgent(), object(), "session-strict-non-stream"

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build)

    try:
        asyncio.run(
            runner.run_agent(
                "hello",
                instructions=None,
                agent_configuration=cfg,
                tools=[],
                mcp_tools=[],
                use_connector_tools=False,
            )
        )
    except RuntimeError as exc:
        assert "Agent run timed out after 1.0s" in str(exc)
    else:
        raise AssertionError("expected RuntimeError from asyncio.timeout expiry")


def test_run_agent_stream_emits_timeout_error_when_provider_stalls_before_first_update(monkeypatch) -> None:
    """Regression for the pre-existing best-effort streaming timeout bug.

    The previous implementation polled `loop.time() > deadline` *inside*
    the `async for update in stream:` loop, so a stalled provider call
    that never yielded its first update bypassed the deadline entirely.
    With per-iteration `asyncio.wait_for(...)` in `_iter_with_deadline`,
    the deadline now fires unconditionally and the runtime emits the SSE
    error event.
    """
    cfg = _openai_agent_configuration(timeout=1)

    async def stalled_stream() -> Any:
        await asyncio.sleep(5.0)
        yield  # unreachable; makes this an async generator

    class StallingAgent:
        def run(self, prompt: str, *, stream: bool = False, session: object = None) -> Any:
            assert stream is True
            return stalled_stream()

    async def fake_build(**_kwargs: Any) -> tuple[object, object, str]:
        return StallingAgent(), object(), "session-stalled-stream"

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build)

    chunks = asyncio.run(
        _collect_stream(
            runner.run_agent_stream(
                "hello",
                instructions=None,
                agent_configuration=cfg,
                tools=[],
                mcp_tools=[],
                use_connector_tools=False,
            )
        )
    )

    error_chunks = [c for c in chunks if '"type": "error"' in c]
    assert error_chunks, f"expected an SSE error chunk, got: {chunks!r}"
    assert "Timeout after 1.0s" in error_chunks[-1]


def test_run_agent_stream_completes_with_done_when_within_timeout(monkeypatch) -> None:
    """A fast stream completes with a `done` SSE event and no timeout error."""
    cfg = _openai_agent_configuration(timeout=10)

    class FakeTextItem:
        text = "hello"

    class FakeUpdate:
        def __init__(self) -> None:
            self.contents = [FakeTextItem()]

    async def fast_stream() -> Any:
        yield FakeUpdate()

    class FastAgent:
        def run(self, prompt: str, *, stream: bool = False, session: object = None) -> Any:
            return fast_stream()

    async def fake_build(**_kwargs: Any) -> tuple[object, object, str]:
        return FastAgent(), object(), "session-fast-stream"

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build)
    monkeypatch.setattr(runner, "_content_type", lambda item: "text")
    monkeypatch.setattr(runner, "_content_text", lambda item: item.text)

    chunks = asyncio.run(
        _collect_stream(
            runner.run_agent_stream(
                "hello",
                instructions=None,
                agent_configuration=cfg,
                tools=[],
                mcp_tools=[],
                use_connector_tools=False,
            )
        )
    )

    assert any('"type": "done"' in c for c in chunks)
    assert not any('"type": "error"' in c for c in chunks)


def test_run_agent_stream_with_no_timeout_disables_deadline(monkeypatch) -> None:
    """`timeout=None` must disable the deadline entirely (no SSE error event)."""
    cfg = _openai_agent_configuration(timeout=None)

    async def empty_stream() -> Any:
        return
        yield  # unreachable; makes this an async generator

    class NoOpAgent:
        def run(self, prompt: str, *, stream: bool = False, session: object = None) -> Any:
            return empty_stream()

    async def fake_build(**_kwargs: Any) -> tuple[object, object, str]:
        return NoOpAgent(), object(), "session-no-timeout-stream"

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build)

    chunks = asyncio.run(
        _collect_stream(
            runner.run_agent_stream(
                "hello",
                instructions=None,
                agent_configuration=cfg,
                tools=[],
                mcp_tools=[],
                use_connector_tools=False,
            )
        )
    )

    assert any('"type": "done"' in c for c in chunks)
    assert not any('"type": "error"' in c for c in chunks)


def test_run_agent_stream_slow_consumer_receives_timeout_error_event(monkeypatch) -> None:
    """Regression: timeout expiry during a slow SSE consumer must yield the
    SSE error event (and NOT cancel the consumer's task).

    `asyncio.timeout(...)` cancels the *task driving the generator* — which,
    for an async generator suspended at `yield`, is the consumer's task. The
    runtime instead uses per-iteration `asyncio.wait_for(...)` so timeout
    cancellation is scoped to the provider's `__anext__()` sub-task and the
    generator stays in control to emit the terminal error chunk.
    """
    cfg = _openai_agent_configuration(timeout=1)

    class FakeTextItem:
        text = "first"

    class FakeUpdate:
        def __init__(self) -> None:
            self.contents = [FakeTextItem()]

    async def quick_then_stall_stream() -> Any:
        yield FakeUpdate()
        await asyncio.sleep(5.0)
        yield FakeUpdate()  # unreachable; deadline expires first

    class FlakyAgent:
        def run(self, prompt: str, *, stream: bool = False, session: object = None) -> Any:
            return quick_then_stall_stream()

    async def fake_build(**_kwargs: Any) -> tuple[object, object, str]:
        return FlakyAgent(), object(), "session-slow-consumer"

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build)
    monkeypatch.setattr(runner, "_content_type", lambda item: "text")
    monkeypatch.setattr(runner, "_content_text", lambda item: item.text)

    async def slow_consumer() -> list[str]:
        out: list[str] = []
        async for chunk in runner.run_agent_stream(
            "hello",
            instructions=None,
            agent_configuration=cfg,
            tools=[],
            mcp_tools=[],
            use_connector_tools=False,
        ):
            out.append(chunk)
            if '"type": "delta"' in chunk:
                await asyncio.sleep(2.0)
        return out

    chunks = asyncio.run(slow_consumer())

    error_chunks = [c for c in chunks if '"type": "error"' in c]
    assert error_chunks, f"expected SSE error chunk after deadline, got: {chunks!r}"
    assert "Timeout after 1.0s" in error_chunks[-1]
