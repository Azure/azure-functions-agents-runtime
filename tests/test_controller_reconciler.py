from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from azure_functions_agents.controller.readiness import session_with_admitted_run
from azure_functions_agents.controller.reconciler import (
    ReconcilerConfig,
    ReconcileReport,
    SessionReconciler,
    reconciler_ncrontab,
    resolve_reconciler_cadence,
)
from azure_functions_agents.execution.backend import RunError, RunResult, RunStatus
from azure_functions_agents.execution.binding import AgentBinding
from azure_functions_agents.execution.run_control import RunJournalProtocolError
from azure_functions_agents.session_state import (
    AdmissionRecords,
    AppIdentity,
    ConcurrencyConflictError,
    DurableOwnerIdempotencyRecord,
    DurableRunRecord,
    DurableSessionOperation,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    SessionNotAdmissibleError,
    SessionOperationFence,
    SessionOperationTarget,
    StaleOperationTokenError,
    TableEntityPage,
    operation_correlation_label,
    owner_partition,
)
from azure_functions_agents.transport.transport_models import (
    SandboxProvisioningError,
    SandboxSnapshot,
    SandboxSummary,
)
from tests.doubles.fake_session_runtime import FakeSessionStateStore


class InventoryProvider:
    def __init__(
        self,
        *,
        sandboxes: tuple[SandboxSummary, ...],
        snapshots: tuple[SandboxSnapshot, ...] = (),
        refreshed_sandboxes: tuple[SandboxSummary, ...] | None = None,
    ) -> None:
        self.sandboxes = sandboxes
        self.refreshed_sandboxes = refreshed_sandboxes
        self.snapshots = {snapshot.snapshot_id: snapshot for snapshot in snapshots}
        self.deleted_sandboxes: list[str] = []
        self.deleted_snapshots: list[str] = []
        self.list_calls = 0

    async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[SandboxSummary, ...]:
        self.list_calls += 1
        sandboxes = (
            self.sandboxes
            if self.list_calls == 1 or self.refreshed_sandboxes is None
            else self.refreshed_sandboxes
        )
        return tuple(
            sandbox
            for sandbox in sandboxes
            if all(sandbox.labels.get(key) == value for key, value in labels.items())
        )

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted_sandboxes.append(sandbox_id)

    async def list_snapshots(self) -> tuple[SandboxSnapshot, ...]:
        return tuple(self.snapshots.values())

    async def delete_snapshot(self, snapshot_id: str) -> None:
        self.deleted_snapshots.append(snapshot_id)
        self.snapshots.pop(snapshot_id, None)


class FailOnceDeleteProvider(InventoryProvider):
    def __init__(self, *, sandboxes: tuple[SandboxSummary, ...]) -> None:
        super().__init__(sandboxes=sandboxes)
        self.fail_next_delete = True

    async def delete_sandbox(self, sandbox_id: str) -> None:
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise SandboxProvisioningError("temporary delete failure")
        await super().delete_sandbox(sandbox_id)


class DeleteThenMissingProvider(InventoryProvider):
    async def delete_sandbox(self, sandbox_id: str) -> None:
        await super().delete_sandbox(sandbox_id)
        self.sandboxes = ()


class FailOnceSnapshotProvider(InventoryProvider):
    def __init__(
        self,
        *,
        sandboxes: tuple[SandboxSummary, ...],
        snapshots: tuple[SandboxSnapshot, ...],
    ) -> None:
        super().__init__(sandboxes=sandboxes, snapshots=snapshots)
        self.fail_next_snapshot_delete = True

    async def delete_snapshot(self, snapshot_id: str) -> None:
        if self.fail_next_snapshot_delete:
            self.fail_next_snapshot_delete = False
            raise SandboxProvisioningError("temporary snapshot delete failure")
        await super().delete_snapshot(snapshot_id)


class SnapshotAlreadyDeletedProvider(InventoryProvider):
    async def delete_snapshot(self, snapshot_id: str) -> None:
        self.snapshots.pop(snapshot_id, None)
        raise SandboxProvisioningError("Snapshot delete found no target.")


class OperationStartedDuringSuspensionProjectionStore(FakeSessionStateStore):
    def __init__(self, session: DurableSessionRecord) -> None:
        super().__init__(session)
        self.session_reads = 0

    async def get_session(self, partition, session_id):  # type: ignore[no-untyped-def]
        self.session_reads += 1
        if self.session_reads == 2:
            assert self.session is not None
            self.session = replace(
                self.session,
                active_operation_id="op-1",
                operation_sequence=1,
            )
            self.etag = "etag-concurrent-operation"
        return await super().get_session(partition, session_id)


class FailOnceOperationStore(FakeSessionStateStore):
    def __init__(self, session: DurableSessionRecord) -> None:
        super().__init__(session)
        self.fail_completion = True

    async def complete_operation(self, **kwargs: object):  # type: ignore[no-untyped-def]
        if self.fail_completion:
            self.fail_completion = False
            raise ConcurrencyConflictError("transient operation completion conflict")
        return await super().complete_operation(**kwargs)


def test_reconciler_cadence_defaults_and_accepts_faster_whole_minutes() -> None:
    assert resolve_reconciler_cadence("", environ=lambda _: None) == 3600
    assert resolve_reconciler_cadence("60") == 60
    assert reconciler_ncrontab(60) == "0 */1 * * * *"
    assert reconciler_ncrontab(3600) == "0 0 * * * *"
    with pytest.raises(ValueError):
        resolve_reconciler_cadence("61")


class ServiceTimeStore(FakeSessionStateStore):
    def __init__(self, session: DurableSessionRecord, service_time: datetime) -> None:
        super().__init__(session)
        self._service_time = service_time

    async def query_entities(
        self,
        *,
        filter_expression: str,
        top: int | None = None,
        continuation_token: str | None = None,
    ) -> TableEntityPage:
        page = await super().query_entities(
            filter_expression=filter_expression,
            top=top,
            continuation_token=continuation_token,
        )
        return replace(page, service_time=self._service_time)


class PartialScanStore(FakeSessionStateStore):
    async def query_entities(
        self,
        *,
        filter_expression: str,
        top: int | None = None,
        continuation_token: str | None = None,
    ) -> TableEntityPage:
        page = await super().query_entities(
            filter_expression=filter_expression,
            top=top,
            continuation_token=continuation_token,
        )
        return replace(page, continuation_token="more")


class SharedStorageStore(FakeSessionStateStore):
    def __init__(
        self,
        session: DurableSessionRecord,
        foreign_session: DurableSessionRecord,
    ) -> None:
        super().__init__(session)
        self.foreign_session = foreign_session
        self.query_filters: list[str] = []

    async def query_entities(
        self,
        *,
        filter_expression: str,
        top: int | None = None,
        continuation_token: str | None = None,
    ) -> TableEntityPage:
        del top, continuation_token
        self.query_filters.append(filter_expression)
        assert self.session is not None
        return TableEntityPage(
            entities=(
                self.session.to_table_entity(),
                self.foreign_session.to_table_entity(),
            ),
            continuation_token=None,
        )


class RotatingPageStore(FakeSessionStateStore):
    def __init__(self, entities: tuple[dict[str, object], ...]) -> None:
        super().__init__()
        self.entities = entities
        self.page_starts: list[str | None] = []

    async def query_entities(
        self,
        *,
        filter_expression: str,
        top: int | None = None,
        continuation_token: str | None = None,
    ) -> TableEntityPage:
        del filter_expression
        self.page_starts.append(continuation_token)
        start = 0 if continuation_token is None else int(continuation_token)
        limit = top or len(self.entities)
        end = min(start + limit, len(self.entities))
        return TableEntityPage(
            entities=self.entities[start:end],
            continuation_token=None if end == len(self.entities) else str(end),
        )


class PairPageStore(FakeSessionStateStore):
    def __init__(
        self,
        session: DurableSessionRecord,
        run: DurableRunRecord,
        entities: tuple[dict[str, object], ...],
    ) -> None:
        super().__init__(session)
        self.runs[run.run_id] = run
        self.entities = entities

    async def query_entities(
        self,
        *,
        filter_expression: str,
        top: int | None = None,
        continuation_token: str | None = None,
    ) -> TableEntityPage:
        del filter_expression
        start = 0 if continuation_token is None else int(continuation_token)
        limit = top or len(self.entities)
        end = min(start + limit, len(self.entities))
        return TableEntityPage(
            entities=self.entities[start:end],
            continuation_token=None if end == len(self.entities) else str(end),
        )


def _owner() -> FunctionAppOwnerContext:
    return FunctionAppOwnerContext.create(
        AppIdentity.create(
            subscription_id="11111111-2222-3333-4444-555555555555",
            site_name="agent-app",
        ),
        "main",
    )


def _app_hash() -> str:
    return owner_partition(_owner()).app_hash


def _foreign_owner() -> FunctionAppOwnerContext:
    return FunctionAppOwnerContext.create(
        AppIdentity.create(
            subscription_id="99999999-2222-3333-4444-555555555555",
            site_name="other-agent-app",
        ),
        "main",
    )


def _session(
    now: datetime,
    *,
    status: str = "ready",
    active_run_id: str | None = None,
    sandbox_id: str | None = "sandbox-1",
    expires_at: datetime | None = None,
    snapshot_ids: tuple[str, ...] = (),
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=owner_partition(_owner()),
        session_id="session-1",
        sandbox_id=sandbox_id,
        generation=1,
        digest_kind="funcs_zip",
        digest="sha256:content",
        protocol="1",
        status=status,  # type: ignore[arg-type]
        last_activity_at=now,
        expires_at=expires_at or now + timedelta(days=1),
        idle_policy_armed=True,
        active_run_id=active_run_id,
        snapshot_ids=snapshot_ids,
        region="westus2",
        state_store_fingerprint="s1-" + ("a" * 52),
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=now,
        updated_at=now,
        active_operation_id=None,
        operation_sequence=0,
    )


def _run(session: DurableSessionRecord, now: datetime, *, status: str = "running") -> DurableRunRecord:
    return DurableRunRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        run_id="run-1",
        generation=session.generation,
        status=status,  # type: ignore[arg-type]
        result_available=False,
        status_reason=None,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )


def _submit_operation(
    session: DurableSessionRecord,
    run: DurableRunRecord,
    now: datetime,
    *,
    lease_expires_at: datetime | None = None,
) -> DurableSessionOperation:
    return DurableSessionOperation.create(
        owner_partition=session.owner_partition,
        target=SessionOperationTarget.create(
            session_id=session.session_id,
            sandbox_id=session.sandbox_id,
            generation=session.generation,
            digest_kind=session.digest_kind,
            digest=session.digest,
            run_id=run.run_id,
        ),
        sequence=1,
        kind="submit_run",
        phase="submit_launching",
        state="active",
        correlation_label=operation_correlation_label(session.session_id, 1),
        token="a" * 32,
        attempt_count=1,
        error_code=None,
        lease_expires_at=lease_expires_at,
        next_attempt_at=None,
        created_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=1),
        finished_at=None,
    )


def _provision_operation(
    session: DurableSessionRecord,
    run: DurableRunRecord,
    now: datetime,
) -> DurableSessionOperation:
    return DurableSessionOperation.create(
        owner_partition=session.owner_partition,
        target=SessionOperationTarget.create(
            session_id=session.session_id,
            sandbox_id=None,
            generation=session.generation,
            digest_kind=session.digest_kind,
            digest=session.digest,
            run_id=run.run_id,
        ),
        sequence=1,
        kind="provision_submit",
        phase="provision_create",
        state="active",
        correlation_label=operation_correlation_label(session.session_id, 1),
        token="b" * 32,
        attempt_count=1,
        error_code=None,
        lease_expires_at=now - timedelta(seconds=1),
        next_attempt_at=None,
        created_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=1),
        finished_at=None,
    )


def _reclaim_operation(
    session: DurableSessionRecord,
    run: DurableRunRecord,
    now: datetime,
    *,
    lease_expires_at: datetime | None = None,
) -> DurableSessionOperation:
    return DurableSessionOperation.create(
        owner_partition=session.owner_partition,
        target=SessionOperationTarget.create(
            session_id=session.session_id,
            sandbox_id=session.sandbox_id,
            generation=session.generation,
            digest_kind=session.digest_kind,
            digest=session.digest,
            run_id=run.run_id,
        ),
        sequence=1,
        kind="reclaim_backing",
        phase="reclaim_fenced",
        state="active",
        correlation_label=operation_correlation_label(session.session_id, 1),
        token="d" * 32,
        attempt_count=1,
        error_code=None,
        lease_expires_at=lease_expires_at,
        next_attempt_at=None,
        created_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=1),
        finished_at=None,
    )


@pytest.mark.asyncio
async def test_reconciler_does_not_take_over_an_unexpired_operation_lease() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(now, status="running", active_run_id="run-1")
    run = _run(base, now, status="accepted")
    operation = _submit_operation(
        base,
        run,
        now,
        lease_expires_at=now + timedelta(minutes=1),
    )
    session = replace(
        base,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation

    await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert store.durable_operations[operation.operation_id].token == operation.token
    assert "takeover_expired_operation" not in store.operations


@pytest.mark.asyncio
async def test_expired_operation_takeover_rejects_the_stale_holder() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(now, status="running", active_run_id="run-1")
    run = _run(base, now, status="accepted")
    operation = _submit_operation(
        base,
        run,
        now,
        lease_expires_at=now - timedelta(seconds=1),
    )
    session = replace(
        base,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    original = SessionOperationFence.create(operation)

    takeover = await store.takeover_expired_operation(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        token="c" * 32,
        updated_at=now,
    )

    assert takeover is not None
    with pytest.raises(StaleOperationTokenError):
        await store.advance_operation(
            fence=original,
            phase="submit_launching",
            updated_at=now,
        )


@pytest.mark.asyncio
async def test_live_operation_holder_can_advance_without_reconciler_preemption() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(now, status="running", active_run_id="run-1")
    run = _run(base, now, status="accepted")
    operation = _submit_operation(
        base,
        run,
        now,
        lease_expires_at=now + timedelta(minutes=1),
    )
    session = replace(
        base,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    holder = SessionOperationFence.create(operation)

    assert (
        await store.takeover_expired_operation(
            owner_partition=session.owner_partition,
            session_id=session.session_id,
            token="c" * 32,
            updated_at=now,
        )
        is None
    )
    advanced = await store.advance_operation(
        fence=holder,
        phase="submit_launching",
        updated_at=now,
    )

    assert advanced.token == holder.token


def _sandbox(
    now: datetime,
    sandbox_id: str = "sandbox-1",
    *,
    session_id: str = "session-1",
    state: str | None = None,
) -> SandboxSummary:
    partition = owner_partition(_owner())
    return SandboxSummary.create(
        sandbox_id=sandbox_id,
        labels={
            "app_hash": partition.app_hash,
            "owner_hash_version": partition.owner_hash_version,
            "owner_kind": partition.owner_kind,
            "owner_hash": partition.owner_hash,
            "session_id": session_id,
        },
        state=state,
        created_at=(now - timedelta(hours=1)).isoformat(),
    )


def _tombstoned_session(now: datetime, session_id: str) -> DurableSessionRecord:
    session = _session(now, sandbox_id=None)
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session_id,
        sandbox_id=None,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="tombstoned",
        last_activity_at=session.last_activity_at,
        expires_at=session.expires_at,
        idle_policy_armed=False,
        active_run_id=None,
        snapshot_ids=(),
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=None,
        tombstone_reason="reclaimed_idle_session",
        created_at=session.created_at,
        updated_at=session.updated_at,
        active_operation_id=None,
        operation_sequence=0,
    )


@pytest.mark.asyncio
async def test_reconciler_adopts_reachable_terminal_before_loss_processing() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now, status="running", active_run_id="run-1")
    run = _run(session, now)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    provider = InventoryProvider(sandboxes=(_sandbox(now),))

    async def terminal_reader(
        _: DurableSessionRecord, __: DurableRunRecord
    ) -> RunStatus:
        return RunStatus(
            run_id="run-1",
            session_id="session-1",
            state="failed",
            last_sequence=1,
            result_available=False,
            error=RunError(code="harness_failed", message="failed"),
        )

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        terminal_reader=terminal_reader,
        now=lambda: now,
    ).run_once()

    assert report.adopted_terminal_runs == 1
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.runs["run-1"].status == "failed"


@pytest.mark.asyncio
async def test_reconciler_validates_terminal_success_before_adoption() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now, status="running", active_run_id="run-1")
    run = replace(_run(session, now), agent_slug="main")
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run

    async def terminal_reader(
        _: DurableSessionRecord, __: DurableRunRecord
    ) -> RunStatus:
        return RunStatus(
            run_id="run-1",
            session_id="session-1",
            state="succeeded",
            last_sequence=1,
            result_available=True,
            result=RunResult(
                content="invalid",
                content_intermediate=[],
                tool_calls=[],
                reasoning=None,
                delegate_error_count=0,
            ),
        )

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        terminal_reader=terminal_reader,
        terminal_bindings={
            "main": AgentBinding(
                agent_name="main",
                output_validator=lambda _: RunError(
                    code="response_validation_failed",
                    message="invalid",
                    fault_domain="app",
                ),
            )
        },
        now=lambda: now,
    ).run_once()

    assert report.adopted_terminal_runs == 1
    assert store.runs["run-1"].status == "failed"
    assert not store.runs["run-1"].result_available


@pytest.mark.asyncio
async def test_reconciler_quarantines_one_corrupt_journal_and_continues_the_page() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    first_base = _session(now)
    first = session_with_admitted_run(first_base, "run-1", updated_at=now)
    first_run = _run(first_base, now)
    second_base = replace(
        _session(now),
        session_id="session-2",
        sandbox_id="sandbox-2",
    )
    second = session_with_admitted_run(second_base, "run-2", updated_at=now)
    second_run = replace(
        _run(second_base, now),
        session_id="session-2",
        run_id="run-2",
    )

    class _TwoSessionPageStore(FakeSessionStateStore):
        async def query_entities(
            self,
            *,
            filter_expression: str,
            top: int | None = None,
            continuation_token: str | None = None,
        ) -> TableEntityPage:
            del filter_expression, top, continuation_token
            return TableEntityPage(
                entities=(
                    first.to_table_entity(),
                    first_run.to_table_entity(),
                    second.to_table_entity(),
                    second_run.to_table_entity(),
                ),
                continuation_token=None,
            )

    store = _TwoSessionPageStore(first)
    store.runs[first_run.run_id] = first_run
    observed_runs: list[str] = []

    async def terminal_reader(
        _: DurableSessionRecord,
        run: DurableRunRecord,
    ) -> RunStatus | None:
        if run.run_id == first_run.run_id:
            raise RunJournalProtocolError("raw journal contents must not escape")
        observed_runs.append(run.run_id)
        return None

    await SessionReconciler(
        store=store,
        provider=InventoryProvider(
            sandboxes=(
                _sandbox(now, sandbox_id="sandbox-1", session_id="session-1"),
                _sandbox(now, sandbox_id="sandbox-2", session_id="session-2"),
            )
        ),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        terminal_reader=terminal_reader,
        now=lambda: now,
    ).run_once()

    assert store.runs[first_run.run_id].status == "failed"
    assert store.runs[first_run.run_id].status_reason == "journal_corrupt"
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.quarantine_reason == "journal_corrupt"
    assert observed_runs == ["run-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_row", ["session", "run"])
async def test_page_size_one_hydrates_active_session_run_pairs(
    first_row: str,
) -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base_session = _session(now)
    session = session_with_admitted_run(base_session, "run-1", updated_at=now)
    run = _run(base_session, now, status="succeeded")
    ordered = (
        (session.to_table_entity(), run.to_table_entity())
        if first_row == "session"
        else (run.to_table_entity(), session.to_table_entity())
    )
    store = PairPageStore(session, run, ordered)

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        config=ReconcilerConfig(page_size=1, max_pages=1),
        now=lambda: now,
    ).run_once()

    assert report.adopted_terminal_runs == 1
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.active_run_id is None


@pytest.mark.asyncio
async def test_page_size_one_hydrates_terminal_run_for_result_eviction() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now, expires_at=now - timedelta(seconds=60))
    run = replace(
        _run(session, now - timedelta(minutes=10), status="succeeded"),
        result_available=True,
    )
    store = PairPageStore(session, run, (run.to_table_entity(), session.to_table_entity()))

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        config=ReconcilerConfig(page_size=1, max_pages=1),
        now=lambda: now,
    ).run_once()

    assert report.evicted_results == 1
    assert store.runs[run.run_id].result_available is False


@pytest.mark.asyncio
async def test_page_local_run_absence_does_not_reclaim_healthy_ready_session() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(
        now - timedelta(days=2),
        expires_at=now + timedelta(hours=1),
    )
    terminal = _run(session, now - timedelta(days=1), status="succeeded")
    store = PairPageStore(
        session,
        terminal,
        (session.to_table_entity(), terminal.to_table_entity()),
    )

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        config=ReconcilerConfig(page_size=1, max_pages=1),
        now=lambda: now,
    ).run_once()

    assert report.tombstoned_sessions == 0
    assert store.session is not None
    assert store.session.status == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_state", ["Stopped", "Suspended"])
async def test_reconciler_projects_stopped_backing_to_suspended_session(
    provider_state: str,
) -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now)
    store = FakeSessionStateStore(session)

    await SessionReconciler(
        store=store,
        provider=InventoryProvider(
            sandboxes=(_sandbox(now, state=provider_state),)
        ),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).reconcile_session(session.owner_partition, session.session_id)

    assert store.session is not None
    assert store.session.status == "suspended"


@pytest.mark.asyncio
async def test_reconciler_does_not_project_foreign_stopped_backing() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now)
    store = FakeSessionStateStore(session)

    await SessionReconciler(
        store=store,
        provider=InventoryProvider(
            sandboxes=(_sandbox(now, session_id="other-session", state="Stopped"),)
        ),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).reconcile_session(session.owner_partition, session.session_id)

    assert store.session is not None
    assert store.session.status == "ready"


@pytest.mark.asyncio
async def test_reconciler_does_not_overwrite_new_operation_with_suspended_state() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now)
    store = OperationStartedDuringSuspensionProjectionStore(session)

    await SessionReconciler(
        store=store,
        provider=InventoryProvider(
            sandboxes=(_sandbox(now, state="Stopped"),)
        ),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).reconcile_session(session.owner_partition, session.session_id)

    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.active_operation_id == "op-1"


@pytest.mark.asyncio
async def test_reconciler_reclaims_suspended_session_after_idle_expiry() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(
        now - timedelta(hours=1),
        status="suspended",
        expires_at=now - timedelta(minutes=10),
    )
    store = FakeSessionStateStore(session)
    provider = InventoryProvider(
        sandboxes=(_sandbox(now, state="Suspended"),)
    )

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).reconcile_session(session.owner_partition, session.session_id)

    assert report.tombstoned_sessions == 1
    assert provider.deleted_sandboxes == ["sandbox-1"]
    assert store.session is not None
    assert store.session.status == "tombstoned"


@pytest.mark.asyncio
async def test_reconciler_tombstones_table_only_lost_backing_after_abandonment() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now, status="running", active_run_id="run-1")
    run = _run(session, now)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=()),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert report.abandoned_runs == 1
    assert report.tombstoned_sessions == 1
    assert store.runs["run-1"].status == "abandoned"
    assert store.session is not None
    assert store.session.status == "tombstoned"


@pytest.mark.asyncio
async def test_missing_backing_deletes_referenced_snapshots_before_tombstone() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now, snapshot_ids=("snapshot-1", "foreign-snapshot"))
    store = FakeSessionStateStore(session)
    snapshots = (
        SandboxSnapshot.create(
            snapshot_id="snapshot-1",
            sandbox_id="sandbox-1",
            created_at=now.isoformat(),
        ),
        SandboxSnapshot.create(
            snapshot_id="foreign-snapshot",
            sandbox_id="foreign-sandbox",
            created_at=now.isoformat(),
        ),
    )
    provider = FailOnceSnapshotProvider(sandboxes=(), snapshots=snapshots)
    reconciler = SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    )

    first = await reconciler.run_once()
    assert first.tombstoned_sessions == 0
    assert store.session is not None
    assert store.session.active_operation_id is not None
    assert "snapshot-1" in provider.snapshots

    second = await reconciler.run_once()

    assert second.tombstoned_sessions == 1
    assert store.session.status == "tombstoned"
    assert provider.deleted_snapshots == ["snapshot-1"]
    assert "foreign-snapshot" in provider.snapshots


@pytest.mark.asyncio
async def test_targeted_missing_backing_cleans_referenced_snapshots_before_tombstone() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now, snapshot_ids=("snapshot-1", "foreign-snapshot"))
    store = FakeSessionStateStore(session)
    provider = FailOnceSnapshotProvider(
        sandboxes=(),
        snapshots=(
            SandboxSnapshot.create(
                snapshot_id="snapshot-1",
                sandbox_id="sandbox-1",
                created_at=now.isoformat(),
            ),
            SandboxSnapshot.create(
                snapshot_id="foreign-snapshot",
                sandbox_id="foreign-sandbox",
                created_at=now.isoformat(),
            ),
        ),
    )
    reconciler = SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    )

    first = await reconciler.reconcile_session(session.owner_partition, session.session_id)
    assert first.tombstoned_sessions == 0
    assert store.session is not None
    assert store.session.active_operation_id is not None

    second = await reconciler.reconcile_session(session.owner_partition, session.session_id)

    assert second.tombstoned_sessions == 1
    assert store.session is not None
    assert store.session.status == "tombstoned"
    assert provider.deleted_snapshots == ["snapshot-1"]
    assert "foreign-snapshot" in provider.snapshots


@pytest.mark.asyncio
async def test_orphan_snapshot_cleanup_precedes_sandbox_delete_and_retries() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    provider = FailOnceSnapshotProvider(
        sandboxes=(_sandbox(now - timedelta(minutes=10)),),
        snapshots=(
            SandboxSnapshot.create(
                snapshot_id="orphan-snapshot",
                sandbox_id="sandbox-1",
                created_at=(now - timedelta(minutes=10)).isoformat(),
            ),
            SandboxSnapshot.create(
                snapshot_id="foreign-snapshot",
                sandbox_id="foreign-sandbox",
                created_at=(now - timedelta(minutes=10)).isoformat(),
            ),
        ),
    )
    reconciler = SessionReconciler(
        store=FakeSessionStateStore(),
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    )

    first = await reconciler.run_once()

    assert first.deleted_snapshots == 0
    assert first.deleted_sandboxes == 0
    assert provider.deleted_sandboxes == []
    assert "orphan-snapshot" in provider.snapshots
    assert "foreign-snapshot" in provider.snapshots

    second = await reconciler.run_once()

    assert second.deleted_snapshots == 1
    assert second.deleted_sandboxes == 1
    assert provider.deleted_snapshots == ["orphan-snapshot"]
    assert provider.deleted_sandboxes == ["sandbox-1"]
    assert "foreign-snapshot" in provider.snapshots


@pytest.mark.asyncio
async def test_orphan_snapshot_not_found_is_idempotent_only_after_target_proof() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    provider = SnapshotAlreadyDeletedProvider(
        sandboxes=(_sandbox(now - timedelta(minutes=10)),),
        snapshots=(
            SandboxSnapshot.create(
                snapshot_id="orphan-snapshot",
                sandbox_id="sandbox-1",
                created_at=(now - timedelta(minutes=10)).isoformat(),
            ),
        ),
    )

    report = await SessionReconciler(
        store=FakeSessionStateStore(),
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert report.deleted_snapshots == 0
    assert report.deleted_sandboxes == 1
    assert provider.deleted_sandboxes == ["sandbox-1"]


@pytest.mark.asyncio
async def test_reconciler_reclaims_orphan_candidate_through_deleting_state() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(
        now - timedelta(minutes=10),
        status="creating",
        expires_at=now + timedelta(days=1),
    )
    store = FakeSessionStateStore(session)
    provider = InventoryProvider(sandboxes=(_sandbox(now),))

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        config=ReconcilerConfig(safety_grace_seconds=300),
        now=lambda: now,
    ).run_once()

    assert report.deleted_sandboxes == 1
    assert store.session is not None
    assert store.session.status == "tombstoned"


@pytest.mark.asyncio
async def test_recent_creating_without_a_sandbox_survives_reconciliation() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now, status="creating", sandbox_id=None)
    store = FakeSessionStateStore(session)

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=()),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert report.tombstoned_sessions == 0
    assert store.session is not None
    assert store.session.status == "creating"


@pytest.mark.asyncio
async def test_recent_creating_with_not_yet_visible_sandbox_survives_reconciliation() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now, status="creating", sandbox_id="new-sandbox")
    store = FakeSessionStateStore(session)

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=()),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert report.tombstoned_sessions == 0
    assert store.session is not None
    assert store.session.status == "creating"


@pytest.mark.asyncio
async def test_stale_creating_that_becomes_ready_before_reread_is_preserved() -> None:
    class ReadyDuringRecheckStore(FakeSessionStateStore):
        async def get_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            assert self.session is not None
            if self.session.status == "creating":
                self.session = replace(
                    self.session,
                    status="ready",
                    sandbox_id="healthy-sandbox",
                    updated_at=datetime(2026, 8, 5, tzinfo=UTC),
                )
                self.etag = "etag-healthy"
            return await super().get_session(*args, **kwargs)

    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(
        now - timedelta(minutes=10),
        status="creating",
        sandbox_id=None,
    )
    store = ReadyDuringRecheckStore(session)

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now, "healthy-sandbox"),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert report.tombstoned_sessions == 0
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.sandbox_id == "healthy-sandbox"


@pytest.mark.asyncio
async def test_reconciler_retains_referenced_snapshot_and_deletes_old_orphan() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now, snapshot_ids=("kept",))
    store = FakeSessionStateStore(session)
    provider = InventoryProvider(
        sandboxes=(_sandbox(now),),
        snapshots=(
            SandboxSnapshot.create(
                snapshot_id="kept",
                sandbox_id="sandbox-1",
                created_at=(now - timedelta(hours=1)).isoformat(),
            ),
            SandboxSnapshot.create(
                snapshot_id="orphan",
                sandbox_id="sandbox-1",
                created_at=(now - timedelta(hours=1)).isoformat(),
            ),
        ),
    )

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert report.deleted_snapshots == 1
    assert provider.deleted_snapshots == ["orphan"]


@pytest.mark.asyncio
async def test_reconciler_deletes_old_platform_only_sandbox() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now)
    store = FakeSessionStateStore(session)
    provider = InventoryProvider(
        sandboxes=(
            _sandbox(now, "sandbox-1"),
            _sandbox(now, "platform-orphan"),
        )
    )

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert report.deleted_sandboxes == 1
    assert provider.deleted_sandboxes == ["platform-orphan"]


@pytest.mark.asyncio
async def test_partial_scan_deletes_only_verifiably_app_owned_orphans() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    store = PartialScanStore(_session(now))
    provider = InventoryProvider(
        sandboxes=(
            _sandbox(now, "sandbox-1"),
            _sandbox(now, "platform-orphan"),
            SandboxSummary.create(
                sandbox_id="other-app-sandbox",
                labels={"app_hash": "other-app"},
                created_at=(now - timedelta(hours=1)).isoformat(),
            ),
        )
    )

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        config=ReconcilerConfig(max_pages=1),
        now=lambda: now,
    ).run_once()

    assert report.deleted_sandboxes == 1
    assert provider.deleted_sandboxes == ["platform-orphan"]


@pytest.mark.asyncio
async def test_reconciler_never_mutates_another_app_in_shared_storage_or_group() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    local = _session(now)
    foreign = DurableSessionRecord.create(
        owner_partition=owner_partition(_foreign_owner()),
        session_id="foreign-session",
        sandbox_id="foreign-sandbox",
        generation=1,
        digest_kind="funcs_zip",
        digest="sha256:foreign",
        protocol="1",
        status="creating",
        last_activity_at=now - timedelta(hours=1),
        expires_at=now - timedelta(hours=1),
        idle_policy_armed=True,
        active_run_id=None,
        snapshot_ids=("foreign-snapshot",),
        region="westus2",
        state_store_fingerprint="s1-" + ("b" * 52),
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
        active_operation_id=None,
        operation_sequence=0,
    )
    store = SharedStorageStore(local, foreign)
    provider = InventoryProvider(
        sandboxes=(
            _sandbox(now),
            SandboxSummary.create(
                sandbox_id="foreign-sandbox",
                labels={
                    "app_hash": foreign.owner_partition.app_hash,
                    "owner_hash_version": foreign.owner_partition.owner_hash_version,
                    "owner_kind": foreign.owner_partition.owner_kind,
                    "owner_hash": foreign.owner_partition.owner_hash,
                    "session_id": foreign.session_id,
                },
                created_at=(now - timedelta(hours=1)).isoformat(),
            ),
        ),
        snapshots=(
            SandboxSnapshot.create(
                snapshot_id="foreign-snapshot",
                sandbox_id="foreign-sandbox",
                created_at=(now - timedelta(hours=1)).isoformat(),
            ),
        ),
    )

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert report.deleted_sandboxes == 0
    assert report.deleted_snapshots == 0
    assert provider.deleted_sandboxes == []
    assert provider.deleted_snapshots == []
    assert store.foreign_session == foreign
    assert all(f"app_hash eq '{_app_hash()}'" in value for value in store.query_filters)


@pytest.mark.asyncio
async def test_reconciler_rotates_the_durable_cursor_across_bounded_pages() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    entities = tuple(
        _tombstoned_session(now, f"session-{index}").to_table_entity()
        for index in range(3)
    )
    store = RotatingPageStore(entities)
    reconciler = SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=()),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        config=ReconcilerConfig(page_size=1, max_pages=1),
        now=lambda: now,
    )

    await reconciler.run_once()
    await reconciler.run_once()
    await reconciler.run_once()
    await reconciler.run_once()

    assert store.page_starts == [None, "1", "2", None]
    cursor = await store.get_reconciler_cursor(_app_hash())
    assert cursor is not None
    assert cursor.continuation_token == "1"


@pytest.mark.asyncio
async def test_reconciler_retains_expired_owner_idempotency_for_live_session() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now)
    store = FakeSessionStateStore(session)
    expired = DurableOwnerIdempotencyRecord.create(
        owner_partition=session.owner_partition,
        idempotency_hash="a" * 64,
        request_hash="b" * 64,
        session_id=session.session_id,
        run_id="run-1",
        expires_at=now - timedelta(seconds=1),
        created_at=now - timedelta(hours=1),
    )
    store.owner_idempotency[expired.idempotency_hash] = expired
    store.owner_idempotency_etags[expired.idempotency_hash] = "expired-etag"

    await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert store.owner_idempotency == {expired.idempotency_hash: expired}


@pytest.mark.asyncio
async def test_reconciler_retains_owner_idempotency_for_long_running_first_run() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(
        now - timedelta(hours=1),
        status="running",
        active_run_id="run-1",
        expires_at=now - timedelta(seconds=1),
    )
    run = replace(_run(session, now), expires_at=now + timedelta(hours=2))
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    expired = DurableOwnerIdempotencyRecord.create(
        owner_partition=session.owner_partition,
        idempotency_hash="c" * 64,
        request_hash="d" * 64,
        session_id=session.session_id,
        run_id=run.run_id,
        expires_at=now - timedelta(seconds=1),
        created_at=now - timedelta(hours=1),
    )
    store.owner_idempotency[expired.idempotency_hash] = expired
    store.owner_idempotency_etags[expired.idempotency_hash] = "long-run-etag"

    await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert store.owner_idempotency == {expired.idempotency_hash: expired}


@pytest.mark.asyncio
async def test_expired_missing_heartbeat_reclaims_only_after_startup_grace() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now, status="running", active_run_id="run-1")
    run = _run(session, now)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    provider = InventoryProvider(sandboxes=(_sandbox(now),))
    verifier_calls = 0

    async def heartbeat_reader(
        _: DurableSessionRecord,
        __: DurableRunRecord,
    ) -> None:
        return None

    async def death_verifier(
        _: DurableSessionRecord,
        __: DurableRunRecord,
    ) -> None:
        nonlocal verifier_calls
        verifier_calls += 1
        return None

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        heartbeat_reader=heartbeat_reader,
        death_verifier=death_verifier,
        now=lambda: now,
    ).run_once()

    assert report.abandoned_runs == 0
    assert verifier_calls == 0
    assert provider.deleted_sandboxes == []

    expired_run = replace(run, expires_at=now - timedelta(seconds=301))
    store.runs[run.run_id] = expired_run
    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        heartbeat_reader=heartbeat_reader,
        death_verifier=death_verifier,
        now=lambda: now,
    ).run_once()

    assert report.abandoned_runs == 1
    assert report.tombstoned_sessions == 1
    assert verifier_calls == 1
    assert provider.deleted_sandboxes == ["sandbox-1"]


@pytest.mark.asyncio
async def test_reclaim_operation_blocks_successor_until_terminal_adoption_is_resolved() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now, status="running", active_run_id="run-1")
    run = replace(_run(session, now), expires_at=now - timedelta(seconds=301))
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    provider = InventoryProvider(sandboxes=(_sandbox(now),))
    terminal_reads = 0
    successor_blocked = False
    lifecycle_fences: list[SessionOperationFence] = []

    async def terminal_reader(
        _: DurableSessionRecord,
        __: DurableRunRecord,
    ) -> RunStatus | None:
        nonlocal successor_blocked, terminal_reads
        terminal_reads += 1
        if terminal_reads == 1:
            return None
        assert store.session is not None
        successor = session_with_admitted_run(
            session,
            "run-2",
            updated_at=now,
        )
        successor_run = DurableRunRecord.create(
            owner_partition=session.owner_partition,
            session_id=session.session_id,
            run_id="run-2",
            generation=session.generation,
            status="accepted",
            result_available=False,
            status_reason=None,
            expires_at=now + timedelta(minutes=1),
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(SessionNotAdmissibleError):
            await store.admit_run(AdmissionRecords.create(successor, successor_run))
        successor_blocked = True
        await store.adopt_terminal_run(
            replace(
                run,
                status="succeeded",
                result_available=False,
                updated_at=now,
            )
        )
        return RunStatus(
            run_id=run.run_id,
            session_id=run.session_id,
            state="succeeded",
            last_sequence=0,
            result_available=False,
        )

    async def heartbeat_reader(
        _: DurableSessionRecord,
        __: DurableRunRecord,
    ) -> None:
        return None

    async def apply_idle_lifecycle(fence: SessionOperationFence) -> bool:
        assert store.session is not None
        assert store.session.active_operation_id == fence.operation_id
        assert store.session.active_run_id == run.run_id
        assert not store.session.idle_policy_armed
        assert store.durable_operations[fence.operation_id].phase == "reclaim_rearm"
        lifecycle_fences.append(fence)
        return True

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        terminal_reader=terminal_reader,
        heartbeat_reader=heartbeat_reader,
        death_verifier=lambda _session, _run: _none(),
        idle_lifecycle_applier=apply_idle_lifecycle,
        reclaim_idle_seconds=120,
        now=lambda: now,
    ).run_once()

    assert successor_blocked
    assert len(lifecycle_fences) == 1
    assert report.adopted_terminal_runs == 1
    assert provider.deleted_sandboxes == []
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.active_run_id is None
    assert store.session.idle_policy_armed
    assert store.session.last_activity_at == now
    assert store.session.expires_at == now + timedelta(seconds=120)
    assert store.operations[-2:] == ["advance_operation", "completed_operation"]


async def _none() -> None:
    return None


def _expired_active_session(now: datetime) -> tuple[DurableSessionRecord, DurableRunRecord]:
    base_session = _session(now)
    session = session_with_admitted_run(base_session, "run-1", updated_at=now)
    run = replace(_run(base_session, now), expires_at=now - timedelta(seconds=301))
    return session, run


@pytest.mark.asyncio
async def test_targeted_reconciliation_resumes_a_persisted_reclaim_operation() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session, run = _expired_active_session(now)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    provider = FailOnceDeleteProvider(sandboxes=(_sandbox(now),))
    clock = [now]
    reconciler = SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: clock[0],
    )

    first = await reconciler.run_once()
    assert first.abandoned_runs == 0
    assert store.session is not None
    assert store.session.active_operation_id is not None

    clock[0] += timedelta(seconds=61)
    report = await reconciler.reconcile_session(session.owner_partition, session.session_id)

    assert report.abandoned_runs == 1
    assert report.tombstoned_sessions == 1
    assert provider.deleted_sandboxes == ["sandbox-1"]
    assert store.session is not None
    assert store.session.status == "tombstoned"


@pytest.mark.asyncio
async def test_targeted_reconciliation_defers_transient_per_session_failure() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session, run = _expired_active_session(now)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    provider = FailOnceDeleteProvider(sandboxes=(_sandbox(now),))
    clock = [now]
    reconciler = SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: clock[0],
    )

    first = await reconciler.reconcile_session(session.owner_partition, session.session_id)
    clock[0] += timedelta(seconds=61)
    second = await reconciler.reconcile_session(session.owner_partition, session.session_id)

    assert first.abandoned_runs == 0
    assert second.abandoned_runs == 1
    assert store.session is not None
    assert store.session.status == "tombstoned"


@pytest.mark.asyncio
async def test_targeted_reconciliation_quarantines_a_corrupt_journal() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(now)
    session = session_with_admitted_run(base, "run-1", updated_at=now)
    run = _run(base, now)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run

    async def terminal_reader(
        _: DurableSessionRecord,
        __: DurableRunRecord,
    ) -> RunStatus:
        raise RunJournalProtocolError("raw malformed journal payload")

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        terminal_reader=terminal_reader,
        now=lambda: now,
    ).reconcile_session(session.owner_partition, session.session_id)

    assert report.adopted_terminal_runs == 0
    assert store.runs[run.run_id].status == "failed"
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.operations[:2] == ["invalidate_journal", "update:quarantined"]


@pytest.mark.asyncio
async def test_provider_failure_leaves_fence_resumable_on_next_pass() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session, run = _expired_active_session(now)
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    provider = FailOnceDeleteProvider(sandboxes=(_sandbox(now),))
    clock = [now]
    reconciler = SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: clock[0],
    )

    first = await reconciler.run_once()
    clock[0] += timedelta(seconds=61)
    second = await reconciler.run_once()

    assert first.abandoned_runs == 0
    assert store.session is not None
    assert store.session.status == "tombstoned"
    assert second.abandoned_runs == 1
    assert provider.deleted_sandboxes == ["sandbox-1"]


@pytest.mark.asyncio
async def test_store_failure_leaves_fence_resumable_on_next_pass() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session, run = _expired_active_session(now)
    store = FailOnceOperationStore(session)
    store.runs[run.run_id] = run
    provider = InventoryProvider(sandboxes=(_sandbox(now),))
    clock = [now]
    reconciler = SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: clock[0],
    )

    first = await reconciler.run_once()
    clock[0] += timedelta(seconds=61)
    second = await reconciler.run_once()

    assert first.abandoned_runs == 0
    assert second.abandoned_runs == 1
    assert store.session is not None
    assert store.session.status == "tombstoned"


@pytest.mark.asyncio
async def test_reclaim_delete_then_crash_completes_when_target_is_missing_next_pass() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session, run = _expired_active_session(now)
    store = FailOnceOperationStore(session)
    store.runs[run.run_id] = run
    provider = DeleteThenMissingProvider(sandboxes=(_sandbox(now),))
    clock = [now]
    reconciler = SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: clock[0],
    )

    first = await reconciler.run_once()
    assert first.abandoned_runs == 0
    assert provider.deleted_sandboxes == ["sandbox-1"]
    assert store.session is not None
    assert store.session.active_operation_id is not None
    assert next(iter(store.durable_operations.values())).phase == "reclaim_deleting"

    clock[0] += timedelta(seconds=61)
    second = await reconciler.run_once()

    assert second.abandoned_runs == 1
    assert store.session.status == "tombstoned"
    assert store.session.active_operation_id is None


@pytest.mark.asyncio
async def test_reconciler_does_not_repair_an_operationless_idle_lifecycle() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = replace(_session(now), idle_policy_armed=False)
    store = FakeSessionStateStore(session)
    applied: list[str] = []

    async def apply_idle_lifecycle(fence: SessionOperationFence) -> bool:
        applied.append(fence.operation_id)
        return True

    await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        idle_lifecycle_applier=apply_idle_lifecycle,
        now=lambda: now,
    ).run_once()

    assert applied == []
    assert store.session is not None
    assert store.session.idle_policy_armed is False


@pytest.mark.asyncio
async def test_reconciler_prunes_completed_durable_operations() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = _session(now)
    store = FakeSessionStateStore(session)
    operation = DurableSessionOperation.create(
        owner_partition=session.owner_partition,
        target=SessionOperationTarget.create(
            session_id=session.session_id,
            sandbox_id=session.sandbox_id,
            generation=session.generation,
            digest_kind=session.digest_kind,
            digest=session.digest,
            run_id="run-1",
        ),
        sequence=1,
        kind="submit_run",
        phase="completed",
        state="completed",
        correlation_label="op-session-1-1",
        token="a" * 32,
        attempt_count=1,
        error_code=None,
        lease_expires_at=None,
        next_attempt_at=None,
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=2),
        finished_at=now - timedelta(days=2),
    )
    store.durable_operations[operation.operation_id] = operation

    await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert store.durable_operations == {}


@pytest.mark.asyncio
async def test_reconciler_preserves_provision_labeled_sandbox_before_pointer_phase() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(
        now - timedelta(minutes=10),
        status="creating",
        active_run_id="run-1",
        sandbox_id=None,
    )
    operation = DurableSessionOperation.create(
        owner_partition=base.owner_partition,
        target=SessionOperationTarget.create(
            session_id=base.session_id,
            sandbox_id=None,
            generation=base.generation,
            digest_kind=base.digest_kind,
            digest=base.digest,
            run_id="run-1",
        ),
        sequence=1,
        kind="provision_submit",
        phase="provision_create",
        state="active",
        correlation_label=operation_correlation_label(base.session_id, 1),
        token="a" * 32,
        attempt_count=0,
        error_code=None,
        lease_expires_at=now + timedelta(minutes=1),
        next_attempt_at=None,
        created_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=10),
        finished_at=None,
    )
    session = replace(
        base,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    labels = {
        "owner_hash_version": session.owner_partition.owner_hash_version,
        "owner_kind": session.owner_partition.owner_kind,
        "owner_hash": session.owner_partition.owner_hash,
        "app_hash": session.owner_partition.app_hash,
        "session_id": session.session_id,
        "operation_label": operation.correlation_label,
    }
    provider = InventoryProvider(
        sandboxes=(SandboxSummary.create(sandbox_id="created-1", labels=labels),)
    )
    store.durable_operations[operation.operation_id] = operation

    await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert provider.deleted_sandboxes == []


@pytest.mark.asyncio
async def test_reconciler_expires_a_no_retry_submit_operation_and_rearms_before_abort() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(now, status="running", active_run_id="run-1")
    run = replace(
        _run(base, now, status="accepted"),
        expires_at=now - timedelta(seconds=301),
    )
    operation = _submit_operation(base, run, now)
    session = replace(
        base,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    lifecycle_fences: list[SessionOperationFence] = []

    async def apply_idle_lifecycle(fence: SessionOperationFence) -> bool:
        assert store.session is not None
        assert store.session.active_operation_id == fence.operation_id
        assert store.session.active_run_id == run.run_id
        assert store.durable_operations[fence.operation_id].phase == "submit_rearm"
        lifecycle_fences.append(fence)
        return True

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        idle_lifecycle_applier=apply_idle_lifecycle,
        now=lambda: now,
    ).run_once()

    assert report.abandoned_runs == 1
    assert store.runs[run.run_id].status == "abandoned"
    assert len(lifecycle_fences) == 1
    assert store.session is not None
    assert store.session.idle_policy_armed
    assert store.operations[-2:] == ["advance_operation", "completed_operation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provision_submit", "submit_run"])
async def test_expired_submit_operation_with_lost_persisted_backing_tombstones_without_remote_reads(
    kind: str,
) -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(
        now,
        status="creating" if kind == "provision_submit" else "running",
        active_run_id="run-1",
    )
    run = _run(base, now, status="accepted")
    operation = (
        _submit_operation(base, run, now, lease_expires_at=now - timedelta(seconds=1))
        if kind == "submit_run"
        else replace(
            _provision_operation(base, run, now),
            target=SessionOperationTarget.create(
                session_id=base.session_id,
                sandbox_id=base.sandbox_id,
                generation=base.generation,
                digest_kind=base.digest_kind,
                digest=base.digest,
                run_id=run.run_id,
            ),
        )
    )
    session = replace(
        base,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    provider = InventoryProvider(sandboxes=())

    remote_reads: list[str] = []

    async def terminal_reader(_: DurableSessionRecord, __: DurableRunRecord) -> None:
        remote_reads.append("terminal")
        return None

    async def heartbeat_reader(_: DurableSessionRecord, __: DurableRunRecord) -> None:
        remote_reads.append("heartbeat")
        return None

    async def apply_idle_lifecycle(_: SessionOperationFence) -> bool:
        raise AssertionError("lost backing must not be rearmed")

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        terminal_reader=terminal_reader,
        heartbeat_reader=heartbeat_reader,
        idle_lifecycle_applier=apply_idle_lifecycle,
        now=lambda: now,
    ).run_once()

    assert provider.list_calls == 2
    assert remote_reads == []
    assert report.abandoned_runs == 1
    assert report.tombstoned_sessions == 1
    assert store.runs[run.run_id].status == "abandoned"
    assert store.runs[run.run_id].status_reason == "sandbox_backing_lost"
    assert store.session is not None
    assert store.session.status == "tombstoned"
    assert store.session.tombstone_reason == "sandbox_backing_lost"
    assert store.session.active_run_id is None
    assert store.session.active_operation_id is None
    assert store.durable_operations[operation.operation_id].state == "completed"
    assert store.operations[-2:] == ["takeover_expired_operation", "completed_operation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provision_submit", "submit_run"])
async def test_expired_submit_operation_freshly_finds_stale_inventory_backing(kind: str) -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(
        now,
        status="creating" if kind == "provision_submit" else "running",
        active_run_id="run-1",
    )
    run = _run(base, now, status="accepted")
    operation = (
        _submit_operation(base, run, now, lease_expires_at=now - timedelta(seconds=1))
        if kind == "submit_run"
        else replace(
            _provision_operation(base, run, now),
            target=SessionOperationTarget.create(
                session_id=base.session_id,
                sandbox_id=base.sandbox_id,
                generation=base.generation,
                digest_kind=base.digest_kind,
                digest=base.digest,
                run_id=run.run_id,
            ),
        )
    )
    session = replace(
        base,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    provider = InventoryProvider(sandboxes=(), refreshed_sandboxes=(_sandbox(now),))
    terminal_reads = 0

    async def terminal_reader(_: DurableSessionRecord, __: DurableRunRecord) -> None:
        nonlocal terminal_reads
        terminal_reads += 1
        return None

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        terminal_reader=terminal_reader,
        now=lambda: now,
    ).run_once()

    assert provider.list_calls == 2
    assert terminal_reads == 1
    assert report.tombstoned_sessions == 0
    assert store.session is not None
    assert store.session.status == base.status
    assert store.session.active_run_id == run.run_id
    assert store.session.active_operation_id == operation.operation_id
    assert store.durable_operations[operation.operation_id].state == "active"


@pytest.mark.asyncio
async def test_reconciler_rearms_terminal_submit_before_completion() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(now, status="running", active_run_id="run-1")
    run = _run(base, now, status="succeeded")
    operation = _submit_operation(base, run, now)
    session = replace(
        base,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    lifecycle_fences: list[SessionOperationFence] = []

    async def apply_idle_lifecycle(fence: SessionOperationFence) -> bool:
        assert store.session is not None
        assert store.session.active_operation_id == fence.operation_id
        assert store.session.active_run_id == run.run_id
        assert store.durable_operations[fence.operation_id].phase == "submit_rearm"
        lifecycle_fences.append(fence)
        return True

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        idle_lifecycle_applier=apply_idle_lifecycle,
        now=lambda: now,
    ).run_once()

    assert report.adopted_terminal_runs == 0
    assert len(lifecycle_fences) == 1
    assert store.session is not None
    assert store.session.idle_policy_armed
    assert store.operations[-2:] == ["advance_operation", "completed_operation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("created_sandbox", [False, True])
async def test_reconciler_expires_pre_pointer_provision_without_raw_orphan_delete(
    created_sandbox: bool,
) -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(
        now - timedelta(minutes=10),
        status="creating",
        active_run_id="run-1",
        sandbox_id=None,
    )
    run = replace(
        _run(base, now, status="accepted"),
        expires_at=now - timedelta(seconds=301),
    )
    operation = _provision_operation(base, run, now)
    session = replace(
        base,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    labels = {
        "owner_hash_version": session.owner_partition.owner_hash_version,
        "owner_kind": session.owner_partition.owner_kind,
        "owner_hash": session.owner_partition.owner_hash,
        "app_hash": session.owner_partition.app_hash,
        "session_id": session.session_id,
        "operation_label": operation.correlation_label,
    }
    provider = InventoryProvider(
        sandboxes=(
            (SandboxSummary.create(sandbox_id="created-1", labels=labels),)
            if created_sandbox
            else ()
        )
    )

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: now,
    ).run_once()

    assert report.tombstoned_sessions == 1
    assert store.session is not None
    assert store.session.status == "tombstoned"
    if created_sandbox:
        assert provider.deleted_sandboxes == ["created-1"]
    else:
        assert provider.deleted_sandboxes == []


@pytest.mark.asyncio
async def test_targeted_reconcile_aborts_expired_missing_submit_run() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(now, status="running", active_run_id="run-1")
    run = _run(base, now, status="accepted")
    operation = _submit_operation(
        base,
        run,
        now,
        lease_expires_at=now - timedelta(seconds=1),
    )
    session = replace(
        base,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.durable_operations[operation.operation_id] = operation
    lifecycle_fences: list[SessionOperationFence] = []

    async def apply_idle_lifecycle(fence: SessionOperationFence) -> bool:
        assert store.session is not None
        assert store.session.active_operation_id == fence.operation_id
        assert store.session.active_run_id == run.run_id
        assert store.durable_operations[fence.operation_id].phase == "submit_rearm"
        lifecycle_fences.append(fence)
        return True

    await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        idle_lifecycle_applier=apply_idle_lifecycle,
        reclaim_idle_seconds=120,
        now=lambda: now,
    ).reconcile_session(session.owner_partition, session.session_id)

    assert len(lifecycle_fences) == 1
    assert store.session is not None
    assert store.session.active_operation_id is None
    assert store.session.active_run_id is None
    assert store.session.idle_policy_armed
    assert store.session.last_activity_at == now
    assert store.session.expires_at == now + timedelta(seconds=120)
    assert store.durable_operations[operation.operation_id].state == "aborted"
    assert store.operations[-2:] == ["advance_operation", "aborted_operation"]


@pytest.mark.asyncio
async def test_reclaim_terminal_lifecycle_failure_retries_with_the_active_fence() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(now, status="running", active_run_id="run-1")
    run = _run(base, now, status="succeeded")
    operation = _reclaim_operation(
        base,
        run,
        now,
        lease_expires_at=now - timedelta(seconds=1),
    )
    session = replace(
        base,
        idle_policy_armed=False,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    calls = 0
    clock = [now]

    async def apply_idle_lifecycle(fence: SessionOperationFence) -> bool:
        nonlocal calls
        calls += 1
        assert store.durable_operations[fence.operation_id].phase == "reclaim_rearm"
        if calls == 1:
            raise SandboxProvisioningError("transient lifecycle failure")
        return True

    reconciler = SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        idle_lifecycle_applier=apply_idle_lifecycle,
        reclaim_idle_seconds=120,
        now=lambda: clock[0],
    )

    first = await reconciler.reconcile_session(session.owner_partition, session.session_id)

    assert first.adopted_terminal_runs == 0
    assert store.session is not None
    assert store.session.active_operation_id == operation.operation_id
    assert store.session.active_run_id == run.run_id
    assert not store.session.idle_policy_armed
    assert store.durable_operations[operation.operation_id].phase == "reclaim_rearm"
    assert (
        store.durable_operations[operation.operation_id].error_code
        == "lifecycle_policy_apply_failed"
    )

    clock[0] += timedelta(seconds=61)
    second = await reconciler.reconcile_session(session.owner_partition, session.session_id)

    assert second.adopted_terminal_runs == 1
    assert calls == 2
    assert store.session.active_operation_id is None
    assert store.session.idle_policy_armed
    assert store.durable_operations[operation.operation_id].state == "completed"


@pytest.mark.asyncio
async def test_stale_reclaim_fence_prevents_idle_lifecycle_application() -> None:
    class StaleAtRearmStore(FakeSessionStateStore):
        async def advance_operation(self, **kwargs: object):  # type: ignore[no-untyped-def]
            if kwargs["phase"] == "reclaim_rearm" and kwargs.get("error_code") is None:
                fence = kwargs["fence"]
                assert isinstance(fence, SessionOperationFence)
                current = self.durable_operations[fence.operation_id]
                self.durable_operations[fence.operation_id] = replace(
                    current,
                    token="e" * 32,
                )
            return await super().advance_operation(**kwargs)

    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(now, status="running", active_run_id="run-1")
    run = _run(base, now, status="succeeded")
    operation = _reclaim_operation(
        base,
        run,
        now,
        lease_expires_at=now - timedelta(seconds=1),
    )
    session = replace(
        base,
        idle_policy_armed=False,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = StaleAtRearmStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    applied: list[str] = []

    async def apply_idle_lifecycle(fence: SessionOperationFence) -> bool:
        applied.append(fence.operation_id)
        return True

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        idle_lifecycle_applier=apply_idle_lifecycle,
        now=lambda: now,
    ).reconcile_session(session.owner_partition, session.session_id)

    assert report.adopted_terminal_runs == 0
    assert applied == []
    assert store.session is not None
    assert store.session.active_operation_id == operation.operation_id
    assert not store.session.idle_policy_armed


@pytest.mark.asyncio
async def test_reclaim_deleting_does_not_rearm_a_terminal_run() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(now, status="running", active_run_id="run-1")
    run = _run(base, now, status="succeeded")
    operation = replace(
        _reclaim_operation(
            base,
            run,
            now,
            lease_expires_at=now - timedelta(seconds=1),
        ),
        phase="reclaim_deleting",
    )
    session = replace(
        base,
        idle_policy_armed=False,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.runs[run.run_id] = run
    store.durable_operations[operation.operation_id] = operation
    applied: list[str] = []

    async def apply_idle_lifecycle(fence: SessionOperationFence) -> bool:
        applied.append(fence.operation_id)
        return True

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        idle_lifecycle_applier=apply_idle_lifecycle,
        now=lambda: now,
    ).reconcile_session(session.owner_partition, session.session_id)

    assert report.tombstoned_sessions == 1
    assert applied == []
    assert store.session is not None
    assert store.session.status == "tombstoned"
    assert store.durable_operations[operation.operation_id].state == "completed"


@pytest.mark.asyncio
async def test_missing_submit_run_rearm_preserves_quarantine() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    base = _session(now)
    run = _run(base, now, status="accepted")
    operation = _submit_operation(
        base,
        run,
        now,
        lease_expires_at=now - timedelta(seconds=1),
    )
    session = replace(
        base,
        status="quarantined",
        idle_policy_armed=False,
        active_run_id=None,
        quarantine_reason="sandbox_manifest_mismatch",
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    store = FakeSessionStateStore(session)
    store.durable_operations[operation.operation_id] = operation

    async def apply_idle_lifecycle(fence: SessionOperationFence) -> bool:
        assert store.durable_operations[fence.operation_id].phase == "submit_rearm"
        return True

    reconciler = SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        idle_lifecycle_applier=apply_idle_lifecycle,
        reclaim_idle_seconds=120,
        now=lambda: now,
    )

    await reconciler._recover_missing_submit_run(session, now, reconciler_report := ReconcileReport())

    assert reconciler_report == ReconcileReport()
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.quarantine_reason == "sandbox_manifest_mismatch"
    assert store.session.idle_policy_armed
    assert store.session.active_operation_id is None


@pytest.mark.asyncio
async def test_service_time_prevents_clock_skew_from_reaping_early() -> None:
    service_time = datetime(2026, 8, 5, tzinfo=UTC)
    controller_time = service_time + timedelta(days=1)
    session = _session(
        service_time - timedelta(hours=1),
        expires_at=service_time + timedelta(minutes=1),
    )
    terminal = _run(session, service_time, status="succeeded")
    store = ServiceTimeStore(session, service_time)
    store.runs[terminal.run_id] = terminal

    report = await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(service_time),)),  # type: ignore[arg-type]
        app_hash=_app_hash(),
        now=lambda: controller_time,
    ).run_once()

    assert report.tombstoned_sessions == 0
    assert store.session is not None
    assert store.session.status == "ready"
