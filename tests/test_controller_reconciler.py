from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from azure_functions_agents.controller.reconciler import (
    ReconcilerConfig,
    SessionReconciler,
    reconciler_ncrontab,
    resolve_reconciler_cadence,
)
from azure_functions_agents.execution.backend import RunError, RunStatus
from azure_functions_agents.session_state import (
    AppIdentity,
    DurableRunRecord,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    TableEntityPage,
    owner_partition,
)
from azure_functions_agents.transport.transport_models import SandboxSnapshot, SandboxSummary
from tests.doubles.fake_session_runtime import FakeSessionStateStore


class InventoryProvider:
    def __init__(
        self,
        *,
        sandboxes: tuple[SandboxSummary, ...],
        snapshots: tuple[SandboxSnapshot, ...] = (),
    ) -> None:
        self.sandboxes = sandboxes
        self.snapshots = {snapshot.snapshot_id: snapshot for snapshot in snapshots}
        self.deleted_sandboxes: list[str] = []
        self.deleted_snapshots: list[str] = []

    async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[SandboxSummary, ...]:
        return tuple(
            sandbox
            for sandbox in self.sandboxes
            if all(sandbox.labels.get(key) == value for key, value in labels.items())
        )

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted_sandboxes.append(sandbox_id)

    async def list_snapshots(self) -> tuple[SandboxSnapshot, ...]:
        return tuple(self.snapshots.values())

    async def delete_snapshot(self, snapshot_id: str) -> None:
        self.deleted_snapshots.append(snapshot_id)
        self.snapshots.pop(snapshot_id, None)


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


def _owner() -> FunctionAppOwnerContext:
    return FunctionAppOwnerContext.create(
        AppIdentity.create(
            subscription_id="11111111-2222-3333-4444-555555555555",
            site_name="agent-app",
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


def _sandbox(now: datetime, sandbox_id: str = "sandbox-1") -> SandboxSummary:
    return SandboxSummary.create(
        sandbox_id=sandbox_id,
        labels={},
        created_at=(now - timedelta(hours=1)).isoformat(),
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
        terminal_reader=terminal_reader,
        now=lambda: now,
    ).run_once()

    assert report.adopted_terminal_runs == 1
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.runs["run-1"].status == "failed"


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
        now=lambda: now,
    ).run_once()

    assert report.abandoned_runs == 1
    assert report.tombstoned_sessions == 1
    assert store.runs["run-1"].status == "abandoned"
    assert store.session is not None
    assert store.session.status == "tombstoned"


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
                sandbox_id=None,
                created_at=(now - timedelta(hours=1)).isoformat(),
            ),
        ),
    )

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
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
        now=lambda: now,
    ).run_once()

    assert report.deleted_sandboxes == 1
    assert provider.deleted_sandboxes == ["platform-orphan"]


@pytest.mark.asyncio
async def test_partial_scan_never_deletes_platform_orphans() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    store = PartialScanStore(_session(now))
    provider = InventoryProvider(
        sandboxes=(
            _sandbox(now, "sandbox-1"),
            _sandbox(now, "platform-orphan"),
        )
    )

    report = await SessionReconciler(
        store=store,
        provider=provider,  # type: ignore[arg-type]
        config=ReconcilerConfig(max_pages=1),
        now=lambda: now,
    ).run_once()

    assert report.deleted_sandboxes == 0
    assert provider.deleted_sandboxes == []


@pytest.mark.asyncio
async def test_reconciler_repairs_previously_unarmed_idle_lifecycle() -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session = replace(_session(now), idle_policy_armed=False)
    store = FakeSessionStateStore(session)
    repaired: list[str] = []

    async def repair(record: DurableSessionRecord) -> bool:
        repaired.append(record.session_id)
        return True

    await SessionReconciler(
        store=store,
        provider=InventoryProvider(sandboxes=(_sandbox(now),)),  # type: ignore[arg-type]
        lifecycle_repair=repair,
        now=lambda: now,
    ).run_once()

    assert repaired == ["session-1"]
    assert store.session is not None
    assert store.session.idle_policy_armed


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
        now=lambda: controller_time,
    ).run_once()

    assert report.tombstoned_sessions == 0
    assert store.session is not None
    assert store.session.status == "ready"
