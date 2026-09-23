"""FRD 0009 C0: characterization tests locking `runner.py`'s current timeout
precedence and result/stream semantics.

No product changes. These exist as a regression baseline before the C1
dependency bump (Durable b2->b3, `durabletask`, MAF trio) — see
docs/frds/0009-durable-agent-loop.md §4.14 (C0) and its test-plan §6 "Model/
replay" row ("whole-run timeout across replay/retry/human wait").

Scope deliberately excludes what's already covered elsewhere:

* Per-agent/global/env timeout *resolution* precedence
  (``config/merge.py:_resolve_timeout``) is locked by
  ``test_config_merge.py::test_resolve_timeout_precedence`` already.
* Delegate specialist timeout / "effective timeout = min(specialist,
  coordinator remaining)" (FRD 0007 Decision #12) is exhaustively covered by
  ``test_runner_delegation.py``.
* Coordinator-deadline bounding of the per-session lock wait and of a
  stalled/never-yielding stream is covered by
  ``test_runner_streaming.py``'s M1/B1/B2a/B2b regression tests.

What's missing and locked here instead:

* ``run_agent``/``run_agent_stream``'s own ``timeout=None -> DEFAULT_TIMEOUT``
  fallback (the module-level default, not the config-merge default) — no
  existing test exercises this path at all.
* ``run_agent``'s own ``asyncio.wait_for(agent.run(...), ...)`` timeout
  branch when the *model call itself* (not lock contention) exceeds the
  budget — the existing M1 regression only covers the lock-contention
  variant of the same error message.
* The current ``AgentResult`` and SSE event-vocabulary shapes for a plain
  happy-path run — locking today's exact fields/event ordering before any
  dependency bump could silently change them.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from azure_functions_agents import runner
from azure_functions_agents.client_manager import InferenceTarget


class _FakeAgent:
    """Mirrors ``test_runner_harness.py``'s ``_FakeAgent`` fixture."""

    def __init__(self, response_text: str = "hello") -> None:
        self._response_text = response_text

    async def run(self, _prompt: str, *, session: Any, options: Any = None) -> Any:
        return _FakeResponse(self._response_text)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.messages: list[Any] = []


class _StallingAgent:
    """A fake agent whose ``run()`` coroutine never completes — used to
    trigger ``run_agent``'s ``asyncio.wait_for(agent.run(...), ...)`` timeout
    branch itself, as opposed to lock contention."""

    async def run(self, _prompt: str, *, session: Any, options: Any = None) -> Any:
        await asyncio.sleep(10.0)
        raise AssertionError("unreachable: bounded by asyncio.wait_for's timeout")


async def _collect_stream(stream: AsyncIterator[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async for chunk in stream:
        assert chunk.startswith("data: ")
        events.append(json.loads(chunk.removeprefix("data: ").strip()))
    return events


# ---------------------------------------------------------------------------
# timeout=None falls back to the module-level DEFAULT_TIMEOUT.
# ---------------------------------------------------------------------------


def test_run_agent_timeout_none_uses_module_default_timeout_for_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "DEFAULT_TIMEOUT", 12_345.0)
    captured: list[dict[str, Any]] = []

    async def fake_build_agent_session(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        captured.append(kwargs)
        return _FakeAgent(), object(), "s", None, InferenceTarget()

    monkeypatch.setattr(runner, "_build_agent_session", fake_build_agent_session)

    loop_time_before = time.monotonic()

    result = asyncio.run(runner.run_agent("hello", timeout=None))

    assert len(captured) == 1
    coordinator_deadline = captured[0]["coordinator_deadline"]
    # `coordinator_deadline = loop.time() + timeout` — asserting it is
    # (loosely) ~12345s out from "now" proves `DEFAULT_TIMEOUT`, not the
    # hardcoded `900.0` fallback literal in `_runtime_timeout_default`'s own
    # module-load-time computation, drove this call's budget.
    assert coordinator_deadline > loop_time_before + 12_000.0
    assert result.content == "hello"


def test_run_agent_stream_timeout_none_uses_module_default_timeout_for_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "DEFAULT_TIMEOUT", 6_789.0)
    captured: list[dict[str, Any]] = []

    async def fake_build_agent_session(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        captured.append(kwargs)
        return _FakeAgent(), object(), "s", None, InferenceTarget()

    monkeypatch.setattr(runner, "_build_agent_session", fake_build_agent_session)

    loop_time_before = time.monotonic()

    events = asyncio.run(_collect_stream(runner.run_agent_stream("hello", timeout=None)))

    assert len(captured) == 1
    coordinator_deadline = captured[0]["coordinator_deadline"]
    assert coordinator_deadline > loop_time_before + 6_500.0
    assert events[0] == {"type": "session", "session_id": "s"}


# ---------------------------------------------------------------------------
# run_agent's own asyncio.wait_for(agent.run(...), ...) timeout branch
# (as opposed to the already-covered lock-contention variant).
# ---------------------------------------------------------------------------


def test_run_agent_raises_runtime_error_reporting_timeout_when_the_model_call_itself_is_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_build_agent_session(
        **_kwargs: Any,
    ) -> tuple[_StallingAgent, object, str, None, InferenceTarget]:
        return _StallingAgent(), object(), "slow-session", None, InferenceTarget()

    monkeypatch.setattr(runner, "_build_agent_session", fake_build_agent_session)

    with pytest.raises(RuntimeError, match=r"Agent run timed out after 0\.05s"):
        asyncio.run(runner.run_agent("hello", timeout=0.05, session_id="slow-session"))


# ---------------------------------------------------------------------------
# Current AgentResult semantics for a plain (no tool calls) happy-path run.
# ---------------------------------------------------------------------------


def test_run_agent_result_leaves_intermediate_reasoning_and_events_fields_at_their_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AgentResult`` declares ``content_intermediate``/``reasoning``/``events``
    fields, but ``run_agent`` never populates them today — only
    ``session_id``/``content``/``tool_calls``/``delegate_error_count`` are
    set from the actual response. Locking this so a future MAF/Durable change
    that starts (or stops) populating them is a deliberate, reviewed
    decision rather than an accidental side effect of the C1 dependency
    bump.
    """

    async def fake_build_agent_session(
        **_kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        return _FakeAgent("plain text reply"), object(), "plain-session", None, InferenceTarget()

    monkeypatch.setattr(runner, "_build_agent_session", fake_build_agent_session)

    result = asyncio.run(runner.run_agent("hello"))

    assert result.session_id == "plain-session"
    assert result.content == "plain text reply"
    assert result.tool_calls == []
    assert result.delegate_error_count == 0
    assert result.content_intermediate == []
    assert result.reasoning is None
    assert result.events == []


# ---------------------------------------------------------------------------
# Current SSE event-vocabulary shape/ordering for a happy-path streamed run
# with one text token, one reasoning token, and one tool call.
# ---------------------------------------------------------------------------


class _Content:
    def __init__(self, type: str, **kwargs: Any) -> None:
        self.type = type
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Update:
    def __init__(self, contents: list[_Content]) -> None:
        self.contents = contents


class _FullVocabularyAgent:
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
        yield _Update([_Content("text_reasoning", text="thinking...")])
        yield _Update([_Content("text", text="hello ")])
        yield _Update([_Content("text", text="world")])
        yield _Update(
            [_Content("function_call", call_id="call_1", name="lookup", arguments="{}")]
        )
        yield _Update([_Content("function_result", call_id="call_1", result="42")])


def test_run_agent_stream_happy_path_emits_the_locked_event_vocabulary_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_build_agent_session(
        **_kwargs: Any,
    ) -> tuple[_FullVocabularyAgent, object, str, None, InferenceTarget]:
        return _FullVocabularyAgent(), object(), "vocab-session", None, InferenceTarget()

    monkeypatch.setattr(runner, "_build_agent_session", fake_build_agent_session)

    events = asyncio.run(_collect_stream(runner.run_agent_stream("hello")))

    assert events == [
        {"type": "session", "session_id": "vocab-session"},
        {"type": "intermediate", "content": "thinking..."},
        {"type": "delta", "content": "hello "},
        {"type": "delta", "content": "world"},
        {
            "type": "tool_start",
            "tool_call_id": "call_1",
            "tool_name": "lookup",
            "arguments": "{}",
        },
        {
            "type": "tool_end",
            "tool_call_id": "call_1",
            "tool_name": None,
            "result": "42",
        },
        {"type": "done"},
    ]
    # The docstring's event vocabulary also lists a "message" event ("full
    # assistant message; ... emitted when MAF returns a non-streaming text
    # item mid-stream"), but no code path in `run_agent_stream` currently
    # emits it — pre-existing doc/code drift, out of scope for this
    # characterization-only slice. Locking its absence here so a change
    # that starts emitting it is a deliberate decision, not a silent
    # side effect.
    assert not any(event["type"] == "message" for event in events)
