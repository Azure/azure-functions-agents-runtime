"""In-memory session-runtime doubles for controller and backend tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta

from azure_functions_agents.controller.package import CONTENT_MANIFEST_SEED_PATH
from azure_functions_agents.harness.sandbox_capabilities import REQUIRED_HARNESS_CAPABILITIES
from azure_functions_agents.journal_paths import HARNESS_PROTOCOL_PATH
from azure_functions_agents.session_state import (
    TERMINAL_RUN_STATUSES,
    ActiveRunConflictError,
    AdmissionOutcome,
    AdmissionRecords,
    AdoptionOutcome,
    DurableIdempotencyRecord,
    DurableOwnerIdempotencyRecord,
    DurableRunRecord,
    DurableSessionOperation,
    DurableSessionRecord,
    IdempotencyConflictError,
    NewSessionAdmissionRecords,
    OperationOutcome,
    OperationRead,
    OwnerIdempotencyRead,
    OwnerPartition,
    ProvisionSubmitOutcome,
    ProvisionSubmitRecords,
    ReconcilerCursorRead,
    RunRead,
    SessionNotAdmissibleError,
    SessionOperationFence,
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
_OPERATION_LEASE_SECONDS = 120


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
        self.seed_file(
            HARNESS_PROTOCOL_PATH,
            (
                '{"protocol_version":"1","capabilities":'
                + json.dumps(dict(REQUIRED_HARNESS_CAPABILITIES), sort_keys=True)
                + "}\n"
            ).encode("utf-8"),
        )

    async def write_file(self, path: str, content: bytes, *, create_dirs: bool = False) -> None:
        await super().write_file(path, content, create_dirs=create_dirs)
        if path == CONTENT_MANIFEST_SEED_PATH:
            self.seed_file(SESSION_MANIFEST_PATH, content)
        if path.endswith("/.boot-ready") and HARNESS_PROTOCOL_PATH not in self._files:
            self.seed_file(
                HARNESS_PROTOCOL_PATH,
                (
                    '{"protocol_version":"1","capabilities":'
                    + json.dumps(dict(REQUIRED_HARNESS_CAPABILITIES), sort_keys=True)
                    + "}\n"
                ).encode("utf-8"),
            )

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
        if request.labels.operation_label is not None:
            for sandbox in self.sandboxes.values():
                if sandbox.labels.get("operation_label") == request.labels.operation_label:
                    return sandbox
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
        self.durable_operations: dict[str, DurableSessionOperation] = {}
        self.owner_idempotency: dict[str, DurableOwnerIdempotencyRecord] = {}
        self.owner_idempotency_etags: dict[str, str] = {}
        self.idempotency: dict[str, DurableIdempotencyRecord] = {}
        self.idempotency_etags: dict[str, str] = {}
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
        assert previous.active_operation_id == updated.active_operation_id
        assert previous.operation_sequence == updated.operation_sequence
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
        if self.session.active_operation_id is not None:
            raise SessionNotAdmissibleError("session has an active durable controller operation")
        if self.session.active_run_id is not None:
            raise ActiveRunConflictError(
                "session already has an active run",
                active_run_id=self.session.active_run_id,
            )
        if self.session.status not in {"ready", "suspended"}:
            raise SessionNotAdmissibleError("session lifecycle state cannot accept a new run")
        self.session = records.session
        self.runs[records.run.run_id] = records.run
        if records.idempotency is not None:
            self.idempotency[records.idempotency.idempotency_hash] = records.idempotency
            self.idempotency_etags[records.idempotency.idempotency_hash] = "idem-etag"
        self.operations.append("admit")
        self.etag = "etag-admitted"
        return AdmissionOutcome(
            run=records.run,
            run_etag="run-etag",
            session_etag=self.etag,
            replayed=False,
        )

    async def admit_operation_run(
        self,
        *,
        fence: SessionOperationFence,
        records: AdmissionRecords,
    ) -> AdmissionOutcome:
        from azure_functions_agents.session_state import StaleOperationTokenError

        assert self.session is not None
        operation = self.durable_operations[fence.operation_id]
        if not fence.matches(self.session, operation):
            raise StaleOperationTokenError("stale")
        if self.session.active_run_id == records.run.run_id:
            return AdmissionOutcome(
                run=self.runs[records.run.run_id],
                run_etag="run-etag",
                session_etag=self.etag,
                replayed=True,
            )
        if operation.phase != "submit_admission":
            from azure_functions_agents.session_state import SessionStateStoreError

            raise SessionStateStoreError(
                "submit operation must reach submit_admission before run admission"
            )
        self.session = records.session
        self.runs[records.run.run_id] = records.run
        if records.idempotency is not None:
            self.idempotency[records.idempotency.idempotency_hash] = records.idempotency
            self.idempotency_etags[records.idempotency.idempotency_hash] = "idem-etag"
        self.durable_operations[fence.operation_id] = replace(
            operation,
            phase="submit_journal",
            lease_expires_at=records.run.updated_at
            + timedelta(seconds=_OPERATION_LEASE_SECONDS),
            updated_at=records.run.updated_at,
        )
        self.etag = "etag-operation-admitted"
        self.operations.append("admit_operation_run")
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
    ):
        del owner_partition, session_id
        from azure_functions_agents.session_state import IdempotencyRead

        record = self.idempotency.get(idempotency_hash)
        if record is None:
            return None
        return IdempotencyRead(
            record=record,
            etag=self.idempotency_etags[idempotency_hash],
        )

    async def delete_idempotency(self, **kwargs: object) -> None:
        previous = kwargs.get("previous")
        if isinstance(previous, DurableIdempotencyRecord):
            self.idempotency.pop(previous.idempotency_hash, None)
            self.idempotency_etags.pop(previous.idempotency_hash, None)
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
        if self.session is not None and self.session.active_operation_id is not None:
            raise SessionNotAdmissibleError("session has an active durable controller operation")
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
        try:
            record = self.runs[run_id]
        except KeyError as exc:
            from azure_functions_agents.session_state import RunRowNotFoundError

            raise RunRowNotFoundError("missing") from exc
        return RunRead(record=record, etag="run-etag")

    async def get_operation(
        self,
        partition: OwnerPartition,
        session_id: str,
        operation_id: str,
    ) -> OperationRead:
        del partition, session_id
        from azure_functions_agents.session_state import OperationRowNotFoundError

        try:
            operation = self.durable_operations[operation_id]
        except KeyError as exc:
            raise OperationRowNotFoundError("missing") from exc
        return OperationRead(record=operation, etag=f"operation-{operation.attempt_count}")

    async def begin_operation(
        self,
        *,
        previous: DurableSessionRecord,
        updated: DurableSessionRecord,
        operation: DurableSessionOperation,
        etag: str,
    ) -> SessionOperationFence:
        assert self.session == previous
        assert self.etag == etag
        assert previous.active_operation_id is None
        assert updated.active_operation_id == operation.operation_id
        operation = replace(
            operation,
            lease_expires_at=operation.updated_at
            + timedelta(seconds=_OPERATION_LEASE_SECONDS),
        )
        self.session = updated
        self.durable_operations[operation.operation_id] = operation
        self.etag = "etag-operation-begun"
        self.operations.append("begin_operation")
        return SessionOperationFence.create(operation)

    async def begin_provision_submit(
        self,
        records: ProvisionSubmitRecords,
    ) -> ProvisionSubmitOutcome:
        owner_idempotency = records.owner_idempotency
        if owner_idempotency is not None:
            existing = self.owner_idempotency.get(owner_idempotency.idempotency_hash)
            if existing is not None:
                if existing.request_hash != owner_idempotency.request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key already used with a different payload",
                        existing_run_id=existing.run_id,
                    )
                run = self.runs[existing.run_id]
                return ProvisionSubmitOutcome(
                    run=run,
                    run_etag="run-etag",
                    session_etag=None,
                    fence=None,
                    replayed=True,
                )
        if self.session is not None:
            raise AssertionError("session row already exists")
        operation = replace(
            records.operation,
            lease_expires_at=records.operation.updated_at
            + timedelta(seconds=_OPERATION_LEASE_SECONDS),
        )
        self.session = records.session
        self.runs[records.run.run_id] = records.run
        self.durable_operations[operation.operation_id] = operation
        if owner_idempotency is not None:
            self.owner_idempotency[owner_idempotency.idempotency_hash] = owner_idempotency
            self.owner_idempotency_etags[owner_idempotency.idempotency_hash] = "owner-idem-etag"
        self.etag = "etag-provision-reserved"
        self.operations.append("begin_provision_submit")
        return ProvisionSubmitOutcome(
            run=records.run,
            run_etag="run-etag",
            session_etag=self.etag,
            fence=SessionOperationFence.create(operation),
            replayed=False,
        )

    async def resume_operation(
        self,
        *,
        owner_partition: OwnerPartition,
        session_id: str,
        token: str,
        updated_at: datetime,
    ) -> SessionOperationFence | None:
        del owner_partition, session_id
        if self.session is None or self.session.active_operation_id is None:
            return None
        operation = self.durable_operations[self.session.active_operation_id]
        resumed = replace(
            operation,
            token=token,
            attempt_count=operation.attempt_count + 1,
            error_code=None,
            lease_expires_at=updated_at
            + timedelta(seconds=_OPERATION_LEASE_SECONDS),
            updated_at=updated_at,
        )
        self.durable_operations[resumed.operation_id] = resumed
        self.etag = "etag-operation-resumed"
        self.operations.append("resume_operation")
        return SessionOperationFence.create(resumed)

    async def takeover_expired_operation(
        self,
        *,
        owner_partition: OwnerPartition,
        session_id: str,
        token: str,
        updated_at: datetime,
    ) -> SessionOperationFence | None:
        del owner_partition, session_id
        if self.session is None or self.session.active_operation_id is None:
            return None
        operation = self.durable_operations[self.session.active_operation_id]
        if (
            operation.lease_expires_at is not None
            and operation.lease_expires_at > updated_at
        ):
            return None
        resumed = replace(
            operation,
            token=token,
            attempt_count=operation.attempt_count + 1,
            error_code=None,
            lease_expires_at=updated_at
            + timedelta(seconds=_OPERATION_LEASE_SECONDS),
            updated_at=updated_at,
        )
        self.durable_operations[resumed.operation_id] = resumed
        self.etag = "etag-operation-taken-over"
        self.operations.append("takeover_expired_operation")
        return SessionOperationFence.create(resumed)

    async def claim_operation_journal(
        self,
        *,
        owner_partition: OwnerPartition,
        session_id: str,
        run_id: str,
        token: str,
        updated_at: datetime,
    ) -> SessionOperationFence | None:
        del owner_partition, session_id
        if self.session is None or self.session.active_operation_id is None:
            return None
        operation = self.durable_operations[self.session.active_operation_id]
        if (
            operation.target.run_id != run_id
            or self.session.active_run_id != run_id
            or operation.phase in {"provision_launching", "submit_launching"}
        ):
            return None
        phase = (
            "provision_launching"
            if operation.kind == "provision_submit"
            else "submit_launching"
        )
        claimed = replace(
            operation,
            token=token,
            phase=phase,
            attempt_count=operation.attempt_count + 1,
            lease_expires_at=updated_at
            + timedelta(seconds=_OPERATION_LEASE_SECONDS),
            updated_at=updated_at,
        )
        self.durable_operations[claimed.operation_id] = claimed
        self.etag = "etag-journal-claimed"
        self.operations.append("claim_operation_journal")
        return SessionOperationFence.create(claimed)

    async def advance_operation(
        self,
        *,
        fence: SessionOperationFence,
        phase: str,
        updated_at: datetime,
        error_code: str | None = None,
        updated_session: DurableSessionRecord | None = None,
        updated_target: object | None = None,
    ) -> SessionOperationFence:
        from azure_functions_agents.session_state import StaleOperationTokenError

        assert self.session is not None
        operation = self.durable_operations[fence.operation_id]
        if not fence.matches(self.session, operation):
            raise StaleOperationTokenError("stale")
        advanced = replace(
            operation,
            phase=phase,
            error_code=error_code,
            lease_expires_at=updated_at
            + timedelta(seconds=_OPERATION_LEASE_SECONDS),
            updated_at=updated_at,
            target=operation.target if updated_target is None else updated_target,
        )
        self.durable_operations[advanced.operation_id] = advanced
        if updated_session is not None:
            self.session = updated_session
        self.etag = "etag-operation-advanced"
        self.operations.append("advance_operation")
        return SessionOperationFence.create(advanced)

    async def complete_operation(
        self,
        *,
        fence: SessionOperationFence,
        updated_session: DurableSessionRecord,
        updated_at: datetime,
        terminal_run: DurableRunRecord | None = None,
    ) -> OperationOutcome:
        return await self._finish_operation(
            fence=fence,
            updated_session=updated_session,
            updated_at=updated_at,
            terminal_run=terminal_run,
            error_code=None,
            state="completed",
        )

    async def abort_operation(
        self,
        *,
        fence: SessionOperationFence,
        updated_session: DurableSessionRecord,
        error_code: str,
        updated_at: datetime,
    ) -> OperationOutcome:
        return await self._finish_operation(
            fence=fence,
            updated_session=updated_session,
            updated_at=updated_at,
            terminal_run=None,
            error_code=error_code,
            state="aborted",
        )

    async def _finish_operation(
        self,
        *,
        fence: SessionOperationFence,
        updated_session: DurableSessionRecord,
        updated_at: datetime,
        terminal_run: DurableRunRecord | None,
        error_code: str | None,
        state: str,
    ) -> OperationOutcome:
        from azure_functions_agents.session_state import StaleOperationTokenError

        assert self.session is not None
        operation = self.durable_operations[fence.operation_id]
        if not fence.matches(self.session, operation):
            raise StaleOperationTokenError("stale")
        completed = replace(
            operation,
            phase=state,
            state=state,
            error_code=error_code,
            lease_expires_at=None,
            next_attempt_at=None,
            updated_at=updated_at,
            finished_at=updated_at,
        )
        self.durable_operations[completed.operation_id] = completed
        if terminal_run is not None:
            self.runs[terminal_run.run_id] = terminal_run
        self.session = updated_session
        self.etag = f"etag-operation-{state}"
        self.operations.append(f"{state}_operation")
        return OperationOutcome(
            operation=completed,
            operation_etag="operation-etag",
            session_etag=self.etag,
            run=terminal_run,
            run_etag=None if terminal_run is None else "run-etag",
        )

    async def adopt_terminal_run(self, terminal_run: DurableRunRecord) -> AdoptionOutcome:
        assert self.session is not None
        existing = self.runs.get(terminal_run.run_id)
        if existing is not None and existing.status in TERMINAL_RUN_STATUSES:
            if (
                self.session.active_run_id == terminal_run.run_id
                and self.session.active_operation_id is None
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
        if self.session.active_operation_id is not None:
            return AdoptionOutcome(run=terminal_run, run_etag="run-etag", slot_released=False)
        self.session = replace(
            self.session,
            status="ready",
            active_run_id=None,
            updated_at=terminal_run.updated_at,
        )
        self.etag = "etag-released"
        return AdoptionOutcome(run=terminal_run, run_etag="run-etag", slot_released=True)

    async def invalidate_journal_run(
        self,
        *,
        owner_partition: OwnerPartition,
        session_id: str,
        run_id: str,
        updated_at: datetime,
    ) -> AdoptionOutcome:
        del owner_partition, session_id
        assert self.session is not None
        current = self.runs[run_id]
        invalidated = DurableRunRecord.create(
            owner_partition=current.owner_partition,
            session_id=current.session_id,
            run_id=current.run_id,
            generation=current.generation,
            status="failed",
            result_available=False,
            status_reason="journal_corrupt",
            expires_at=current.expires_at,
            created_at=current.created_at,
            updated_at=updated_at,
            agent_slug=current.agent_slug,
        )
        changed = invalidated != current
        if changed:
            self.runs[run_id] = invalidated
            self.operations.append("invalidate_journal")
        owns_slot = (
            self.session.active_run_id == run_id
            and self.session.status in {"running", "canceling"}
            and self.session.active_operation_id is None
        )
        if owns_slot:
            self.session = replace(
                self.session,
                status="ready",
                active_run_id=None,
                updated_at=updated_at,
            )
            self.etag = "etag-released"
        return AdoptionOutcome(
            run=invalidated,
            run_etag="run-etag",
            slot_released=owns_slot,
        )

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

    async def delete_operation(
        self,
        *,
        previous: DurableSessionOperation,
        etag: str,
    ) -> None:
        del etag
        self.durable_operations.pop(previous.operation_id, None)
        self.operations.append("delete_operation")

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
        entities.extend(record.to_table_entity() for record in self.durable_operations.values())
        entities.extend(record.to_table_entity() for record in self.idempotency.values())
        entities.extend(record.to_table_entity() for record in self.owner_idempotency.values())
        return TableEntityPage(
            entities=tuple(entities[:top] if top is not None else entities),
            continuation_token=None,
        )
