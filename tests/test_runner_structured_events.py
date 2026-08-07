from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import azure_functions_agents.runner as runner


@pytest.mark.asyncio
async def test_structured_event_seam_is_the_shared_event_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_events(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        yield {"type": "session", "session_id": "session-1"}
        yield {"type": "delta", "content": "hello"}

    monkeypatch.setattr(runner, "_run_agent_event_stream", fake_events)

    events = [event async for event in runner.run_agent_events("prompt")]

    assert events == [
        {"type": "session", "session_id": "session-1"},
        {"type": "delta", "content": "hello"},
    ]


@pytest.mark.asyncio
async def test_sse_adapter_renders_the_shared_structured_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_events(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        yield {"type": "delta", "content": "hello"}

    monkeypatch.setattr(runner, "run_agent_events", fake_events)

    frames = [frame async for frame in runner.run_agent_stream("prompt")]

    assert frames == [f"data: {json.dumps({'type': 'delta', 'content': 'hello'})}\n\n"]


def test_private_sandbox_history_directory_overrides_local_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_directory = tmp_path / "run" / "history"
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(history_directory))

    assert runner._resolve_sessions_dir() == history_directory.resolve()
