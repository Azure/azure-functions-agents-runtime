"""In-memory session-runtime doubles for controller and backend tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime

from azure_functions_agents.controller.package import CONTENT_MANIFEST_SEED_PATH
from azure_functions_agents.session_state import (
    TERMINAL_RUN_STATUSES,
    ActiveRunConflictError,
    AdmissionOutcome,
    AdmissionRecords,
    AdoptionOutcome,
    DurableOwnerIdempotencyRecord,
    DurableRunRecord,
    DurableSessionRecord,
    IdempotencyConflictError,
    NewSessionAdmissionRecords,
    OwnerIdempotencyRead,
    OwnerPartition,
    ReclaimFence,
    ReconcilerCursorRead,
    RunRead,
    SessionNotAdmissibleError,
    SessionRead,
    TableEntityPage,
)
from azure_functions_agents.transport.manifest import SESSION_MANIFEST_PATH
from azure_functions_agents.transport.transport_models import (
    ProvisionedSandboxIdentity,
    SandboxCreateRequest,
    SandboxGroupBinding,
    SandboxGroupIdentity,
    SandboxLifecyclePolicy,
    SandboxSnapshot,
    SandboxSummary,
)

from .fake_sandbox_transport import FakeSandboxTransport

DEFAULT_GROUP_RESOURCE_ID = (
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/sandboxGroups/group"
)


class FakeSandboxSessionHandle(FakeSandboxTransport):
    """A closeable file/process handle that can publish a live manifest seed."""

    def __init__(
        self,
        sandbox_id: str = "sandbox-1",
        *,
        group_resource_id: str = DEFAULT_GROUP_RESOURCE_ID,
    ) -> None:
        super().__init__()
        self.identity = ProvisionedSandboxIdentity.create(
            sandbox_id=sandbox_id,
            group_resource_id=group_resource_id,
            region="westus2",
        )
        self.closed = False
        self.stop_calls = 0
        self.resume_calls = 0
        self.delete_calls = 0
        self.labels: dict[str, str] = {}
        self.lifecycle_policy = SandboxLifecyclePolicy.create(
            auto_suspend_seconds=300,
            auto_suspend_mode="Disk",
            auto_delete_seconds=90_300,
        )
        self.lifecycle_policy_history: list[SandboxLifecyclePolicy] = [self.lifecycle_policy]

    async def write_file(self, path: str, content: bytes, *, create_dirs: bool = False) -> None:
        await super().write_file(path, content, create_dirs=create_dirs)
        if path == CONTENT_MANIFEST_SEED_PATH:
            self.seed_file(SESSION_MANIFEST_PATH, content)

    async def stop(self) -> None:
        self.stop_calls += 1

    async def resume(self) -> None:
        self.resume_calls += 1

    async def delete(self) -> None:
        self.delete_calls += 1

    async def get_lifecycle_policy(self) -> SandboxLifecyclePolicy:
        return self.lifecycle_policy

    async def set_lifecycle_policy(self, policy: SandboxLifecyclePolicy) -> None:
        self.lifecycle_policy = policy
        self.lifecycle_policy_history.append(policy)

    async def close(self) -> None:
        self.closed = True


class FakeSandboxSessionProvider:
    """A configurable provider double for one fixed Sandbox Group."""

    def __init__(
        self,
        handle: FakeSandboxSessionHandle,
        *,
        group_resource_id: str = DEFAULT_GROUP_RESOURCE_ID,
    ) -> None:
        self.group = SandboxGroupIdentity(
            resource_id=group_resource_id,
            subscription_id="sub",
            resource_group="rg",
            group_name="group",
            region="westus2",
        )
        self.handle = handle
        self.attach_calls = 0
        self.resume_calls = 0
        self.create_calls: list[SandboxCreateRequest] = []
        self.create_errors: list[Exception] = []
        self.attach_error: Exception | None = None
        self.attach_delay = 0.0
        self.closed = False
        self.sandboxes: dict[str, FakeSandboxSessionHandle] = {handle.identity.sandbox_id: handle}
        self.snapshots: dict[str, SandboxSnapshot] = {}
        self.deleted_sandbox_ids: list[str] = []
        self.deleted_snapshot_ids: list[str] = []

    async def create(
        self,
        request: SandboxCreateRequest,
        *,
        persisted_group: SandboxGroupBinding,
    ) -> FakeSandboxSessionHandle:
        assert persisted_group.region == self.group.region
        self.create_calls.append(request)
        if self.create_errors:
            raise self.create_errors.pop(0)
        self.handle.labels = request.labels.to_provider_labels()
        self.sandboxes[self.handle.identity.sandbox_id] = self.handle
        return self.handle

    async def attach(self, *args: object, **kwargs: object) -> FakeSandboxSessionHandle:
        del args, kwargs
        self.attach_calls += 1
        if self.attach_delay:
            await asyncio.sleep(self.attach_delay)
        if self.attach_error is not None:
            raise self.attach_error
        return self.handle

    async def resume(self, *args: object, **kwargs: object) -> FakeSandboxSessionHandle:
        del args, kwargs
        self.resume_calls += 1
        if self.attach_error is not None:
            raise self.attach_error
        return self.handle

    async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[SandboxSummary, ...]:
        return tuple(
            SandboxSummary.create(
                sandbox_id=sandbox_id,
                labels=handle.labels,
            )
            for sandbox_id, handle in self.sandboxes.items()
            if all(handle.labels.get(key) == value for key, value in labels.items())
        )

    async def delete_sandbox(self, sandbox_id: str) -> None:
        handle = self.sandboxes.pop(sandbox_id, None)
        if handle is not None:
            await handle.delete()
        self.deleted_sandbox_ids.append(sandbox_id)

    async def list_snapshots(self) -> tuple[SandboxSnapshot, ...]:
        return tuple(self.snapshots.values())

    async def delete_snapshot(self, snapshot_id: str) -> None:
        self.snapshots.pop(snapshot_id, None)
        self.deleted_snapshot_ids.append(snapshot_id)

    async def close(self) -> None:
        self.closed = True


class FakeSessionStateStore:
    """A minimal in-memory state-store double with observable write ordering."""

    def __init__(self, session: DurableSessionRecord | None = None) -> None:
        self.session = session
        self.etag = "etag-1"
        self.adopted: list[DurableRunRecord] = []
        self.operations: list[str] = []
        self.runs: dict[str, DurableRunRecord] = {}
        self.owner_idempotency: dict[str, DurableOwnerIdempotencyRecord] = {}
        self.owner_idempotency_etags: dict[str, str] = {}
        self.admission_expected_session_etags: list[str | None] = []
        self.reconciler_cursors: dict[str, ReconcilerCursorRead] = {}

    async def create_session(self, record: DurableSessionRecord) -> str:
        if self.session is not None:
            raise AssertionError("session row already exists")
        self.session = record
        self.etag = "etag-2"
        self.operations.append("create")
        return self.etag

    async def get_session(
        self, partition: OwnerPartition, session_id: str
    ) -> SessionRead:
        if self.session is None or self.session.session_id != session_id:
            from azure_functions_agents.session_state import SessionRowNotFoundError

            raise SessionRowNotFoundError("missing")
        del partition
        return SessionRead(record=self.session, etag=self.etag)

    async def update_session(
        self,
        *,
        previous: DurableSessionRecord,
        updated: DurableSessionRecord,
        etag: str,
        backing_rebind: bool = False,
    ) -> str:
        del backing_rebind
        assert self.session == previous
        assert self.etag == etag
        self.session = updated
        self.etag = f"etag-{len(self.operations) + 3}"
        self.operations.append(f"update:{updated.status}")
        return self.etag

    async def tombstone_session(
        self,
        *,
        previous: DurableSessionRecord,
        etag: str,
        tombstone_reason: str,
        updated_at: datetime,
    ) -> str:
        assert self.session == previous
        assert self.etag == etag
        self.session = replace(
            previous,
            status="tombstoned",
            active_run_id=None,
            tombstone_reason=tombstone_reason,
            updated_at=updated_at,
        )
        self.operations.append("tombstone")
        self.etag = "etag-tombstone"
        return self.etag

    async def admit_run(
        self,
        records: AdmissionRecords,
        *,
        expected_session_etag: str | None = None,
    ) -> AdmissionOutcome:
        assert self.session is not None
        if expected_session_etag is not None:
            assert self.etag == expected_session_etag
        self.admission_expected_session_etags.append(expected_session_etag)
        if self.session.active_run_id is not None:
            raise ActiveRunConflictError(
                "session already has an active run",
                active_run_id=self.session.active_run_id,
            )
        if self.session.status not in {"ready", "suspended"}:
            raise SessionNotAdmissibleError("session lifecycle state cannot accept a new run")
        self.session = records.session
        self.runs[records.run.run_id] = records.run
        self.operations.append("admit")
        self.etag = "etag-admitted"
        return AdmissionOutcome(
            run=records.run,
            run_etag="run-etag",
            session_etag=self.etag,
            replayed=False,
        )

    async def get_owner_idempotency(
        self,
        owner_partition: OwnerPartition,
        idempotency_hash: str,
    ) -> OwnerIdempotencyRead | None:
        del owner_partition
        record = self.owner_idempotency.get(idempotency_hash)
        if record is None:
            return None
        return OwnerIdempotencyRead(
            record=record,
            etag=self.owner_idempotency_etags[idempotency_hash],
        )

    async def delete_owner_idempotency(
        self,
        *,
        previous: DurableOwnerIdempotencyRecord,
        etag: str,
    ) -> None:
        assert self.owner_idempotency_etags.get(previous.idempotency_hash) == etag
        self.owner_idempotency.pop(previous.idempotency_hash, None)
        self.owner_idempotency_etags.pop(previous.idempotency_hash, None)
        self.operations.append("delete_owner_idempotency")

    async def get_idempotency(
        self,
        owner_partition: OwnerPartition,
        session_id: str,
        idempotency_hash: str,
    ) -> None:
        del owner_partition, session_id, idempotency_hash
        return None

    async def delete_idempotency(self, **_: object) -> None:
        self.operations.append("delete_idempotency")

    async def admit_new_session_run(
        self,
        records: NewSessionAdmissionRecords,
        *,
        expected_session_etag: str | None = None,
    ) -> AdmissionOutcome:
        existing = self.owner_idempotency.get(records.owner_idempotency.idempotency_hash)
        if existing is not None:
            if existing.request_hash != records.owner_idempotency.request_hash:
                raise IdempotencyConflictError(
                    "idempotency key already used with a different payload",
                    existing_run_id=existing.run_id,
                )
            run = self.runs[existing.run_id]
            return AdmissionOutcome(
                run=run,
                run_etag="run-etag",
                session_etag=None,
                replayed=True,
            )
        outcome = await self.admit_run(
            AdmissionRecords.create(records.session, records.run),
            expected_session_etag=expected_session_etag,
        )
        self.owner_idempotency[records.owner_idempotency.idempotency_hash] = records.owner_idempotency
        self.owner_idempotency_etags[records.owner_idempotency.idempotency_hash] = "owner-idem-etag"
        self.operations.append("admit_new_session")
        return outcome

    async def get_run(
        self,
        partition: OwnerPartition,
        session_id: str,
        run_id: str,
    ) -> RunRead:
        del partition, session_id
        return RunRead(record=self.runs[run_id], etag="run-etag")

    async def adopt_terminal_run(self, terminal_run: DurableRunRecord) -> AdoptionOutcome:
        assert self.session is not None
        existing = self.runs.get(terminal_run.run_id)
        if existing is not None and existing.status in TERMINAL_RUN_STATUSES:
            if (
                self.session.status != "reclaiming"
                and self.session.active_run_id == terminal_run.run_id
            ):
                self.session = replace(
                    self.session,
                    status="ready",
                    active_run_id=None,
                    updated_at=existing.updated_at,
                )
                self.etag = "etag-released"
                return AdoptionOutcome(run=existing, run_etag="run-etag", slot_released=True)
            return AdoptionOutcome(run=existing, run_etag="run-etag", slot_released=False)
        self.adopted.append(terminal_run)
        self.operations.append("adopt")
        self.runs[terminal_run.run_id] = terminal_run
        if self.session.status == "reclaiming":
            return AdoptionOutcome(run=terminal_run, run_etag="run-etag", slot_released=False)
        self.session = replace(
            self.session,
            status="ready",
            active_run_id=None,
            updated_at=terminal_run.updated_at,
        )
        self.etag = "etag-released"
        return AdoptionOutcome(run=terminal_run, run_etag="run-etag", slot_released=True)

    async def acquire_reclaim_fence(
        self,
        *,
        session: DurableSessionRecord,
        run: DurableRunRecord,
        token: str,
        updated_at: datetime,
    ) -> ReclaimFence | None:
        if self.session is None or self.runs.get(run.run_id) is None:
            return None
        current_run = self.runs[run.run_id]
        if self.session.status == "reclaiming":
            if (
                self.session.reclaim_fence_token != token
                or self.session.active_run_id != run.run_id
                or self.session.sandbox_id != session.sandbox_id
                or self.session.generation != run.generation
            ):
                return None
            return ReclaimFence.create(session=self.session, run=current_run, token=token)
        if self.session != session or current_run != run:
            return None
        if self.session.status not in {"running", "canceling"}:
            return None
        fence = ReclaimFence.create(session=session, run=run, token=token)
        self.session = replace(
            session,
            status="reclaiming",
            idle_policy_armed=False,
            reclaim_fence_token=token,
            updated_at=updated_at,
        )
        self.operations.append("fence")
        self.etag = "etag-fenced"
        return fence

    async def resolve_reclaim_fence_terminal(
        self,
        *,
        fence: ReclaimFence,
        terminal_run: DurableRunRecord,
    ) -> AdoptionOutcome | None:
        if self.session is None or not fence.matches(self.session):
            return None
        current = self.runs[fence.run_id]
        if current.status not in TERMINAL_RUN_STATUSES:
            self.runs[fence.run_id] = terminal_run
            current = terminal_run
        self.session = replace(
            self.session,
            status="ready",
            active_run_id=None,
            reclaim_fence_token=None,
            updated_at=current.updated_at,
        )
        self.operations.append("resolve_fence")
        self.etag = "etag-fence-resolved"
        return AdoptionOutcome(run=current, run_etag="run-etag", slot_released=True)

    async def tombstone_reclaim_fence(
        self,
        *,
        fence: ReclaimFence,
        terminal_run: DurableRunRecord,
        tombstone_reason: str,
        updated_at: datetime,
    ) -> AdoptionOutcome | None:
        if self.session is None or not fence.matches(self.session):
            return None
        current = self.runs[fence.run_id]
        if current.status not in TERMINAL_RUN_STATUSES:
            self.runs[fence.run_id] = terminal_run
            current = terminal_run
        self.session = replace(
            self.session,
            status="tombstoned",
            active_run_id=None,
            reclaim_fence_token=None,
            tombstone_reason=tombstone_reason,
            updated_at=updated_at,
        )
        self.operations.append("tombstone_fence")
        self.etag = "etag-fence-tombstoned"
        return AdoptionOutcome(run=current, run_etag="run-etag", slot_released=True)

    async def get_reconciler_cursor(self, app_hash: str) -> ReconcilerCursorRead | None:
        return self.reconciler_cursors.get(app_hash)

    async def advance_reconciler_cursor(
        self,
        *,
        app_hash: str,
        previous: ReconcilerCursorRead | None,
        continuation_token: str | None,
    ) -> ReconcilerCursorRead:
        current = self.reconciler_cursors.get(app_hash)
        if current != previous:
            from azure_functions_agents.session_state import ConcurrencyConflictError

            raise ConcurrencyConflictError("cursor changed")
        cursor = ReconcilerCursorRead(
            app_hash=app_hash,
            continuation_token=continuation_token,
            etag=f"cursor-{len(self.operations) + 1}",
        )
        self.reconciler_cursors[app_hash] = cursor
        self.operations.append("advance_cursor")
        return cursor

    async def delete_run(self, *, previous: DurableRunRecord, etag: str) -> None:
        del etag
        self.runs.pop(previous.run_id, None)
        self.operations.append("delete_run")

    async def delete_session(self, *, previous: DurableSessionRecord, etag: str) -> None:
        assert self.session == previous
        assert self.etag == etag
        self.session = None
        self.operations.append("delete_session")

    async def evict_run_result(
        self,
        *,
        previous: DurableRunRecord,
        etag: str,
        updated_at: datetime,
    ) -> str:
        del etag
        self.runs[previous.run_id] = replace(
            previous,
            result_available=False,
            updated_at=updated_at,
        )
        self.operations.append("evict_result")
        return "run-etag-evicted"

    async def query_entities(
        self,
        *,
        filter_expression: str,
        top: int | None = None,
        continuation_token: str | None = None,
    ) -> TableEntityPage:
        del filter_expression, continuation_token
        entities: list[dict[str, object]] = []
        if self.session is not None:
            entities.append(self.session.to_table_entity())
        entities.extend(record.to_table_entity() for record in self.runs.values())
        entities.extend(record.to_table_entity() for record in self.owner_idempotency.values())
        return TableEntityPage(
            entities=tuple(entities[:top] if top is not None else entities),
            continuation_token=None,
        )
