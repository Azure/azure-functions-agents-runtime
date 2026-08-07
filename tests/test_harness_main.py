from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import azure_functions_agents.harness.__main__ as harness_main
from azure_functions_agents.conformance.capability_map import validate_capability_coverage
from azure_functions_agents.conformance.trace import parse_trace
from azure_functions_agents.harness import SANDBOX_MARKER_ENV_VAR
from azure_functions_agents.harness.atomic_commit import AtomicCommitError, AtomicCommitStore
from azure_functions_agents.harness.journal_writer import HarnessJournalError
from azure_functions_agents.harness.sandbox_capabilities import HARNESS_CAPABILITIES


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
