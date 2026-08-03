from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from azure_functions_agents.execution.backend import EventCursorExpiredError, RunContext
from azure_functions_agents.execution.run_control import (
    RUNS_PATH,
    RunControlError,
    RunEnvelope,
    SandboxRunControl,
)
from tests.doubles.fake_sandbox_transport import FakeSandboxTransport


def _context() -> RunContext:
    return RunContext(run_id="run-1", session_id="session-1")


def _status(
    *,
    state: str = "accepted",
    last_sequence: int = 0,
    result_available: bool = False,
) -> bytes:
    return json.dumps(
        {
            "run_id": "run-1",
            "session_id": "session-1",
            "state": state,
            "last_sequence": last_sequence,
            "result_available": result_available,
            "error": None,
        }
    ).encode("utf-8")


def _event(sequence: int, event_type: str) -> str:
    return json.dumps(
        {
            "sequence": sequence,
            "type": event_type,
            "data": {"sequence": sequence},
            "timestamp": datetime(2026, 8, 3, tzinfo=UTC).isoformat(),
        }
    )


def _envelope() -> RunEnvelope:
    return RunEnvelope.create(
        run_id="run-1",
        session_id="session-1",
        agent_name="main",
        prompt="hello",
        timeout=60.0,
    )


@pytest.mark.asyncio
async def test_submit_waits_for_the_harness_to_journal_acceptance() -> None:
    transport = FakeSandboxTransport()
    control = SandboxRunControl(event_poll_interval_seconds=0.001)

    async def accept_after_launch(command: str) -> None:
        assert "setsid nohup" in command
        transport.seed_file(f"{RUNS_PATH}/run-1/status.json", _status())

    transport.exec_hook = accept_after_launch
    status = await control.submit(transport, "run-1", _envelope(), timeout_seconds=1.0)

    assert status.state == "accepted"
    assert [call.operation for call in transport.calls] == [
        "read_file",
        "write_file",
        "exec",
        "read_file",
    ]
    inbox = json.loads(await transport.read_file("/var/lib/azure-functions-agents/inbox/run-1.json"))
    assert inbox == {
        "agent_name": "main",
        "prompt": "hello",
        "run_id": "run-1",
        "session_id": "session-1",
        "timeout": 60.0,
    }


@pytest.mark.asyncio
async def test_submit_reuses_existing_run_status_without_a_second_launch() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(f"{RUNS_PATH}/run-1/status.json", _status(state="running"))

    async def unexpected_launch(_command: str) -> None:
        raise AssertionError("duplicate run must not launch a second harness process")

    transport.exec_hook = unexpected_launch
    status = await SandboxRunControl().submit(
        transport,
        "run-1",
        _envelope(),
        timeout_seconds=1.0,
    )

    assert status.state == "running"
    assert [call.operation for call in transport.calls] == ["read_file"]


@pytest.mark.asyncio
async def test_get_status_reads_terminal_result_when_available() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="succeeded", last_sequence=4, result_available=True),
    )
    transport.seed_file(
        f"{RUNS_PATH}/run-1/result.json",
        json.dumps(
            {
                "content": "answer",
                "content_intermediate": ["partial"],
                "tool_calls": [{"name": "lookup"}],
                "reasoning": "because",
                "delegate_error_count": 2,
            }
        ).encode("utf-8"),
    )

    status = await SandboxRunControl().get_status(transport, _context())

    assert status.state == "succeeded"
    assert status.result_available is True
    assert status.result is not None
    assert status.result.content == "answer"
    assert status.result.delegate_error_count == 2


@pytest.mark.asyncio
async def test_get_status_allows_evicted_success_result() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="succeeded", last_sequence=4, result_available=False),
    )

    status = await SandboxRunControl().get_status(transport, _context())

    assert status.state == "succeeded"
    assert status.result_available is False
    assert status.result is None


@pytest.mark.asyncio
async def test_read_events_enforces_exclusive_cursor_and_eviction_rules() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="succeeded", last_sequence=5),
    )
    transport.seed_file(
        f"{RUNS_PATH}/run-1/events.jsonl",
        ("\n".join([_event(3, "delta"), _event(4, "delta"), _event(5, "done")]) + "\n").encode(
            "utf-8"
        ),
    )
    control = SandboxRunControl()

    all_events = [event async for event in control.read_events(transport, _context(), 0)]
    resumed = [event async for event in control.read_events(transport, _context(), 3)]
    final = [event async for event in control.read_events(transport, _context(), 5)]

    assert [event.sequence for event in all_events] == [3, 4, 5]
    assert [event.sequence for event in resumed] == [4, 5]
    assert final == []
    with pytest.raises(EventCursorExpiredError):
        _ = [event async for event in control.read_events(transport, _context(), 1)]


@pytest.mark.asyncio
async def test_cancel_signals_the_recorded_process_group_and_waits_for_journal() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(f"{RUNS_PATH}/run-1/status.json", _status(state="running"))
    transport.seed_file(f"{RUNS_PATH}/run-1/process.json", b'{"process_group_id":42}')

    async def journal_canceled(command: str) -> None:
        assert command == "kill -TERM -- -42"
        transport.seed_file(f"{RUNS_PATH}/run-1/status.json", _status(state="canceled"))

    transport.exec_hook = journal_canceled
    status = await SandboxRunControl(event_poll_interval_seconds=0.001).cancel(transport, _context())

    assert status.state == "canceled"
    assert [call.operation for call in transport.calls][-2:] == ["exec", "read_file"]


@pytest.mark.asyncio
async def test_invalid_journal_document_fails_closed_without_echoing_payload() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        b'{"run_id":"run-1","run_id":"forged"}',
    )

    with pytest.raises(RunControlError, match="status document is invalid"):
        await SandboxRunControl().get_status(transport, _context())
