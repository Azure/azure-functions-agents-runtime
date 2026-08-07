"""Strict writer for controller-readable sandbox run journals."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ..conformance.trace import SemanticTrace, TraceEvent, normalize_trace
from ..execution.backend import RunError, RunResult, RunState
from ..execution.run_control import (
    MAX_EVENT_SEGMENT_BYTES,
    MAX_EVENT_SEGMENTS,
    MAX_RESULT_BYTES,
    MAX_RUN_ENVELOPE_BYTES,
)
from ..journal_paths import PROCESS_FILENAME
from ..session_state import validate_run_id, validate_session_id
from ..strict_json import DuplicateJsonKeyError, decode_json_object

type JournalTerminalState = Literal["succeeded", "failed", "canceled", "timed_out", "abandoned"]
_RUN_CLAIM_FILENAME = ".run_claim.json"
_RUN_CLAIM_STALE_SECONDS = 5.0


class HarnessJournalError(RuntimeError):
    """A sandbox run journal document was malformed or unsafe."""


@dataclass(slots=True)
class RunClaim:
    """An exclusive filesystem claim for one harness run."""

    path: Path
    token: str
    descriptor: int
    recovered: bool
    _released: bool = False

    def release(self) -> None:
        """Release this claim without removing a successor's claim."""

        if self._released:
            return
        self._released = True
        try:
            os.close(self.descriptor)
        finally:
            if _claim_token(self.path) == self.token:
                self.path.unlink(missing_ok=True)
                _sync_directory(self.path.parent)


def acquire_run_claim(
    run_directory: Path,
    *,
    stale_after_seconds: float = _RUN_CLAIM_STALE_SECONDS,
) -> RunClaim | None:
    """Acquire one cross-process run claim, recovering only dead or stale owners."""

    if stale_after_seconds < 0:
        raise ValueError("stale_after_seconds must not be negative")
    run_directory.mkdir(parents=True, exist_ok=True)
    path = run_directory / _RUN_CLAIM_FILENAME
    recovered = False
    while True:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            if not _claim_is_stale(path, stale_after_seconds):
                return None
            try:
                path.unlink()
                _sync_directory(run_directory)
            except FileNotFoundError:
                pass
            recovered = True
            continue
        token = uuid.uuid4().hex
        try:
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "process_group_id": _process_group_id(),
                    "token": token,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            os.write(descriptor, payload)
            os.fsync(descriptor)
            _sync_directory(run_directory)
            return RunClaim(
                path=path,
                token=token,
                descriptor=descriptor,
                recovered=recovered,
            )
        except BaseException:
            os.close(descriptor)
            path.unlink(missing_ok=True)
            _sync_directory(run_directory)
            raise


def _claim_is_stale(path: Path, stale_after_seconds: float) -> bool:
    try:
        metadata = _read_claim(path)
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    if metadata is not None:
        pid = metadata.get("pid")
        process_group_id = metadata.get("process_group_id")
        if (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and pid > 0
            and isinstance(process_group_id, int)
            and not isinstance(process_group_id, bool)
            and process_group_id > 0
        ):
            return not _process_is_alive(pid)
    return age >= stale_after_seconds


def _read_claim(path: Path) -> dict[str, object] | None:
    try:
        return decode_json_object(path.read_bytes())
    except (
        DuplicateJsonKeyError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None


def _claim_token(path: Path) -> str | None:
    try:
        claim = _read_claim(path)
    except FileNotFoundError:
        return None
    if claim is None:
        return None
    token = claim.get("token")
    return token if isinstance(token, str) else None


def _process_group_id() -> int:
    return int(getattr(os, "getpgrp", os.getpid)())


def _process_is_alive(pid: int) -> bool:
    if os.name == "nt":
        return pid == os.getpid()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _InboxDocument(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    run_id: str
    session_id: str
    agent_name: str
    prompt: str
    timeout: float | None


@dataclass(frozen=True, slots=True)
class HarnessRunEnvelope:
    """Strictly validated input supplied by the controller inbox."""

    run_id: str
    session_id: str
    agent_name: str
    prompt: str
    timeout: float | None

    @classmethod
    def parse(cls, payload: bytes) -> HarnessRunEnvelope:
        if len(payload) > MAX_RUN_ENVELOPE_BYTES:
            raise HarnessJournalError("Sandbox run inbox exceeds its size limit.")
        try:
            decoded = decode_json_object(payload)
            document = _InboxDocument.model_validate(decoded)
            run_id = validate_run_id(document.run_id)
            session_id = validate_session_id(document.session_id)
        except (
            DuplicateJsonKeyError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ):
            raise HarnessJournalError("Sandbox run inbox is invalid.") from None
        if not document.agent_name.strip():
            raise HarnessJournalError("Sandbox run inbox has no agent name.")
        if document.timeout is not None and document.timeout <= 0:
            raise HarnessJournalError("Sandbox run inbox timeout is invalid.")
        return cls(
            run_id=run_id,
            session_id=session_id,
            agent_name=document.agent_name,
            prompt=document.prompt,
            timeout=document.timeout,
        )


class JournalWriter:
    """Atomically publish one run's status, events, process, and result documents."""

    def __init__(self, envelope: HarnessRunEnvelope, *, journal_root: Path) -> None:
        self._envelope = envelope
        self._journal_root = journal_root
        self._run_directory = journal_root / "runs" / envelope.run_id
        self._sequence = self._read_last_sequence()

    @property
    def envelope(self) -> HarnessRunEnvelope:
        """Return the controller-validated envelope."""

        return self._envelope

    @property
    def run_directory(self) -> Path:
        """Return the private journal directory for this run."""

        return self._run_directory

    def terminal_exists(self) -> bool:
        """Return whether a duplicate launch must leave an existing terminal journal unchanged."""

        status = self._read_status()
        return status is not None and status.get("state") in {
            "succeeded",
            "failed",
            "canceled",
            "timed_out",
            "abandoned",
        }

    def status_exists(self) -> bool:
        """Return whether any prior launch already owns this run journal."""

        return self._read_status() is not None

    def write_process_group(self, process_group_id: int) -> None:
        """Persist the process group used by controller cancellation."""

        if process_group_id <= 0:
            raise HarnessJournalError("Sandbox process group is invalid.")
        self._write_json(
            self._run_directory / PROCESS_FILENAME,
            {"process_group_id": process_group_id},
        )

    def write_accepted(self) -> None:
        """Publish the idempotent accepted state before executing the agent."""

        self._write_status("accepted")

    def write_running(self) -> None:
        """Publish the running state once the harness has reconstructed its agent."""

        self._write_status("running")

    def append_event(self, event_type: str, data: dict[str, object]) -> None:
        """Append one monotonically numbered event to a bounded JSONL segment set."""

        if not event_type:
            raise HarnessJournalError("Sandbox journal event type is invalid.")
        self._sequence += 1
        payload = {
            "sequence": self._sequence,
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        path = self._active_event_path(encoded)
        with path.open("ab") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        self._sync_directory(path.parent)
        self._prune_event_segments()

    def write_success(self, result: RunResult) -> None:
        """Publish result bytes before succeeded status makes them controller-visible."""

        payload = {
            "content": result.content,
            "content_intermediate": result.content_intermediate,
            "tool_calls": result.tool_calls,
            "reasoning": result.reasoning,
            "delegate_error_count": result.delegate_error_count,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            raise HarnessJournalError("Sandbox run result exceeds its size limit.")
        self._write_bytes(self._run_directory / "result.json", encoded + b"\n")
        self.append_event("done", {"state": "succeeded"})
        self._write_status("succeeded", result_available=True)

    def write_failure(self, state: JournalTerminalState, error: RunError) -> None:
        """Publish one terminal failure without exposing internal exception details."""

        if state == "succeeded":
            raise HarnessJournalError("Successful runs must publish a result.")
        self.append_event("error", {"state": state, "code": error.code})
        self._write_status(
            state,
            error=(
                None
                if state == "canceled"
                else {
                    "code": error.code,
                    "message": error.message,
                    "fault_domain": error.fault_domain,
                }
            ),
        )

    def write_conformance_trace(
        self,
        *,
        capabilities: tuple[str, ...],
        terminal_state: JournalTerminalState,
    ) -> None:
        """Publish a runtime-produced semantic trace beside the completed journal."""

        trace = normalize_trace(
            SemanticTrace(
                name=self._envelope.run_id,
                capabilities=capabilities,
                events=tuple(self._conformance_events()),
                terminal_state=terminal_state,
            )
        )
        self._write_json(
            self._run_directory / "conformance.trace.json",
            trace.model_dump(mode="json"),
        )

    def _write_status(
        self,
        state: RunState,
        *,
        result_available: bool = False,
        error: dict[str, object] | None = None,
    ) -> None:
        self._write_json(
            self._run_directory / "status.json",
            {
                "run_id": self._envelope.run_id,
                "session_id": self._envelope.session_id,
                "state": state,
                "last_sequence": self._sequence,
                "result_available": result_available,
                "error": error,
            },
        )

    def _read_status(self) -> dict[str, object] | None:
        path = self._run_directory / "status.json"
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return decode_json_object(payload)
        except (DuplicateJsonKeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise HarnessJournalError("Existing sandbox run status is invalid.") from None

    def _read_last_sequence(self) -> int:
        self._run_directory.mkdir(parents=True, exist_ok=True)
        sequence = 0
        for path in sorted(self._run_directory.glob("events*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                try:
                    document = decode_json_object(line.encode("utf-8"))
                    value = document["sequence"]
                except (
                    DuplicateJsonKeyError,
                    KeyError,
                    TypeError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                ):
                    raise HarnessJournalError("Existing sandbox events are invalid.") from None
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value != sequence + 1
                ):
                    raise HarnessJournalError("Existing sandbox events are not contiguous.")
                sequence = value
        return sequence

    def _active_event_path(self, payload: bytes) -> Path:
        paths = sorted(self._run_directory.glob("events-*.jsonl"))
        if not paths:
            return self._run_directory / "events-0000.jsonl"
        active = paths[-1]
        if active.stat().st_size + len(payload) <= MAX_EVENT_SEGMENT_BYTES:
            return active
        next_index = int(active.stem.removeprefix("events-")) + 1
        return self._run_directory / f"events-{next_index:04d}.jsonl"

    def _conformance_events(self) -> list[TraceEvent]:
        events: list[TraceEvent] = []
        for path in sorted(self._run_directory.glob("events-*.jsonl")):
            for line in path.read_bytes().splitlines():
                try:
                    document = decode_json_object(line)
                    event_type = document["type"]
                    data = document["data"]
                except (
                    DuplicateJsonKeyError,
                    KeyError,
                    TypeError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                ):
                    raise HarnessJournalError("Sandbox journal events are invalid.") from None
                if not isinstance(event_type, str) or not isinstance(data, dict):
                    raise HarnessJournalError("Sandbox journal events are invalid.")
                events.append(TraceEvent(type=event_type, data=data))
        return events

    def _prune_event_segments(self) -> None:
        paths = sorted(self._run_directory.glob("events-*.jsonl"))
        while len(paths) > MAX_EVENT_SEGMENTS:
            paths.pop(0).unlink()

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        self._write_bytes(
            path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n",
        )

    def _write_bytes(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        self._sync_directory(path.parent)

    def _sync_directory(self, path: Path) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
