from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from azure_functions_agents.execution.backend import EventCursorExpiredError, RunContext
from azure_functions_agents.execution.run_control import (
    MAX_EVENT_SEGMENT_BYTES,
    MAX_EVENT_SEGMENTS,
    MAX_RESULT_BYTES,
    MAX_STATUS_BYTES,
    RUNS_PATH,
    RunControlError,
    RunEnvelope,
    RunJournalProtocolError,
    RunSubmissionDefinitiveFailureError,
    RunSubmissionIndeterminateError,
    SandboxRunControl,
)
from azure_functions_agents.transport.transport_models import SandboxExecResult
from tests.doubles.fake_sandbox_transport import FakeSandboxTransport


def _context() -> RunContext:
    return RunContext(run_id="run-1", session_id="session-1")


def _status(
    *,
    state: str = "accepted",
    last_sequence: int = 0,
    result_available: bool | None = None,
) -> bytes:
    resolved_result_available = state == "succeeded" if result_available is None else result_available
    return json.dumps(
        {
            "run_id": "run-1",
            "session_id": "session-1",
            "state": state,
            "last_sequence": last_sequence,
            "result_available": resolved_result_available,
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
async def test_submit_keeps_launch_indeterminate_when_acceptance_times_out() -> None:
    transport = FakeSandboxTransport()
    control = SandboxRunControl(event_poll_interval_seconds=0.001)

    with pytest.raises(RunSubmissionIndeterminateError):
        await control.submit(transport, "run-1", _envelope(), timeout_seconds=0.05)

    assert [call.operation for call in transport.calls[:3]] == [
        "read_file",
        "write_file",
        "exec",
    ]


@pytest.mark.asyncio
async def test_submit_marks_nonzero_launch_exit_as_definitive_failure() -> None:
    transport = FakeSandboxTransport()
    transport.next_exec_result = SandboxExecResult(exit_code=1, stdout="", stderr="")

    with pytest.raises(RunSubmissionDefinitiveFailureError):
        await SandboxRunControl().submit(transport, "run-1", _envelope(), timeout_seconds=1.0)

    assert [call.operation for call in transport.calls] == [
        "read_file",
        "write_file",
        "exec",
    ]


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
async def test_get_status_rejects_succeeded_status_without_a_result() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="succeeded", last_sequence=4, result_available=False),
    )

    with pytest.raises(RunJournalProtocolError, match="status document is invalid"):
        await SandboxRunControl().get_status(transport, _context())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        {
            "state": "succeeded",
            "result_available": True,
            "error": {"code": "bad", "message": "bad"},
        },
        {"state": "accepted", "result_available": True, "error": None},
        {
            "state": "running",
            "result_available": False,
            "error": {"code": "bad", "message": "bad"},
        },
        {
            "state": "canceled",
            "result_available": False,
            "error": {"code": "bad", "message": "bad"},
        },
        {"state": "failed", "result_available": False, "error": None},
        {"state": "timed_out", "result_available": False, "error": None},
        {"state": "abandoned", "result_available": False, "error": None},
    ],
)
async def test_get_status_rejects_invalid_terminal_status_combinations(
    status: dict[str, object],
) -> None:
    transport = FakeSandboxTransport()
    payload = {
        "run_id": "run-1",
        "session_id": "session-1",
        "last_sequence": 0,
        **status,
    }
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        json.dumps(payload).encode("utf-8"),
    )

    with pytest.raises(RunJournalProtocolError, match="status document is invalid"):
        await SandboxRunControl().get_status(transport, _context())


@pytest.mark.asyncio
async def test_get_status_treats_a_missing_advertised_result_as_protocol_corruption() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="succeeded", result_available=True),
    )

    with pytest.raises(RunJournalProtocolError, match="result is missing"):
        await SandboxRunControl().get_status(transport, _context())


@pytest.mark.asyncio
async def test_get_status_rejects_oversized_status_payload() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        b"x" * (MAX_STATUS_BYTES + 1),
    )

    with pytest.raises(RunJournalProtocolError, match="status exceeds its size limit"):
        await SandboxRunControl().get_status(transport, _context())


@pytest.mark.asyncio
async def test_get_status_rejects_oversized_result_payload() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="succeeded", result_available=True),
    )
    transport.seed_file(
        f"{RUNS_PATH}/run-1/result.json",
        b"x" * (MAX_RESULT_BYTES + 1),
    )

    with pytest.raises(RunJournalProtocolError, match="result exceeds its size limit"):
        await SandboxRunControl().get_status(transport, _context())


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
async def test_read_events_rejects_too_many_retained_segments() -> None:
    transport = FakeSandboxTransport()
    for sequence in range(1, MAX_EVENT_SEGMENTS + 2):
        transport.seed_file(
            f"{RUNS_PATH}/run-1/events.{sequence:02}.jsonl",
            _event(sequence, "delta").encode("utf-8") + b"\n",
        )

    with pytest.raises(RunJournalProtocolError, match="too many retained event segments"):
        _ = [event async for event in SandboxRunControl().read_events(transport, _context(), 0)]


@pytest.mark.asyncio
async def test_read_events_rejects_oversized_event_segment() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/events.jsonl",
        b"x" * (MAX_EVENT_SEGMENT_BYTES + 1),
    )

    with pytest.raises(RunJournalProtocolError, match="event segment exceeds its size limit"):
        _ = [event async for event in SandboxRunControl().read_events(transport, _context(), 0)]


@pytest.mark.asyncio
async def test_read_events_caches_completed_segments_between_polls() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="running", last_sequence=2),
    )
    completed_path = f"{RUNS_PATH}/run-1/events.000001.jsonl"
    active_path = f"{RUNS_PATH}/run-1/events.jsonl"
    transport.seed_file(completed_path, _event(1, "delta").encode("utf-8") + b"\n")
    transport.seed_file(active_path, _event(2, "delta").encode("utf-8") + b"\n")
    events = SandboxRunControl(event_poll_interval_seconds=0.001).read_events(
        transport,
        _context(),
        0,
    )

    assert (await anext(events)).sequence == 1
    assert (await anext(events)).sequence == 2

    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="succeeded", last_sequence=2),
    )

    with pytest.raises(StopAsyncIteration):
        await anext(events)

    reads = [call.path for call in transport.calls if call.operation == "read_file"]
    assert reads.count(completed_path) == 1
    assert reads.count(active_path) == 2


@pytest.mark.asyncio
async def test_read_events_rejects_a_persistent_gap_in_retained_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "azure_functions_agents.execution.run_control.JOURNAL_VISIBILITY_TIMEOUT_SECONDS",
        0.02,
    )
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="succeeded", last_sequence=3),
    )
    transport.seed_file(
        f"{RUNS_PATH}/run-1/events.jsonl",
        ("\n".join([_event(1, "delta"), _event(3, "done")]) + "\n").encode("utf-8"),
    )

    with pytest.raises(RunJournalProtocolError, match="event history is inconsistent"):
        _ = [
            event
            async for event in SandboxRunControl(
                event_poll_interval_seconds=0.001
            ).read_events(transport, _context(), 0)
        ]


@pytest.mark.asyncio
async def test_read_events_stops_at_the_first_terminal_event() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="succeeded", last_sequence=2),
    )
    transport.seed_file(
        f"{RUNS_PATH}/run-1/events.jsonl",
        ("\n".join([_event(1, "done"), _event(2, "delta")]) + "\n").encode("utf-8"),
    )

    events = [
        event async for event in SandboxRunControl().read_events(transport, _context(), 0)
    ]

    assert [event.sequence for event in events] == [1]


@pytest.mark.asyncio
async def test_read_events_waits_for_event_visibility_before_terminal_return() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="succeeded", last_sequence=2),
    )
    event_path = f"{RUNS_PATH}/run-1/events.jsonl"
    transport.seed_file(event_path, _event(1, "done").encode("utf-8") + b"\n")
    events = SandboxRunControl(event_poll_interval_seconds=0.001).read_events(
        transport,
        _context(),
        0,
    )

    pending_event = asyncio.create_task(anext(events))
    await asyncio.sleep(0.01)

    assert pending_event.done() is False

    transport.seed_file(
        event_path,
        ("\n".join([_event(1, "done"), _event(2, "delta")]) + "\n").encode("utf-8"),
    )

    assert (await pending_event).sequence == 1
    with pytest.raises(StopAsyncIteration):
        await anext(events)


@pytest.mark.asyncio
async def test_read_events_waits_for_terminal_status_after_done_event() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(f"{RUNS_PATH}/run-1/status.json", _status(state="running", last_sequence=1))
    transport.seed_file(
        f"{RUNS_PATH}/run-1/events.jsonl",
        _event(1, "done").encode("utf-8") + b"\n",
    )
    events = SandboxRunControl(event_poll_interval_seconds=0.001).read_events(
        transport,
        _context(),
        0,
    )

    first = await anext(events)
    pending_completion = asyncio.create_task(anext(events))
    await asyncio.sleep(0.01)

    assert first.type == "done"
    assert pending_completion.done() is False

    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        _status(state="succeeded", last_sequence=1),
    )
    with pytest.raises(StopAsyncIteration):
        await pending_completion


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
@pytest.mark.parametrize(
    "payload",
    [
        b'{"process_group_id":42,"process_group_id":99}',
        b'{"process_group_id":"42"}',
        b'{"process_group_id":0}',
    ],
)
async def test_process_group_reader_rejects_untrusted_journal_documents(payload: bytes) -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(f"{RUNS_PATH}/run-1/process.json", payload)

    with pytest.raises(RunJournalProtocolError):
        await SandboxRunControl().read_process_group_id(transport, _context())


@pytest.mark.asyncio
async def test_process_group_reader_rejects_oversized_journal_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "azure_functions_agents.execution.run_control.MAX_PROCESS_BYTES",
        1,
    )
    transport = FakeSandboxTransport()
    transport.seed_file(f"{RUNS_PATH}/run-1/process.json", b"{}")

    with pytest.raises(RunJournalProtocolError):
        await SandboxRunControl().read_process_group_id(transport, _context())


@pytest.mark.asyncio
async def test_invalid_journal_document_fails_closed_without_echoing_payload() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(
        f"{RUNS_PATH}/run-1/status.json",
        b'{"run_id":"run-1","run_id":"forged"}',
    )

    with pytest.raises(RunControlError, match="status document is invalid"):
        await SandboxRunControl().get_status(transport, _context())
