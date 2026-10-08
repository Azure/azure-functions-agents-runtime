from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from azure_functions_agents.streaming_events import (
    AgentStreamEvent,
    AgentStreamEventKind,
    HostedSkillEvent,
    HostedSkillEventKind,
    agent_event_to_sse,
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
            HostedSkillEvent(HostedSkillEventKind.MESSAGE, content="complete"),
            'data: {"type": "message", "content": "complete"}\n\n',
        ),
        (
            HostedSkillEvent(HostedSkillEventKind.INTERMEDIATE, content="thinking"),
            'data: {"type": "intermediate", "content": "thinking"}\n\n',
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
            HostedSkillEvent(
                HostedSkillEventKind.TOOL_START,
                tool_call_id="call-two",
                tool_name="lookup",
            ),
            'data: {"type": "tool_start", "tool_call_id": "call-two", '
            '"tool_name": "lookup", "arguments": null}\n\n',
        ),
        (
            HostedSkillEvent(
                HostedSkillEventKind.TOOL_END,
                tool_call_id="call-one",
                tool_name="lookup",
                result=complex(1, 2),
            ),
            'data: {"type": "tool_end", "tool_call_id": "call-one", '
            '"tool_name": "lookup", "result": "(1+2j)"}\n\n',
        ),
        (
            HostedSkillEvent(HostedSkillEventKind.ERROR, content="\u00e9chec"),
            'data: {"type": "error", "content": "\\u00e9chec"}\n\n',
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
    assert agent_event_to_sse(event) == expected


def test_hosted_skill_event_names_are_runtime_aliases() -> None:
    assert HostedSkillEvent is AgentStreamEvent
    assert HostedSkillEventKind is AgentStreamEventKind


def test_event_is_frozen() -> None:
    event = HostedSkillEvent(HostedSkillEventKind.DONE)

    with pytest.raises(FrozenInstanceError):
        event.content = "changed"  # type: ignore[misc]