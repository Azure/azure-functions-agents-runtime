"""Logical run-control verbs over the sandbox's file-backed journal."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from .._logger import logger
from ..journal_paths import (
    INBOX_PATH as _INBOX_PATH,
)
from ..journal_paths import (
    JOURNAL_ROOT_PATH as _JOURNAL_ROOT_PATH,
)
from ..journal_paths import (
    LAUNCH_STDERR_FILENAME,
    inbox_path,
    launch_stderr_path,
    process_path,
    result_path,
    run_path,
    status_path,
)
from ..journal_paths import (
    RUNS_PATH as _RUNS_PATH,
)
from ..session_state import TERMINAL_RUN_STATUSES, validate_run_id, validate_session_id
from ..strict_json import DuplicateJsonKeyError, decode_json_object
from ..transport.ports import SandboxSessionHandle
from ..transport.transport_models import SandboxFileNotFoundError
from .backend import (
    TERMINAL_EVENT_TYPES,
    RunContext,
    RunError,
    RunEvent,
    RunResult,
    RunState,
    RunStatus,
    assert_event_cursor_available,
)

JOURNAL_ROOT_PATH = _JOURNAL_ROOT_PATH
INBOX_PATH = _INBOX_PATH
RUNS_PATH = _RUNS_PATH
MAX_JOURNAL_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_RUN_ENVELOPE_BYTES = MAX_JOURNAL_DOCUMENT_BYTES
MAX_STATUS_BYTES = MAX_JOURNAL_DOCUMENT_BYTES
MAX_RESULT_BYTES = MAX_JOURNAL_DOCUMENT_BYTES
MAX_PROCESS_BYTES = MAX_JOURNAL_DOCUMENT_BYTES
MAX_LAUNCH_STDERR_BYTES = 64 * 1024
MAX_EVENT_SEGMENTS = 16
MAX_EVENT_SEGMENT_BYTES = 1024 * 1024
EVENT_POLL_INTERVAL_SECONDS = 1.0
JOURNAL_VISIBILITY_TIMEOUT_SECONDS = 2.0
CANCEL_CONFIRM_TIMEOUT_SECONDS = 5.0
LAUNCH_STDERR_READ_TIMEOUT_SECONDS = 2.0

_JOURNAL_ENTRYPOINT = "setsid nohup python -m azure_functions_agents.harness"

type _JournalText = Annotated[str, StringConstraints(min_length=1)]


class RunControlError(RuntimeError):
    """The sandbox run journal cannot safely satisfy a controller operation."""


class RunJournalProtocolError(RunControlError):
    """An untrusted journal document violated the agreed runtime wire contract."""


class RunControlTimeoutError(TimeoutError):
    """A bounded run-control operation did not reach its required journal state."""


class RunSubmissionDefinitiveFailureError(RunControlError):
    """Submission failed before a harness process could start."""


class RunSubmissionIndeterminateError(RunControlError):
    """A harness launch may have started but journal acceptance was not confirmed."""


@dataclass(frozen=True, slots=True)
class RunEnvelope:
    """The serializable, credential-free input submitted to the sandbox harness."""

    run_id: str
    session_id: str
    agent_name: str
    prompt: str
    timeout: float | None

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        session_id: str,
        agent_name: str,
        prompt: str,
        timeout: float | None,
    ) -> RunEnvelope:
        if not agent_name.strip():
            raise ValueError("agent_name must be non-empty")
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be positive and finite when specified")
        return cls(
            run_id=validate_run_id(run_id),
            session_id=validate_session_id(session_id),
            agent_name=agent_name,
            prompt=prompt,
            timeout=timeout,
        )

    def render(self) -> bytes:
        """Render the canonical inbox payload and enforce its file-plane limit."""
        payload = json.dumps(
            {
                "agent_name": self.agent_name,
                "prompt": self.prompt,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "timeout": self.timeout,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > MAX_RUN_ENVELOPE_BYTES:
            raise RunControlError("Run envelope exceeds the journal inbox size limit.")
        return payload


class _JournalRunError(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    code: _JournalText
    message: _JournalText
    fault_domain: str | None = None


class _JournalRunStatus(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    run_id: _JournalText
    session_id: _JournalText
    state: RunState
    last_sequence: int = Field(ge=0)
    result_available: bool
    error: _JournalRunError | None = None

    @model_validator(mode="after")
    def _validate_terminal_shape(self) -> _JournalRunStatus:
        if self.state == "succeeded":
            if not self.result_available or self.error is not None:
                raise ValueError("succeeded journal status must include its result and no error")
            return self
        if self.result_available:
            raise ValueError("non-succeeded journal status must not advertise a result")
        if self.state in {"failed", "timed_out", "abandoned"} and self.error is None:
            raise ValueError("terminal failure journal status requires an error")
        if self.state in {"accepted", "running", "canceled"} and self.error is not None:
            raise ValueError("non-failure journal status must not include an error")
        return self


class _JournalRunResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    content: str
    content_intermediate: list[str]
    tool_calls: list[dict[str, object]]
    reasoning: str | None = None
    delegate_error_count: int = Field(ge=0)


class _JournalRunEvent(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    type: _JournalText
    data: dict[str, object]
    timestamp: _JournalText


class _JournalProcess(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    process_group_id: int = Field(gt=0)


class SandboxRunControl:
    """Submit, inspect, tail, and cancel harness runs through direct file APIs."""

    def __init__(
        self,
        *,
        event_poll_interval_seconds: float = EVENT_POLL_INTERVAL_SECONDS,
        cancel_confirm_timeout_seconds: float = CANCEL_CONFIRM_TIMEOUT_SECONDS,
    ) -> None:
        if event_poll_interval_seconds <= 0:
            raise ValueError("event_poll_interval_seconds must be positive")
        if cancel_confirm_timeout_seconds <= 0:
            raise ValueError("cancel_confirm_timeout_seconds must be positive")
        self._event_poll_interval_seconds = event_poll_interval_seconds
        self._cancel_confirm_timeout_seconds = cancel_confirm_timeout_seconds

    async def submit(
        self,
        handle: SandboxSessionHandle,
        run_id: str,
        envelope: RunEnvelope,
        *,
        timeout_seconds: float,
    ) -> RunStatus:
        """Write one inbox envelope, launch once, and wait for journal acceptance."""
        try:
            normalized_run_id = _validate_submission_inputs(
                run_id,
                envelope,
                timeout_seconds,
            )
        except Exception as exc:
            raise RunSubmissionDefinitiveFailureError(
                "Run submission could not be prepared before launch."
            ) from exc

        context = RunContext(run_id=normalized_run_id, session_id=envelope.session_id)
        try:
            return await self.get_status(handle, context)
        except SandboxFileNotFoundError:
            pass
        except RunJournalProtocolError:
            raise
        except Exception as exc:
            raise RunSubmissionIndeterminateError(
                "Existing run state could not be confirmed before launch."
            ) from exc

        deadline = time.monotonic() + timeout_seconds
        try:
            await self._with_deadline(
                handle.write_file(
                    _inbox_path(normalized_run_id),
                    envelope.render(),
                    create_dirs=True,
                ),
                deadline,
            )
            # Pre-create an empty launch-stderr sidecar so the shell's `2>` redirect has an
            # existing target; the detached harness only creates the run directory lazily.
            await self._with_deadline(
                handle.write_file(
                    _launch_stderr_path(normalized_run_id),
                    b"",
                    create_dirs=True,
                ),
                deadline,
            )
            launch_timeout_seconds = _remaining_seconds(deadline)
        except Exception as exc:
            raise RunSubmissionDefinitiveFailureError(
                "Run request could not be written before launch."
            ) from exc

        try:
            launch_result = await self._with_deadline(
                handle.exec(
                    _launch_command(normalized_run_id),
                    timeout_seconds=launch_timeout_seconds,
                ),
                deadline,
            )
        except Exception as exc:
            raise RunSubmissionIndeterminateError(
                "Run launch may have started but could not be confirmed."
            ) from exc
        # Defense-in-depth: the backgrounded launch normally reports 0, so this rarely fires,
        # but a non-zero exit still means the harness never started.
        if launch_result.exit_code != 0:
            raise RunSubmissionDefinitiveFailureError(
                "Run launch failed before harness acceptance."
            )

        try:
            return await self._wait_for_acceptance(handle, context, deadline)
        except RunJournalProtocolError:
            raise
        except Exception as exc:
            launch_stderr = await self._read_launch_stderr(handle, normalized_run_id)
            if launch_stderr.strip():
                # Untrusted sandbox stderr: logged for operators, never surfaced in the caller
                # exception. Stays indeterminate; benign startup noise must not orphan a live run.
                logger.error(
                    "Sandbox harness emitted launch stderr before acceptance for run %s: %s",
                    normalized_run_id,
                    launch_stderr,
                )
            raise RunSubmissionIndeterminateError(
                "Run launch may have started but journal acceptance was not confirmed."
            ) from exc

    async def get_status(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
    ) -> RunStatus:
        """Read the authoritative status and terminal result when it remains available."""
        journal_status = await self._read_status(handle, context)
        result: RunResult | None = None
        if journal_status.state == "succeeded" and journal_status.result_available:
            try:
                result = await self._read_result(handle, context)
            except SandboxFileNotFoundError:
                raise RunJournalProtocolError(
                    "Sandbox run journal result is missing."
                ) from None
        error = (
            None
            if journal_status.error is None
            else RunError(
                code=journal_status.error.code,
                message=journal_status.error.message,
                fault_domain=journal_status.error.fault_domain,
            )
        )
        return RunStatus(
            run_id=context.run_id,
            session_id=context.session_id,
            state=journal_status.state,
            last_sequence=journal_status.last_sequence,
            result_available=journal_status.result_available,
            result=result,
            error=error,
        )

    async def read_events(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
        after_sequence: int,
    ) -> AsyncIterator[RunEvent]:
        """Tail retained journal events strictly after an exclusive cursor."""
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        _validate_context(context)
        cursor = after_sequence
        completed_segments: dict[str, tuple[RunEvent, ...]] = {}
        visibility_deadline: float | None = None
        while True:
            events = await self._read_events(handle, context, completed_segments)
            assert_event_cursor_available(
                events[0].sequence if events else None,
                cursor,
            )
            status = await self._read_status(handle, context)
            if not _event_history_matches_status(events, status):
                visibility_deadline = visibility_deadline or (
                    time.monotonic() + JOURNAL_VISIBILITY_TIMEOUT_SECONDS
                )
                await self._wait_for_event_visibility(visibility_deadline)
                continue
            visibility_deadline = None
            for event in events:
                if event.sequence > cursor:
                    cursor = event.sequence
                    yield event
                    if event.type in TERMINAL_EVENT_TYPES:
                        # The event can arrive before the terminal status write.
                        break
            if status.state in TERMINAL_RUN_STATUSES:
                return
            await asyncio.sleep(self._event_poll_interval_seconds)

    async def cancel(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
    ) -> RunStatus:
        """Signal the recorded process group and wait for harness confirmation."""
        status = await self.get_status(handle, context)
        if status.state in TERMINAL_RUN_STATUSES:
            return status
        process_group_id = await self.read_process_group_id(handle, context)
        deadline = time.monotonic() + self._cancel_confirm_timeout_seconds
        await handle.exec(
            _signal_process_group(process_group_id, force=False),
            timeout_seconds=_remaining_seconds(deadline),
        )
        try:
            return await self._wait_for_terminal(handle, context, deadline)
        except RunControlTimeoutError:
            await handle.exec(
                _signal_process_group(process_group_id, force=True),
                timeout_seconds=self._cancel_confirm_timeout_seconds,
            )
            final_deadline = time.monotonic() + self._cancel_confirm_timeout_seconds
            return await self._wait_for_terminal(handle, context, final_deadline)

    async def read_process_group_id(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
    ) -> int:
        """Read one strictly validated process-group identifier."""
        _validate_context(context)
        process_payload = await handle.read_file(_process_path(context.run_id))
        _assert_journal_payload_size(process_payload, MAX_PROCESS_BYTES, "process")
        return _parse_process(process_payload).process_group_id

    async def _wait_for_acceptance(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
        deadline: float,
    ) -> RunStatus:
        while True:
            try:
                status = await self._with_deadline(self.get_status(handle, context), deadline)
            except SandboxFileNotFoundError:
                status = None
            if status is not None and status.state in {
                "accepted",
                "running",
                "succeeded",
                "failed",
                "canceled",
                "timed_out",
                "abandoned",
            }:
                return status
            await self._sleep_until_poll(deadline)

    async def _wait_for_terminal(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
        deadline: float,
    ) -> RunStatus:
        while True:
            status = await self._with_deadline(self.get_status(handle, context), deadline)
            if status.state in TERMINAL_RUN_STATUSES:
                return status
            await self._sleep_until_poll(deadline)

    async def _sleep_until_poll(self, deadline: float) -> None:
        await self._with_deadline(
            asyncio.sleep(min(self._event_poll_interval_seconds, _remaining_seconds(deadline))),
            deadline,
        )

    async def _wait_for_event_visibility(self, deadline: float) -> None:
        try:
            await self._sleep_until_poll(deadline)
        except RunControlTimeoutError:
            raise RunJournalProtocolError(
                "Sandbox run journal event history is inconsistent."
            ) from None

    async def _read_status(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
    ) -> _JournalRunStatus:
        _validate_context(context)
        status_payload = await handle.read_file(_status_path(context.run_id))
        _assert_journal_payload_size(status_payload, MAX_STATUS_BYTES, "status")
        status = _parse_status(status_payload)
        if status.run_id != context.run_id or status.session_id != context.session_id:
            raise RunJournalProtocolError("Run journal status does not match the requested context.")
        return status

    async def _read_launch_stderr(
        self,
        handle: SandboxSessionHandle,
        run_id: str,
    ) -> str:
        """Best-effort, size-capped read of the untrusted per-run launch stderr.

        The sidecar is untrusted sandbox text: never JSON-decoded, bounded in time and
        size, and any read failure is swallowed so it cannot mask the original error.
        """
        try:
            async with asyncio.timeout(LAUNCH_STDERR_READ_TIMEOUT_SECONDS):
                payload = await handle.read_file(_launch_stderr_path(run_id))
        except Exception as exc:
            logger.debug("Could not read launch stderr sidecar for run %s: %r", run_id, exc)
            return ""
        return payload[:MAX_LAUNCH_STDERR_BYTES].decode("utf-8", errors="replace")

    async def _read_result(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
    ) -> RunResult:
        result_payload = await handle.read_file(_result_path(context.run_id))
        _assert_journal_payload_size(result_payload, MAX_RESULT_BYTES, "result")
        result = _parse_result(result_payload)
        return RunResult(
            content=result.content,
            content_intermediate=result.content_intermediate,
            tool_calls=result.tool_calls,
            reasoning=result.reasoning,
            delegate_error_count=result.delegate_error_count,
        )

    async def _read_events(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
        completed_segments: dict[str, tuple[RunEvent, ...]],
    ) -> list[RunEvent]:
        entries = await handle.list_files(_run_path(context.run_id))
        paths = sorted(
            entry.path
            for entry in entries
            if not entry.is_directory
            and entry.name != LAUNCH_STDERR_FILENAME
            and entry.name.startswith("events")
            and entry.name.endswith(".jsonl")
        )
        if len(paths) > MAX_EVENT_SEGMENTS:
            raise RunJournalProtocolError(
                "Sandbox run journal has too many retained event segments."
            )
        active_tail = paths[-1] if paths else None
        for cached_path in tuple(completed_segments):
            if cached_path not in paths:
                del completed_segments[cached_path]
        events: list[RunEvent] = []
        for path in paths:
            cached_events = completed_segments.get(path)
            if cached_events is not None:
                events.extend(cached_events)
                continue
            payload = await handle.read_file(path)
            _assert_journal_payload_size(payload, MAX_EVENT_SEGMENT_BYTES, "event segment")
            parsed_events = _parse_event_lines(payload)
            events.extend(parsed_events)
            if path != active_tail:
                completed_segments[path] = tuple(parsed_events)
        ordered = sorted(events, key=lambda event: event.sequence)
        if len({event.sequence for event in ordered}) != len(ordered):
            raise RunJournalProtocolError(
                "Sandbox run journal event segments contain duplicate sequences."
            )
        return ordered

    @staticmethod
    async def _with_deadline[T](operation: Awaitable[T], deadline: float) -> T:
        try:
            remaining = _remaining_seconds(deadline)
        except RunControlTimeoutError:
            close = getattr(operation, "close", None)
            if close is not None:
                close()
            raise
        try:
            async with asyncio.timeout(remaining):
                return await operation
        except TimeoutError:
            raise RunControlTimeoutError(
                "Sandbox run-control operation did not complete before its deadline."
            ) from None


def _inbox_path(run_id: str) -> str:
    return inbox_path(validate_run_id(run_id))


def _validate_submission_inputs(
    run_id: str,
    envelope: RunEnvelope,
    timeout_seconds: float,
) -> str:
    normalized_run_id = validate_run_id(run_id)
    if envelope.run_id != normalized_run_id:
        raise RunControlError("Run envelope does not match the requested run.")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    return normalized_run_id


def _run_path(run_id: str) -> str:
    return run_path(validate_run_id(run_id))


def _status_path(run_id: str) -> str:
    return status_path(validate_run_id(run_id))


def _result_path(run_id: str) -> str:
    return result_path(validate_run_id(run_id))


def _process_path(run_id: str) -> str:
    return process_path(validate_run_id(run_id))


def _launch_stderr_path(run_id: str) -> str:
    return launch_stderr_path(validate_run_id(run_id))


def _launch_command(run_id: str) -> str:
    normalized_run_id = validate_run_id(run_id)
    return (
        f"{_JOURNAL_ENTRYPOINT} --run-id {normalized_run_id} "
        f">/dev/null 2>{launch_stderr_path(normalized_run_id)} &"
    )


def _signal_process_group(process_group_id: int, *, force: bool) -> str:
    signal = "-KILL" if force else "-TERM"
    return f"kill {signal} -- -{process_group_id}"


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RunControlTimeoutError(
            "Sandbox run-control operation did not complete before its deadline."
        )
    return remaining


def _validate_context(context: RunContext) -> None:
    validate_run_id(context.run_id)
    validate_session_id(context.session_id)


def _parse_status(payload: bytes) -> _JournalRunStatus:
    return _parse_model(payload, _JournalRunStatus, "status")


def _parse_result(payload: bytes) -> _JournalRunResult:
    return _parse_model(payload, _JournalRunResult, "result")


def _parse_process(payload: bytes) -> _JournalProcess:
    return _parse_model(payload, _JournalProcess, "process")


def _assert_journal_payload_size(payload: bytes, maximum: int, document_name: str) -> None:
    if len(payload) > maximum:
        raise RunJournalProtocolError(
            f"Sandbox run journal {document_name} exceeds its size limit."
        )


def _parse_model[T: BaseModel](payload: bytes, model: type[T], document_name: str) -> T:
    try:
        decoded = _decode_json_object(payload)
        return model.model_validate(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, DuplicateJsonKeyError):
        raise RunJournalProtocolError(
            f"Sandbox run journal {document_name} document is invalid."
        ) from None


def _parse_event_lines(payload: bytes) -> list[RunEvent]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise RunJournalProtocolError("Sandbox run journal event segment is invalid.") from None
    events: list[RunEvent] = []
    for line in text.splitlines():
        if not line:
            continue
        try:
            parsed = _JournalRunEvent.model_validate(_decode_json_object(line.encode("utf-8")))
            timestamp = _parse_timestamp(parsed.timestamp)
        except (json.JSONDecodeError, ValidationError, DuplicateJsonKeyError, ValueError):
            raise RunJournalProtocolError(
                "Sandbox run journal event segment is invalid."
            ) from None
        events.append(
            RunEvent(
                sequence=parsed.sequence,
                type=parsed.type,
                data=parsed.data,
                timestamp=timestamp,
            )
        )
    return events


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _event_history_matches_status(
    events: list[RunEvent],
    status: _JournalRunStatus,
) -> bool:
    if not events:
        return status.last_sequence == 0
    if status.last_sequence > events[-1].sequence:
        return False
    if status.state in TERMINAL_RUN_STATUSES and status.last_sequence < events[-1].sequence:
        return False
    return all(
        current.sequence == previous.sequence + 1
        for previous, current in pairwise(events)
    )


def _decode_json_object(payload: bytes) -> dict[str, object]:
    try:
        return decode_json_object(payload)
    except TypeError:
        raise RunJournalProtocolError("Sandbox run journal document must be an object.") from None
