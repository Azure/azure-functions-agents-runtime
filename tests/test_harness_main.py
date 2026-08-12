from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from types import SimpleNamespace

import pytest

import azure_functions_agents.harness.__main__ as harness_main
import azure_functions_agents.harness.journal_writer as journal_writer
from azure_functions_agents.conformance.capability_map import validate_capability_coverage
from azure_functions_agents.conformance.trace import parse_trace
from azure_functions_agents.harness import SANDBOX_MARKER_ENV_VAR
from azure_functions_agents.harness.atomic_commit import AtomicCommitError, AtomicCommitStore
from azure_functions_agents.harness.journal_writer import HarnessJournalError, acquire_run_lock
from azure_functions_agents.harness.sandbox_capabilities import HARNESS_CAPABILITIES


class _TestRunLock:
    def release(self) -> None:
        return


@pytest.fixture(autouse=True)
def _mock_run_lock_outside_posix(monkeypatch: pytest.MonkeyPatch):
    if os.name != "posix":
        monkeypatch.setattr(harness_main, "acquire_run_lock", lambda _: _TestRunLock())
    yield


def test_python_module_entrypoint_exposes_the_harness_run_argument() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = {**os.environ, "PYTHONPATH": str(source_root)}

    result = subprocess.run(
        [sys.executable, "-m", "azure_functions_agents.harness", "--help"],
        capture_output=True,
        check=False,
        cwd=source_root.parent,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--run-id" in result.stdout


@pytest.mark.asyncio
async def test_per_run_entrypoint_writes_controller_readable_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "a" * 32
    journal_root = tmp_path / "journal"
    inbox = journal_root / "inbox" / f"{run_id}.json"
    inbox.parent.mkdir(parents=True)
    inbox.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "session_id": "session-1",
                "agent_name": "agent",
                "prompt": "hello",
                "timeout": 30.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")

    async def fake_events(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        yield {"type": "session", "session_id": "session-1"}
        yield {"type": "delta", "content": "hello"}
        yield {"type": "done", "delegate_error_count": 2}

    entry = SimpleNamespace(
        resolved=SimpleNamespace(
            instructions="instructions",
            model=None,
            subagents=[],
        ),
        capabilities=SimpleNamespace(
            filtered_user_tools=[],
            filtered_mcp_tools=[],
            enabled_skill_paths=[],
            web_request_tools=[],
        ),
    )
    monkeypatch.setattr(harness_main, "run_agent_events", fake_events)
    monkeypatch.setattr(harness_main, "rebuild_agent_catalog", lambda _: {"agent": entry})

    exit_code = await harness_main._run(run_id, journal_root, tmp_path / "app")

    run_directory = journal_root / "runs" / run_id
    status = json.loads((run_directory / "status.json").read_text(encoding="utf-8"))
    result = json.loads((run_directory / "result.json").read_text(encoding="utf-8"))
    trace = json.loads((run_directory / "conformance.trace.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert status["state"] == "succeeded"
    assert result["content"] == "hello"
    assert result["delegate_error_count"] == 2
    assert [event["type"] for event in trace["events"]] == ["session", "delta", "done"]
    assert set(trace["capabilities"]) == {
        "atomic_commit_v1",
        "watchdog_v1",
        "bootstrap_v1",
        "delegation_v1",
    }
    validate_capability_coverage(
        HARNESS_CAPABILITIES,
        (parse_trace((run_directory / "conformance.trace.json").read_bytes()),),
    )


@pytest.mark.asyncio
async def test_duplicate_terminal_launch_does_not_rewrite_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "b" * 32
    journal_root = tmp_path / "journal"
    run_directory = journal_root / "runs" / run_id
    run_directory.mkdir(parents=True)
    status = {
        "run_id": run_id,
        "session_id": "session-1",
        "state": "succeeded",
        "last_sequence": 0,
        "result_available": True,
        "error": None,
    }
    (run_directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
    inbox = journal_root / "inbox" / f"{run_id}.json"
    inbox.parent.mkdir(parents=True)
    inbox.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "session_id": "session-1",
                "agent_name": "agent",
                "prompt": "hello",
                "timeout": 30.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")
    before = (run_directory / "status.json").read_bytes()

    exit_code = await harness_main._run(run_id, journal_root, tmp_path / "app")

    assert exit_code == 0
    assert (run_directory / "status.json").read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="run locks use the Linux harness process contract")
async def test_active_run_lock_prevents_a_second_harness_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "e" * 32
    journal_root = tmp_path / "journal"
    inbox = journal_root / "inbox" / f"{run_id}.json"
    inbox.parent.mkdir(parents=True)
    inbox.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "session_id": "session-1",
                "agent_name": "agent",
                "prompt": "hello",
                "timeout": 30.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")
    lock = acquire_run_lock(journal_root / "runs" / run_id)
    assert lock is not None

    try:
        assert await harness_main._run(run_id, journal_root, tmp_path / "app") == 0
    finally:
        lock.release()

    assert not (journal_root / "runs" / run_id / "status.json").exists()


@pytest.mark.asyncio
async def test_existing_nonterminal_status_keeps_the_durable_run_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "h" * 32
    journal_root = tmp_path / "journal"
    run_directory = journal_root / "runs" / run_id
    run_directory.mkdir(parents=True)
    (run_directory / "status.json").write_text('{"state":"accepted"}\n', encoding="utf-8")
    inbox = journal_root / "inbox" / f"{run_id}.json"
    inbox.parent.mkdir(parents=True)
    inbox.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "session_id": "session-1",
                "agent_name": "agent",
                "prompt": "hello",
                "timeout": 30.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")
    before = (run_directory / "status.json").read_bytes()
    monkeypatch.setattr(
        harness_main,
        "rebuild_agent_catalog",
        lambda _: pytest.fail("durable run status must prevent re-execution"),
    )

    assert await harness_main._run(run_id, journal_root, tmp_path / "app") == 0
    assert (run_directory / "status.json").read_bytes() == before


@pytest.mark.asyncio
async def test_canceled_harness_run_publishes_canceled_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "c" * 32
    journal_root = tmp_path / "journal"
    inbox = journal_root / "inbox" / f"{run_id}.json"
    inbox.parent.mkdir(parents=True)
    inbox.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "session_id": "session-1",
                "agent_name": "agent",
                "prompt": "hello",
                "timeout": 30.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")

    async def blocking_events(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        yield {"type": "session", "session_id": "session-1"}
        await asyncio.Event().wait()

    entry = SimpleNamespace(
        resolved=SimpleNamespace(instructions="instructions", model=None, subagents=[]),
        capabilities=SimpleNamespace(
            filtered_user_tools=[],
            filtered_mcp_tools=[],
            enabled_skill_paths=[],
            web_request_tools=[],
        ),
    )
    monkeypatch.setattr(harness_main, "run_agent_events", blocking_events)
    monkeypatch.setattr(harness_main, "rebuild_agent_catalog", lambda _: {"agent": entry})

    task = asyncio.create_task(harness_main._run(run_id, journal_root, tmp_path / "app"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status = json.loads(
        (journal_root / "runs" / run_id / "status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "canceled"


@pytest.mark.asyncio
async def test_agent_error_is_preserved_as_a_typed_terminal_journal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "d" * 32
    journal_root = tmp_path / "journal"
    inbox = journal_root / "inbox" / f"{run_id}.json"
    inbox.parent.mkdir(parents=True)
    inbox.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "session_id": "session-1",
                "agent_name": "agent",
                "prompt": "hello",
                "timeout": 30.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")

    async def failed_events(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        yield {"type": "error", "content": "agent failure"}

    entry = SimpleNamespace(
        resolved=SimpleNamespace(instructions="instructions", model=None, subagents=[]),
        capabilities=SimpleNamespace(
            filtered_user_tools=[],
            filtered_mcp_tools=[],
            enabled_skill_paths=[],
            web_request_tools=[],
        ),
    )
    monkeypatch.setattr(harness_main, "run_agent_events", failed_events)
    monkeypatch.setattr(harness_main, "rebuild_agent_catalog", lambda _: {"agent": entry})

    assert await harness_main._run(run_id, journal_root, tmp_path / "app") == 1

    status = json.loads(
        (journal_root / "runs" / run_id / "status.json").read_text(encoding="utf-8")
    )
    assert status["error"]["code"] == "agent_run_failed"


@pytest.mark.asyncio
async def test_oversized_runner_event_records_a_readable_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "f" * 32
    journal_root = tmp_path / "journal"
    inbox = journal_root / "inbox" / f"{run_id}.json"
    inbox.parent.mkdir(parents=True)
    inbox.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "session_id": "session-1",
                "agent_name": "agent",
                "prompt": "hello",
                "timeout": 30.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")
    monkeypatch.setattr(journal_writer, "MAX_EVENT_SEGMENT_BYTES", 512)

    async def oversized_events(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        yield {"type": "delta", "content": "x" * 1024}
        yield {"type": "done", "delegate_error_count": 0}

    entry = SimpleNamespace(
        resolved=SimpleNamespace(instructions="instructions", model=None, subagents=[]),
        capabilities=SimpleNamespace(
            filtered_user_tools=[],
            filtered_mcp_tools=[],
            enabled_skill_paths=[],
            web_request_tools=[],
        ),
    )
    monkeypatch.setattr(harness_main, "run_agent_events", oversized_events)
    monkeypatch.setattr(harness_main, "rebuild_agent_catalog", lambda _: {"agent": entry})

    assert await harness_main._run(run_id, journal_root, tmp_path / "app") == 1

    run_directory = journal_root / "runs" / run_id
    status = json.loads((run_directory / "status.json").read_text(encoding="utf-8"))
    event_paths = tuple(run_directory.glob("events-*.jsonl"))
    assert status["state"] == "failed"
    assert status["error"]["code"] == "sandbox_storage_failure"
    assert event_paths
    assert all(path.stat().st_size <= journal_writer.MAX_EVENT_SEGMENT_BYTES for path in event_paths)
    assert [json.loads(line)["type"] for line in event_paths[0].read_text(encoding="utf-8").splitlines()] == [
        "error"
    ]


@pytest.mark.asyncio
async def test_harness_result_keeps_ordinary_tool_errors_out_of_delegate_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "g" * 32
    journal_root = tmp_path / "journal"
    inbox = journal_root / "inbox" / f"{run_id}.json"
    inbox.parent.mkdir(parents=True)
    inbox.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "session_id": "session-1",
                "agent_name": "agent",
                "prompt": "hello",
                "timeout": 30.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")

    async def tool_error_events(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        yield {
            "type": "tool_end",
            "tool_call_id": "call-1",
            "tool_name": "sandbox_exec",
            "result": '{"error":"boom"}',
        }
        yield {"type": "done", "delegate_error_count": 0}

    entry = SimpleNamespace(
        resolved=SimpleNamespace(instructions="instructions", model=None, subagents=[]),
        capabilities=SimpleNamespace(
            filtered_user_tools=[],
            filtered_mcp_tools=[],
            enabled_skill_paths=[],
            web_request_tools=[],
        ),
    )
    monkeypatch.setattr(harness_main, "run_agent_events", tool_error_events)
    monkeypatch.setattr(harness_main, "rebuild_agent_catalog", lambda _: {"agent": entry})

    assert await harness_main._run(run_id, journal_root, tmp_path / "app") == 0

    result = json.loads(
        (journal_root / "runs" / run_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["delegate_error_count"] == 0
    assert result["tool_calls"][0]["result"] == '{"error":"boom"}'


def test_checkpoint_restores_session_history_and_relative_working_files(tmp_path: Path) -> None:
    store = AtomicCommitStore(tmp_path / "session")
    store.commit(
        conversation=b'{"role":"assistant","content":"prior"}\n',
        working_files={"artifacts/answer.txt": b"persisted"},
    )
    work_directory = tmp_path / "runs" / "run-1" / "work"

    harness_main._restore_checkpoint(store, work_directory, "session-1")

    assert (work_directory / ".history" / "session-1.jsonl").read_bytes() == (
        b'{"role":"assistant","content":"prior"}\n'
    )
    assert (work_directory / "artifacts" / "answer.txt").read_bytes() == b"persisted"
    assert harness_main._conversation_snapshot(work_directory, "session-1") == (
        b'{"role":"assistant","content":"prior"}\n'
    )
    assert harness_main._collect_working_files(work_directory, "session-1") == {
        "artifacts/answer.txt": b"persisted"
    }


def test_checkpoint_separates_customer_conversation_file_from_history(tmp_path: Path) -> None:
    store = AtomicCommitStore(tmp_path / "session")
    checkpoint = store.commit(
        conversation=b'{"role":"assistant","content":"history"}\n',
        working_files={"conversation.json": b"customer working file"},
    )
    work_directory = tmp_path / "runs" / "run-1" / "work"

    assert (checkpoint.path / "conversation.json").read_bytes() == (
        b'{"role":"assistant","content":"history"}\n'
    )
    assert (checkpoint.path / "working" / "conversation.json").read_bytes() == (
        b"customer working file"
    )

    harness_main._restore_checkpoint(store, work_directory, "session-1")

    assert (work_directory / ".history" / "session-1.jsonl").read_bytes() == (
        b'{"role":"assistant","content":"history"}\n'
    )
    assert (work_directory / "conversation.json").read_bytes() == b"customer working file"
    assert harness_main._collect_working_files(work_directory, "session-1") == {
        "conversation.json": b"customer working file"
    }


def test_conversation_history_is_bounded_before_checkpoint_commit(tmp_path: Path) -> None:
    work_directory = tmp_path / "work"
    history = work_directory / ".history"
    history.mkdir(parents=True)
    (history / "session-1.jsonl").write_bytes(
        b"x" * (harness_main._MAX_CONVERSATION_BYTES + 1)
    )

    with pytest.raises(AtomicCommitError, match="conversation history"):
        harness_main._conversation_snapshot(work_directory, "session-1")


@pytest.mark.asyncio
async def test_entrypoint_rejects_an_unsafe_run_id_before_reading_the_inbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")

    with pytest.raises(HarnessJournalError, match="identifier"):
        await harness_main._run("../outside", tmp_path, tmp_path / "app")


def test_main_emits_controlled_stderr_diagnostic_on_pre_accept_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")

    exit_code = harness_main.main(["--run-id", "../outside"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert harness_main._LAUNCH_DIAGNOSTIC_PREFIX in captured.err
    assert "identifier" in captured.err
    assert "Traceback" not in captured.err


def test_main_emits_non_promoting_marker_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")

    def _raise_cancelled(coro: Coroutine[object, object, int]) -> int:
        # Close the unused coroutine (avoids a "never awaited" warning) before raising.
        coro.close()
        raise asyncio.CancelledError

    monkeypatch.setattr(harness_main.asyncio, "run", _raise_cancelled)

    exit_code = harness_main.main(["--run-id", "run-1"])

    assert exit_code == 1
    captured = capsys.readouterr()
    # Recorded only so a human can see why the run stopped; nothing parses this text.
    assert harness_main._LAUNCH_DIAGNOSTIC_PREFIX in captured.err
    assert "canceled" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.skipif(os.name != "posix", reason="fd redirection semantics are POSIX-only")
def test_silence_launch_stderr_stops_writes_reaching_the_sidecar(tmp_path: Path) -> None:
    sidecar = tmp_path / "launch.stderr"
    saved_fd2 = os.dup(2)
    sidecar_fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.dup2(sidecar_fd, 2)
        os.write(2, b"launch-window line reaches the sidecar\n")
        harness_main._silence_launch_stderr()
        os.write(2, b"post-acceptance chatter must not reach the sidecar\n")
    finally:
        os.dup2(saved_fd2, 2)
        os.close(saved_fd2)
        os.close(sidecar_fd)

    contents = sidecar.read_bytes()
    assert b"launch-window line reaches the sidecar" in contents
    assert b"post-acceptance chatter must not reach the sidecar" not in contents
