from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from azure_functions_agents.streaming_events import (
    HostedSkillEvent,
    HostedSkillEventKind,
)


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            HostedSkillEvent(HostedSkillEventKind.SESSION, session_id="session-one"),
            'data: {"type": "session", "session_id": "session-one"}\n\n',
        ),
        (
            HostedSkillEvent(HostedSkillEventKind.DELTA, content="hello"),
            'data: {"type": "delta", "content": "hello"}\n\n',
        ),
        (
            HostedSkillEvent(
                HostedSkillEventKind.TOOL_START,
                tool_call_id="call-one",
                tool_name="lookup",
                arguments='{"id": 1}',
            ),
            'data: {"type": "tool_start", "tool_call_id": "call-one", '
            '"tool_name": "lookup", "arguments": "{\\"id\\": 1}"}\n\n',
        ),
        (
            HostedSkillEvent(HostedSkillEventKind.DONE),
            'data: {"type": "done"}\n\n',
        ),
    ],
)
def test_event_serializes_to_existing_sse_shape(
    event: HostedSkillEvent,
    expected: str,
) -> None:
    assert event.to_sse() == expected


def test_event_is_frozen() -> None:
    event = HostedSkillEvent(HostedSkillEventKind.DONE)

    with pytest.raises(FrozenInstanceError):
        event.content = "changed"  # type: ignore[misc]