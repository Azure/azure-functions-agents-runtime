from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from azure_functions_agents.controller.readiness import (
    SessionActivationSetupTimeoutError,
    SessionRunOwnershipChangedError,
    SessionRuntimeBinding,
    StateStoreBinding,
)
from azure_functions_agents.execution.aca_sandbox import AcaSandboxExecutionBackend
from azure_functions_agents.execution.backend import (
    AgentExecutionBackend,
    EventCursorExpiredError,
    RunContext,
    StartRunRequest,
)
from azure_functions_agents.execution.binding import AgentBinding
from azure_functions_agents.execution.run_control import (
    RunSubmissionDefinitiveFailureError,
    RunSubmissionIndeterminateError,
    SandboxRunControl,
)
from azure_functions_agents.execution.setup_budget import SetupBudget
from azure_functions_agents.registration.endpoints import _run_agent_stream
from azure_functions_agents.session_state import (
    AdmissionOutcome,
    AdmissionRecords,
    AppIdentity,
    DurableRunRecord,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    OwnerPartition,
    SessionRead,
    owner_partition,
)
from azure_functions_agents.transport.transport_models import (
    DiskSource,
    SandboxFileOperationError,
)
from tests.doubles.content_package import content_package
from tests.doubles.fake_session_runtime import (
    DEFAULT_GROUP_RESOURCE_ID,
    FakeSandboxSessionHandle,
    FakeSandboxSessionProvider,
    FakeSessionStateStore,
)
from tests.test_execution_backend import assert_event_cursor_conformance

_FINGERPRINT = "s1-" + ("a" * 52)
pytestmark = pytest.mark.usefixtures("deterministic_content_package")


def _owner() -> FunctionAppOwnerContext:
    app = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name="agent-app",
    )
    return FunctionAppOwnerContext.create(app, "main")


def _binding() -> AgentBinding:
    return AgentBinding(agent_name="main")


def _script_root(tmp_path: Path) -> Path:
    (tmp_path / "function_app.py").write_text("app = object()\n", encoding="utf-8")
    return tmp_path


def _session(script_root: Path, *, status: str = "ready") -> DurableSessionRecord:
    owner = _owner()
    package = content_package()
    now = datetime.now(UTC)
    return DurableSessionRecord.create(
        owner_partition=owner_partition(owner),
        session_id="session-1",
        sandbox_id="sandbox-1",
        generation=1,
        digest_kind=package.digest_kind,
        digest=package.digest,
        protocol="1",
        status=status,  # type: ignore[arg-type]
        last_activity_at=now,
        expires_at=now + timedelta(hours=24),
        idle_policy_armed=True,
        active_run_id=None,
        snapshot_ids=(),
        region="westus2",
        state_store_fingerprint=_FINGERPRINT,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=now,
        updated_at=now,
    )


def _run(session: DurableSessionRecord, *, state: str = "accepted") -> DurableRunRecord:
    now = datetime.now(UTC)
    return DurableRunRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        run_id="run-1",
        generation=session.generation,
        status=state,  # type: ignore[arg-type]
        result_available=False,
        status_reason=None,
        expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )


def _runtime(
    script_root: Path,
    provider: FakeSandboxSessionProvider,
    store: FakeSessionStateStore,
) -> SessionRuntimeBinding:
    async def provider_factory() -> FakeSandboxSessionProvider:
        return provider

    async def store_factory() -> StateStoreBinding:
        return StateStoreBinding.create(
            store=store,
            state_store_fingerprint=_FINGERPRINT,
        )

    return SessionRuntimeBinding.create(
        app_identity=_owner().app_identity,
        sandbox_group_resource_id=DEFAULT_GROUP_RESOURCE_ID,
        script_root=script_root,
        provider_factory=provider_factory,
        state_store_factory=store_factory,
        creation_source=DiskSource.create("test-harness"),
    )


def _status(
    *,
    state: str,
    last_sequence: int = 0,
    result_available: bool = False,
    run_id: str = "run-1",
    session_id: str = "session-1",
) -> bytes:
    return json.dumps(
        {
            "run_id": run_id,
            "session_id": session_id,
            "state": state,
            "last_sequence": last_sequence,
            "result_available": result_available,
            "error": None,
        }
    ).encode("utf-8")


def _quarantined_session(session: DurableSessionRecord) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="quarantined",
        last_activity_at=session.last_activity_at,
        expires_at=session.expires_at,
        idle_policy_armed=session.idle_policy_armed,
        active_run_id=None,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason="sandbox_manifest_mismatch",
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


class _QuarantiningSessionStore(FakeSessionStateStore):
    async def admit_run(
        self,
        records: AdmissionRecords,
        *,
        expected_session_etag: str | None = None,
    ) -> AdmissionOutcome:
        outcome = await super().admit_run(
            records,
            expected_session_etag=expected_session_etag,
        )
        failed = DurableRunRecord.create(
            owner_partition=records.run.owner_partition,
            session_id=records.run.session_id,
            run_id=records.run.run_id,
            generation=records.run.generation,
            status="failed",
            result_available=False,
            status_reason="sandbox_manifest_mismatch",
            expires_at=records.run.expires_at,
            created_at=records.run.created_at,
            updated_at=records.run.updated_at,
        )
        await self.adopt_terminal_run(failed)
        assert self.session is not None
        released = self.session
        await self.update_session(
            previous=released,
            updated=_quarantined_session(released),
            etag=self.etag,
        )
        return outcome


class _StallingAdmissionStore(FakeSessionStateStore):
    def __init__(self, session: DurableSessionRecord) -> None:
        super().__init__(session)
        self.admission_started = asyncio.Event()
        self.release_admission = asyncio.Event()

    async def admit_run(
        self,
        records: AdmissionRecords,
        *,
        expected_session_etag: str | None = None,
    ) -> AdmissionOutcome:
        self.admission_started.set()
        await self.release_admission.wait()
        return await super().admit_run(
            records,
            expected_session_etag=expected_session_etag,
        )


class _StallingRevalidationStore(FakeSessionStateStore):
    def __init__(self, session: DurableSessionRecord) -> None:
        super().__init__(session)
        self._session_reads = 0
        self.revalidation_started = asyncio.Event()
        self.release_revalidation = asyncio.Event()

    async def get_session(self, partition: OwnerPartition, session_id: str) -> SessionRead:
        self._session_reads += 1
        if self._session_reads == 2:
            self.revalidation_started.set()
            await self.release_revalidation.wait()
        return await super().get_session(partition, session_id)


@pytest.mark.asyncio
async def test_backend_satisfies_the_lifecycle_seam_and_submits_after_admission(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle("sandbox-1")
    provider = FakeSandboxSessionProvider(handle)
    store = FakeSessionStateStore()

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(
            await handle.read_file(f"/var/lib/azure-functions-agents/inbox/{run_id}.json")
        )
        handle.seed_file(
            f"/var/lib/azure-functions-agents/runs/{run_id}/status.json",
            _status(
                state="accepted",
                run_id=run_id,
                session_id=inbox["session_id"],
            ),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    assert isinstance(backend, AgentExecutionBackend)

    run_handle = await backend.start_run(StartRunRequest(prompt="hello"))

    assert run_handle.state == "accepted"
    assert store.session is not None
    assert store.session.status == "running"
    assert store.session.active_run_id == run_handle.run_id
    assert store.admission_expected_session_etags == ["etag-5"]
    assert provider.create_calls
    assert handle.closed


@pytest.mark.asyncio
async def test_backend_does_not_submit_after_a_concurrent_quarantine(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = _QuarantiningSessionStore(session)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    with pytest.raises(SessionRunOwnershipChangedError):
        await backend.start_run(
            StartRunRequest(prompt="hello", session_id=session.session_id)
        )

    assert [call for call in handle.calls if call.operation == "exec"] == []
    assert len(store.adopted) == 1
    assert store.adopted[0].status == "failed"
    assert store.adopted[0].status_reason == "sandbox_manifest_mismatch"
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.quarantine_reason == "sandbox_manifest_mismatch"
    assert store.session.active_run_id is None
    durable_run = store.runs[store.adopted[0].run_id]
    assert durable_run.status_reason == "sandbox_manifest_mismatch"


@pytest.mark.asyncio
async def test_backend_retains_admitted_slot_when_acceptance_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    original_start = SetupBudget.start
    monkeypatch.setattr(
        "azure_functions_agents.execution.aca_sandbox.SetupBudget.start",
        lambda: original_start(setup_seconds=0.05),
    )
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
        run_control=SandboxRunControl(event_poll_interval_seconds=0.001),
    )

    with pytest.raises(RunSubmissionIndeterminateError):
        await backend.start_run(StartRunRequest(prompt="hello", session_id=session.session_id))

    assert [call.operation for call in handle.calls[:3]] == [
        "read_file",
        "write_file",
        "exec",
    ]
    assert store.adopted == []
    assert store.session is not None
    assert store.session.status == "running"
    assert len(store.runs) == 1
    admitted_run = next(iter(store.runs.values()))
    assert admitted_run.status == "accepted"
    assert store.session.active_run_id == admitted_run.run_id


@pytest.mark.asyncio
async def test_backend_releases_admitted_slot_when_request_write_fails(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    handle = FakeSandboxSessionHandle()
    handle.write_errors.append(SandboxFileOperationError("request write failed"))
    provider = FakeSandboxSessionProvider(handle)
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    with pytest.raises(RunSubmissionDefinitiveFailureError):
        await backend.start_run(StartRunRequest(prompt="hello", session_id=session.session_id))

    assert [call.operation for call in handle.calls] == ["read_file", "write_file"]
    assert len(store.adopted) == 1
    assert store.adopted[0].status == "failed"
    assert store.adopted[0].status_reason == "submission_failed"
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.active_run_id is None


@pytest.mark.asyncio
async def test_backend_setup_deadline_bounds_session_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    provider = FakeSandboxSessionProvider(FakeSandboxSessionHandle())
    runtime = _runtime(script_root, provider, store)
    backend = AcaSandboxExecutionBackend(_binding(), runtime=runtime, owner=_owner())
    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()
    original_start = SetupBudget.start
    monkeypatch.setattr(
        "azure_functions_agents.execution.aca_sandbox.SetupBudget.start",
        lambda: original_start(setup_seconds=0.05),
    )

    async def hold_lock() -> None:
        async with runtime.hold_session(session.session_id):
            lock_acquired.set()
            await release_lock.wait()

    holder = asyncio.create_task(hold_lock())
    await asyncio.wait_for(lock_acquired.wait(), timeout=1.0)
    try:
        with pytest.raises(SessionActivationSetupTimeoutError):
            await asyncio.wait_for(
                backend.start_run(StartRunRequest(prompt="hello", session_id=session.session_id)),
                timeout=1.0,
            )
    finally:
        release_lock.set()
        await holder


@pytest.mark.asyncio
async def test_backend_setup_deadline_bounds_run_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = _StallingAdmissionStore(session)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    original_start = SetupBudget.start
    monkeypatch.setattr(
        "azure_functions_agents.execution.aca_sandbox.SetupBudget.start",
        lambda: original_start(setup_seconds=0.05),
    )
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    start = asyncio.create_task(
        backend.start_run(StartRunRequest(prompt="hello", session_id=session.session_id))
    )
    await asyncio.wait_for(store.admission_started.wait(), timeout=1.0)

    with pytest.raises(SessionActivationSetupTimeoutError):
        await asyncio.wait_for(start, timeout=1.0)

    assert [call for call in handle.calls if call.operation == "exec"] == []


@pytest.mark.asyncio
async def test_backend_setup_deadline_bounds_submission_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = _StallingRevalidationStore(session)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    original_start = SetupBudget.start
    monkeypatch.setattr(
        "azure_functions_agents.execution.aca_sandbox.SetupBudget.start",
        lambda: original_start(setup_seconds=0.05),
    )
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    start = asyncio.create_task(
        backend.start_run(StartRunRequest(prompt="hello", session_id=session.session_id))
    )
    await asyncio.wait_for(store.revalidation_started.wait(), timeout=1.0)

    with pytest.raises(SessionActivationSetupTimeoutError):
        await asyncio.wait_for(start, timeout=1.0)

    assert [call for call in handle.calls if call.operation == "exec"] == []


@pytest.mark.asyncio
async def test_terminal_stream_releases_slot_for_followup_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DelayedTerminalHandle(FakeSandboxSessionHandle):
        def __init__(self) -> None:
            super().__init__()
            self.event_path: str | None = None
            self.status_path: str | None = None
            self.terminal_run_id: str | None = None
            self.event_read = False
            self.status_reads_after_event = 0

        async def read_file(self, path: str) -> bytes:
            if path == self.status_path and self.event_read:
                self.status_reads_after_event += 1
                if (
                    self.status_reads_after_event == 2
                    and self.terminal_run_id is not None
                ):
                    self.seed_file(
                        path,
                        _status(
                            state="succeeded",
                            last_sequence=1,
                            run_id=self.terminal_run_id,
                        ),
                    )
            content = await super().read_file(path)
            if path == self.event_path:
                self.event_read = True
            return content

    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    handle = DelayedTerminalHandle()
    provider = FakeSandboxSessionProvider(handle)
    launched_run_ids: list[str] = []

    async def accept_then_complete(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        launched_run_ids.append(run_id)
        status_path = f"/var/lib/azure-functions-agents/runs/{run_id}/status.json"
        handle.seed_file(
            status_path,
            _status(state="accepted", run_id=run_id, session_id=session.session_id),
        )
        if len(launched_run_ids) != 1:
            handle.event_path = None
            return
        handle.event_path = f"/var/lib/azure-functions-agents/runs/{run_id}/events.jsonl"
        handle.status_path = status_path
        handle.terminal_run_id = run_id
        handle.seed_file(
            handle.event_path,
            json.dumps(
                {
                    "sequence": 1,
                    "type": "done",
                    "data": {"content": "answer"},
                    "timestamp": "2026-08-03T00:00:00+00:00",
                }
            ).encode("utf-8")
            + b"\n",
        )

    handle.exec_hook = accept_then_complete
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        lambda **_kwargs: backend,
    )

    stream = _run_agent_stream(
        prompt="hello",
        session_id=session.session_id,
        agent_name="main",
    )
    events = [event async for event in stream]

    assert events == ['data: {"type": "done", "content": "answer"}\n\n']
    assert len(store.adopted) == 1
    assert store.adopted[0].status == "succeeded"
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.active_run_id is None

    followup = await backend.start_run(
        StartRunRequest(prompt="next", session_id=session.session_id)
    )

    assert followup.run_id != launched_run_ids[0]
    assert store.session is not None
    assert store.session.status == "running"
    assert store.session.active_run_id == followup.run_id


@pytest.mark.asyncio
async def test_client_disconnect_leaves_streamed_run_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)

    async def accept_with_delta(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        handle.seed_file(
            f"/var/lib/azure-functions-agents/runs/{run_id}/status.json",
            _status(state="accepted", run_id=run_id, session_id=session.session_id),
        )
        handle.seed_file(
            f"/var/lib/azure-functions-agents/runs/{run_id}/events.jsonl",
            json.dumps(
                {
                    "sequence": 1,
                    "type": "delta",
                    "data": {"content": "partial"},
                    "timestamp": "2026-08-03T00:00:00+00:00",
                }
            ).encode("utf-8")
            + b"\n",
        )

    handle.exec_hook = accept_with_delta
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    monkeypatch.setattr(
        "azure_functions_agents.registration.endpoints.create_execution_backend",
        lambda **_kwargs: backend,
    )

    stream = _run_agent_stream(
        prompt="hello",
        session_id=session.session_id,
        agent_name="main",
    )
    first_event = await anext(stream)
    await stream.aclose()

    assert first_event == 'data: {"type": "delta", "content": "partial"}\n\n'
    assert store.adopted == []
    assert store.session is not None
    assert store.session.status == "running"
    assert store.session.active_run_id is not None
    active_run_id = store.session.active_run_id
    status = await backend.get_run(
        RunContext(run_id=active_run_id, session_id=session.session_id)
    )

    assert status.state == "accepted"
    assert store.adopted == []
    assert store.session.active_run_id == active_run_id


@pytest.mark.asyncio
async def test_backend_reads_replayable_events_and_adopts_terminal_result(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    store.runs["run-1"] = _run(session)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        "/var/lib/azure-functions-agents/runs/run-1/status.json",
        _status(state="succeeded", last_sequence=5, result_available=True),
    )
    handle.seed_file(
        "/var/lib/azure-functions-agents/runs/run-1/events.jsonl",
        (
            "\n".join(
                [
                    json.dumps(
                        {
                            "sequence": 3,
                            "type": "delta",
                            "data": {"content": "a"},
                            "timestamp": "2026-08-03T00:00:00+00:00",
                        }
                    ),
                    json.dumps(
                        {
                            "sequence": 4,
                            "type": "delta",
                            "data": {"content": "b"},
                            "timestamp": "2026-08-03T00:00:00+00:00",
                        }
                    ),
                    json.dumps(
                        {
                            "sequence": 5,
                            "type": "done",
                            "data": {},
                            "timestamp": "2026-08-03T00:00:00+00:00",
                        }
                    ),
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )
    handle.seed_file(
        "/var/lib/azure-functions-agents/runs/run-1/result.json",
        json.dumps(
            {
                "content": "answer",
                "content_intermediate": [],
                "tool_calls": [],
                "reasoning": None,
                "delegate_error_count": 0,
            }
        ).encode("utf-8"),
    )
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    context = RunContext(run_id="run-1", session_id="session-1")

    await assert_event_cursor_conformance(
        backend,
        context,
        retained_sequences=(3, 4, 5),
        earliest_available_sequence=3,
        too_old_cursor=1,
    )
    status = await backend.get_run(context)

    assert status.state == "succeeded"
    assert status.result is not None
    assert status.result.content == "answer"
    assert store.adopted[-1].status == "succeeded"
    with pytest.raises(EventCursorExpiredError):
        _ = [event async for event in backend.read_events(context, 1)]


@pytest.mark.asyncio
async def test_backend_cancels_through_the_live_handle_and_adopts_the_terminal_row(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    store.runs["run-1"] = _run(session, state="running")
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        "/var/lib/azure-functions-agents/runs/run-1/status.json",
        _status(state="running"),
    )
    handle.seed_file(
        "/var/lib/azure-functions-agents/runs/run-1/process.json",
        b'{"process_group_id":42}',
    )

    async def journal_canceled(_command: str) -> None:
        handle.seed_file(
            "/var/lib/azure-functions-agents/runs/run-1/status.json",
            _status(state="canceled"),
        )

    handle.exec_hook = journal_canceled
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    status = await backend.cancel_run(RunContext(run_id="run-1", session_id="session-1"))

    assert status.state == "canceled"
    assert store.adopted[-1].status == "canceled"
