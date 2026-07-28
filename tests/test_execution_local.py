from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import azure_functions_agents.execution.local as local_execution
from azure_functions_agents import runner
from azure_functions_agents.execution import (
    DEFAULT_EXECUTION_PROVIDER,
    AgentBinding,
    AgentExecutionBackend,
    AgentResult,
    RunContext,
    RunEvent,
    RunHandle,
    RunResult,
    RunStatus,
    StartRunRequest,
    collect_terminal_run,
    create_execution_backend,
    render_sse_event,
    status_to_agent_result,
)
from azure_functions_agents.execution.local import LocalExecutionBackend
from azure_functions_agents.harness import SANDBOX_MARKER_ENV_VAR, _ensure_sandbox
from azure_functions_agents.registration import _handlers, endpoints
from tests.test_execution_backend import assert_event_cursor_conformance, collect_run_events


async def _collect_stream(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


def _binding(**overrides: Any) -> AgentBinding:
    values: dict[str, Any] = {
        "instructions": "Be helpful.",
        "tools": ["user-tool"],
        "mcp_tools": ["mcp-tool"],
        "skill_paths": [],
        "model": "test-model",
        "sandbox_tools": ["sandbox-tool"],
        "system_addendum": "Additional instructions.",
        "workflow_enabled": True,
        "workflow_durable_client": "durable-client",
        "agent_name": "test-agent",
        "display_name": "Test Agent",
        "web_request_tools": ["web-tool"],
        "subagents": [],
        "catalog": "catalog",
    }
    values.update(overrides)
    return AgentBinding(**values)


def _install_runner(
    monkeypatch: pytest.MonkeyPatch,
    run_agent: Any,
    run_agent_stream: Any,
) -> None:
    runner_module = SimpleNamespace(run_agent=run_agent, run_agent_stream=run_agent_stream)
    monkeypatch.setattr(local_execution, "import_module", lambda _: runner_module)


async def _wait_for_terminal(
    backend: AgentExecutionBackend,
    context: RunContext,
) -> RunStatus:
    while True:
        status = await backend.get_run(context)
        if status.state in {"succeeded", "failed", "canceled", "timed_out", "abandoned"}:
            return status
        await asyncio.sleep(0)


def test_factory_returns_the_default_in_process_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    import_calls: list[str] = []

    def fail_if_runner_imported(name: str) -> Any:
        import_calls.append(name)
        raise AssertionError("runner should not be imported while resolving a backend")

    monkeypatch.setattr(local_execution, "import_module", fail_if_runner_imported)
    backend = create_execution_backend(binding=_binding())

    assert DEFAULT_EXECUTION_PROVIDER == "in_process"
    assert isinstance(backend, LocalExecutionBackend)
    assert import_calls == []
    with pytest.raises(ValueError, match="Unsupported execution provider"):
        create_execution_backend(binding=_binding(), provider="unsupported")


def test_registration_import_defers_runner_loading() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(source_root)}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import azure_functions_agents.registration.endpoints; "
            "print('azure_functions_agents.runner' in sys.modules)",
        ],
        capture_output=True,
        check=True,
        cwd=source_root.parent,
        env=env,
        text=True,
    )

    assert result.stdout.strip() == "False"


def test_local_backend_routes_non_streaming_runs_through_the_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    expected = AgentResult(
        session_id="session-1",
        content="answer",
        content_intermediate=["partial"],
        tool_calls=[{"name": "lookup"}],
        reasoning="because",
        events=[{"type": "message", "content": "answer"}],
        delegate_error_count=2,
    )

    async def fake_run_agent(*args: Any, **kwargs: Any) -> AgentResult:
        calls.append((args, kwargs))
        return expected

    async def fake_run_agent_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        yield "data: {\"type\": \"done\"}\n\n"

    _install_runner(monkeypatch, fake_run_agent, fake_run_agent_stream)

    async def exercise() -> None:
        backend = LocalExecutionBackend(_binding())
        assert not hasattr(backend, "run_agent")
        assert not hasattr(backend, "run_agent_stream")
        handle = await backend.start_run(
            StartRunRequest(
                prompt="hello",
                session_id="session-1",
                idempotency_key="request-1",
                timeout=60.0,
            )
        )
        context = RunContext(run_id=handle.run_id, session_id=handle.session_id)
        status, events = await collect_terminal_run(backend, context)

        assert status_to_agent_result(status, events) == expected

    asyncio.run(exercise())
    assert calls == [
        (
            ("hello",),
            {
                **_binding().runner_kwargs(stream=False),
                "session_id": "session-1",
                "timeout": 60.0,
            },
        )
    ]


class _StreamContent:
    def __init__(self, type: str, **kwargs: Any) -> None:
        self.type = type
        for key, value in kwargs.items():
            setattr(self, key, value)


class _StreamUpdate:
    def __init__(self, *contents: _StreamContent) -> None:
        self.contents = list(contents)


class _StreamingAgent:
    def run(
        self,
        _prompt: str,
        *,
        stream: bool,
        session: object,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[_StreamUpdate]:
        assert stream is True
        assert session is not None
        assert options is None
        return self._updates()

    async def _updates(self) -> AsyncIterator[_StreamUpdate]:
        yield _StreamUpdate(
            _StreamContent("text", text="first "),
            _StreamContent("text_reasoning", text="considering"),
        )
        yield _StreamUpdate(
            _StreamContent(
                "function_call",
                call_id="call-1",
                name="lookup",
                arguments='{"id":"42"}',
            )
        )
        yield _StreamUpdate(_StreamContent("function_result", call_id="call-1", result="found"))
        yield _StreamUpdate(_StreamContent("text", text="answer"))


def test_local_backend_stream_round_trips_real_runner_sse_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_build_agent_session_history(
        **_kwargs: Any,
    ) -> tuple[_StreamingAgent, object, str, None]:
        return _StreamingAgent(), object(), "session-1", None

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build_agent_session_history)
    binding = _binding()

    async def exercise() -> None:
        expected = await _collect_stream(
            runner.run_agent_stream(
                "hello",
                session_id="session-1",
                timeout=60.0,
                **binding.runner_kwargs(stream=True),
            )
        )
        backend = LocalExecutionBackend(binding, stream_events=True)
        handle = await backend.start_run(
            StartRunRequest(prompt="hello", session_id="session-1", timeout=60.0)
        )
        context = RunContext(run_id=handle.run_id, session_id=handle.session_id)
        actual = [
            render_sse_event(event)
            async for event in backend.read_events(context, after_sequence=0)
        ]

        assert actual == expected
        status = await backend.get_run(context)
        assert status.state == "succeeded"

    asyncio.run(exercise())


def test_closing_a_stream_reader_does_not_cancel_the_local_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        allow_completion = asyncio.Event()

        async def fake_run_agent(*args: Any, **kwargs: Any) -> AgentResult:
            raise AssertionError("streaming backend must not invoke run_agent")

        async def fake_run_agent_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
            yield "data: {\"type\": \"session\", \"session_id\": \"session-1\"}\n\n"
            await allow_completion.wait()
            yield "data: {\"type\": \"done\"}\n\n"

        _install_runner(monkeypatch, fake_run_agent, fake_run_agent_stream)
        backend = LocalExecutionBackend(_binding(), stream_events=True)
        handle = await backend.start_run(StartRunRequest(prompt="hello", session_id="session-1"))
        context = RunContext(run_id=handle.run_id, session_id=handle.session_id)
        reader = backend.read_events(context, after_sequence=0)

        first_event = await anext(reader)
        assert first_event.type == "session"
        await reader.aclose()

        allow_completion.set()
        status = await _wait_for_terminal(backend, context)
        assert status.state == "succeeded"

    asyncio.run(exercise())


def test_local_backend_reuses_event_cursor_conformance_harness(
    monkeypatch: pytest.MonkeyPatch,
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
        backend = LocalExecutionBackend(_binding(), event_retention=3)
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
        status = await _wait_for_terminal(backend, context)

        await assert_event_cursor_conformance(
            backend,
            context,
            retained_sequences=(3, 4, 5),
            earliest_available_sequence=3,
            too_old_cursor=1,
        )
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
    async def fake_build_agent_session_history(
        *args: Any,
        **kwargs: Any,
    ) -> tuple[Any, Any, str, None]:
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
        backend = LocalExecutionBackend(_binding())
        handle = await backend.start_run(StartRunRequest(prompt="hello", session_id="session-1"))
        context = RunContext(run_id=handle.run_id, session_id=handle.session_id)

        assert await collect_run_events(backend, context, after_sequence=0) == []
        status = await backend.get_run(context)
        assert status.state == "timed_out"
        assert status.error is not None
        assert status.error.code == "run_timed_out"

    asyncio.run(exercise())


def test_local_backend_preserves_runner_value_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_agent(*args: Any, **kwargs: Any) -> AgentResult:
        raise ValueError("Invalid session_id (must match [A-Za-z0-9._-]{1,128})")

    async def fake_run_agent_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        yield "data: {\"type\": \"done\"}\n\n"

    _install_runner(monkeypatch, fake_run_agent, fake_run_agent_stream)

    async def exercise() -> None:
        backend = LocalExecutionBackend(_binding())
        handle = await backend.start_run(StartRunRequest(prompt="hello", session_id="invalid session"))
        context = RunContext(run_id=handle.run_id, session_id=handle.session_id)
        status, events = await collect_terminal_run(backend, context)

        assert status.error is not None
        assert status.error.code == "invalid_argument"
        assert status.error.fault_domain == "app"
        with pytest.raises(ValueError, match="Invalid session_id"):
            status_to_agent_result(status, events)

    asyncio.run(exercise())


class _RecordingBackend:
    def __init__(self, result: RunResult, events: list[RunEvent]) -> None:
        self._result = result
        self._events = events
        self.requests: list[StartRunRequest] = []

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        self.requests.append(request)
        return RunHandle("run-1", request.session_id or "generated-session", "accepted", datetime.now(UTC))

    async def get_run(self, context: RunContext) -> RunStatus:
        return RunStatus(
            run_id=context.run_id,
            session_id=context.session_id,
            state="succeeded",
            last_sequence=len(self._events),
            result_available=True,
            result=self._result,
        )

    async def read_events(
        self,
        context: RunContext,
        after_sequence: int,
    ) -> AsyncIterator[RunEvent]:
        for event in self._events:
            if event.sequence > after_sequence:
                yield event

    async def cancel_run(self, context: RunContext) -> RunStatus:
        return await self.get_run(context)


@pytest.mark.parametrize("module", [endpoints, _handlers])
def test_registration_non_streaming_wrappers_use_only_lifecycle_methods(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
) -> None:
    backend = _RecordingBackend(
        RunResult(
            content="answer",
            content_intermediate=["partial"],
            tool_calls=[{"tool_name": "lookup"}],
            reasoning="because",
            delegate_error_count=1,
        ),
        [RunEvent(1, "message", {"content": "answer"}, datetime.now(UTC))],
    )
    received_bindings: list[AgentBinding] = []

    def fake_factory(
        *,
        binding: AgentBinding,
        provider: str = DEFAULT_EXECUTION_PROVIDER,
        stream_events: bool = False,
    ) -> AgentExecutionBackend:
        assert provider == DEFAULT_EXECUTION_PROVIDER
        assert stream_events is False
        received_bindings.append(binding)
        return backend

    monkeypatch.setattr(module, "create_execution_backend", fake_factory)

    result = asyncio.run(
        module._run_agent(
            "hello",
            session_id="session-1",
            timeout=60.0,
            model="test-model",
            tools=["user-tool"],
            workflow_enabled=True,
        )
    )

    assert backend.requests == [
        StartRunRequest(prompt="hello", session_id="session-1", timeout=60.0)
    ]
    assert received_bindings == [
        AgentBinding(
            model="test-model",
            tools=["user-tool"],
            workflow_enabled=True,
        )
    ]
    assert result == AgentResult(
        session_id="session-1",
        content="answer",
        content_intermediate=["partial"],
        tool_calls=[{"tool_name": "lookup"}],
        reasoning="because",
        events=[{"type": "message", "content": "answer"}],
        delegate_error_count=1,
    )


def test_registration_stream_wrapper_uses_lifecycle_events(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        RunEvent(1, "session", {"session_id": "session-1"}, datetime.now(UTC)),
        RunEvent(2, "delta", {"content": "answer"}, datetime.now(UTC)),
        RunEvent(3, "done", {}, datetime.now(UTC)),
    ]
    backend = _RecordingBackend(
        RunResult("answer", [], [], None, 0),
        events,
    )
    received_bindings: list[AgentBinding] = []

    def fake_factory(
        *,
        binding: AgentBinding,
        provider: str = DEFAULT_EXECUTION_PROVIDER,
        stream_events: bool = False,
    ) -> AgentExecutionBackend:
        assert provider == DEFAULT_EXECUTION_PROVIDER
        assert stream_events is True
        received_bindings.append(binding)
        return backend

    monkeypatch.setattr(endpoints, "create_execution_backend", fake_factory)

    result = asyncio.run(
        _collect_stream(
            endpoints._run_agent_stream(
                "hello",
                session_id="session-1",
                timeout=60.0,
                model="test-model",
                display_name="Test Agent",
            )
        )
    )

    assert backend.requests == [
        StartRunRequest(prompt="hello", session_id="session-1", timeout=60.0)
    ]
    assert received_bindings == [AgentBinding(model="test-model", display_name="Test Agent")]
    assert result == [
        "data: {\"type\": \"session\", \"session_id\": \"session-1\"}\n\n",
        "data: {\"type\": \"delta\", \"content\": \"answer\"}\n\n",
        "data: {\"type\": \"done\"}\n\n",
    ]


def test_harness_guard_rejects_controller_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SANDBOX_MARKER_ENV_VAR, raising=False)

    with pytest.raises(RuntimeError, match="requires a sandbox process"):
        _ensure_sandbox()


def test_harness_guard_allows_sandbox_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")

    _ensure_sandbox()
