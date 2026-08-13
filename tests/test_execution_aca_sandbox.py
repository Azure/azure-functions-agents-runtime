from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import azure_functions_agents.app as app_module
from azure_functions_agents.controller.budget import RequestBudget
from azure_functions_agents.controller.http import (
    cancel_run as cancel_controller_run,
)
from azure_functions_agents.controller.http import (
    read_result,
    read_status,
    submit_run,
)
from azure_functions_agents.controller.idempotency import IdempotencyResultUnavailableError
from azure_functions_agents.controller.package import ContentDeliveryVerificationError
from azure_functions_agents.controller.readiness import (
    ActivatedSession,
    ProvisionedSubmission,
    SessionActivationNotFoundError,
    SessionActivationSetupTimeoutError,
    SessionRunOwnershipChangedError,
    SessionRuntimeBinding,
    StateStoreBinding,
    begin_submit_operation,
    disarm_submit_lifecycle,
    session_with_admitted_run,
    terminal_run,
)
from azure_functions_agents.controller.reconciler import ReconcileReport
from azure_functions_agents.controller.streaming import render_events
from azure_functions_agents.execution.aca_sandbox import (
    AcaSandboxExecutionBackend,
    _ensure_replay_result_available,
)
from azure_functions_agents.execution.backend import (
    SESSION_TOMBSTONED_ERROR_CODE,
    AgentExecutionBackend,
    EventCursorExpiredError,
    RunContext,
    RunError,
    StartRunRequest,
)
from azure_functions_agents.execution.binding import AgentBinding
from azure_functions_agents.execution.run_control import (
    RunSubmissionDefinitiveFailureError,
    RunSubmissionIndeterminateError,
    SandboxRunControl,
)
from azure_functions_agents.execution.setup_budget import SetupBudget
from azure_functions_agents.journal_paths import (
    inbox_path,
    process_path,
    result_path,
    run_path,
    status_path,
)
from azure_functions_agents.registration.endpoints import _run_agent_stream
from azure_functions_agents.session_state import (
    ActiveRunConflictError,
    AdmissionOutcome,
    AdmissionRecords,
    AppIdentity,
    ConcurrencyConflictError,
    DurableRunRecord,
    DurableSessionOperation,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    IdempotencyConflictError,
    OwnerPartition,
    ProvisionSubmitOutcome,
    SessionOperationFence,
    SessionOperationTarget,
    SessionRead,
    operation_correlation_label,
    owner_partition,
)
from azure_functions_agents.transport.transport_models import (
    DiskSource,
    SandboxCapacityError,
    SandboxFileNotFoundError,
    SandboxFileOperationError,
    SandboxGroupAuthorizationError,
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


def _expire_provision_lease(store: FakeSessionStateStore, operation: DurableSessionOperation) -> None:
    store.durable_operations[operation.operation_id] = replace(
        operation,
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )


def _complete_provisioning(store: FakeSessionStateStore, operation: DurableSessionOperation) -> None:
    assert store.session is not None
    store.durable_operations[operation.operation_id] = replace(
        operation,
        state="completed",
        finished_at=datetime.now(UTC),
    )
    store.session = replace(
        store.session,
        status="running",
        active_operation_id=None,
    )


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
        active_operation_id=None,
        operation_sequence=0,
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
    *,
    targeted_reconciler: Callable[[OwnerPartition, str], Awaitable[None]] | None = None,
    post_create_reconciler: Callable[[], Awaitable[None]] | None = None,
    capacity_reaper: Callable[[], Awaitable[None]] | None = None,
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
        targeted_reconciler=targeted_reconciler,
        post_create_reconciler=post_create_reconciler,
        capacity_reaper=capacity_reaper,
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
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


class _QuarantiningSessionStore(FakeSessionStateStore):
    async def admit_operation_run(
        self,
        records: AdmissionRecords,
        fence,
    ) -> AdmissionOutcome:
        outcome = await super().admit_operation_run(fence=fence, records=records)
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
        assert self.session is not None
        self.adopted.append(failed)
        self.runs[failed.run_id] = failed
        self.session = _quarantined_session(self.session)
        operation = self.durable_operations[fence.operation_id]
        self.durable_operations[fence.operation_id] = replace(
            operation,
            state="aborted",
            phase="aborted",
            finished_at=failed.updated_at,
        )
        return outcome


class _StallingAdmissionStore(FakeSessionStateStore):
    def __init__(self, session: DurableSessionRecord) -> None:
        super().__init__(session)
        self.admission_started = asyncio.Event()
        self.release_admission = asyncio.Event()

    async def admit_operation_run(
        self,
        *,
        fence,
        records: AdmissionRecords,
    ) -> AdmissionOutcome:
        self.admission_started.set()
        await self.release_admission.wait()
        return await super().admit_operation_run(fence=fence, records=records)


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
            await handle.read_file(inbox_path(run_id))
        )
        handle.seed_file(
            status_path(run_id),
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
    assert store.admission_expected_session_etags == []
    assert provider.create_calls
    operation = next(iter(store.durable_operations.values()))
    assert operation.kind == "provision_submit"
    assert operation.phase == "provision_launching"
    assert operation.target.sandbox_id == store.session.sandbox_id
    assert provider.create_calls[0].labels.operation_label == operation.correlation_label
    assert handle.closed
    assert handle.lifecycle_policy.auto_suspend_seconds is None
    assert store.session.idle_policy_armed is False


@pytest.mark.asyncio
async def test_new_submit_reserves_owner_claim_run_and_operation_before_create(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle("sandbox-1")
    store = FakeSessionStateStore()

    class ReservingProvider(FakeSandboxSessionProvider):
        async def create(self, request, *, persisted_group):  # type: ignore[no-untyped-def]
            assert store.session is not None
            assert store.session.status == "creating"
            assert store.session.active_run_id is not None
            assert store.session.active_operation_id is not None
            assert store.runs
            assert store.owner_idempotency
            assert request.labels.operation_label is not None
            return await super().create(request, persisted_group=persisted_group)

    provider = ReservingProvider(handle)

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(
            await handle.read_file(inbox_path(run_id))
        )
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=inbox["session_id"]),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    await backend.start_run(StartRunRequest(prompt="hello", idempotency_key="first-call"))

    assert len(provider.create_calls) == 1
    assert store.session is not None
    owner_key = next(iter(store.owner_idempotency.values()))
    assert owner_key.expires_at >= store.session.expires_at


@pytest.mark.asyncio
async def test_replayed_provision_never_submits_local_loser_identifiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_root = _script_root(tmp_path)
    candidate_session = _session(script_root)
    candidate_store = FakeSessionStateStore(candidate_session)
    candidate_handle = FakeSandboxSessionHandle("candidate-sandbox")
    candidate = ActivatedSession.create(
        handle=candidate_handle,
        session=candidate_session,
        etag=candidate_store.etag,
        partition=candidate_session.owner_partition,
        store=candidate_store,
    )
    winner = DurableRunRecord.create(
        owner_partition=candidate_session.owner_partition,
        session_id="winner-session",
        run_id="winner-run",
        generation=1,
        status="failed",
        result_available=False,
        status_reason="submission_failed",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    async def replayed_provision(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return ProvisionedSubmission(
            outcome=ProvisionSubmitOutcome(
                run=winner,
                run_etag="winner-etag",
                session_etag=None,
                fence=None,
                replayed=True,
            ),
            activated=candidate,
        )

    monkeypatch.setattr(
        "azure_functions_agents.execution.aca_sandbox.provision_new_session_submit",
        replayed_provision,
    )
    provider = FakeSandboxSessionProvider(candidate_handle)
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, candidate_store),
        owner=_owner(),
    )

    replay = await backend.start_run(
        StartRunRequest(prompt="hello", idempotency_key="winner-key")
    )

    assert replay.run_id == "winner-run"
    assert replay.session_id == "winner-session"
    assert candidate_handle.closed
    assert [call for call in candidate_handle.calls if call.operation in {"exec", "write_file"}] == []


@pytest.mark.asyncio
async def test_new_submit_recovers_an_ambiguous_stable_label_create(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle("sandbox-1")
    store = FakeSessionStateStore()

    class AmbiguousProvider(FakeSandboxSessionProvider):
        def __init__(self, handle: FakeSandboxSessionHandle) -> None:
            super().__init__(handle)
            self.fail_once = True

        async def create(self, request, *, persisted_group):  # type: ignore[no-untyped-def]
            if self.fail_once:
                self.fail_once = False
                self.create_calls.append(request)
                self.handle.labels = request.labels.to_provider_labels()
                self.sandboxes[self.handle.identity.sandbox_id] = self.handle
                raise SandboxFileOperationError("ambiguous create response")
            return await super().create(request, persisted_group=persisted_group)

    provider = AmbiguousProvider(handle)

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(
            await handle.read_file(inbox_path(run_id))
        )
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=inbox["session_id"]),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    with pytest.raises(SessionActivationSetupTimeoutError):
        await backend.start_run(StartRunRequest(prompt="hello", idempotency_key="retryable"))

    _expire_provision_lease(store, next(iter(store.durable_operations.values())))
    recovered = await backend.start_run(
        StartRunRequest(prompt="hello", idempotency_key="retryable")
    )

    assert recovered.state == "accepted"
    assert len(provider.sandboxes) == 1
    labels = next(iter(provider.sandboxes.values())).labels
    assert labels["operation_label"].startswith("op-")


@pytest.mark.asyncio
async def test_duplicate_submit_reuses_run_after_launch_response_loss(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)

    class LostResponseRunControl(SandboxRunControl):
        def __init__(self) -> None:
            super().__init__()
            self.lose_once = True

        async def submit(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            status = await super().submit(*args, **kwargs)
            if self.lose_once:
                self.lose_once = False
                raise RunSubmissionIndeterminateError("launch response was lost")
            return status

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(
            await handle.read_file(inbox_path(run_id))
        )
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=inbox["session_id"]),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
        run_control=LostResponseRunControl(),
    )

    request = StartRunRequest(
        prompt="hello",
        session_id=session.session_id,
        idempotency_key="same-run",
    )
    with pytest.raises(RunSubmissionIndeterminateError):
        await backend.start_run(request)

    replay = await backend.start_run(request)

    assert replay.state == "accepted"
    assert len([call for call in handle.calls if call.operation == "exec"]) == 1


@pytest.mark.asyncio
async def test_concurrent_retry_cannot_take_an_unexpired_journal_launch(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    launch_written = asyncio.Event()
    release_launch = asyncio.Event()

    class LaunchGateHandle(FakeSandboxSessionHandle):
        async def write_file(self, path, content, *, create_dirs=False):  # type: ignore[no-untyped-def]
            await super().write_file(path, content, create_dirs=create_dirs)
            if "/inbox/" in path:
                launch_written.set()

    handle = LaunchGateHandle()
    provider = FakeSandboxSessionProvider(handle)
    runtime = _runtime(script_root, provider, store)
    activated = ActivatedSession.create(
        handle=handle,
        session=session,
        etag=store.etag,
        partition=session.owner_partition,
        store=store,
    )
    run = _run(session)
    prepared, fence = await begin_submit_operation(activated, run)
    prepared, fence = await disarm_submit_lifecycle(runtime, prepared, fence)
    admitted = session_with_admitted_run(
        prepared.session,
        run.run_id,
        updated_at=run.updated_at,
    )
    await store.admit_operation_run(
        fence=fence,
        records=AdmissionRecords.create(admitted, run),
    )

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        await release_launch.wait()
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=run.session_id),
        )

    handle.exec_hook = accept
    first_backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=runtime,
        owner=_owner(),
    )
    second_backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=runtime,
        owner=_owner(),
    )
    request = StartRunRequest(
        prompt="hello",
        session_id=session.session_id,
        idempotency_key="retry-race",
    )
    first = asyncio.create_task(
        first_backend._resume_journal_submission(run, request, SetupBudget.start())
    )
    await asyncio.wait_for(launch_written.wait(), timeout=1.0)

    with pytest.raises(ActiveRunConflictError):
        await second_backend._resume_journal_submission(run, request, SetupBudget.start())

    release_launch.set()
    result = await first

    assert result.run_id == run.run_id
    assert len([call for call in handle.calls if call.operation == "exec"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [None, 409, 423, 425, 429, 500, 502, 503, 504])
async def test_retryable_provision_content_failure_leaves_a_resumable_operation(
    tmp_path: Path,
    status_code: int | None,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle("sandbox-1")
    handle.write_errors.append(
        SandboxFileOperationError("content write failed", status_code=status_code)
    )
    provider = FakeSandboxSessionProvider(handle)
    store = FakeSessionStateStore()

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(
            await handle.read_file(inbox_path(run_id))
        )
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=inbox["session_id"]),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    request = StartRunRequest(prompt="hello", idempotency_key="content-retry")

    with pytest.raises(SessionActivationSetupTimeoutError):
        await backend.start_run(request)

    assert store.session is not None
    operation = next(iter(store.durable_operations.values()))
    assert store.session.active_operation_id == operation.operation_id
    assert operation.phase == "provision_content"
    assert store.session.active_run_id == operation.target.run_id
    assert store.session.sandbox_id == operation.target.sandbox_id

    _expire_provision_lease(store, operation)
    recovered = await backend.start_run(request)

    assert recovered.session_id == operation.target.session_id
    assert recovered.run_id == operation.target.run_id
    assert recovered.state == "accepted"
    assert len(provider.sandboxes) == 1
    assert len(provider.create_calls) == 1
    assert len([call for call in handle.calls if call.operation == "exec"]) == 1


@pytest.mark.asyncio
async def test_missing_provision_artifact_leaves_a_resumable_operation(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle("sandbox-1")
    handle.write_errors.append(SandboxFileNotFoundError("/sandbox/runtime/content"))
    provider = FakeSandboxSessionProvider(handle)
    store = FakeSessionStateStore()

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(await handle.read_file(inbox_path(run_id)))
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=inbox["session_id"]),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    request = StartRunRequest(prompt="hello", idempotency_key="missing-artifact-retry")

    with pytest.raises(SessionActivationSetupTimeoutError):
        await backend.start_run(request)

    assert store.session is not None
    operation = next(iter(store.durable_operations.values()))
    assert operation.phase == "provision_content"
    assert store.session.active_run_id == operation.target.run_id
    assert store.session.sandbox_id == operation.target.sandbox_id

    _expire_provision_lease(store, operation)
    recovered = await backend.start_run(request)

    assert recovered.session_id == operation.target.session_id
    assert recovered.run_id == operation.target.run_id
    assert len(provider.sandboxes) == 1
    assert len(provider.create_calls) == 1
    assert len([call for call in handle.calls if call.operation == "exec"]) == 1


@pytest.mark.asyncio
async def test_nonretryable_provision_content_failure_remains_fatal(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle("sandbox-1")
    handle.write_errors.append(SandboxFileOperationError("content write rejected", status_code=400))
    provider = FakeSandboxSessionProvider(handle)
    store = FakeSessionStateStore()
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    with pytest.raises(SandboxFileOperationError):
        await backend.start_run(StartRunRequest(prompt="hello", idempotency_key="content-rejected"))


@pytest.mark.asyncio
async def test_content_delivery_verification_failure_is_not_reclassified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle("sandbox-1")
    provider = FakeSandboxSessionProvider(handle)
    store = FakeSessionStateStore()

    async def fail_verification(*_: object, **__: object) -> None:
        raise ContentDeliveryVerificationError("content verification failed")

    monkeypatch.setattr(
        "azure_functions_agents.controller.readiness.deliver_content_and_bootstrap",
        fail_verification,
    )
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    with pytest.raises(ContentDeliveryVerificationError):
        await backend.start_run(StartRunRequest(prompt="hello", idempotency_key="content-integrity"))


@pytest.mark.asyncio
async def test_provision_lifecycle_failure_leaves_a_resumable_operation(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)

    class FailOnceLifecycleHandle(FakeSandboxSessionHandle):
        def __init__(self) -> None:
            super().__init__("sandbox-1")
            self.fail_once = True

        async def set_lifecycle_policy(self, policy):  # type: ignore[no-untyped-def]
            if self.fail_once:
                self.fail_once = False
                raise SandboxFileOperationError("lifecycle apply failed")
            await super().set_lifecycle_policy(policy)

    handle = FailOnceLifecycleHandle()
    provider = FakeSandboxSessionProvider(handle)
    store = FakeSessionStateStore()

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(
            await handle.read_file(inbox_path(run_id))
        )
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=inbox["session_id"]),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    request = StartRunRequest(prompt="hello", idempotency_key="lifecycle-retry")

    with pytest.raises(SessionActivationSetupTimeoutError):
        await backend.start_run(request)

    operation = next(iter(store.durable_operations.values()))
    assert operation.phase == "provision_lifecycle"
    assert store.session is not None
    assert store.session.active_operation_id == operation.operation_id

    _expire_provision_lease(store, operation)
    recovered = await backend.start_run(request)

    assert recovered.state == "accepted"


@pytest.mark.asyncio
async def test_provision_manifest_failure_leaves_a_resumable_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle("sandbox-1")
    provider = FakeSandboxSessionProvider(handle)
    store = FakeSessionStateStore()
    original_wait = (
        __import__(
            "azure_functions_agents.controller.readiness",
            fromlist=["_wait_for_created_manifest"],
        )._wait_for_created_manifest
    )
    failed_once = False

    async def fail_once(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise SessionActivationSetupTimeoutError("manifest unavailable")
        await original_wait(*args, **kwargs)

    monkeypatch.setattr(
        "azure_functions_agents.controller.readiness._wait_for_created_manifest",
        fail_once,
    )

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(
            await handle.read_file(inbox_path(run_id))
        )
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=inbox["session_id"]),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    request = StartRunRequest(prompt="hello", idempotency_key="manifest-retry")

    with pytest.raises(SessionActivationSetupTimeoutError):
        await backend.start_run(request)

    operation = next(iter(store.durable_operations.values()))
    assert operation.phase == "provision_manifest"

    _expire_provision_lease(store, operation)
    recovered = await backend.start_run(request)

    assert recovered.state == "accepted"


@pytest.mark.asyncio
async def test_new_session_creation_awaits_bounded_post_create_reconciliation(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle("sandbox-1")
    provider = FakeSandboxSessionProvider(handle)
    store = FakeSessionStateStore()
    cleanup_calls = 0

    async def post_create_cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(
            await handle.read_file(inbox_path(run_id))
        )
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=inbox["session_id"]),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(
            script_root,
            provider,
            store,
            post_create_reconciler=post_create_cleanup,
        ),
        owner=_owner(),
    )

    await backend.start_run(StartRunRequest(prompt="hello"))

    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_capacity_failure_reaps_once_before_retrying_new_session_creation(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle("sandbox-1")
    provider = FakeSandboxSessionProvider(handle)
    provider.create_errors.append(SandboxCapacityError("capacity exhausted"))
    store = FakeSessionStateStore()
    reap_calls = 0

    async def reap_capacity() -> None:
        nonlocal reap_calls
        reap_calls += 1

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(
            await handle.read_file(inbox_path(run_id))
        )
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=inbox["session_id"]),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(
            script_root,
            provider,
            store,
            capacity_reaper=reap_capacity,
        ),
        owner=_owner(),
    )

    await backend.start_run(StartRunRequest(prompt="hello"))

    assert reap_calls == 1
    assert len(provider.create_calls) == 2


@pytest.mark.asyncio
async def test_active_conflict_reconciles_once_before_returning_or_admitting(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    base_session = _session(script_root)
    session = session_with_admitted_run(
        base_session,
        "run-1",
        updated_at=datetime.now(UTC),
    )
    active_run = _run(base_session, state="accepted")
    store = FakeSessionStateStore(session)
    store.runs[active_run.run_id] = active_run
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    reconcile_calls = 0

    async def targeted_reconcile(_: OwnerPartition, __: str) -> None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        if reconcile_calls == 2:
            await store.adopt_terminal_run(
                terminal_run(
                    active_run,
                    status="abandoned",
                    result_available=False,
                    reason="verified_harness_death",
                    updated_at=datetime.now(UTC),
                )
            )

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=session.session_id),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(
            script_root,
            provider,
            store,
            targeted_reconciler=targeted_reconcile,
        ),
        owner=_owner(),
    )

    admitted = await backend.start_run(
        StartRunRequest(prompt="next", session_id=session.session_id)
    )

    assert reconcile_calls == 2
    assert admitted.run_id != active_run.run_id


@pytest.mark.asyncio
async def test_nonterminal_status_poll_uses_targeted_reconciliation(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    run = _run(session)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    calls = 0
    handle.seed_file(
        status_path("run-1"),
        _status(state="running"),
    )

    async def targeted_reconcile(_: OwnerPartition, __: str) -> None:
        nonlocal calls
        calls += 1

    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(
            script_root,
            provider,
            store,
            targeted_reconciler=targeted_reconcile,
        ),
        owner=_owner(),
    )

    status = await backend.get_run(RunContext(run_id=run.run_id, session_id=run.session_id))

    assert status.state == "running"
    assert calls == 1


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

    operations = [call.operation for call in handle.calls]
    assert operations.count("read_file") >= 3
    assert operations.index("write_file") < operations.index("exec")
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

    operations = [call.operation for call in handle.calls]
    assert operations[-1] == "write_file"
    assert operations.count("read_file") >= 3
    assert len(store.adopted) == 1
    assert store.adopted[0].status == "failed"
    assert store.adopted[0].status_reason == "submission_failed"
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.active_run_id is None
    assert store.session.active_operation_id is None
    assert next(iter(store.durable_operations.values())).state == "completed"
    assert store.session.idle_policy_armed
    assert handle.lifecycle_policy.auto_suspend_seconds == 300


@pytest.mark.asyncio
async def test_admission_conflict_restores_idle_policy_when_no_slot_is_held(tmp_path: Path) -> None:
    class ConflictingStore(FakeSessionStateStore):
        async def admit_operation_run(
            self,
            *,
            fence,
            records: AdmissionRecords,
        ) -> AdmissionOutcome:
            del fence, records
            raise ConcurrencyConflictError("admission lost")

    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = ConflictingStore(session)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    with pytest.raises(ConcurrencyConflictError):
        await backend.start_run(StartRunRequest(prompt="hello", session_id=session.session_id))

    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.idle_policy_armed
    assert handle.lifecycle_policy_history[-2].auto_suspend_seconds is None
    assert handle.lifecycle_policy_history[-1].auto_suspend_seconds == 300


@pytest.mark.asyncio
async def test_replayed_admission_restores_idle_policy_when_winner_is_terminal(tmp_path: Path) -> None:
    class ReplayingStore(FakeSessionStateStore):
        async def admit_operation_run(
            self,
            *,
            fence,
            records: AdmissionRecords,
        ) -> AdmissionOutcome:
            del fence, records
            return AdmissionOutcome(
                run=self.runs["run-1"],
                run_etag="run-etag",
                session_etag=None,
                replayed=True,
            )

    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = ReplayingStore(session)
    store.runs["run-1"] = replace(
        _run(session, state="succeeded"),
        result_available=True,
    )
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    replay = await backend.start_run(
        StartRunRequest(prompt="hello", session_id=session.session_id)
    )

    assert replay.run_id == "run-1"
    assert store.session is not None
    assert store.session.idle_policy_armed
    assert handle.lifecycle_policy_history[-2].auto_suspend_seconds is None
    assert handle.lifecycle_policy_history[-1].auto_suspend_seconds == 300


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
        async with runtime.hold_session(owner_partition(_owner()), session.session_id):
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
                            result_available=True,
                            run_id=self.terminal_run_id,
                        ),
                    )
                    self.seed_file(
                        result_path(self.terminal_run_id),
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
        journal_status_path = status_path(run_id)
        handle.seed_file(
            journal_status_path,
            _status(state="accepted", run_id=run_id, session_id=session.session_id),
        )
        if len(launched_run_ids) != 1:
            handle.event_path = None
            return
        handle.event_path = f"{run_path(run_id)}/events.jsonl"
        handle.status_path = journal_status_path
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
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=session.session_id),
        )
        handle.seed_file(
            f"{run_path(run_id)}/events.jsonl",
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
        status_path("run-1"),
        _status(state="succeeded", last_sequence=5, result_available=True),
    )
    handle.seed_file(
        f"{run_path('run-1')}/events.jsonl",
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
        result_path("run-1"),
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
async def test_durable_result_eviction_masks_a_live_success_result_without_resurrection(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    run = replace(_run(session, state="succeeded"), result_available=False)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        status_path("run-1"),
        _status(state="succeeded", result_available=True),
    )
    handle.seed_file(
        result_path("run-1"),
        json.dumps(
            {
                "content": "stale live result",
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
    context = RunContext(run_id=run.run_id, session_id=run.session_id)

    first_status = await read_status(backend, context)
    first_result = await read_result(backend, context)
    second_status = await read_status(backend, context)
    second_result = await read_result(backend, context)

    assert first_status.status_code == second_status.status_code == 200
    assert isinstance(first_status.body, dict)
    assert isinstance(second_status.body, dict)
    assert first_status.body["result_available"] is False
    assert second_status.body["result_available"] is False
    assert first_result.status_code == second_result.status_code == 410
    assert provider.attach_calls == 0
    assert store.runs[run.run_id].result_available is False


@pytest.mark.asyncio
async def test_durable_success_fallback_without_materialized_result_is_retryable(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = replace(
        _session(script_root),
        status="quarantined",
        quarantine_reason="journal_corrupt",
    )
    initial = _run(session, state="running")
    run = DurableRunRecord.create(
        owner_partition=initial.owner_partition,
        session_id=initial.session_id,
        run_id=initial.run_id,
        generation=initial.generation,
        status="succeeded",
        result_available=True,
        status_reason=None,
        expires_at=initial.expires_at,
        created_at=initial.created_at,
        updated_at=initial.updated_at,
        agent_slug=initial.agent_slug,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(
            script_root,
            FakeSandboxSessionProvider(FakeSandboxSessionHandle()),
            store,
        ),
        owner=_owner(),
    )

    response = await read_result(
        backend,
        RunContext(run_id=run.run_id, session_id=run.session_id),
    )

    assert response.status_code == 503
    assert response.body == {
        "error": "result_temporarily_unavailable",
        "state": "succeeded",
    }
    assert response.headers == {"Retry-After": "2"}


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
        status_path("run-1"),
        _status(state="running"),
    )
    handle.seed_file(
        process_path("run-1"),
        b'{"process_group_id":42}',
    )

    async def journal_canceled(_command: str) -> None:
        handle.seed_file(
            status_path("run-1"),
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
    assert not status.result_available
    assert status.error is None
    assert store.adopted[-1].status == "canceled"


@pytest.mark.asyncio
async def test_cancel_natural_success_with_invalid_output_returns_failed_projection(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    run = _run(session, state="running")
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        status_path("run-1"),
        _status(state="succeeded", result_available=True),
    )
    handle.seed_file(
        result_path("run-1"),
        json.dumps(
            {
                "content": "not-valid",
                "content_intermediate": [],
                "tool_calls": [],
                "reasoning": None,
                "delegate_error_count": 0,
            }
        ).encode("utf-8"),
    )
    backend = AcaSandboxExecutionBackend(
        AgentBinding(
            agent_name="main",
            output_validator=lambda _: RunError(
                code="response_validation_failed",
                message="invalid",
                fault_domain="app",
            ),
        ),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    status = await backend.cancel_run(RunContext(run_id=run.run_id, session_id=run.session_id))

    assert status.state == "failed"
    assert not status.result_available
    assert status.result is None
    assert status.error is not None
    assert status.error.code == "response_validation_failed"
    assert store.runs[run.run_id].status == "failed"
    assert store.runs[run.run_id].status_reason == "response_validation_failed"


@pytest.mark.asyncio
async def test_cancel_natural_success_preserves_a_valid_result(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    run = _run(session, state="running")
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        status_path("run-1"),
        _status(state="succeeded", result_available=True),
    )
    handle.seed_file(
        result_path("run-1"),
        json.dumps(
            {
                "content": '{"answer":"ok"}',
                "content_intermediate": [],
                "tool_calls": [],
                "reasoning": None,
                "delegate_error_count": 0,
            }
        ).encode("utf-8"),
    )
    backend = AcaSandboxExecutionBackend(
        AgentBinding(
            agent_name="main",
            output_validator=lambda _: None,
        ),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    status = await backend.cancel_run(RunContext(run_id=run.run_id, session_id=run.session_id))

    assert status.state == "succeeded"
    assert status.result_available
    assert status.result is not None
    assert status.result.content == '{"answer":"ok"}'
    assert store.runs[run.run_id].status == "succeeded"


@pytest.mark.asyncio
async def test_management_cancel_maps_unknown_session_and_run_through_aca_backend(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    unknown_session_backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(
            script_root,
            FakeSandboxSessionProvider(FakeSandboxSessionHandle()),
            FakeSessionStateStore(),
        ),
        owner=_owner(),
    )

    unknown_session = await cancel_controller_run(
        unknown_session_backend,
        RunContext(run_id="run-1", session_id="missing-session"),
    )

    session = _session(script_root)
    unknown_run_backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(
            script_root,
            FakeSandboxSessionProvider(FakeSandboxSessionHandle()),
            FakeSessionStateStore(session),
        ),
        owner=_owner(),
    )
    unknown_run = await cancel_controller_run(
        unknown_run_backend,
        RunContext(run_id="missing-run", session_id=session.session_id),
    )

    assert unknown_session.status_code == 404
    assert unknown_session.body == {"error": "run_not_found"}
    assert unknown_run.status_code == 404
    assert unknown_run.body == {"error": "run_not_found"}


@pytest.mark.asyncio
async def test_management_cancel_maps_tombstoned_session_through_aca_backend(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = replace(
        _session(script_root),
        status="tombstoned",
        tombstone_reason="sandbox_backing_lost",
    )
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(
            script_root,
            FakeSandboxSessionProvider(FakeSandboxSessionHandle()),
            FakeSessionStateStore(session),
        ),
        owner=_owner(),
    )

    response = await cancel_controller_run(
        backend,
        RunContext(run_id="run-1", session_id=session.session_id),
    )

    assert response.status_code == 410
    assert response.body == {"error": "session_gone"}


@pytest.mark.asyncio
async def test_new_session_owner_idempotency_replays_winner_and_rejects_payload_reuse(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    store = FakeSessionStateStore()

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(
            await handle.read_file(inbox_path(run_id))
        )
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=inbox["session_id"]),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    winner = await backend.start_run(
        StartRunRequest(prompt="hello", idempotency_key="caller-key")
    )
    _complete_provisioning(store, next(iter(store.durable_operations.values())))
    replay = await backend.start_run(
        StartRunRequest(prompt="hello", idempotency_key="caller-key")
    )

    assert replay.run_id == winner.run_id
    assert len(provider.create_calls) == 1
    assert len([call for call in handle.calls if call.operation == "exec"]) == 1
    with pytest.raises(IdempotencyConflictError):
        await backend.start_run(
            StartRunRequest(prompt="different", idempotency_key="caller-key")
        )


@pytest.mark.asyncio
async def test_new_session_authorization_failure_replays_same_terminal_response(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    provider.create_errors.append(SandboxGroupAuthorizationError())
    store = FakeSessionStateStore()
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    request = StartRunRequest(
        prompt="hello",
        idempotency_key="authorization-failure-key",
    )

    first = await submit_run(
        backend,
        request,
        agent_slug="main",
        respond_async=True,
        budget=RequestBudget.start(authored_timeout=None),
    )
    replay = await submit_run(
        backend,
        request,
        agent_slug="main",
        respond_async=True,
        budget=RequestBudget.start(authored_timeout=None),
    )

    expected_body = {
        "error": "sandbox_group_authorization_failed",
        "reason": "sandbox_group_authorization_failed",
        "message": (
            "Sandbox Group data-plane authorization failed. Grant the controller "
            "identity 'Container Apps SandboxGroup Data Owner' on the configured "
            "Sandbox Group."
        ),
    }
    assert first.status_code == 503
    assert first.body == expected_body
    assert replay.status_code == 503
    assert replay.body == expected_body
    assert len(provider.create_calls) == 1
    assert store.session is not None
    assert store.session.status == "deleting"
    assert store.session.active_run_id is None
    assert store.session.active_operation_id is None
    [run] = store.runs.values()
    assert run.status == "failed"
    assert run.status_reason == "sandbox_group_authorization_failed"
    [operation] = store.durable_operations.values()
    assert operation.state == "completed"


@pytest.mark.asyncio
async def test_live_same_key_provision_replay_does_not_take_over_or_double_create(
    tmp_path: Path,
) -> None:
    class _BlockingProvider(FakeSandboxSessionProvider):
        def __init__(self, handle: FakeSandboxSessionHandle) -> None:
            super().__init__(handle)
            self.create_started = asyncio.Event()
            self.release_create = asyncio.Event()
            self.create_attempts = 0

        async def create(
            self,
            *args: object,
            **kwargs: object,
        ) -> FakeSandboxSessionHandle:
            self.create_attempts += 1
            self.create_started.set()
            await self.release_create.wait()
            return await super().create(*args, **kwargs)

    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle()
    provider = _BlockingProvider(handle)
    store = FakeSessionStateStore()

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=next(iter(store.runs.values())).session_id),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    request = StartRunRequest(prompt="hello", idempotency_key="same-key")

    first = asyncio.create_task(backend.start_run(request))
    await asyncio.wait_for(provider.create_started.wait(), timeout=1.0)
    [operation_before] = store.durable_operations.values()

    with pytest.raises(SessionActivationSetupTimeoutError):
        await backend.start_run(request)

    assert provider.create_attempts == 1
    assert store.durable_operations[operation_before.operation_id].token == operation_before.token
    assert [call for call in handle.calls if call.operation == "exec"] == []

    provider.release_create.set()
    winner = await first
    _complete_provisioning(store, store.durable_operations[operation_before.operation_id])
    observed = await backend.start_run(request)

    assert winner.run_id == observed.run_id
    assert provider.create_attempts == 1
    assert len([call for call in handle.calls if call.operation == "exec"]) == 1


@pytest.mark.asyncio
async def test_canceled_provision_replay_waits_for_lease_then_resumes_same_run(
    tmp_path: Path,
) -> None:
    class _BlockingProvider(FakeSandboxSessionProvider):
        def __init__(self, handle: FakeSandboxSessionHandle) -> None:
            super().__init__(handle)
            self.create_started = asyncio.Event()
            self.release_create = asyncio.Event()
            self.create_attempts = 0

        async def create(
            self,
            *args: object,
            **kwargs: object,
        ) -> FakeSandboxSessionHandle:
            self.create_attempts += 1
            created = await super().create(*args, **kwargs)
            self.create_started.set()
            await self.release_create.wait()
            return created

    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle()
    provider = _BlockingProvider(handle)
    store = FakeSessionStateStore()

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=next(iter(store.runs.values())).session_id),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    request = StartRunRequest(prompt="hello", idempotency_key="canceled-provision-key")

    first = asyncio.create_task(backend.start_run(request))
    await asyncio.wait_for(provider.create_started.wait(), timeout=1.0)
    [operation] = store.durable_operations.values()
    assert len(provider.create_calls) == 1
    assert handle.labels["operation_label"] == operation.correlation_label
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    with pytest.raises(SessionActivationSetupTimeoutError):
        await backend.start_run(request)
    assert provider.create_attempts == 1
    assert store.durable_operations[operation.operation_id].token == operation.token

    store.durable_operations[operation.operation_id] = replace(
        operation,
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    provider.release_create.set()
    resumed = await backend.start_run(request)

    assert resumed.session_id == operation.target.session_id
    assert resumed.run_id == operation.target.run_id
    assert provider.create_attempts == 2
    assert len(provider.create_calls) == 1
    assert handle.labels["operation_label"] == operation.correlation_label
    assert len([call for call in handle.calls if call.operation == "exec"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("respond_async", [False, True])
async def test_existing_session_evicted_success_replay_returns_gone_without_launching(
    respond_async: bool,
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        handle.seed_file(
            status_path(run_id),
            _status(state="accepted", run_id=run_id, session_id=session.session_id),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    request = StartRunRequest(
        prompt="hello",
        session_id=session.session_id,
        idempotency_key="evicted-success",
    )
    first = await backend.start_run(request)
    store.runs[first.run_id] = replace(
        store.runs[first.run_id],
        status="succeeded",
        result_available=False,
    )

    response = await submit_run(
        backend,
        request,
        agent_slug="main",
        respond_async=respond_async,
        budget=RequestBudget.start(authored_timeout=None),
    )

    assert response.status_code == 410
    assert response.body == {"error": "result_unavailable"}
    assert len([call for call in handle.calls if call.operation == "exec"]) == 1


@pytest.mark.parametrize(
    ("status", "result_available", "raises"),
    [
        ("succeeded", False, True),
        ("succeeded", True, False),
        ("failed", False, False),
        ("accepted", False, False),
    ],
)
def test_replay_result_guard_only_rejects_evicted_success(
    status: str,
    result_available: bool,
    raises: bool,
    tmp_path: Path,
) -> None:
    run = replace(
        _run(_session(_script_root(tmp_path))),
        status=status,
        result_available=result_available,
    )

    if raises:
        with pytest.raises(IdempotencyResultUnavailableError):
            _ensure_replay_result_available(run)
    else:
        _ensure_replay_result_available(run)


@pytest.mark.asyncio
async def test_controller_output_validation_terminalizes_async_success_as_failed(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    run = _run(session)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        status_path("run-1"),
        _status(state="succeeded", result_available=True),
    )
    handle.seed_file(
        result_path("run-1"),
        json.dumps(
            {
                "content": "not-json",
                "content_intermediate": [],
                "tool_calls": [],
                "reasoning": None,
                "delegate_error_count": 0,
            }
        ).encode("utf-8"),
    )
    backend = AcaSandboxExecutionBackend(
        AgentBinding(
            agent_name="main",
            output_validator=lambda _: RunError(
                code="response_validation_failed",
                message="invalid",
                fault_domain="app",
            ),
        ),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    status = await backend.get_run(RunContext(run_id="run-1", session_id="session-1"))

    assert status.state == "failed"
    assert status.error is not None
    assert status.error.code == "response_validation_failed"
    assert store.runs["run-1"].status == "failed"


@pytest.mark.asyncio
async def test_malformed_status_quarantines_after_terminalizing_the_run(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    base = _session(script_root)
    initial = _run(base, state="running")
    run = DurableRunRecord.create(
        owner_partition=initial.owner_partition,
        session_id=initial.session_id,
        run_id=initial.run_id,
        generation=initial.generation,
        status="succeeded",
        result_available=True,
        status_reason=None,
        expires_at=initial.expires_at,
        created_at=initial.created_at,
        updated_at=initial.updated_at,
        agent_slug=initial.agent_slug,
    )
    store = FakeSessionStateStore(base)
    store.runs[run.run_id] = run
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        status_path("run-1"),
        b'{"run_id":"run-1","run_id":"secret","session_id":"session-1"}',
    )
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    context = RunContext(run_id=run.run_id, session_id=run.session_id)

    status = await backend.get_run(context)
    status_response = await read_status(backend, context)
    result_response = await read_result(backend, context)
    retry_response = await submit_run(
        backend,
        StartRunRequest(prompt="retry", session_id=base.session_id),
        agent_slug="main",
        respond_async=True,
        budget=RequestBudget.start(authored_timeout=None),
    )
    retry_stream = [
        frame
        async for frame in render_events(
            backend,
            context,
            after_sequence=0,
        )
    ]

    assert status.state == "failed"
    assert status.error is not None
    assert status.error.code == "journal_corrupt"
    assert "secret" not in status.error.message
    assert store.runs[run.run_id].status == "failed"
    assert store.runs[run.run_id].status_reason == "journal_corrupt"
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.quarantine_reason == "journal_corrupt"
    assert store.operations[:2] == ["invalidate_journal", "update:quarantined"]
    assert status_response.status_code == 200
    assert isinstance(status_response.body, dict)
    assert status_response.body["error"]["code"] == "journal_corrupt"
    assert result_response.status_code == 410
    assert retry_response.status_code == 404
    assert len(retry_stream) == 1
    assert "journal_corrupt" in retry_stream[0]


@pytest.mark.asyncio
async def test_missing_advertised_journal_result_quarantines_management_status(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    base = _session(script_root)
    run = _run(base, state="running")
    session = session_with_admitted_run(base, run.run_id, updated_at=run.updated_at)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        status_path("run-1"),
        _status(state="succeeded", result_available=True),
    )
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    context = RunContext(run_id=run.run_id, session_id=run.session_id)

    response = await read_status(backend, context)
    repeated = await read_status(backend, context)
    result = await read_result(backend, context)

    assert response.status_code == 200
    assert isinstance(response.body, dict)
    assert response.body["error"]["code"] == "journal_corrupt"
    assert repeated.status_code == 200
    assert isinstance(repeated.body, dict)
    assert repeated.body["error"]["code"] == "journal_corrupt"
    assert store.runs[run.run_id].status == "failed"
    assert not store.runs[run.run_id].result_available
    assert result.status_code == 410
    assert store.session is not None
    assert store.session.status == "quarantined"


@pytest.mark.asyncio
async def test_corrupt_event_stream_emits_redacted_terminal_error_and_closes(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    base = _session(script_root)
    run = _run(base, state="running")
    session = session_with_admitted_run(base, run.run_id, updated_at=run.updated_at)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        status_path("run-1"),
        _status(state="running"),
    )
    handle.seed_file(
        f"{run_path('run-1')}/events.jsonl",
        b'{"sequence":1,"sequence":2,"type":"delta","data":{"secret":"raw"}}\n',
    )
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    frames = [
        frame
        async for frame in render_events(
            backend,
            RunContext(run_id=run.run_id, session_id=run.session_id),
            after_sequence=0,
        )
    ]

    assert len(frames) == 1
    assert "event: error" in frames[0]
    assert "journal_corrupt" in frames[0]
    assert "secret" not in frames[0]
    assert store.session is not None
    assert store.session.status == "quarantined"


@pytest.mark.asyncio
async def test_gapped_event_stream_quarantines_without_exposing_event_contents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import azure_functions_agents.execution.run_control as run_control_module

    monkeypatch.setattr(run_control_module, "JOURNAL_VISIBILITY_TIMEOUT_SECONDS", 0.001)
    script_root = _script_root(tmp_path)
    base = _session(script_root)
    run = _run(base, state="running")
    session = session_with_admitted_run(base, run.run_id, updated_at=run.updated_at)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        status_path("run-1"),
        _status(state="running", last_sequence=3),
    )
    handle.seed_file(
        f"{run_path('run-1')}/events.jsonl",
        (
            b'{"sequence":1,"type":"delta","data":{"content":"safe"},'
            b'"timestamp":"2026-08-03T00:00:00+00:00"}\n'
            b'{"sequence":3,"type":"delta","data":{"secret":"raw-gap"},'
            b'"timestamp":"2026-08-03T00:00:00+00:00"}\n'
        ),
    )
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
        run_control=SandboxRunControl(event_poll_interval_seconds=0.001),
    )

    frames = [
        frame
        async for frame in render_events(
            backend,
            RunContext(run_id=run.run_id, session_id=run.session_id),
            after_sequence=0,
            heartbeat_seconds=0.01,
        )
    ]

    assert len(frames) == 1
    assert "journal_corrupt" in frames[0]
    assert "raw-gap" not in frames[0]
    assert store.runs[run.run_id].status == "failed"


async def _admitted_submit_for_journal_test(
    script_root: Path,
) -> tuple[
    AcaSandboxExecutionBackend,
    ActivatedSession,
    DurableRunRecord,
    FakeSessionStateStore,
    FakeSandboxSessionHandle,
]:
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    runtime = _runtime(script_root, provider, store)
    initial = ActivatedSession.create(
        handle=handle,
        session=session,
        etag=store.etag,
        partition=session.owner_partition,
        store=store,
    )
    run = _run(session)
    prepared, fence = await begin_submit_operation(initial, run)
    prepared, fence = await disarm_submit_lifecycle(runtime, prepared, fence)
    admitted = session_with_admitted_run(
        prepared.session,
        run.run_id,
        updated_at=run.updated_at,
    )
    await store.admit_operation_run(
        fence=fence,
        records=AdmissionRecords.create(admitted, run),
    )
    current = await store.get_session(session.owner_partition, session.session_id)
    return (
        AcaSandboxExecutionBackend(_binding(), runtime=runtime, owner=_owner()),
        ActivatedSession.create(
            handle=handle,
            session=current.record,
            etag=current.etag,
            partition=session.owner_partition,
            store=store,
        ),
        run,
        store,
        handle,
    )


@pytest.mark.asyncio
async def test_submission_corrupt_existing_status_quarantines_and_releases_operation(
    tmp_path: Path,
) -> None:
    backend, activated, run, store, handle = await _admitted_submit_for_journal_test(
        _script_root(tmp_path)
    )
    handle.seed_file(
        status_path("run-1"),
        b'{"run_id":"run-1","run_id":"raw","session_id":"session-1"}',
    )

    with pytest.raises(SessionActivationNotFoundError, match="cannot be trusted"):
        await backend._submit_fenced_journal(
            activated,
            run,
            StartRunRequest(prompt="hello", session_id=run.session_id),
            SetupBudget.start(),
        )

    assert [call for call in handle.calls if call.operation == "exec"] == []
    assert store.runs[run.run_id].status == "failed"
    assert store.runs[run.run_id].status_reason == "journal_corrupt"
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.active_operation_id is None
    assert next(iter(store.durable_operations.values())).state == "completed"
    assert store.operations.index("invalidate_journal") < store.operations.index(
        "update:quarantined"
    )


@pytest.mark.asyncio
async def test_submission_corrupt_acceptance_quarantines_without_relaunching(
    tmp_path: Path,
) -> None:
    backend, activated, run, store, handle = await _admitted_submit_for_journal_test(
        _script_root(tmp_path)
    )

    async def corrupt_acceptance(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        handle.seed_file(
            status_path(run_id),
            b'{"run_id":"run-1","session_id":"session-1","state":"accepted"',
        )

    handle.exec_hook = corrupt_acceptance
    request = StartRunRequest(prompt="hello", session_id=run.session_id)

    with pytest.raises(SessionActivationNotFoundError, match="cannot be trusted"):
        await backend._submit_fenced_journal(
            activated,
            run,
            request,
            SetupBudget.start(),
        )

    assert len([call for call in handle.calls if call.operation == "exec"]) == 1
    assert store.runs[run.run_id].status == "failed"
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.active_operation_id is None
    assert next(iter(store.durable_operations.values())).state == "completed"
    with pytest.raises(SessionActivationNotFoundError):
        await backend.start_run(
            StartRunRequest(prompt="retry", session_id=run.session_id)
        )
    assert len([call for call in handle.calls if call.operation == "exec"]) == 1


@pytest.mark.asyncio
async def test_status_corruption_finalizes_the_matching_submit_operation(
    tmp_path: Path,
) -> None:
    backend, _activated, run, store, handle = await _admitted_submit_for_journal_test(
        _script_root(tmp_path)
    )
    handle.seed_file(
        status_path("run-1"),
        b'{"run_id":"run-1","run_id":"forged","session_id":"session-1"}',
    )

    status = await backend.get_run(RunContext(run_id=run.run_id, session_id=run.session_id))

    assert status.state == "failed"
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.active_operation_id is None
    assert next(iter(store.durable_operations.values())).state == "completed"


@pytest.mark.asyncio
async def test_cancel_corruption_finalizes_the_matching_submit_operation(
    tmp_path: Path,
) -> None:
    backend, _activated, run, store, handle = await _admitted_submit_for_journal_test(
        _script_root(tmp_path)
    )
    handle.seed_file(
        status_path("run-1"),
        b'{"run_id":"run-1","run_id":"forged","session_id":"session-1"}',
    )

    status = await backend.cancel_run(RunContext(run_id=run.run_id, session_id=run.session_id))

    assert status.state == "failed"
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.active_operation_id is None
    assert next(iter(store.durable_operations.values())).state == "completed"


@pytest.mark.asyncio
async def test_event_corruption_finalizes_the_matching_submit_operation(
    tmp_path: Path,
) -> None:
    backend, _activated, run, store, handle = await _admitted_submit_for_journal_test(
        _script_root(tmp_path)
    )
    handle.seed_file(
        status_path("run-1"),
        _status(state="running"),
    )
    handle.seed_file(
        f"{run_path('run-1')}/events.jsonl",
        b'{"sequence":1,"sequence":2,"type":"delta","data":{},"timestamp":"2026-08-03T00:00:00+00:00"}\n',
    )

    events = [
        event
        async for event in backend.read_events(
            RunContext(run_id=run.run_id, session_id=run.session_id),
            0,
        )
    ]

    assert events == []
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.active_operation_id is None
    assert next(iter(store.durable_operations.values())).state == "completed"


@pytest.mark.asyncio
async def test_app_timer_terminal_reader_quarantines_a_corrupt_journal(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    base = _session(script_root)
    run = _run(base, state="running")
    session = session_with_admitted_run(base, run.run_id, updated_at=run.updated_at)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    handle = FakeSandboxSessionHandle()
    handle.labels = {
        "app_hash": session.owner_partition.app_hash,
        "owner_hash_version": session.owner_partition.owner_hash_version,
        "owner_kind": session.owner_partition.owner_kind,
        "owner_hash": session.owner_partition.owner_hash,
        "session_id": session.session_id,
    }
    provider = FakeSandboxSessionProvider(handle)
    runtime = _runtime(script_root, provider, store)
    state_binding = StateStoreBinding.create(
        store=store,
        state_store_fingerprint=_FINGERPRINT,
    )
    handle.seed_file(
        status_path("run-1"),
        b'{"run_id":"run-1","session_id":"session-1","state":"running"',
    )

    report = await app_module._build_session_reconciler(
        runtime,
        state_binding,
        provider,
        cadence_seconds=60,
    ).run_once()

    assert report.adopted_terminal_runs == 0
    assert store.runs[run.run_id].status == "failed"
    assert store.session is not None
    assert store.session.status == "quarantined"


@pytest.mark.asyncio
async def test_app_reconciler_rearms_intact_reclaim_before_completion(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    base = _session(script_root)
    run = _run(base, state="succeeded")
    operation = DurableSessionOperation.create(
        owner_partition=base.owner_partition,
        target=SessionOperationTarget.create(
            session_id=base.session_id,
            sandbox_id=base.sandbox_id,
            generation=base.generation,
            digest_kind=base.digest_kind,
            digest=base.digest,
            run_id=run.run_id,
        ),
        sequence=1,
        kind="reclaim_backing",
        phase="reclaim_fenced",
        state="active",
        correlation_label=operation_correlation_label(base.session_id, 1),
        token="f" * 32,
        attempt_count=0,
        error_code=None,
        lease_expires_at=None,
        next_attempt_at=None,
        created_at=run.created_at,
        updated_at=run.updated_at,
        finished_at=None,
    )
    session = replace(
        base,
        status="running",
        idle_policy_armed=False,
        active_run_id=run.run_id,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    lifecycle_phases: list[str] = []

    class ProbeHandle(FakeSandboxSessionHandle):
        async def set_lifecycle_policy(self, policy):  # type: ignore[no-untyped-def]
            assert store.session is not None
            assert store.session.active_operation_id == operation.operation_id
            assert store.session.active_run_id == run.run_id
            lifecycle_phases.append(store.durable_operations[operation.operation_id].phase)
            await super().set_lifecycle_policy(policy)

    handle = ProbeHandle()
    provider = FakeSandboxSessionProvider(handle)
    runtime = _runtime(script_root, provider, store)
    state_binding = StateStoreBinding.create(
        store=store,
        state_store_fingerprint=_FINGERPRINT,
    )

    report = await app_module._build_session_reconciler(
        runtime,
        state_binding,
        provider,
        cadence_seconds=60,
    )._complete_reclaim_operation(
        fence=SessionOperationFence.create(operation),
        terminal=run,
        now=run.updated_at,
        report=ReconcileReport(),
    )

    assert report.adopted_terminal_runs == 1
    assert lifecycle_phases == ["reclaim_rearm"]
    assert len(handle.lifecycle_policy_history) == 2
    assert handle.lifecycle_policy.auto_suspend_seconds == runtime.auto_suspend_seconds
    assert handle.lifecycle_policy.auto_delete_seconds == (
        runtime.reclaim_idle_seconds + 3_900
    )
    assert store.session is not None
    assert store.session.active_operation_id is None
    assert store.session.idle_policy_armed


@pytest.mark.asyncio
async def test_app_reconciler_skips_remote_lifecycle_when_fence_turns_stale(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    base = _session(script_root)
    run = _run(base, state="succeeded")
    operation = DurableSessionOperation.create(
        owner_partition=base.owner_partition,
        target=SessionOperationTarget.create(
            session_id=base.session_id,
            sandbox_id=base.sandbox_id,
            generation=base.generation,
            digest_kind=base.digest_kind,
            digest=base.digest,
            run_id=run.run_id,
        ),
        sequence=1,
        kind="reclaim_backing",
        phase="reclaim_fenced",
        state="active",
        correlation_label=operation_correlation_label(base.session_id, 1),
        token="f" * 32,
        attempt_count=0,
        error_code=None,
        lease_expires_at=None,
        next_attempt_at=None,
        created_at=run.created_at,
        updated_at=run.updated_at,
        finished_at=None,
    )
    session = replace(
        base,
        status="running",
        idle_policy_armed=False,
        active_run_id=run.run_id,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    handle = FakeSandboxSessionHandle()

    class StaleAttachProvider(FakeSandboxSessionProvider):
        async def attach(self, *args: object, **kwargs: object) -> FakeSandboxSessionHandle:
            attached = await super().attach(*args, **kwargs)
            current = store.durable_operations[operation.operation_id]
            store.durable_operations[operation.operation_id] = replace(
                current,
                token="e" * 32,
            )
            return attached

    provider = StaleAttachProvider(handle)
    runtime = _runtime(script_root, provider, store)
    state_binding = StateStoreBinding.create(
        store=store,
        state_store_fingerprint=_FINGERPRINT,
    )

    report = await app_module._build_session_reconciler(
        runtime,
        state_binding,
        provider,
        cadence_seconds=60,
    )._complete_reclaim_operation(
        fence=SessionOperationFence.create(operation),
        terminal=run,
        now=run.updated_at,
        report=ReconcileReport(),
    )

    assert report.adopted_terminal_runs == 0
    assert len(handle.lifecycle_policy_history) == 1
    assert store.session is not None
    assert store.session.active_operation_id == operation.operation_id
    assert not store.session.idle_policy_armed


@pytest.mark.asyncio
async def test_tombstoned_abandoned_run_keeps_status_but_result_route_is_gone(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    session = replace(
        _session(script_root),
        status="tombstoned",
        tombstone_reason="sandbox_backing_lost",
    )
    run = _run(session, state="abandoned")
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(
            script_root,
            FakeSandboxSessionProvider(FakeSandboxSessionHandle()),
            store,
        ),
        owner=_owner(),
    )
    context = RunContext(run_id=run.run_id, session_id=session.session_id)

    status_response = await read_status(backend, context)
    result_response = await read_result(backend, context)

    assert status_response.status_code == 200
    assert isinstance(status_response.body, dict)
    assert status_response.body["error"]["code"] == SESSION_TOMBSTONED_ERROR_CODE
    assert result_response.status_code == 410
