"""Logical run-control verbs over the sandbox's file-backed journal."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from ..session_state import validate_run_id, validate_session_id
from ..transport.ports import SandboxSessionHandle
from ..transport.transport_models import SandboxFileNotFoundError
from .backend import (
    EventCursorExpiredError,
    RunContext,
    RunError,
    RunEvent,
    RunResult,
    RunState,
    RunStatus,
)

JOURNAL_ROOT_PATH = "/var/lib/azure-functions-agents"
INBOX_PATH = f"{JOURNAL_ROOT_PATH}/inbox"
RUNS_PATH = f"{JOURNAL_ROOT_PATH}/runs"
MAX_RUN_ENVELOPE_BYTES = 4 * 1024 * 1024
EVENT_POLL_INTERVAL_SECONDS = 0.25
CANCEL_CONFIRM_TIMEOUT_SECONDS = 5.0

_TERMINAL_STATES: frozenset[RunState] = frozenset(
    {"succeeded", "failed", "canceled", "timed_out", "abandoned"}
)
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
        if launch_result.exit_code != 0:
            raise RunSubmissionDefinitiveFailureError(
                "Run launch failed before harness acceptance."
            )

        try:
            return await self._wait_for_acceptance(handle, context, deadline)
        except Exception as exc:
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
            result = await self._read_result(handle, context)
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
        while True:
            events = await self._read_events(handle, context)
            _assert_cursor_available(events, cursor)
            for event in events:
                if event.sequence > cursor:
                    cursor = event.sequence
                    yield event
                    if event.type in {"done", "error"}:
                        # The event can arrive before the terminal status write.
                        break
            status = await self.get_status(handle, context)
            if status.state in _TERMINAL_STATES:
                return
            await asyncio.sleep(self._event_poll_interval_seconds)

    async def cancel(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
    ) -> RunStatus:
        """Signal the recorded process group and wait for harness confirmation."""
        status = await self.get_status(handle, context)
        if status.state in _TERMINAL_STATES:
            return status
        process = _parse_process(
            await handle.read_file(_process_path(context.run_id))
        )
        deadline = time.monotonic() + self._cancel_confirm_timeout_seconds
        await handle.exec(
            _signal_process_group(process.process_group_id, force=False),
            timeout_seconds=_remaining_seconds(deadline),
        )
        try:
            return await self._wait_for_terminal(handle, context, deadline)
        except RunControlTimeoutError:
            await handle.exec(
                _signal_process_group(process.process_group_id, force=True),
                timeout_seconds=self._cancel_confirm_timeout_seconds,
            )
            final_deadline = time.monotonic() + self._cancel_confirm_timeout_seconds
            return await self._wait_for_terminal(handle, context, final_deadline)

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
            if status.state in _TERMINAL_STATES:
                return status
            await self._sleep_until_poll(deadline)

    async def _sleep_until_poll(self, deadline: float) -> None:
        await self._with_deadline(
            asyncio.sleep(min(self._event_poll_interval_seconds, _remaining_seconds(deadline))),
            deadline,
        )

    async def _read_status(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
    ) -> _JournalRunStatus:
        _validate_context(context)
        status = _parse_status(await handle.read_file(_status_path(context.run_id)))
        if status.run_id != context.run_id or status.session_id != context.session_id:
            raise RunJournalProtocolError("Run journal status does not match the requested context.")
        return status

    async def _read_result(
        self,
        handle: SandboxSessionHandle,
        context: RunContext,
    ) -> RunResult:
        result = _parse_result(await handle.read_file(_result_path(context.run_id)))
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
    ) -> list[RunEvent]:
        entries = await handle.list_files(_run_path(context.run_id))
        paths = sorted(
            entry.path
            for entry in entries
            if not entry.is_directory
            and entry.name.startswith("events")
            and entry.name.endswith(".jsonl")
        )
        events: list[RunEvent] = []
        for path in paths:
            events.extend(_parse_event_lines(await handle.read_file(path)))
        ordered = sorted(events, key=lambda event: event.sequence)
        if len({event.sequence for event in ordered}) != len(ordered):
            raise RunJournalProtocolError(
                "Sandbox run journal event segments contain duplicate sequences."
            )
        return ordered

    @staticmethod
    async def _with_deadline[T](operation: Awaitable[T], deadline: float) -> T:
        try:
            async with asyncio.timeout(_remaining_seconds(deadline)):
                return await operation
        except TimeoutError:
            raise RunControlTimeoutError(
                "Sandbox run-control operation did not complete before its deadline."
            ) from None


def _inbox_path(run_id: str) -> str:
    return f"{INBOX_PATH}/{validate_run_id(run_id)}.json"


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
    return f"{RUNS_PATH}/{validate_run_id(run_id)}"


def _status_path(run_id: str) -> str:
    return f"{_run_path(run_id)}/status.json"


def _result_path(run_id: str) -> str:
    return f"{_run_path(run_id)}/result.json"


def _process_path(run_id: str) -> str:
    return f"{_run_path(run_id)}/process.json"


def _launch_command(run_id: str) -> str:
    return f"{_JOURNAL_ENTRYPOINT} --run-id {validate_run_id(run_id)} >/dev/null 2>&1 &"


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


def _parse_model[T: BaseModel](payload: bytes, model: type[T], document_name: str) -> T:
    try:
        decoded = _decode_json_object(payload)
        return model.model_validate(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, _DuplicateJsonKeyError):
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
        except (json.JSONDecodeError, ValidationError, _DuplicateJsonKeyError, ValueError):
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


def _assert_cursor_available(events: list[RunEvent], after_sequence: int) -> None:
    if not events or after_sequence == 0:
        return
    earliest = events[0].sequence
    if after_sequence < earliest - 1:
        raise EventCursorExpiredError(
            f"Event cursor {after_sequence} expired; earliest retained event is {earliest}"
        )


class _DuplicateJsonKeyError(ValueError):
    """A journal JSON document contained more than one value for one key."""


def _decode_json_object(payload: bytes) -> dict[str, object]:
    decoded: object = json.loads(payload.decode("utf-8"), object_pairs_hook=_json_object)
    if not isinstance(decoded, dict):
        raise RunJournalProtocolError("Sandbox run journal document must be an object.")
    return decoded


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result
