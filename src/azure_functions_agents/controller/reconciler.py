"""One-pass, provider-neutral lifecycle reconciliation for sandbox sessions."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .._logger import logger
from ..execution.backend import RunStatus
from ..sandbox_runtime_limits import (
    DEFAULT_RECONCILER_CADENCE_SECONDS,
    MAX_RECONCILER_CADENCE_SECONDS,
    RECLAIM_SAFETY_GRACE_SECONDS,
)
from ..session_state import (
    OWNER_CANONICALIZERS,
    TERMINAL_RUN_STATUSES,
    ConcurrencyConflictError,
    DurableIdempotencyRecord,
    DurableOwnerIdempotencyRecord,
    DurableRunRecord,
    DurableSessionRecord,
    IdempotencyRowKey,
    OwnerIdempotencyRowKey,
    OwnerPartition,
    ReclaimFence,
    RunRowKey,
    RunRowNotFoundError,
    SessionRead,
    SessionRowKey,
    SessionRowNotFoundError,
    SessionStateContractError,
    SessionStateStore,
    SessionStatus,
    parse_row_key,
    validate_session_id,
)
from ..transport.ports import SandboxSessionProvider
from ..transport.transport_models import (
    SandboxSnapshot,
    SandboxSummary,
    SandboxTransportError,
)
from .readiness import terminal_run

_RECLAIMABLE_SESSION_STATUSES = frozenset({"creating", "ready", "quarantined"})
_STATUSES_REQUIRING_BACKING = frozenset(
    {"ready", "running", "canceling", "suspended", "resuming", "quarantined"}
)
RECONCILER_CADENCE_SETTING = "AZURE_FUNCTIONS_AGENTS_RECONCILER_CADENCE_SECONDS"

type TerminalReader = Callable[[DurableSessionRecord, DurableRunRecord], Awaitable[RunStatus | None]]
type DeathVerifier = Callable[[DurableSessionRecord, DurableRunRecord], Awaitable[bool | None]]
type HeartbeatReader = Callable[[DurableSessionRecord, DurableRunRecord], Awaitable[datetime | None]]
type LifecycleRepair = Callable[[DurableSessionRecord], Awaitable[bool]]
type _SessionKey = tuple[str, str]

_SESSION_RECONCILIATION_ERRORS = (
    ConcurrencyConflictError,
    RunRowNotFoundError,
    SessionRowNotFoundError,
    SandboxTransportError,
)


@dataclass(slots=True)
class ReconcilerConfig:
    """Fixed safety limits and bounded scan policy for one reconciliation pass."""

    cadence_seconds: int = DEFAULT_RECONCILER_CADENCE_SECONDS
    safety_grace_seconds: int = RECLAIM_SAFETY_GRACE_SECONDS
    heartbeat_stale_seconds: int = 90
    result_hold_seconds: int = 300
    terminal_retention_seconds: int = 86_400
    tombstone_retention_seconds: int = 86_400
    page_size: int = 100
    max_pages: int = 10

    def __post_init__(self) -> None:
        if (
            self.cadence_seconds < 60
            or self.cadence_seconds > MAX_RECONCILER_CADENCE_SECONDS
            or self.cadence_seconds % 60 != 0
        ):
            raise ValueError(
                "cadence_seconds must be a whole-minute value from 60 through "
                f"{MAX_RECONCILER_CADENCE_SECONDS}"
            )
        if self.safety_grace_seconds <= 0 or self.heartbeat_stale_seconds <= 0:
            raise ValueError("reconciler safety intervals must be positive")
        if (
            self.result_hold_seconds <= 0
            or self.terminal_retention_seconds <= 0
            or self.tombstone_retention_seconds <= 0
            or self.page_size <= 0
            or self.max_pages <= 0
        ):
            raise ValueError("reconciler bounds must be positive")


def resolve_reconciler_cadence(
    value: str | None = None,
    *,
    environ: Callable[[str], str | None] = os.getenv,
) -> int:
    """Read the app setting as a whole-minute cadence no slower than one hour."""
    raw = value if value is not None else environ(RECONCILER_CADENCE_SETTING)
    if raw is None or not raw.strip():
        return DEFAULT_RECONCILER_CADENCE_SECONDS
    try:
        cadence = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{RECONCILER_CADENCE_SETTING} must be an integer number of seconds"
        ) from exc
    ReconcilerConfig(cadence_seconds=cadence)
    return cadence


def reconciler_ncrontab(cadence_seconds: int) -> str:
    """Render the six-field Functions timer expression for a validated cadence."""
    ReconcilerConfig(cadence_seconds=cadence_seconds)
    if cadence_seconds == MAX_RECONCILER_CADENCE_SECONDS:
        return "0 0 * * * *"
    return f"0 */{cadence_seconds // 60} * * * *"


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Observed state changes made during one bounded reconciliation pass."""

    adopted_terminal_runs: int = 0
    abandoned_runs: int = 0
    tombstoned_sessions: int = 0
    deleted_sandboxes: int = 0
    deleted_snapshots: int = 0
    evicted_results: int = 0


class SessionReconciler:
    """Reconcile durable rows against provider inventory without an Azure SDK dependency."""

    def __init__(
        self,
        *,
        store: SessionStateStore,
        provider: SandboxSessionProvider,
        app_hash: str,
        config: ReconcilerConfig | None = None,
        terminal_reader: TerminalReader | None = None,
        heartbeat_reader: HeartbeatReader | None = None,
        death_verifier: DeathVerifier | None = None,
        lifecycle_repair: LifecycleRepair | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._provider = provider
        if not app_hash:
            raise ValueError("app_hash must be non-empty")
        self._app_hash = app_hash
        self._config = config or ReconcilerConfig()
        self._terminal_reader = terminal_reader
        self._heartbeat_reader = heartbeat_reader
        self._death_verifier = death_verifier
        self._lifecycle_repair = lifecycle_repair
        self._now = now

    async def run_once(self) -> ReconcileReport:
        """Perform one bounded, idempotent pass over Table records and platform inventory."""
        controller_now = _utc(self._now())
        sessions, runs, idempotencies, service_time = await self._load_working_set()
        now = service_time or controller_now
        inventory = {
            item.sandbox_id: item
            for item in await self._provider.list_sandboxes(
                labels={"app_hash": self._app_hash}
            )
        }
        snapshots = await self._provider.list_snapshots()
        snapshots_by_id = {snapshot.snapshot_id: snapshot for snapshot in snapshots}
        report = ReconcileReport()

        sessions, runs = await self._hydrate_page_pairs(sessions, runs)
        runs_by_session = _runs_by_session(runs)

        for session in sessions:
            try:
                session_runs = runs_by_session.get(_session_key(session), ())
                active_run = _active_run(session, session_runs)
                if active_run is not None:
                    report = await self._reconcile_active(
                        session,
                        active_run,
                        inventory,
                        now,
                        report,
                    )
                    continue
                report = await self._reconcile_idle(
                    session,
                    session_runs,
                    inventory,
                    snapshots_by_id,
                    now,
                    report,
                )
            except _SESSION_RECONCILIATION_ERRORS as exc:
                logger.warning(
                    "Sandbox session reconciliation deferred: session_id=%s error=%s",
                    session.session_id,
                    type(exc).__name__,
                )

        report = await self._prune_expired_records(
            sessions,
            runs,
            idempotencies,
            now,
            report,
        )
        report = await self._reconcile_labeled_orphans(
            inventory,
            snapshots,
            now,
            report,
        )
        return report

    async def _load_working_set(
        self,
    ) -> tuple[
        tuple[DurableSessionRecord, ...],
        tuple[DurableRunRecord, ...],
        tuple[DurableIdempotencyRecord | DurableOwnerIdempotencyRecord, ...],
        datetime | None,
    ]:
        sessions: list[DurableSessionRecord] = []
        runs: list[DurableRunRecord] = []
        idempotencies: list[DurableIdempotencyRecord | DurableOwnerIdempotencyRecord] = []
        service_times: list[datetime] = []
        cursor = await self._store.get_reconciler_cursor(self._app_hash)
        continuation = None if cursor is None else cursor.continuation_token
        for _ in range(self._config.max_pages):
            page = await self._store.query_entities(
                filter_expression=_app_scoped_query_filter(self._app_hash),
                top=self._config.page_size,
                continuation_token=continuation,
            )
            for entity in page.entities:
                try:
                    row_key = parse_row_key(str(entity["RowKey"]))
                except (KeyError, SessionStateContractError):
                    continue
                if isinstance(row_key, RunRowKey):
                    try:
                        run = DurableRunRecord.from_table_entity(entity)
                    except SessionStateContractError:
                        continue
                    if run.owner_partition.app_hash == self._app_hash:
                        runs.append(run)
                elif isinstance(row_key, IdempotencyRowKey):
                    try:
                        idempotency = DurableIdempotencyRecord.from_table_entity(entity)
                    except SessionStateContractError:
                        continue
                    if idempotency.owner_partition.app_hash == self._app_hash:
                        idempotencies.append(idempotency)
                elif isinstance(row_key, OwnerIdempotencyRowKey):
                    try:
                        owner_idempotency = DurableOwnerIdempotencyRecord.from_table_entity(
                            entity
                        )
                    except SessionStateContractError:
                        continue
                    if owner_idempotency.owner_partition.app_hash == self._app_hash:
                        idempotencies.append(owner_idempotency)
                elif isinstance(row_key, SessionRowKey):
                    try:
                        session = DurableSessionRecord.from_table_entity(entity)
                    except SessionStateContractError:
                        continue
                    if session.owner_partition.app_hash == self._app_hash:
                        sessions.append(session)
            if page.service_time is not None:
                service_times.append(_utc(page.service_time))
            continuation = page.continuation_token
            if continuation is None:
                break
        with suppress(ConcurrencyConflictError):
            await self._store.advance_reconciler_cursor(
                app_hash=self._app_hash,
                previous=cursor,
                continuation_token=continuation,
            )
        return (
            tuple(sessions),
            tuple(runs),
            tuple(idempotencies),
            max(service_times, default=None),
        )

    async def _hydrate_page_pairs(
        self,
        sessions: tuple[DurableSessionRecord, ...],
        runs: tuple[DurableRunRecord, ...],
    ) -> tuple[tuple[DurableSessionRecord, ...], tuple[DurableRunRecord, ...]]:
        """Fill page-split session/run pairs with bounded exact reads."""
        sessions_by_key = {_session_key(session): session for session in sessions}
        runs_by_key = {_run_key(run): run for run in runs}
        for session in tuple(sessions_by_key.values()):
            if session.active_run_id is None:
                continue
            run_key = _run_key_from_session(session, session.active_run_id)
            if run_key in runs_by_key:
                continue
            try:
                run_read = await self._store.get_run(
                    session.owner_partition,
                    session.session_id,
                    session.active_run_id,
                )
            except RunRowNotFoundError:
                continue
            runs_by_key[run_key] = run_read.record
        for loaded_run in tuple(runs_by_key.values()):
            session_key = _session_key_from_run(loaded_run)
            if session_key in sessions_by_key:
                continue
            try:
                session_read = await self._store.get_session(
                    loaded_run.owner_partition,
                    loaded_run.session_id,
                )
            except SessionRowNotFoundError:
                continue
            sessions_by_key[session_key] = session_read.record
        return tuple(sessions_by_key.values()), tuple(runs_by_key.values())

    async def _reconcile_active(
        self,
        session: DurableSessionRecord,
        run: DurableRunRecord,
        inventory: dict[str, SandboxSummary],
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        if session.status == "reclaiming":
            return await self._resume_reclaim_fence(
                session,
                run,
                inventory,
                now,
                report,
            )
        if run.status in TERMINAL_RUN_STATUSES:
            outcome = await self._store.adopt_terminal_run(run)
            if outcome.slot_released and self._lifecycle_repair is not None:
                await self._lifecycle_repair(session)
            return _replace_report(
                report,
                adopted_terminal_runs=report.adopted_terminal_runs + int(outcome.slot_released),
            )
        if session.sandbox_id not in inventory:
            return await self._fence_missing_active_backing(session, run, now, report)

        terminal = (
            None
            if self._terminal_reader is None
            else await self._terminal_reader(session, run)
        )
        if terminal is not None and terminal.state in TERMINAL_RUN_STATUSES:
            outcome = await self._store.adopt_terminal_run(
                terminal_run(
                    run,
                    status=terminal.state,
                    result_available=terminal.result_available,
                    reason=_terminal_reason(terminal),
                    updated_at=now,
                )
            )
            if outcome.slot_released and self._lifecycle_repair is not None:
                await self._lifecycle_repair(session)
            return _replace_report(
                report,
                adopted_terminal_runs=report.adopted_terminal_runs + int(outcome.slot_released),
            )

        heartbeat = (
            None
            if self._heartbeat_reader is None
            else await self._heartbeat_reader(session, run)
        )
        deadline_elapsed = now >= run.expires_at + timedelta(
            seconds=self._config.safety_grace_seconds
        )
        if heartbeat is None:
            if not deadline_elapsed:
                return report
        elif _utc(heartbeat) > now - timedelta(seconds=self._config.heartbeat_stale_seconds):
            return report
        verified_dead = (
            None
            if self._death_verifier is None
            else await self._death_verifier(session, run)
        )
        if verified_dead is True:
            return await self._mark_abandoned_intact(session, run, now, report)
        if verified_dead is False:
            return report
        return await self._fence_then_reclaim(session, run, now, report)

    async def _fence_missing_active_backing(
        self,
        observed: DurableSessionRecord,
        run: DurableRunRecord,
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        """Fence a missing backing before terminalizing its active run."""
        latest = await self._store.get_session(observed.owner_partition, observed.session_id)
        if (
            latest.record.status != observed.status
            or latest.record.active_run_id != run.run_id
            or latest.record.sandbox_id != observed.sandbox_id
        ):
            return report
        fresh_inventory = {
            item.sandbox_id
            for item in await self._provider.list_sandboxes(
                labels={"app_hash": self._app_hash}
            )
        }
        if latest.record.sandbox_id is not None and latest.record.sandbox_id in fresh_inventory:
            return report
        current_run = await self._store.get_run(run.owner_partition, run.session_id, run.run_id)
        fence = await self._store.acquire_reclaim_fence(
            session=latest.record,
            run=current_run.record,
            token=uuid4().hex,
            updated_at=now,
        )
        if fence is None:
            return report
        return await self._continue_reclaim_fence(
            latest.record,
            current_run.record,
            fence,
            backing_present=False,
            now=now,
            report=report,
        )

    async def _mark_abandoned_intact(
        self,
        session: DurableSessionRecord,
        run: DurableRunRecord,
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        outcome = await self._store.adopt_terminal_run(
            terminal_run(
                run,
                status="abandoned",
                result_available=False,
                reason="verified_harness_death",
                updated_at=now,
            )
        )
        if outcome.slot_released and self._lifecycle_repair is not None:
            await self._lifecycle_repair(session)
        return _replace_report(
            report,
            abandoned_runs=report.abandoned_runs + int(outcome.slot_released),
        )

    async def _fence_then_reclaim(
        self,
        session: DurableSessionRecord,
        run: DurableRunRecord,
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        fence = await self._store.acquire_reclaim_fence(
            session=session,
            run=run,
            token=uuid4().hex,
            updated_at=now,
        )
        if fence is None:
            return report
        return await self._continue_reclaim_fence(
            session,
            run,
            fence,
            backing_present=True,
            now=now,
            report=report,
        )

    async def _resume_reclaim_fence(
        self,
        session: DurableSessionRecord,
        run: DurableRunRecord,
        inventory: dict[str, SandboxSummary],
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        token = session.reclaim_fence_token
        if token is None:
            return report
        fence = await self._store.acquire_reclaim_fence(
            session=session,
            run=run,
            token=token,
            updated_at=now,
        )
        if fence is None:
            return report
        return await self._continue_reclaim_fence(
            session,
            run,
            fence,
            backing_present=session.sandbox_id in inventory,
            now=now,
            report=report,
        )

    async def _continue_reclaim_fence(
        self,
        session: DurableSessionRecord,
        run: DurableRunRecord,
        fence: ReclaimFence,
        *,
        backing_present: bool,
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        terminal = (
            None
            if self._terminal_reader is None or not backing_present
            else await self._terminal_reader(session, run)
        )
        if terminal is not None and terminal.state in TERMINAL_RUN_STATUSES:
            outcome = await self._store.resolve_reclaim_fence_terminal(
                fence=fence,
                terminal_run=terminal_run(
                    run,
                    status=terminal.state,
                    result_available=terminal.result_available,
                    reason=_terminal_reason(terminal),
                    updated_at=now,
                ),
            )
            if outcome is None:
                return report
            if self._lifecycle_repair is not None:
                await self._lifecycle_repair(session)
            return _replace_report(
                report,
                adopted_terminal_runs=report.adopted_terminal_runs + int(outcome.slot_released),
            )
        current_session = await self._store.get_session(fence.owner_partition, fence.session_id)
        if not fence.matches(current_session.record):
            return report
        current_run = await self._store.get_run(fence.owner_partition, fence.session_id, fence.run_id)
        if current_run.record.status in TERMINAL_RUN_STATUSES:
            outcome = await self._store.resolve_reclaim_fence_terminal(
                fence=fence,
                terminal_run=current_run.record,
            )
            if outcome is None:
                return report
            if self._lifecycle_repair is not None:
                await self._lifecycle_repair(session)
            return _replace_report(
                report,
                adopted_terminal_runs=report.adopted_terminal_runs + int(outcome.slot_released),
            )
        if not backing_present:
            return await self._tombstone_reclaim_fence(
                current_run.record,
                fence,
                now,
                report,
            )
        before_delete = await self._store.get_session(
            fence.owner_partition,
            fence.session_id,
        )
        if not fence.matches(before_delete.record):
            return report
        await self._provider.delete_sandbox(fence.sandbox_id)
        return await self._tombstone_reclaim_fence(
            current_run.record,
            fence,
            now,
            report,
        )

    async def _tombstone_reclaim_fence(
        self,
        run: DurableRunRecord,
        fence: ReclaimFence,
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        outcome = await self._store.tombstone_reclaim_fence(
            fence=fence,
            terminal_run=terminal_run(
                run,
                status="abandoned",
                result_available=False,
                reason="sandbox_backing_lost",
                updated_at=now,
            ),
            tombstone_reason="sandbox_backing_lost",
            updated_at=now,
        )
        if outcome is None:
            return report
        return _replace_report(
            report,
            abandoned_runs=report.abandoned_runs + 1,
            tombstoned_sessions=report.tombstoned_sessions + 1,
        )

    async def _reconcile_idle(
        self,
        session: DurableSessionRecord,
        runs: tuple[DurableRunRecord, ...],
        inventory: dict[str, SandboxSummary],
        snapshots: dict[str, SandboxSnapshot],
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        if session.status == "deleting":
            return await self._finish_deleting(session, inventory, snapshots, now, report)
        if session.status == "creating":
            if _is_older_than(session.created_at, now, self._config.safety_grace_seconds):
                return await self._begin_reclaim(session, inventory, snapshots, now, report)
            return report
        if (
            session.status == "ready"
            and not session.idle_policy_armed
            and session.sandbox_id in inventory
            and self._lifecycle_repair is not None
        ):
            repaired = await self._lifecycle_repair(session)
            if repaired:
                latest = await self._store.get_session(session.owner_partition, session.session_id)
                if latest.record.status == "ready" and not latest.record.idle_policy_armed:
                    session = _rearmed_session(latest, now)
                    await self._store.update_session(
                        previous=latest.record,
                        updated=session,
                        etag=latest.etag,
                    )
        if session.status in _STATUSES_REQUIRING_BACKING and (
            session.sandbox_id is None or session.sandbox_id not in inventory
        ):
            return await self._tombstone_missing_backing_if_unchanged(session, now, report)

        for run in runs:
            if (
                run.status in TERMINAL_RUN_STATUSES
                and run.result_available
                and now >= max(
                    run.updated_at + timedelta(seconds=self._config.result_hold_seconds),
                    session.expires_at,
                )
            ):
                run_read = await self._store.get_run(run.owner_partition, run.session_id, run.run_id)
                await self._store.evict_run_result(
                    previous=run_read.record,
                    etag=run_read.etag,
                    updated_at=now,
                )
                report = _replace_report(report, evicted_results=report.evicted_results + 1)

        no_run_candidate = session.status == "ready" and not runs
        due = session.expires_at <= now - timedelta(seconds=self._config.safety_grace_seconds)
        if session.status in _RECLAIMABLE_SESSION_STATUSES and due:
            return await self._begin_reclaim(session, inventory, snapshots, now, report)
        if no_run_candidate and _is_older_than(
            session.created_at,
            now,
            self._config.safety_grace_seconds,
        ):
            return await self._begin_reclaim(session, inventory, snapshots, now, report)
        return report

    async def _tombstone_missing_backing_if_unchanged(
        self,
        observed: DurableSessionRecord,
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        """Use a fresh row and inventory read before treating live backing as lost."""
        latest = await self._store.get_session(observed.owner_partition, observed.session_id)
        if (
            latest.record.status != observed.status
            or latest.record.sandbox_id != observed.sandbox_id
            or latest.record.status not in _STATUSES_REQUIRING_BACKING
        ):
            return report
        fresh_inventory = {
            item.sandbox_id
            for item in await self._provider.list_sandboxes(
                labels={"app_hash": self._app_hash}
            )
        }
        if latest.record.sandbox_id is not None and latest.record.sandbox_id in fresh_inventory:
            return report
        await self._store.tombstone_session(
            previous=latest.record,
            etag=latest.etag,
            tombstone_reason="sandbox_backing_lost",
            updated_at=now,
        )
        return _replace_report(
            report,
            tombstoned_sessions=report.tombstoned_sessions + 1,
        )

    async def _begin_reclaim(
        self,
        session: DurableSessionRecord,
        inventory: dict[str, SandboxSummary],
        snapshots: dict[str, SandboxSnapshot],
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        latest = await self._store.get_session(session.owner_partition, session.session_id)
        if (
            latest.record.active_run_id is not None
            or latest.record.status != session.status
            or latest.record.sandbox_id != session.sandbox_id
            or latest.record.created_at != session.created_at
            or latest.record.expires_at != session.expires_at
        ):
            return report
        deleting = _with_status(latest, "deleting", now)
        try:
            await self._store.update_session(
                previous=latest.record,
                updated=deleting,
                etag=latest.etag,
            )
        except ConcurrencyConflictError:
            return report
        return await self._finish_deleting(deleting, inventory, snapshots, now, report)

    async def _finish_deleting(
        self,
        session: DurableSessionRecord,
        inventory: dict[str, SandboxSummary],
        snapshots: dict[str, SandboxSnapshot],
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        if session.active_run_id is not None:
            return report
        if session.sandbox_id is not None and session.sandbox_id in inventory:
            await self._provider.delete_sandbox(session.sandbox_id)
            report = _replace_report(report, deleted_sandboxes=report.deleted_sandboxes + 1)
        for snapshot_id in session.snapshot_ids:
            snapshot = snapshots.get(snapshot_id)
            if (
                snapshot is not None
                and snapshot.sandbox_id == session.sandbox_id
                and session.sandbox_id in inventory
            ):
                await self._provider.delete_snapshot(snapshot_id)
                report = _replace_report(report, deleted_snapshots=report.deleted_snapshots + 1)
        latest = await self._store.get_session(session.owner_partition, session.session_id)
        if latest.record.status == "deleting" and latest.record.active_run_id is None:
            await self._store.tombstone_session(
                previous=latest.record,
                etag=latest.etag,
                tombstone_reason="reclaimed_idle_session",
                updated_at=now,
            )
            report = _replace_report(
                report,
                tombstoned_sessions=report.tombstoned_sessions + 1,
            )
        return report

    async def reconcile_session(
        self,
        owner_partition: OwnerPartition,
        session_id: str,
    ) -> ReconcileReport:
        """Reconcile one app-owned session without a broad Table scan."""
        if owner_partition.app_hash != self._app_hash:
            return ReconcileReport()
        try:
            session_read = await self._store.get_session(owner_partition, session_id)
        except SessionRowNotFoundError:
            return ReconcileReport()
        session = session_read.record
        inventory = {
            item.sandbox_id: item
            for item in await self._provider.list_sandboxes(
                labels={"app_hash": self._app_hash}
            )
        }
        now = _utc(self._now())
        try:
            if session.active_run_id is None:
                return await self._reconcile_idle(
                    session,
                    (),
                    inventory,
                    {},
                    now,
                    ReconcileReport(),
                )
            run_read = await self._store.get_run(
                owner_partition,
                session_id,
                session.active_run_id,
            )
            return await self._reconcile_active(
                session,
                run_read.record,
                inventory,
                now,
                ReconcileReport(),
            )
        except _SESSION_RECONCILIATION_ERRORS as exc:
            logger.warning(
                "Sandbox session reconciliation deferred: session_id=%s error=%s",
                session_id,
                type(exc).__name__,
            )
            return ReconcileReport()

    async def _reconcile_labeled_orphans(
        self,
        inventory: dict[str, SandboxSummary],
        snapshots: tuple[SandboxSnapshot, ...],
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        sessions_by_sandbox: dict[str, DurableSessionRecord | None] = {}
        for sandbox in inventory.values():
            session, is_verifiable = await self._session_for_labeled_sandbox(sandbox)
            if not is_verifiable:
                continue
            sessions_by_sandbox[sandbox.sandbox_id] = session
            if (
                session is None
                or session.sandbox_id != sandbox.sandbox_id
            ) and _is_older_than(
                sandbox.created_at or sandbox.modified_at,
                now,
                self._config.safety_grace_seconds,
            ):
                await self._provider.delete_sandbox(sandbox.sandbox_id)
                report = _replace_report(
                    report,
                    deleted_sandboxes=report.deleted_sandboxes + 1,
                )

        for snapshot in snapshots:
            if snapshot.sandbox_id is None:
                continue
            session = sessions_by_sandbox.get(snapshot.sandbox_id)
            if snapshot.sandbox_id not in sessions_by_sandbox:
                continue
            if (
                (session is None or snapshot.snapshot_id not in session.snapshot_ids)
                and _is_older_than(
                    snapshot.created_at,
                    now,
                    self._config.safety_grace_seconds,
                )
            ):
                await self._provider.delete_snapshot(snapshot.snapshot_id)
                report = _replace_report(
                    report,
                    deleted_snapshots=report.deleted_snapshots + 1,
                )
        return report

    async def _prune_expired_records(
        self,
        sessions: tuple[DurableSessionRecord, ...],
        runs: tuple[DurableRunRecord, ...],
        idempotencies: tuple[DurableIdempotencyRecord | DurableOwnerIdempotencyRecord, ...],
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        for idempotency in idempotencies:
            if idempotency.expires_at > now:
                continue
            if isinstance(idempotency, DurableOwnerIdempotencyRecord):
                owner_idempotency_read = await self._store.get_owner_idempotency(
                    idempotency.owner_partition,
                    idempotency.idempotency_hash,
                )
                if (
                    owner_idempotency_read is not None
                    and owner_idempotency_read.record.expires_at <= now
                ):
                    with suppress(ConcurrencyConflictError):
                        await self._store.delete_owner_idempotency(
                            previous=owner_idempotency_read.record,
                            etag=owner_idempotency_read.etag,
                        )
                continue
            idempotency_read = await self._store.get_idempotency(
                idempotency.owner_partition,
                idempotency.session_id,
                idempotency.idempotency_hash,
            )
            if idempotency_read is not None and idempotency_read.record.expires_at <= now:
                with suppress(ConcurrencyConflictError):
                    await self._store.delete_idempotency(
                        previous=idempotency_read.record,
                        etag=idempotency_read.etag,
                    )

        sessions_by_key = {
            (session.owner_partition.partition_key, session.session_id): session
            for session in sessions
        }
        for run in runs:
            session = sessions_by_key.get((run.owner_partition.partition_key, run.session_id))
            if (
                session is None
                or session.status not in {"tombstoned", "deleted"}
                or run.status not in TERMINAL_RUN_STATUSES
                or now
                < max(
                    run.updated_at
                    + timedelta(seconds=self._config.terminal_retention_seconds),
                    session.updated_at
                    + timedelta(seconds=self._config.tombstone_retention_seconds),
                )
            ):
                continue
            run_read = await self._store.get_run(
                run.owner_partition,
                run.session_id,
                run.run_id,
            )
            if (
                run_read.record.status in TERMINAL_RUN_STATUSES
                and run_read.record.updated_at
                + timedelta(seconds=self._config.terminal_retention_seconds)
                <= now
            ):
                with suppress(ConcurrencyConflictError, RunRowNotFoundError):
                    await self._store.delete_run(
                        previous=run_read.record,
                        etag=run_read.etag,
                    )

        for session in sessions:
            if (
                session.status not in {"tombstoned", "deleted"}
                or session.updated_at
                + timedelta(seconds=self._config.tombstone_retention_seconds)
                > now
            ):
                continue
            references = await self._store.query_entities(
                filter_expression=_session_reference_filter(session),
                top=1,
            )
            if references.entities:
                continue
            session_read = await self._store.get_session(
                session.owner_partition,
                session.session_id,
            )
            if (
                session_read.record.status in {"tombstoned", "deleted"}
                and session_read.record.updated_at
                + timedelta(seconds=self._config.tombstone_retention_seconds)
                <= now
            ):
                with suppress(ConcurrencyConflictError, SessionRowNotFoundError):
                    await self._store.delete_session(
                        previous=session_read.record,
                        etag=session_read.etag,
                    )
        return report

    async def _session_for_labeled_sandbox(
        self,
        sandbox: SandboxSummary,
    ) -> tuple[DurableSessionRecord | None, bool]:
        labels = sandbox.labels
        try:
            partition = OwnerPartition.parse(
                ":".join(
                    (
                        labels["owner_hash_version"],
                        labels["app_hash"],
                        labels["owner_kind"],
                        labels["owner_hash"],
                    )
                )
            )
            session_id = validate_session_id(labels["session_id"])
        except (KeyError, SessionStateContractError):
            return None, False
        if partition.app_hash != self._app_hash:
            return None, False
        try:
            return (
                (
                    await self._store.get_session(
                        partition,
                        session_id,
                    )
                ).record,
                True,
            )
        except SessionRowNotFoundError:
            return None, True


def _runs_by_session(
    runs: tuple[DurableRunRecord, ...],
) -> dict[_SessionKey, tuple[DurableRunRecord, ...]]:
    grouped: dict[_SessionKey, list[DurableRunRecord]] = {}
    for run in runs:
        grouped.setdefault(_session_key_from_run(run), []).append(run)
    return {key: tuple(records) for key, records in grouped.items()}


def _session_key(session: DurableSessionRecord) -> _SessionKey:
    return session.owner_partition.partition_key, session.session_id


def _session_key_from_run(run: DurableRunRecord) -> _SessionKey:
    return run.owner_partition.partition_key, run.session_id


def _run_key(run: DurableRunRecord) -> tuple[str, str, str]:
    return run.owner_partition.partition_key, run.session_id, run.run_id


def _run_key_from_session(
    session: DurableSessionRecord,
    run_id: str,
) -> tuple[str, str, str]:
    return session.owner_partition.partition_key, session.session_id, run_id


def _app_scoped_query_filter(app_hash: str) -> str:
    literal = _odata_literal(app_hash)
    partition_ranges = []
    for owner_version in sorted(OWNER_CANONICALIZERS):
        prefix = f"{owner_version}:{app_hash}:"
        upper_bound = prefix + "~"
        partition_ranges.append(
            "("
            f"PartitionKey ge {_odata_literal(prefix)} and "
            f"PartitionKey lt {_odata_literal(upper_bound)}"
            ")"
        )
    return "(" + " or ".join([f"app_hash eq {literal}", *partition_ranges]) + ")"


def _odata_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _session_reference_filter(session: DurableSessionRecord) -> str:
    partition = _odata_literal(session.owner_partition.partition_key)
    session_id = _odata_literal(session.session_id)
    run_prefix = f"run:{session.session_id}:"
    idempotency_prefix = f"idem:{session.session_id}:"
    owner_idempotency_prefix = "owner-idem:"
    run_upper_bound = run_prefix + "~"
    idempotency_upper_bound = idempotency_prefix + "~"
    owner_idempotency_upper_bound = owner_idempotency_prefix + "~"
    return (
        f"PartitionKey eq {partition} and ("
        f"(RowKey ge {_odata_literal(run_prefix)} and RowKey lt {_odata_literal(run_upper_bound)})"
        " or "
        f"(RowKey ge {_odata_literal(idempotency_prefix)} and "
        f"RowKey lt {_odata_literal(idempotency_upper_bound)})"
        " or "
        f"(RowKey ge {_odata_literal(owner_idempotency_prefix)} and "
        f"RowKey lt {_odata_literal(owner_idempotency_upper_bound)} and "
        f"session_id eq {session_id})"
        ")"
    )


def _active_run(
    session: DurableSessionRecord,
    runs: tuple[DurableRunRecord, ...],
) -> DurableRunRecord | None:
    if session.active_run_id is None:
        return None
    return next((run for run in runs if run.run_id == session.active_run_id), None)


def _with_status(read: SessionRead, status: SessionStatus, updated_at: datetime) -> DurableSessionRecord:
    record = read.record
    return DurableSessionRecord.create(
        owner_partition=record.owner_partition,
        session_id=record.session_id,
        sandbox_id=record.sandbox_id,
        generation=record.generation,
        digest_kind=record.digest_kind,
        digest=record.digest,
        protocol=record.protocol,
        status=status,
        last_activity_at=record.last_activity_at,
        expires_at=record.expires_at,
        idle_policy_armed=record.idle_policy_armed,
        active_run_id=None,
        snapshot_ids=record.snapshot_ids,
        region=record.region,
        state_store_fingerprint=record.state_store_fingerprint,
        quarantine_reason=record.quarantine_reason,
        tombstone_reason=record.tombstone_reason,
        created_at=record.created_at,
        updated_at=updated_at,
    )


def _rearmed_session(read: SessionRead, now: datetime) -> DurableSessionRecord:
    record = read.record
    idle_window = record.expires_at - record.last_activity_at
    if idle_window <= timedelta(0):
        idle_window = timedelta(seconds=300)
    return DurableSessionRecord.create(
        owner_partition=record.owner_partition,
        session_id=record.session_id,
        sandbox_id=record.sandbox_id,
        generation=record.generation,
        digest_kind=record.digest_kind,
        digest=record.digest,
        protocol=record.protocol,
        status="ready",
        last_activity_at=now,
        expires_at=now + idle_window,
        idle_policy_armed=True,
        active_run_id=None,
        snapshot_ids=record.snapshot_ids,
        region=record.region,
        state_store_fingerprint=record.state_store_fingerprint,
        quarantine_reason=record.quarantine_reason,
        tombstone_reason=record.tombstone_reason,
        created_at=record.created_at,
        updated_at=now,
    )


def _terminal_reason(status: RunStatus) -> str | None:
    return None if status.error is None else status.error.code


def _is_older_than(value: str | datetime | None, now: datetime, seconds: int) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
    return _utc(value) <= now - timedelta(seconds=seconds)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reconciler timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _replace_report(report: ReconcileReport, **changes: int) -> ReconcileReport:
    values = {
        "adopted_terminal_runs": report.adopted_terminal_runs,
        "abandoned_runs": report.abandoned_runs,
        "tombstoned_sessions": report.tombstoned_sessions,
        "deleted_sandboxes": report.deleted_sandboxes,
        "deleted_snapshots": report.deleted_snapshots,
        "evicted_results": report.evicted_results,
    }
    values.update(changes)
    return ReconcileReport(**values)
