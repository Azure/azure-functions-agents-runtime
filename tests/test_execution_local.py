from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

import azure_functions_agents.execution.local as local_execution
from azure_functions_agents import runner
from azure_functions_agents.execution import (
    DEFAULT_EXECUTION_PROVIDER,
    AgentExecutionBackend,
    RunContext,
    RunResult,
    StartRunRequest,
    create_execution_backend,
)
from azure_functions_agents.execution.local import LocalExecutionBackend
from azure_functions_agents.harness import SANDBOX_MARKER_ENV_VAR, _ensure_sandbox
from azure_functions_agents.runner import AgentResult
from tests.test_execution_backend import assert_event_cursor_conformance, collect_run_events


async def _collect_stream(stream: AsyncIterator[str]) -> list[str]:
    return [event async for event in stream]


def _install_runner(
    monkeypatch: pytest.MonkeyPatch,
    run_agent: Any,
    run_agent_stream: Any,
) -> None:
    runner_module = SimpleNamespace(run_agent=run_agent, run_agent_stream=run_agent_stream)
    monkeypatch.setattr(local_execution, "import_module", lambda _: runner_module)


def test_factory_returns_the_default_in_process_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    import_calls: list[str] = []

    def fail_if_runner_imported(name: str) -> Any:
        import_calls.append(name)
        raise AssertionError("runner should not be imported while resolving a backend")

    monkeypatch.setattr(local_execution, "import_module", fail_if_runner_imported)
    backend = create_execution_backend()

    assert DEFAULT_EXECUTION_PROVIDER == "in_process"
    assert isinstance(backend, LocalExecutionBackend)
    assert import_calls == []
    with pytest.raises(ValueError, match="Unsupported execution provider"):
        create_execution_backend("unsupported")


def test_local_backend_matches_runner_for_non_streaming_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def fake_run_agent(*args: Any, **kwargs: Any) -> AgentResult:
        calls.append((args, kwargs))
        return AgentResult(
            session_id="session-1",
            content="answer",
            content_intermediate=["partial"],
            tool_calls=[{"name": "lookup"}],
            reasoning="because",
            events=[{"type": "message", "content": "answer"}],
            delegate_error_count=2,
        )

    async def fake_run_agent_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        yield "data: {\"type\": \"done\"}\n\n"

    _install_runner(monkeypatch, fake_run_agent, fake_run_agent_stream)

    async def exercise() -> None:
        expected = await fake_run_agent("hello", model="test-model")
        backend = LocalExecutionBackend()
        actual = await backend.run_agent("hello", model="test-model")

        assert actual == expected

    asyncio.run(exercise())
    assert calls == [
        (("hello",), {"model": "test-model"}),
        (("hello",), {"model": "test-model"}),
    ]


def test_local_backend_matches_runner_for_streaming_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def fake_run_agent(*args: Any, **kwargs: Any) -> AgentResult:
        return AgentResult(session_id="session-1", content="answer")

    async def fake_run_agent_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        calls.append((args, kwargs))
        yield "data: {\"type\": \"session\", \"session_id\": \"session-1\"}\n\n"
        yield "data: {\"type\": \"delta\", \"content\": \"answer\"}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    _install_runner(monkeypatch, fake_run_agent, fake_run_agent_stream)

    async def exercise() -> None:
        expected = await _collect_stream(fake_run_agent_stream("hello", model="test-model"))
        backend = LocalExecutionBackend()
        actual = await _collect_stream(backend.run_agent_stream("hello", model="test-model"))

        assert actual == expected

    asyncio.run(exercise())
    assert calls == [
        (("hello",), {"model": "test-model"}),
        (("hello",), {"model": "test-model"}),
    ]


@pytest.mark.parametrize(
    ("event_retention", "retained_sequences", "earliest_available_sequence", "too_old_cursor"),
    [(3, (3, 4, 5), 3, 1)],
)
def test_local_backend_reuses_event_cursor_conformance_harness(
    monkeypatch: pytest.MonkeyPatch,
    event_retention: int,
    retained_sequences: tuple[int, ...],
    earliest_available_sequence: int,
    too_old_cursor: int,
) -> None:
    async def fake_run_agent(*args: Any, **kwargs: Any) -> AgentResult:
        return AgentResult(
            session_id="session-1",
            content="answer",
            content_intermediate=["partial"],
            tool_calls=[{"name": "lookup", "result": "found"}],
            reasoning="because",
            events=[
                {"type": "session", "session_id": "session-1"},
                {"type": "delta", "content": "part"},
                {"type": "message", "content": "answer"},
                {"type": "tool_start", "tool_name": "lookup"},
                {"type": "done"},
            ],
            delegate_error_count=1,
        )

    async def fake_run_agent_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        yield "data: {\"type\": \"done\"}\n\n"

    _install_runner(monkeypatch, fake_run_agent, fake_run_agent_stream)

    async def exercise() -> None:
        backend = LocalExecutionBackend(event_retention=event_retention)
        assert isinstance(backend, AgentExecutionBackend)
        handle = await backend.start_run(
            StartRunRequest(
                prompt="hello",
                session_id="session-1",
                idempotency_key="request-1",
                timeout=60.0,
            )
        )
        context = RunContext(run_id=handle.run_id, session_id=handle.session_id)

        await assert_event_cursor_conformance(
            backend,
            context,
            retained_sequences=retained_sequences,
            earliest_available_sequence=earliest_available_sequence,
            too_old_cursor=too_old_cursor,
        )

        status = await backend.get_run(context)
        assert status.state == "succeeded"
        assert status.result == RunResult(
            content="answer",
            content_intermediate=["partial"],
            tool_calls=[{"name": "lookup", "result": "found"}],
            reasoning="because",
            delegate_error_count=1,
        )
        events = await collect_run_events(backend, context, after_sequence=0)
        assert [event.type for event in events] == ["message", "tool_start", "done"]
        assert [event.data for event in events] == [
            {"content": "answer"},
            {"tool_name": "lookup"},
            {},
        ]

    asyncio.run(exercise())


def test_local_backend_maps_runner_timeout_to_timed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_build_agent_session_history(*args: Any, **kwargs: Any) -> tuple[Any, Any, str, None]:
        return object(), object(), "session-1", None

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build_agent_session_history)

    async def exercise() -> None:
        with pytest.raises(RuntimeError) as raised:
            await runner.run_agent("hello", session_id="session-1", timeout=0.0)
        timeout_error = raised.value
        assert str(timeout_error).startswith(local_execution._RUNNER_TIMEOUT_PREFIX)

        async def fake_run_agent(*args: Any, **kwargs: Any) -> AgentResult:
            raise timeout_error

        async def fake_run_agent_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
            yield "data: {\"type\": \"done\"}\n\n"

        _install_runner(monkeypatch, fake_run_agent, fake_run_agent_stream)
        backend = LocalExecutionBackend()
        handle = await backend.start_run(StartRunRequest(prompt="hello", session_id="session-1"))
        context = RunContext(run_id=handle.run_id, session_id=handle.session_id)

        assert await collect_run_events(backend, context, after_sequence=0) == []
        status = await backend.get_run(context)
        assert status.state == "timed_out"
        assert status.error is not None
        assert status.error.code == "run_timed_out"

    asyncio.run(exercise())


def test_harness_guard_rejects_controller_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SANDBOX_MARKER_ENV_VAR, raising=False)

    with pytest.raises(RuntimeError, match="requires a sandbox process"):
        _ensure_sandbox()


def test_harness_guard_allows_sandbox_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")

    _ensure_sandbox()
