"""Tests for the controller-only ACA checkpoint history reader.

The fakes prove controller ordering and typed file-plane handling only; they do
not prove ACA SDK or deployed Sandbox behavior.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from agent_framework import Message

import azure_functions_agents.controller.history_reader as history_reader
from azure_functions_agents.controller.history_reader import (
    MAX_CHECKPOINT_CONVERSATION_BYTES,
    SessionHistoryGoneError,
    SessionHistoryNotFoundError,
    SessionHistoryRead,
    SessionHistoryUnavailableError,
    read_session_history,
)
from azure_functions_agents.controller.readiness import (
    ActivatedSession,
    SessionActivationGoneError,
    SessionActivationNotFoundError,
    SessionActivationUnavailableError,
    SessionActivationUntrustedError,
)
from azure_functions_agents.execution.setup_budget import SetupBudget
from azure_functions_agents.journal_paths import (
    ATOMIC_CHECKPOINT_POINTER_PATH,
    checkpoint_conversation_path,
)
from azure_functions_agents.session_state import (
    AppIdentity,
    DurableRunRecord,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    owner_partition,
)
from azure_functions_agents.transport.transport_models import (
    SandboxFileNotFoundError,
    SandboxFileOperationError,
    SandboxFileStat,
)
from tests.doubles.fake_session_runtime import (
    FakeSandboxSessionHandle,
    FakeSessionStateStore,
)

_FINGERPRINT = "s1-" + ("a" * 52)


def _owner() -> FunctionAppOwnerContext:
    return FunctionAppOwnerContext.create(
        AppIdentity.create(
            subscription_id="11111111-2222-3333-4444-555555555555",
            site_name="agent-app",
        ),
        "main",
    )


def _session(
    *,
    checkpoint_expectation: str = "required",
    status: str = "ready",
    active_run_id: str | None = None,
) -> DurableSessionRecord:
    now = datetime.now(UTC)
    return DurableSessionRecord.create(
        owner_partition=owner_partition(_owner()),
        session_id="session-1",
        sandbox_id="sandbox-1",
        generation=1,
        digest_kind="sha256",
        digest="a" * 64,
        protocol="1",
        checkpoint_expectation=checkpoint_expectation,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        last_activity_at=now,
        expires_at=now + timedelta(hours=24),
        idle_policy_armed=True,
        active_run_id=active_run_id,
        snapshot_ids=(),
        region="westus2",
        state_store_fingerprint=_FINGERPRINT,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=now,
        updated_at=now,
        active_operation_id=None,
        operation_sequence=0,
    )


def _jsonl(*messages: Message) -> bytes:
    return b"".join(
        json.dumps(message.to_dict(), separators=(",", ":")).encode("utf-8") + b"\n"
        for message in messages
    )


def _activated(
    *,
    checkpoint_name: str | None = None,
    checkpoint_expectation: str = "required",
    resumed: bool = False,
    status: str = "ready",
    active_run_id: str | None = None,
) -> tuple[ActivatedSession, FakeSandboxSessionHandle, FakeSessionStateStore]:
    session = _session(
        checkpoint_expectation=checkpoint_expectation,
        status=status,
        active_run_id=active_run_id,
    )
    handle = FakeSandboxSessionHandle()
    store = FakeSessionStateStore(session)
    return (
        ActivatedSession.create(
            handle=handle,
            session=session,
            etag=store.etag,
            partition=session.owner_partition,
            store=store,
            checkpoint_name=checkpoint_name,
            resumed=resumed,
        ),
        handle,
        store,
    )


class _Runtime:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.partition = None
        self.session_id = None

    async def reconcile_session(self, partition, session_id: str) -> None:  # type: ignore[no-untyped-def]
        self.events.append("reconcile")
        self.partition = partition
        self.session_id = session_id


def _install_activation(
    monkeypatch: pytest.MonkeyPatch,
    activated: ActivatedSession | Exception,
    events: list[str] | None = None,
) -> None:
    async def activate(*_args: object, **_kwargs: object) -> ActivatedSession:
        if events is not None:
            events.append("activate")
        if isinstance(activated, Exception):
            raise activated
        return activated

    monkeypatch.setattr(history_reader, "activate_session", activate)


@pytest.mark.asyncio
async def test_reconciles_owner_session_before_activation_and_reads_canonical_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = f"checkpoint_{uuid4().hex}"
    activated, handle, store = _activated(
        checkpoint_name=checkpoint,
        status="running",
        active_run_id="run-1",
    )
    path = checkpoint_conversation_path(checkpoint)
    handle.seed_file(path, _jsonl(Message(role="user", contents=["first"])))
    handle.seed_file(ATOMIC_CHECKPOINT_POINTER_PATH, b"unexpected-pointer")
    events: list[str] = []
    runtime = _Runtime(events)
    _install_activation(monkeypatch, activated, events)

    result = await read_session_history(runtime, _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]

    assert result == SessionHistoryRead(
        messages=[{"role": "user", "text": "first"}],
        truncated=False,
        resumed=False,
    )
    assert events == ["reconcile", "activate"]
    assert runtime.partition == owner_partition(_owner())
    assert runtime.session_id == "session-1"
    assert [(call.operation, call.path) for call in handle.calls] == [
        ("stat_file", path),
        ("read_file", path),
    ]
    assert handle.closed is True
    assert store.operations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expectation", "error"),
    [
        ("none", None),
        ("required", SessionHistoryUnavailableError),
        ("unknown", SessionHistoryUnavailableError),
    ],
)
async def test_missing_validated_pointer_uses_checkpoint_expectation(
    monkeypatch: pytest.MonkeyPatch,
    expectation: str,
    error: type[SessionHistoryUnavailableError] | None,
) -> None:
    activated, handle, _ = _activated(
        checkpoint_name=None,
        checkpoint_expectation=expectation,
    )
    _install_activation(monkeypatch, activated)

    if error is None:
        result = await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]
        assert result.messages == []
    else:
        with pytest.raises(error):
            await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]

    assert handle.calls == []
    assert handle.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("activation_error", "expected"),
    [
        (SessionActivationNotFoundError("missing"), SessionHistoryNotFoundError),
        (SessionActivationGoneError("gone"), SessionHistoryGoneError),
        (SessionActivationUnavailableError("unavailable"), SessionHistoryUnavailableError),
        (SessionActivationUntrustedError("untrusted"), SessionHistoryUnavailableError),
    ],
)
async def test_maps_activation_errors_to_history_domain_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    activation_error: Exception,
    expected: type[Exception],
) -> None:
    _install_activation(monkeypatch, activation_error)

    with pytest.raises(expected):
        await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stat",
    [
        SandboxFileStat(path="/conversation", size=None, is_directory=True),
        SandboxFileStat(path="/conversation", size=None, is_directory=False),
        SandboxFileStat(path="/conversation", size=-1, is_directory=False),
        SandboxFileStat(
            path="/conversation",
            size=MAX_CHECKPOINT_CONVERSATION_BYTES + 1,
            is_directory=False,
        ),
    ],
)
async def test_invalid_checkpoint_stat_quarantines_verified_binding(
    monkeypatch: pytest.MonkeyPatch,
    stat: SandboxFileStat,
) -> None:
    activated, handle, store = _activated(checkpoint_name=f"checkpoint_{uuid4().hex}")

    async def invalid_stat(_path: str) -> SandboxFileStat:
        return stat

    monkeypatch.setattr(handle, "stat_file", invalid_stat)
    _install_activation(monkeypatch, activated)

    with pytest.raises(SessionHistoryUnavailableError):
        await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]

    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.quarantine_reason == "checkpoint_corrupt"
    assert handle.closed is True


@pytest.mark.asyncio
async def test_post_read_oversize_and_malformed_content_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = f"checkpoint_{uuid4().hex}"
    path = checkpoint_conversation_path(checkpoint)
    for content in (b"x" * (MAX_CHECKPOINT_CONVERSATION_BYTES + 1), b"{not json}\n"):
        activated, handle, store = _activated(checkpoint_name=checkpoint)
        handle.seed_file(path, b"small")

        async def stat_file(_path: str) -> SandboxFileStat:
            return SandboxFileStat(path=path, size=1, is_directory=False)

        async def read_file(_path: str, value: bytes = content) -> bytes:
            return value

        monkeypatch.setattr(handle, "stat_file", stat_file)
        monkeypatch.setattr(handle, "read_file", read_file)
        _install_activation(monkeypatch, activated)

        with pytest.raises(SessionHistoryUnavailableError):
            await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]

        assert store.session is not None
        assert store.session.quarantine_reason == "checkpoint_corrupt"
        assert handle.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [SandboxFileNotFoundError("missing"), SandboxFileOperationError("transient")],
)
async def test_file_read_errors_are_unavailable_without_quarantine(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    checkpoint = f"checkpoint_{uuid4().hex}"
    activated, handle, store = _activated(checkpoint_name=checkpoint)
    path = checkpoint_conversation_path(checkpoint)
    handle.seed_file(path, b"small")
    handle.read_errors.append(error)
    _install_activation(monkeypatch, activated)

    with pytest.raises(SessionHistoryUnavailableError):
        await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]

    assert store.session is not None
    assert store.session.status == "ready"
    assert handle.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [SandboxFileNotFoundError("missing"), SandboxFileOperationError("transient")],
)
async def test_checkpoint_stat_errors_are_unavailable_without_quarantine(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    checkpoint = f"checkpoint_{uuid4().hex}"
    activated, handle, store = _activated(checkpoint_name=checkpoint)

    async def stat_file(_path: str) -> SandboxFileStat:
        raise error

    monkeypatch.setattr(handle, "stat_file", stat_file)
    _install_activation(monkeypatch, activated)

    with pytest.raises(SessionHistoryUnavailableError):
        await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]

    assert store.session is not None
    assert store.session.status == "ready"
    assert handle.closed is True


@pytest.mark.asyncio
async def test_returns_resumed_metadata_and_latest_filtered_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = f"checkpoint_{uuid4().hex}"
    activated, handle, _ = _activated(checkpoint_name=checkpoint, resumed=True)
    messages = [
        Message(role="tool", contents=["ignored"]),
        *[
            Message(role="user", contents=[f"message-{index:03d}"])
            for index in range(201)
        ],
    ]
    handle.seed_file(checkpoint_conversation_path(checkpoint), _jsonl(*messages))
    _install_activation(monkeypatch, activated)

    result = await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]

    assert result.resumed is True
    assert result.truncated is True
    assert len(result.messages) == 200
    assert result.messages[0]["text"] == "message-001"
    assert result.messages[-1]["text"] == "message-200"
    assert handle.stop_calls == 0
    assert handle.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"\xff", b"[]\n"])
async def test_invalid_utf8_or_message_shape_quarantines_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
) -> None:
    checkpoint = f"checkpoint_{uuid4().hex}"
    activated, handle, store = _activated(checkpoint_name=checkpoint)
    path = checkpoint_conversation_path(checkpoint)
    handle.seed_file(path, content)
    _install_activation(monkeypatch, activated)

    with pytest.raises(SessionHistoryUnavailableError):
        await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]

    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.quarantine_reason == "checkpoint_corrupt"
    assert handle.closed is True


@pytest.mark.asyncio
async def test_corrupt_checkpoint_terminalizes_active_run_before_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = f"checkpoint_{uuid4().hex}"
    activated, handle, store = _activated(
        checkpoint_name=checkpoint,
        status="running",
        active_run_id="run-1",
    )
    now = datetime.now(UTC)
    store.runs["run-1"] = DurableRunRecord.create(
        owner_partition=activated.session.owner_partition,
        session_id=activated.session.session_id,
        run_id="run-1",
        generation=activated.session.generation,
        status="running",
        result_available=False,
        status_reason=None,
        expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )

    async def corrupt_stat(_path: str) -> SandboxFileStat:
        return SandboxFileStat(path=checkpoint, size=None, is_directory=True)

    monkeypatch.setattr(handle, "stat_file", corrupt_stat)
    _install_activation(monkeypatch, activated)

    with pytest.raises(SessionHistoryUnavailableError):
        await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]

    assert store.operations[-2:] == ["adopt", "update:quarantined"]
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.active_run_id is None
    assert handle.closed is True


@pytest.mark.asyncio
async def test_cancellation_closes_the_activated_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = f"checkpoint_{uuid4().hex}"
    activated, handle, _ = _activated(checkpoint_name=checkpoint)

    async def cancelled_stat(_path: str) -> SandboxFileStat:
        raise asyncio.CancelledError

    monkeypatch.setattr(handle, "stat_file", cancelled_stat)
    _install_activation(monkeypatch, activated)

    with pytest.raises(asyncio.CancelledError):
        await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]

    assert handle.closed is True


@pytest.mark.asyncio
async def test_concurrent_corrupt_checkpoint_reads_return_unavailable_after_stale_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ConcurrentQuarantineStore(FakeSessionStateStore):
        async def update_session(self, **kwargs: object) -> str:  # type: ignore[no-untyped-def]
            etag = kwargs["etag"]
            if etag != self.etag:
                from azure_functions_agents.session_state import ConcurrencyConflictError

                raise ConcurrencyConflictError("stale checkpoint quarantine")
            return await super().update_session(**kwargs)  # type: ignore[arg-type]

    session = _session()
    store = _ConcurrentQuarantineStore(session)
    handles = [FakeSandboxSessionHandle(), FakeSandboxSessionHandle()]
    activated_sessions = [
        ActivatedSession.create(
            handle=handle,
            session=session,
            etag=store.etag,
            partition=session.owner_partition,
            store=store,
            checkpoint_name=f"checkpoint_{uuid4().hex}",
        )
        for handle in handles
    ]
    for activated in activated_sessions:
        async def corrupt_stat(_path: str) -> SandboxFileStat:
            return SandboxFileStat(path="/conversation", size=None, is_directory=True)

        monkeypatch.setattr(activated.handle, "stat_file", corrupt_stat)

    async def activate(*_args: object, **_kwargs: object) -> ActivatedSession:
        return activated_sessions.pop(0)

    monkeypatch.setattr(history_reader, "activate_session", activate)
    results = await asyncio.gather(
        read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start()),  # type: ignore[arg-type]
        read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start()),  # type: ignore[arg-type]
        return_exceptions=True,
    )

    assert all(isinstance(result, SessionHistoryUnavailableError) for result in results)
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert all(handle.closed for handle in handles)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement", "expected_error"),
    [
        ("quarantined", SessionHistoryUnavailableError),
        ("tombstoned", SessionHistoryGoneError),
        ("deleted", SessionHistoryGoneError),
        ("epoch_changed", SessionHistoryGoneError),
    ],
)
async def test_stale_checkpoint_quarantine_rereads_owner_session_outcome(
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
    expected_error: type[Exception],
) -> None:
    class _StaleQuarantineStore(FakeSessionStateStore):
        def __init__(self, session: DurableSessionRecord) -> None:
            super().__init__(session)
            self.session_reads = 0

        async def get_session(self, partition, session_id: str):  # type: ignore[no-untyped-def]
            self.session_reads += 1
            return await super().get_session(partition, session_id)

        async def update_session(self, **kwargs: object) -> str:  # type: ignore[no-untyped-def]
            previous = kwargs["previous"]
            assert isinstance(previous, DurableSessionRecord)
            if replacement == "quarantined":
                self.session = replace(
                    previous,
                    status="quarantined",
                    quarantine_reason="checkpoint_corrupt",
                )
            elif replacement == "epoch_changed":
                self.session = replace(previous, generation=previous.generation + 1)
            else:
                self.session = replace(previous, status=replacement, active_run_id=None)
            self.etag = "etag-winner"
            from azure_functions_agents.session_state import ConcurrencyConflictError

            raise ConcurrencyConflictError("stale checkpoint quarantine")

    session = _session()
    handle = FakeSandboxSessionHandle()
    store = _StaleQuarantineStore(session)
    activated = ActivatedSession.create(
        handle=handle,
        session=session,
        etag=store.etag,
        partition=session.owner_partition,
        store=store,
        checkpoint_name=f"checkpoint_{uuid4().hex}",
    )

    async def corrupt_stat(_path: str) -> SandboxFileStat:
        return SandboxFileStat(path="/conversation", size=None, is_directory=True)

    monkeypatch.setattr(handle, "stat_file", corrupt_stat)
    _install_activation(monkeypatch, activated)

    with pytest.raises(expected_error):
        await read_session_history(_Runtime([]), _owner(), "session-1", SetupBudget.start())  # type: ignore[arg-type]

    assert store.session_reads == 1
    assert handle.closed is True
