from __future__ import annotations

import json
import multiprocessing
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from azure_functions_agents.execution import run_control
from azure_functions_agents.execution.backend import RunError, RunResult
from azure_functions_agents.harness import journal_writer
from azure_functions_agents.harness.atomic_commit import AtomicCommitStore
from azure_functions_agents.harness.journal_writer import (
    HarnessJournalError,
    HarnessRunEnvelope,
    JournalWriter,
    acquire_run_lock,
)


def _envelope_payload() -> bytes:
    return json.dumps(
        {
            "run_id": "a" * 32,
            "session_id": "session-1",
            "agent_name": "agent",
            "prompt": "hello",
            "timeout": 30.0,
        }
    ).encode("utf-8")


def test_writer_documents_round_trip_through_controller_parsers(tmp_path: Path) -> None:
    envelope = HarnessRunEnvelope.parse(_envelope_payload())
    writer = JournalWriter(envelope, journal_root=tmp_path)

    writer.write_process_group(123)
    writer.write_accepted()
    writer.write_running()
    writer.append_event("session", {"session_id": envelope.session_id})
    writer.write_success(
        RunResult(
            content="complete",
            content_intermediate=[],
            tool_calls=[],
            reasoning=None,
            delegate_error_count=0,
        )
    )

    status = run_control._parse_status((writer.run_directory / "status.json").read_bytes())
    result = run_control._parse_result((writer.run_directory / "result.json").read_bytes())
    events = run_control._parse_event_lines(
        (writer.run_directory / "events-0000.jsonl").read_bytes()
    )

    assert status.state == "succeeded"
    assert status.result_available
    assert result.content == "complete"
    assert [event.sequence for event in events] == [1, 2]
    assert [event.type for event in events] == ["session", "done"]


def test_writer_keeps_existing_terminal_run_immutable(tmp_path: Path) -> None:
    envelope = HarnessRunEnvelope.parse(_envelope_payload())
    writer = JournalWriter(envelope, journal_root=tmp_path)
    writer.write_accepted()
    writer.write_failure(
        "failed",
        RunError(code="failed", message="failed", fault_domain="harness"),
    )
    before = (writer.run_directory / "status.json").read_bytes()

    duplicate = JournalWriter(envelope, journal_root=tmp_path)

    assert duplicate.terminal_exists()
    assert (duplicate.run_directory / "status.json").read_bytes() == before


def test_writer_rejects_noncontiguous_existing_event_history(tmp_path: Path) -> None:
    envelope = HarnessRunEnvelope.parse(_envelope_payload())
    run_directory = tmp_path / "runs" / envelope.run_id
    run_directory.mkdir(parents=True)
    (run_directory / "events-0000.jsonl").write_text(
        '\n'.join(
            (
                '{"sequence":1,"type":"delta","data":{},"timestamp":"now"}',
                '{"sequence":3,"type":"delta","data":{},"timestamp":"now"}',
            )
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HarnessJournalError, match="contiguous"):
        JournalWriter(envelope, journal_root=tmp_path)


def test_writer_rejects_oversized_event_before_sequence_or_segment_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:
            return datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(journal_writer, "datetime", FixedDatetime)
    data = {"content": "x" * 32}
    encoded = (
        json.dumps(
            {
                "sequence": 1,
                "type": "delta",
                "data": data,
                "timestamp": "2026-08-10T12:00:00Z",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    envelope = HarnessRunEnvelope.parse(_envelope_payload())

    monkeypatch.setattr(journal_writer, "MAX_EVENT_SEGMENT_BYTES", len(encoded))
    boundary = JournalWriter(envelope, journal_root=tmp_path / "boundary")
    boundary.append_event("delta", data)

    assert (boundary.run_directory / "events-0000.jsonl").read_bytes() == encoded
    assert boundary._sequence == 1

    monkeypatch.setattr(journal_writer, "MAX_EVENT_SEGMENT_BYTES", len(encoded) - 1)
    overflow = JournalWriter(envelope, journal_root=tmp_path / "overflow")
    with pytest.raises(HarnessJournalError, match="event exceeds"):
        overflow.append_event("delta", data)

    assert overflow._sequence == 0
    assert not tuple(overflow.run_directory.glob("events-*.jsonl"))


def _lock_worker(
    run_directory: Path,
    checkpoint_root: Path,
    marker_path: Path,
    barrier,
) -> None:
    barrier.wait()
    lock = acquire_run_lock(run_directory)
    if lock is None:
        return
    try:
        with marker_path.open("a", encoding="utf-8") as marker:
            marker.write(f"{os.getpid()}\n")
            marker.flush()
            os.fsync(marker.fileno())
        time.sleep(0.5)
        AtomicCommitStore(checkpoint_root).commit(conversation=b"state", working_files={})
    finally:
        lock.release()


def _crash_lock_holder(run_directory: Path, marker_path: Path) -> None:
    lock = acquire_run_lock(run_directory)
    if lock is None:
        os._exit(1)
    marker_path.write_text("held", encoding="utf-8")
    os._exit(0)


@pytest.mark.skipif(os.name != "posix", reason="run locks use the Linux harness process contract")
def test_concurrent_processes_hold_one_lock_and_later_retry_acquires(tmp_path: Path) -> None:
    run_directory = tmp_path / "runs" / ("b" * 32)
    checkpoint_root = tmp_path / "session"
    marker_path = tmp_path / "executions.txt"
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    workers = [
        context.Process(
            target=_lock_worker,
            args=(run_directory, checkpoint_root, marker_path, barrier),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join()
            raise AssertionError("Concurrent claim worker did not exit.")
        assert worker.exitcode == 0

    assert marker_path.read_text(encoding="utf-8").count("\n") == 1
    checkpoint = AtomicCommitStore(checkpoint_root).recover()
    assert checkpoint is not None
    assert (checkpoint.path / "conversation.json").read_bytes() == b"state"

    crash_marker = tmp_path / "crash-lock-held.txt"
    crashing_holder = context.Process(
        target=_crash_lock_holder,
        args=(run_directory, crash_marker),
    )
    crashing_holder.start()
    crashing_holder.join(10)
    assert crashing_holder.exitcode == 0
    assert crash_marker.read_text(encoding="utf-8") == "held"

    retry = acquire_run_lock(run_directory)
    assert retry is not None
    retry.release()
    assert (run_directory / ".run.lock").exists()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"run_id":"' + (b"a" * 32) + b'","run_id":"duplicate"}',
        b'{"run_id":"../unsafe","session_id":"session","agent_name":"agent","prompt":"x","timeout":1}',
    ],
)
def test_inbox_parser_rejects_unsafe_documents(payload: bytes) -> None:
    with pytest.raises(HarnessJournalError):
        HarnessRunEnvelope.parse(payload)
