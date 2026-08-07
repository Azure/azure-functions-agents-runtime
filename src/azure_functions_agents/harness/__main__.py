"""Per-run sandbox harness entrypoint invoked by the controller journal."""

from __future__ import annotations

import argparse
import asyncio
import errno
import os
import shutil
import signal
from pathlib import Path

from ..execution.backend import RunError, RunResult
from ..journal_paths import JOURNAL_ROOT_PATH
from ..runner import run_agent_events
from ..session_state import validate_run_id
from . import _ensure_sandbox
from .atomic_commit import AtomicCommitError, AtomicCommitStore
from .delegation import rebuild_agent_catalog
from .journal_writer import (
    HarnessJournalError,
    HarnessRunEnvelope,
    JournalWriter,
    acquire_run_claim,
)
from .sandbox_capabilities import REQUIRED_HARNESS_CAPABILITIES
from .watchdog import Watchdog, WatchdogTimeoutError

_MAX_WORKING_FILE_BYTES = 4 * 1024 * 1024
_MAX_WORKING_TREE_BYTES = 16 * 1024 * 1024
_MAX_CONVERSATION_BYTES = 4 * 1024 * 1024


class HarnessRunFailureError(Exception):
    """An agent execution failure that already has a safe journal error."""

    def __init__(self, error: RunError) -> None:
        super().__init__(error.message)
        self.error = error


def main(argv: list[str] | None = None) -> int:
    """Run one controller-submitted sandbox journal envelope."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--journal-root", type=Path, default=Path(JOURNAL_ROOT_PATH))
    parser.add_argument("--app-root", type=Path, default=Path("/app"))
    arguments = parser.parse_args(argv)
    try:
        return asyncio.run(_run(arguments.run_id, arguments.journal_root, arguments.app_root))
    except asyncio.CancelledError:
        return 1
    except HarnessJournalError:
        return 1


async def _run(run_id: str, journal_root: Path, app_root: Path) -> int:
    _ensure_sandbox()
    try:
        run_id = validate_run_id(run_id)
    except ValueError:
        raise HarnessJournalError("Sandbox run identifier is invalid.") from None
    envelope = _read_envelope(journal_root, run_id)
    claim = acquire_run_claim(journal_root / "runs" / run_id)
    if claim is None:
        return 0
    try:
        return await _run_claimed(
            envelope,
            journal_root,
            app_root,
            recovered_claim=claim.recovered,
        )
    finally:
        claim.release()


async def _run_claimed(
    envelope: HarnessRunEnvelope,
    journal_root: Path,
    app_root: Path,
    *,
    recovered_claim: bool,
) -> int:
    writer = JournalWriter(envelope, journal_root=journal_root)
    if writer.terminal_exists() or (writer.status_exists() and not recovered_claim):
        return 0
    writer.write_process_group(_process_group_id())
    writer.write_accepted()
    watchdog = Watchdog(writer.run_directory)
    watchdog.write_process_group(_process_group_id())
    checkpoint_store = AtomicCommitStore(journal_root / "session")
    work_directory = writer.run_directory / "work"
    _install_cancellation_handler()
    try:
        _restore_checkpoint(checkpoint_store, work_directory, envelope.session_id)
        result = await watchdog.supervise(
            _execute_envelope(envelope, writer, app_root, work_directory),
            timeout_seconds=envelope.timeout or 900.0,
        )
        checkpoint_store.commit(
            conversation=_conversation_snapshot(work_directory, envelope.session_id),
            working_files=_collect_working_files(work_directory, envelope.session_id),
        )
        writer.write_success(result)
        writer.write_conformance_trace(
            capabilities=tuple(REQUIRED_HARNESS_CAPABILITIES.values()),
            terminal_state="succeeded",
        )
        return 0
    except WatchdogTimeoutError:
        writer.write_failure(
            "timed_out",
            RunError(
                code="run_timed_out",
                message="Run exceeded its authored deadline.",
                fault_domain="harness",
            ),
        )
        writer.write_conformance_trace(
            capabilities=tuple(REQUIRED_HARNESS_CAPABILITIES.values()),
            terminal_state="timed_out",
        )
    except asyncio.CancelledError:
        writer.write_failure(
            "canceled",
            RunError(
                code="run_canceled",
                message="Run was canceled by the controller.",
                fault_domain="harness",
            ),
        )
        writer.write_conformance_trace(
            capabilities=tuple(REQUIRED_HARNESS_CAPABILITIES.values()),
            terminal_state="canceled",
        )
        raise
    except HarnessRunFailureError as exc:
        writer.write_failure("failed", exc.error)
        writer.write_conformance_trace(
            capabilities=tuple(REQUIRED_HARNESS_CAPABILITIES.values()),
            terminal_state="failed",
        )
    except MemoryError:
        writer.write_failure(
            "failed",
            RunError(
                code="out_of_memory",
                message="Sandbox run failed due to memory exhaustion.",
                fault_domain="harness",
            ),
        )
        writer.write_conformance_trace(
            capabilities=tuple(REQUIRED_HARNESS_CAPABILITIES.values()),
            terminal_state="failed",
        )
    except OSError as exc:
        error = (
            RunError(
                code="disk_full",
                message="Sandbox run failed because storage is full.",
                fault_domain="harness",
            )
            if exc.errno == errno.ENOSPC
            else RunError(
                code="sandbox_storage_failure",
                message="Sandbox run could not persist its durable state.",
                fault_domain="harness",
            )
        )
        writer.write_failure("failed", error)
        writer.write_conformance_trace(
            capabilities=tuple(REQUIRED_HARNESS_CAPABILITIES.values()),
            terminal_state="failed",
        )
    except (AtomicCommitError, HarnessJournalError) as exc:
        writer.write_failure(
            "failed",
            RunError(
                code="sandbox_storage_failure",
                message="Sandbox run could not persist its durable state.",
                fault_domain="harness",
            ),
        )
        writer.write_conformance_trace(
            capabilities=tuple(REQUIRED_HARNESS_CAPABILITIES.values()),
            terminal_state="failed",
        )
        del exc
    except Exception:
        writer.write_failure(
            "failed",
            RunError(
                code="harness_run_failed",
                message="Sandbox harness could not complete the run.",
                fault_domain="harness",
            ),
        )
        writer.write_conformance_trace(
            capabilities=tuple(REQUIRED_HARNESS_CAPABILITIES.values()),
            terminal_state="failed",
        )
    return 1


def _read_envelope(journal_root: Path, run_id: str) -> HarnessRunEnvelope:
    path = journal_root / "inbox" / f"{run_id}.json"
    try:
        envelope = HarnessRunEnvelope.parse(path.read_bytes())
    except FileNotFoundError:
        raise HarnessJournalError("Sandbox run inbox is missing.") from None
    if envelope.run_id != run_id:
        raise HarnessJournalError("Sandbox run inbox does not match the requested run.")
    return envelope


async def _execute_envelope(
    envelope: HarnessRunEnvelope,
    writer: JournalWriter,
    app_root: Path,
    work_directory: Path,
) -> RunResult:
    os.environ["AZURE_FUNCTIONS_AGENTS_APP_ROOT"] = str(app_root)
    os.environ["AZURE_FUNCTIONS_AGENTS_SESSION_DIR"] = str(work_directory / ".history")
    os.chdir(work_directory)
    catalog = rebuild_agent_catalog(app_root)
    entry = catalog.get(envelope.agent_name)
    if entry is None:
        raise HarnessJournalError("Sandbox run requested an unknown agent.")
    writer.write_running()
    resolved = entry.resolved
    capabilities = entry.capabilities
    content_parts: list[str] = []
    intermediates: list[str] = []
    tool_calls: list[dict[str, object]] = []
    delegate_error_count = 0
    error: RunError | None = None
    async for event in run_agent_events(
        envelope.prompt,
        instructions=resolved.instructions,
        timeout=envelope.timeout,
        tools=capabilities.filtered_user_tools,
        mcp_tools=capabilities.filtered_mcp_tools,
        skill_paths=capabilities.enabled_skill_paths,
        model=resolved.model,
        session_id=envelope.session_id,
        web_request_tools=capabilities.web_request_tools,
        subagents=resolved.subagents,
        catalog=catalog,
    ):
        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise HarnessJournalError("Sandbox runner emitted an invalid event.")
        if event_type == "done":
            value = event.get("delegate_error_count", 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HarnessJournalError("Sandbox runner emitted an invalid completion event.")
            delegate_error_count = value
            continue
        if event_type == "error":
            content = event.get("content")
            error = RunError(
                code="agent_run_failed",
                message=content if isinstance(content, str) else "Sandbox agent run failed.",
                fault_domain="runtime",
            )
            continue
        data = {str(key): value for key, value in event.items() if key != "type"}
        writer.append_event(event_type, data)
        if event_type in {"delta", "message"}:
            content = data.get("content")
            if isinstance(content, str):
                content_parts.append(content)
        elif event_type == "intermediate":
            content = data.get("content")
            if isinstance(content, str):
                intermediates.append(content)
        elif event_type in {"tool_start", "tool_end"}:
            tool_calls.append(data)
    if error is not None:
        raise HarnessRunFailureError(error)
    return RunResult(
        content="".join(content_parts),
        content_intermediate=intermediates,
        tool_calls=tool_calls,
        reasoning=None,
        delegate_error_count=delegate_error_count,
    )


def _restore_checkpoint(
    store: AtomicCommitStore,
    work_directory: Path,
    session_id: str,
) -> None:
    checkpoint = store.recover()
    shutil.rmtree(work_directory, ignore_errors=True)
    work_directory.mkdir(parents=True)
    if checkpoint is None:
        return
    conversation = checkpoint.path / "conversation.json"
    if conversation.is_file():
        if conversation.stat().st_size > _MAX_CONVERSATION_BYTES:
            raise AtomicCommitError("Sandbox conversation history exceeds the durable size bound.")
        history_path = _history_path(work_directory, session_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(conversation, history_path)
    for path in checkpoint.path.rglob("*"):
        if path.is_dir() or path == conversation:
            continue
        relative = path.relative_to(checkpoint.path)
        destination = work_directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def _collect_working_files(work_directory: Path, session_id: str) -> dict[str, bytes]:
    collected: dict[str, bytes] = {}
    total = 0
    for path in work_directory.rglob("*"):
        if path.is_dir():
            continue
        if path == _history_path(work_directory, session_id):
            continue
        if path.is_symlink():
            raise AtomicCommitError("Sandbox working files must not be symlinks.")
        size = path.stat().st_size
        if size > _MAX_WORKING_FILE_BYTES:
            raise AtomicCommitError("Sandbox working file exceeds the durable size bound.")
        total += size
        if total > _MAX_WORKING_TREE_BYTES:
            raise AtomicCommitError("Sandbox working tree exceeds the durable size bound.")
        collected[path.relative_to(work_directory).as_posix()] = path.read_bytes()
    return collected


def _conversation_snapshot(work_directory: Path, session_id: str) -> bytes:
    history_path = _history_path(work_directory, session_id)
    try:
        conversation = history_path.read_bytes()
    except FileNotFoundError:
        return b""
    if len(conversation) > _MAX_CONVERSATION_BYTES:
        raise AtomicCommitError("Sandbox conversation history exceeds the durable size bound.")
    return conversation


def _history_path(work_directory: Path, session_id: str) -> Path:
    return work_directory / ".history" / f"{session_id}.jsonl"


def _process_group_id() -> int:
    return int(getattr(os, "getpgrp", os.getpid)())


def _install_cancellation_handler() -> None:
    if os.name != "posix":
        return
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:
        return
    loop.add_signal_handler(signal.SIGTERM, task.cancel)


if __name__ == "__main__":
    raise SystemExit(main())
