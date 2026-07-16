from __future__ import annotations

import asyncio
import contextlib
import json
import textwrap
import time
from collections.abc import AsyncIterator, Iterator
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


class _StallingAgent:
    """A fake agent whose stream never produces a *first* update.

    Regression fixture for B1 (streaming): before the fix, ``deadline``
    expiry was only checked *after* ``async for update in stream:`` handed
    back an update, so a generator that produces nothing at all (e.g. a
    hung tool/model call) could block ``run_agent_stream`` past its
    coordinator deadline indefinitely — nothing bounded the ``anext()``
    call itself.
    """

    def run(
        self,
        _prompt: str,
        *,
        stream: bool,
        session: object,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[_Update]:
        assert stream is True
        return self._updates()

    async def _updates(self) -> AsyncIterator[_Update]:
        await asyncio.sleep(10.0)
        yield _Update([_Content("text", text="unreachable")])  # pragma: no cover


class _ToolErrorAgent:
    """A fake agent whose one tool call's result carries the sandbox-style
    JSON error envelope ``_looks_like_tool_error`` recognizes as a failure —
    but with no delegate-tracker failure at all. Regression fixture for M3
    (streaming): proves ``run_agent_stream`` counts *ordinary* (non-delegate)
    tool-call failures on their own rather than relying entirely on
    ``delegate_error_tracker.count``.
    """

    def run(
        self,
        _prompt: str,
        *,
        stream: bool,
        session: object,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[_Update]:
        assert stream is True
        return self._updates()

    async def _updates(self) -> AsyncIterator[_Update]:
        yield _Update(
            [_Content("function_call", call_id="call_1", name="sandbox_exec", arguments="{}")]
        )
        yield _Update([_Content("function_result", call_id="call_1", result='{"error": "boom"}')])


class _CapturedSpan:
    """Fake ``RuntimeSpan`` — mirrors ``test_web_request.py``'s ``_CapturedSpan``.

    ``run_agent_stream`` opens its *own* span (unlike ``run_agent``, whose
    callers wrap it in theirs — see the comment above ``start_span`` in
    ``runner.py``), so tests that assert on that span's attributes replace
    ``runner.start_span`` itself rather than ``runner.current_span``.
    """

    def __init__(self, attributes: dict[str, Any]) -> None:
        self.attributes: dict[str, Any] = dict(attributes)
        self.errors: list[tuple[str, str]] = []
        self.exceptions: list[BaseException] = []

    def set_attribute(self, key: str, value: Any) -> None:
        if value is not None:
            self.attributes[key] = value

    def set_content(self, key: str, value: str) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        pass

    def set_error(self, message: str, *, fault_domain: str) -> None:
        self.errors.append((message, fault_domain))

    def record_exception(self, exc: BaseException, *, fault_domain: str | None = None) -> None:
        self.exceptions.append(exc)
        self.errors.append((str(exc), fault_domain or "unknown"))


def _install_start_span_capture(monkeypatch: Any) -> list[_CapturedSpan]:
    spans: list[_CapturedSpan] = []

    @contextlib.contextmanager
    def _fake_start_span(
        name: str,
        *,
        fault_domain: str | None = None,
        lifecycle_stage: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[_CapturedSpan]:
        span = _CapturedSpan(attributes or {})
        spans.append(span)
        yield span

    monkeypatch.setattr(runner, "start_span", _fake_start_span)
    return spans


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


def test_run_agent_stream_bounds_stalled_generator_by_coordinator_deadline(
    monkeypatch: Any,
) -> None:
    """B1 (streaming): a stalled stream must not exceed the coordinator deadline.

    ``_StallingAgent``'s stream never yields a first update (it sleeps for
    10s). With a 0.05s timeout, ``run_agent_stream`` must still terminate
    with a timeout error in well under 10s — proving each wait for the
    *next* update is itself bounded by the remaining deadline, not just
    the gap between updates that have already arrived.
    """
    spans = _install_start_span_capture(monkeypatch)

    async def fake_build_agent_session_history(
        **_kwargs: Any,
    ) -> tuple[_StallingAgent, object, str, None]:
        return _StallingAgent(), object(), "test-session", None

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build_agent_session_history)

    async def collect() -> list[str]:
        return [
            chunk async for chunk in runner.run_agent_stream("prompt", timeout=0.05)
        ]

    started = time.monotonic()
    events = _events_from_sse(asyncio.run(collect()))
    elapsed = time.monotonic() - started

    # Generous relative to the 0.05s timeout, but far tighter than the 10s
    # the stalled generator would otherwise force us to wait.
    assert elapsed < 5.0

    error_events = [event for event in events if event["type"] == "error"]
    assert error_events == [{"type": "error", "content": "Timeout after 0.05s"}]

    [span] = spans
    assert span.attributes["af.agent.outcome"] == "error"
    assert any(isinstance(exc, TimeoutError) for exc in span.exceptions)


def test_run_agent_bounds_lock_wait_by_coordinator_deadline(monkeypatch: Any) -> None:
    """M1 (non-streaming): the coordinator deadline must also bound the wait
    for the per-session lock, not just the agent-run call after it.

    Before the fix, ``run_agent`` computed ``coordinator_deadline`` but the
    lock-acquire itself had no bound, and the subsequent ``agent.run(...)``
    reused the FULL original ``timeout`` again (not the remaining budget
    after the lock wait) — so a long lock wait let total wall-clock exceed
    ``timeout``, and once the real ``coordinator_deadline`` had already
    passed, the run kept going on a fresh/unbounded budget instead of
    aborting the whole turn (FRD 0006 §4.6). Simulate lock contention the
    same way a concurrent turn on the same ``session_id`` would: acquire
    the session's lock *before* calling ``run_agent``.
    """
    resolved_id = "test-m1-lock-contention-non-streaming"

    async def fake_build_agent_session_history(
        **_kwargs: Any,
    ) -> tuple[_Agent, object, str, None]:
        return _Agent(), object(), resolved_id, None

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build_agent_session_history)

    async def scenario() -> BaseException | None:
        lock = await runner._get_session_lock(resolved_id)
        await lock.acquire()
        try:
            await runner.run_agent("prompt", timeout=0.05, session_id=resolved_id)
        except BaseException as exc:  # captured for assertion below, not swallowed silently
            return exc
        finally:
            lock.release()
        return None

    started = time.monotonic()
    exc = asyncio.run(scenario())
    elapsed = time.monotonic() - started

    # Generous relative to the 0.05s timeout, but proves the run didn't
    # silently continue on a fresh budget once the deadline had passed.
    assert elapsed < 5.0
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "Agent run timed out after 0.05s"


def test_run_agent_stream_bounds_lock_wait_by_coordinator_deadline(monkeypatch: Any) -> None:
    """M1 (streaming): mirrors the non-streaming case above for
    ``run_agent_stream`` — the lock-acquire before the per-update streaming
    loop must also be bounded by the same absolute ``deadline``, emitting
    the same ``error`` SSE event shape the existing per-update-timeout
    branch already emits rather than continuing on a fresh budget.
    """
    spans = _install_start_span_capture(monkeypatch)
    resolved_id = "test-m1-lock-contention-streaming"

    async def fake_build_agent_session_history(
        **_kwargs: Any,
    ) -> tuple[_Agent, object, str, None]:
        return _Agent(), object(), resolved_id, None

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build_agent_session_history)

    async def scenario() -> list[str]:
        lock = await runner._get_session_lock(resolved_id)
        await lock.acquire()
        try:
            return [
                chunk
                async for chunk in runner.run_agent_stream(
                    "prompt", timeout=0.05, session_id=resolved_id
                )
            ]
        finally:
            lock.release()

    started = time.monotonic()
    events = _events_from_sse(asyncio.run(scenario()))
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert events == [
        {"type": "session", "session_id": resolved_id},
        {"type": "error", "content": "Timeout after 0.05s"},
    ]

    [span] = spans
    assert span.attributes["af.agent.outcome"] == "error"
    assert any(isinstance(exc, TimeoutError) for exc in span.exceptions)


def test_run_agent_stream_reports_delegate_error_count_on_span(monkeypatch: Any) -> None:
    """B3 (streaming): a recoverable delegate failure must land on the run's own span.

    The non-streaming path surfaces ``delegate_error_count`` via
    ``AgentResult`` (consumed by ``_set_run_result_attributes``). Streaming
    has no equivalent result object, so ``run_agent_stream`` must apply the
    shared ``_DelegateErrorTracker``'s final count directly to its own
    span's ``af.agent.tool_error_count`` once the stream completes.
    """
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY", raising=False)
    spans = _install_start_span_capture(monkeypatch)

    tracker = runner._DelegateErrorTracker()
    tracker.record_error()
    tracker.record_error()

    async def fake_build_agent_session_history(
        **_kwargs: Any,
    ) -> tuple[_Agent, object, str, runner._DelegateErrorTracker]:
        return _Agent(), object(), "test-session", tracker

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build_agent_session_history)

    async def collect() -> list[str]:
        return [chunk async for chunk in runner.run_agent_stream("prompt")]

    events = _events_from_sse(asyncio.run(collect()))
    assert any(event["type"] == "done" for event in events)

    [span] = spans
    assert span.attributes["af.agent.outcome"] == "success"
    assert span.attributes["af.agent.tool_error_count"] == 2


def test_run_agent_stream_reports_zero_tool_errors_without_delegation(monkeypatch: Any) -> None:
    """B3 (streaming): a run with no delegate tracker still reports a zero count.

    ``delegate_error_tracker`` is ``None`` whenever a run has no
    ``subagents``/``catalog`` configured at all — the ``finally`` block
    must still set ``af.agent.tool_error_count`` to 0 in that case rather
    than leaving the attribute unset.
    """
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY", raising=False)
    spans = _install_start_span_capture(monkeypatch)

    async def fake_build_agent_session_history(
        **_kwargs: Any,
    ) -> tuple[_Agent, object, str, None]:
        return _Agent(), object(), "test-session", None

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build_agent_session_history)

    async def collect() -> list[str]:
        return [chunk async for chunk in runner.run_agent_stream("prompt")]

    asyncio.run(collect())

    [span] = spans
    assert span.attributes["af.agent.tool_error_count"] == 0


def test_run_agent_stream_counts_ordinary_tool_errors_without_delegation(
    monkeypatch: Any,
) -> None:
    """M3 (streaming): a failed *ordinary* (non-delegate) tool call must
    still be reflected in ``af.agent.tool_error_count`` even when there is
    no delegate tracker at all (no ``subagents``/``catalog`` configured).

    Before the fix, the streaming path's final count came ONLY from
    ``delegate_error_tracker.count``, so a genuinely-failed sandbox/
    web_request tool call with zero delegate failures reported a count of
    0 — silently wrong telemetry. ``_ToolErrorAgent`` yields a
    ``function_result`` whose ``result`` carries the same JSON error
    envelope (``{"error": ...}``) ``_looks_like_tool_error`` recognizes.
    """
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY", raising=False)
    spans = _install_start_span_capture(monkeypatch)

    async def fake_build_agent_session_history(
        **_kwargs: Any,
    ) -> tuple[_ToolErrorAgent, object, str, None]:
        return _ToolErrorAgent(), object(), "test-session", None

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build_agent_session_history)

    async def collect() -> list[str]:
        return [chunk async for chunk in runner.run_agent_stream("prompt")]

    events = _events_from_sse(asyncio.run(collect()))
    assert any(event["type"] == "done" for event in events)

    [span] = spans
    assert span.attributes["af.agent.tool_error_count"] >= 1


def test_run_agent_stream_sums_ordinary_and_delegate_tool_errors(monkeypatch: Any) -> None:
    """M3 (mixed scenario): ordinary tool-call failures and delegate
    failures come from independent sources and must both be counted —
    summed, not one overwriting the other. There's no double-counting risk
    between the two: a specialist's sanitized delegate-failure text is
    never valid JSON, so ``_looks_like_tool_error`` can never also match a
    delegate failure.
    """
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY", raising=False)
    spans = _install_start_span_capture(monkeypatch)

    tracker = runner._DelegateErrorTracker()
    tracker.record_error()  # one delegate failure, independent of the tool error below

    async def fake_build_agent_session_history(
        **_kwargs: Any,
    ) -> tuple[_ToolErrorAgent, object, str, runner._DelegateErrorTracker]:
        return _ToolErrorAgent(), object(), "test-session", tracker

    monkeypatch.setattr(runner, "_build_agent_session_history", fake_build_agent_session_history)

    async def collect() -> list[str]:
        return [chunk async for chunk in runner.run_agent_stream("prompt")]

    asyncio.run(collect())

    [span] = spans
    # 1 delegate failure + 1 ordinary tool-call failure from `_ToolErrorAgent`.
    assert span.attributes["af.agent.tool_error_count"] == 2


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
