"""One-pass, provider-neutral lifecycle reconciliation for sandbox sessions."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..execution.backend import RunStatus
from ..session_state import (
    TERMINAL_RUN_STATUSES,
    DurableRunRecord,
    DurableSessionRecord,
    RunRowKey,
    SessionRead,
    SessionRowKey,
    SessionStateContractError,
    SessionStateStore,
    SessionStatus,
    parse_row_key,
)
from ..transport.ports import SandboxSessionProvider
from ..transport.transport_models import SandboxSummary
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


@dataclass(slots=True)
class ReconcilerConfig:
    """Fixed safety limits and bounded scan policy for one reconciliation pass."""

    cadence_seconds: int = 3600
    safety_grace_seconds: int = 300
    heartbeat_stale_seconds: int = 90
    result_hold_seconds: int = 300
    page_size: int = 100
    max_pages: int = 10

    def __post_init__(self) -> None:
        if (
            self.cadence_seconds < 60
            or self.cadence_seconds > 3600
            or self.cadence_seconds % 60 != 0
        ):
            raise ValueError("cadence_seconds must be a whole-minute value from 60 through 3600")
        if self.safety_grace_seconds <= 0 or self.heartbeat_stale_seconds <= 0:
            raise ValueError("reconciler safety intervals must be positive")
        if self.result_hold_seconds <= 0 or self.page_size <= 0 or self.max_pages <= 0:
            raise ValueError("reconciler bounds must be positive")


def resolve_reconciler_cadence(
    value: str | None = None,
    *,
    environ: Callable[[str], str | None] = os.getenv,
) -> int:
    """Read the app setting as a whole-minute cadence no slower than one hour."""
    raw = value if value is not None else environ(RECONCILER_CADENCE_SETTING)
    if raw is None or not raw.strip():
        return 3600
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
    if cadence_seconds == 3600:
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
        config: ReconcilerConfig | None = None,
        terminal_reader: TerminalReader | None = None,
        heartbeat_reader: HeartbeatReader | None = None,
        death_verifier: DeathVerifier | None = None,
        lifecycle_repair: LifecycleRepair | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._provider = provider
        self._config = config or ReconcilerConfig()
        self._terminal_reader = terminal_reader
        self._heartbeat_reader = heartbeat_reader
        self._death_verifier = death_verifier
        self._lifecycle_repair = lifecycle_repair
        self._now = now

    async def run_once(self) -> ReconcileReport:
        """Perform one bounded, idempotent pass over Table records and platform inventory."""
        controller_now = _utc(self._now())
        sessions, runs, service_time, complete_scan = await self._load_working_set()
        now = service_time or controller_now
        inventory = {
            item.sandbox_id: item
            for item in await self._provider.list_sandboxes(labels={})
        }
        snapshots = await self._provider.list_snapshots()
        report = ReconcileReport()

        sessions_by_id = {session.session_id: session for session in sessions}
        runs_by_session = _runs_by_session(runs)
        referenced_snapshots = {
            snapshot_id
            for session in sessions
            if session.status not in {"tombstoned", "deleted"}
            for snapshot_id in session.snapshot_ids
        }

        for session in sessions:
            session_runs = runs_by_session.get(session.session_id, ())
            active_run = _active_run(session, session_runs)
            if active_run is not None:
                report = await self._reconcile_active(session, active_run, inventory, now, report)
                continue
            report = await self._reconcile_idle(session, session_runs, inventory, now, report)

        if complete_scan:
            for sandbox in inventory.values():
                if sandbox.sandbox_id in {session.sandbox_id for session in sessions if session.sandbox_id}:
                    continue
                if _is_older_than(
                    sandbox.created_at or sandbox.modified_at,
                    now,
                    self._config.safety_grace_seconds,
                ):
                    await self._provider.delete_sandbox(sandbox.sandbox_id)
                    report = _replace_report(report, deleted_sandboxes=report.deleted_sandboxes + 1)

            for snapshot in snapshots:
                if snapshot.snapshot_id in referenced_snapshots:
                    continue
                if _is_older_than(snapshot.created_at, now, self._config.safety_grace_seconds):
                    await self._provider.delete_snapshot(snapshot.snapshot_id)
                    report = _replace_report(report, deleted_snapshots=report.deleted_snapshots + 1)

        del sessions_by_id
        return report

    async def _load_working_set(
        self,
    ) -> tuple[
        tuple[DurableSessionRecord, ...],
        tuple[DurableRunRecord, ...],
        datetime | None,
        bool,
    ]:
        sessions: list[DurableSessionRecord] = []
        runs: list[DurableRunRecord] = []
        service_times: list[datetime] = []
        continuation: str | None = None
        for _ in range(self._config.max_pages):
            page = await self._store.query_entities(
                filter_expression="",
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
                        runs.append(DurableRunRecord.from_table_entity(entity))
                    except SessionStateContractError:
                        continue
                elif isinstance(row_key, SessionRowKey):
                    try:
                        sessions.append(DurableSessionRecord.from_table_entity(entity))
                    except SessionStateContractError:
                        continue
            if page.service_time is not None:
                service_times.append(_utc(page.service_time))
            continuation = page.continuation_token
            if continuation is None:
                break
        return (
            tuple(sessions),
            tuple(runs),
            max(service_times, default=None),
            continuation is None,
        )

    async def _reconcile_active(
        self,
        session: DurableSessionRecord,
        run: DurableRunRecord,
        inventory: dict[str, SandboxSummary],
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        if session.sandbox_id not in inventory:
            return await self._mark_active_missing_backing_if_unchanged(session, run, now, report)

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

        if self._heartbeat_reader is None:
            return report
        heartbeat = await self._heartbeat_reader(session, run)
        if heartbeat is None or _utc(heartbeat) > now - timedelta(
            seconds=self._config.heartbeat_stale_seconds
        ):
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
        # Suspicion alone is insufficient. Removing the backing establishes death
        # before the active slot can be released.
        if session.sandbox_id is not None:
            await self._provider.delete_sandbox(session.sandbox_id)
        return await self._mark_lost(session, run, now, report)

    async def _mark_active_missing_backing_if_unchanged(
        self,
        observed: DurableSessionRecord,
        run: DurableRunRecord,
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        """Re-read active ownership and inventory before converting a run to abandoned."""
        latest = await self._store.get_session(observed.owner_partition, observed.session_id)
        if (
            latest.record.status != observed.status
            or latest.record.active_run_id != run.run_id
            or latest.record.sandbox_id != observed.sandbox_id
        ):
            return report
        fresh_inventory = {
            item.sandbox_id
            for item in await self._provider.list_sandboxes(labels={})
        }
        if latest.record.sandbox_id is not None and latest.record.sandbox_id in fresh_inventory:
            return report
        current_run = await self._store.get_run(run.owner_partition, run.session_id, run.run_id)
        if current_run.record.status in TERMINAL_RUN_STATUSES:
            return report
        return await self._mark_lost(latest.record, current_run.record, now, report)

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

    async def _mark_lost(
        self,
        session: DurableSessionRecord,
        run: DurableRunRecord,
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        await self._store.adopt_terminal_run(
            terminal_run(
                run,
                status="abandoned",
                result_available=False,
                reason="sandbox_backing_lost",
                updated_at=now,
            )
        )
        latest = await self._store.get_session(session.owner_partition, session.session_id)
        tombstoned = 0
        if latest.record.status not in {"tombstoned", "deleted"}:
            await self._store.tombstone_session(
                previous=latest.record,
                etag=latest.etag,
                tombstone_reason="sandbox_backing_lost",
                updated_at=now,
            )
            tombstoned = 1
        return _replace_report(
            report,
            abandoned_runs=report.abandoned_runs + 1,
            tombstoned_sessions=report.tombstoned_sessions + tombstoned,
        )

    async def _reconcile_idle(
        self,
        session: DurableSessionRecord,
        runs: tuple[DurableRunRecord, ...],
        inventory: dict[str, SandboxSummary],
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        if session.status == "deleting":
            return await self._finish_deleting(session, now, report)
        if session.status == "creating":
            if _is_older_than(session.created_at, now, self._config.safety_grace_seconds):
                return await self._begin_reclaim(session, now, report)
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
            return await self._begin_reclaim(session, now, report)
        if no_run_candidate and _is_older_than(
            session.created_at,
            now,
            self._config.safety_grace_seconds,
        ):
            return await self._begin_reclaim(session, now, report)
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
            for item in await self._provider.list_sandboxes(labels={})
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
        except Exception:
            return report
        return await self._finish_deleting(deleting, now, report)

    async def _finish_deleting(
        self,
        session: DurableSessionRecord,
        now: datetime,
        report: ReconcileReport,
    ) -> ReconcileReport:
        if session.active_run_id is not None:
            return report
        if session.sandbox_id is not None:
            await self._provider.delete_sandbox(session.sandbox_id)
            report = _replace_report(report, deleted_sandboxes=report.deleted_sandboxes + 1)
        for snapshot_id in session.snapshot_ids:
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


def _runs_by_session(
    runs: tuple[DurableRunRecord, ...],
) -> dict[str, tuple[DurableRunRecord, ...]]:
    grouped: dict[str, list[DurableRunRecord]] = {}
    for run in runs:
        grouped.setdefault(run.session_id, []).append(run)
    return {session_id: tuple(records) for session_id, records in grouped.items()}


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
