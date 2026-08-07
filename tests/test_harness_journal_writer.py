from __future__ import annotations

import json
from pathlib import Path

import pytest

from azure_functions_agents.execution import run_control
from azure_functions_agents.execution.backend import RunError, RunResult
from azure_functions_agents.harness.journal_writer import (
    HarnessJournalError,
    HarnessRunEnvelope,
    JournalWriter,
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
