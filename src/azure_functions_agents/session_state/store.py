"""Azure Table-backed session state store: CRUD, ETag/CAS, and admission EGT.

Table connection/settings resolution lives in :mod:`.connection`, pure
identity/row contracts in :mod:`.identity` and :mod:`.session_models`; this
module is the async I/O layer that reads and writes
:class:`~.session_models.DurableSessionRecord`,
:class:`~.session_models.DurableRunRecord`, and
:class:`~.session_models.DurableIdempotencyRecord` rows against a real Azure
Table (or Azurite).

Scope boundary: this module owns Table I/O, ETag/CAS, one-active-run
admission, idempotency dedup, terminal adoption, same-partition durable
operations, and tombstoning. It does
**not** verify a live sandbox manifest, bind a region or storage epoch, or
implement reaper/reconciliation policy -- see ``docs/architecture.md`` for
which later stage owns each of those.

The Azure Tables SDK is imported lazily (inside functions/methods, never at
module import time) so importing this module never requires the
``[aca_sandbox]`` extra to be installed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from .errors import (
    ActiveRunConflictError,
    ConcurrencyConflictError,
    CorruptEntityError,
    GenerationConflictError,
    IdempotencyConflictError,
    OperationRowNotFoundError,
    RowAlreadyExistsError,
    RunRowNotFoundError,
    SessionNotAdmissibleError,
    SessionRowNotFoundError,
    SessionStateStoreError,
    StaleOperationTokenError,
    StateStoreUnavailableError,
    TerminalStateConflictError,
)
from .identity import operation_row_key, run_row_key, session_row_key
from .session_models import (
    TABLE_NAME,
    TERMINAL_RUN_STATUSES,
    AdmissionRecords,
    DurableIdempotencyRecord,
    DurableOperationKind,
    DurableOperationPhase,
    DurableOperationState,
    DurableOwnerIdempotencyRecord,
    DurableRunRecord,
    DurableSessionOperation,
    DurableSessionRecord,
    IdempotencyRowKey,
    NewSessionAdmissionRecords,
    OwnerIdempotencyRowKey,
    OwnerPartition,
    ProvisionSubmitRecords,
    SessionOperationTarget,
    SessionStateContractError,
    TableEntity,
    parse_operation_id,
    validate_generation_transition,
    validate_operation_phase_transition,
)

if TYPE_CHECKING:
    from azure.core import MatchConditions
    from azure.core.exceptions import HttpResponseError
    from azure.data.tables import TableEntity as SdkTableEntity
    from azure.data.tables import TableTransactionError
    from azure.data.tables.aio import TableClient, TableServiceClient

# One Table entity-group-transaction operation: a verb ("create"/"update")
# paired with the entity dict and, for updates, the SDK's conditional-write
# kwargs. Mirrors `TableClient.submit_transaction`'s own declared operations
# signature (verified against the installed `azure-data-tables` stub).
type _TransactionOp = tuple[str, TableEntity] | tuple[str, TableEntity, Mapping[str, Any]]

# Run statuses that are terminal (never transition further). Kept local to the
# store because "terminal" is an I/O-layer concept (adoption/slot release).
# Re-exported from session_models.py so the literal status set is defined
# exactly once.
_TERMINAL_RUN_STATUSES = TERMINAL_RUN_STATUSES

# Terminal adoption may release only these active-run owning session states.
_STATUSES_OWNING_ACTIVE_RUN = frozenset({"running", "canceling"})

# A new run can start from an idle session, or after readiness resumed a suspended one.
_STATUSES_ADMITTING_RUN: frozenset[str] = frozenset({"ready", "suspended"})

_MAX_ADOPTION_ATTEMPTS = 5
_OPERATION_LEASE_SECONDS = 60
_JOURNAL_INTEGRITY_FAILURE_REASON = "journal_corrupt"
_RECONCILER_CURSOR_PARTITION_PREFIX = "reconciler:"
_RECONCILER_CURSOR_ROW_KEY = "cursor"
_MAX_RECONCILER_CURSOR_BYTES = 32 * 1024


# ---------------------------------------------------------------------------
# Read/outcome DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionRead:
    """A session row plus the ETag it was read with."""

    record: DurableSessionRecord
    etag: str


@dataclass(frozen=True, slots=True)
class RunRead:
    """A run row plus the ETag it was read with."""

    record: DurableRunRecord
    etag: str


@dataclass(frozen=True, slots=True)
class IdempotencyRead:
    """An idempotency row plus the ETag it was read with."""

    record: DurableIdempotencyRecord
    etag: str


@dataclass(frozen=True, slots=True)
class OwnerIdempotencyRead:
    """An owner-scoped idempotency row plus the ETag it was read with."""

    record: DurableOwnerIdempotencyRecord
    etag: str


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    """Result of :meth:`SessionStateStore.admit_run`.

    ``session_etag`` is ``None`` only when ``replayed`` is ``True`` -- an
    idempotent replay returns the previously admitted run without reading or
    writing the session row again.
    """

    run: DurableRunRecord
    run_etag: str
    session_etag: str | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class ProvisionSubmitOutcome:
    """Result of atomically reserving a new session's first submitted run."""

    run: DurableRunRecord
    run_etag: str
    session_etag: str | None
    fence: SessionOperationFence | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class AdoptionOutcome:
    """Result of :meth:`SessionStateStore.adopt_terminal_run`."""

    run: DurableRunRecord
    run_etag: str
    slot_released: bool


@dataclass(frozen=True, slots=True)
class OperationRead:
    """A durable operation row plus the ETag it was read with."""

    record: DurableSessionOperation
    etag: str


@dataclass(frozen=True, slots=True)
class SessionOperationFence:
    """A token-bound lease for one active durable session operation."""

    owner_partition: OwnerPartition
    session_id: str
    operation_id: str
    sequence: int
    kind: DurableOperationKind
    target: SessionOperationTarget
    correlation_label: str
    token: str

    @classmethod
    def create(cls, operation: DurableSessionOperation) -> SessionOperationFence:
        return cls(
            owner_partition=operation.owner_partition,
            session_id=operation.target.session_id,
            operation_id=operation.operation_id,
            sequence=operation.sequence,
            kind=operation.kind,
            target=operation.target,
            correlation_label=operation.correlation_label,
            token=operation.token,
        )

    def matches(
        self,
        session: DurableSessionRecord,
        operation: DurableSessionOperation,
    ) -> bool:
        return (
            session.owner_partition.partition_key == self.owner_partition.partition_key
            and session.session_id == self.session_id
            and session.active_operation_id == self.operation_id
            and operation.owner_partition.partition_key == self.owner_partition.partition_key
            and operation.target == self.target
            and operation.sequence == self.sequence
            and operation.operation_id == self.operation_id
            and operation.kind == self.kind
            and operation.correlation_label == self.correlation_label
            and operation.state == "active"
            and operation.token == self.token
        )


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    """The terminal durable state committed for one fenced operation."""

    operation: DurableSessionOperation
    operation_etag: str
    session_etag: str
    run: DurableRunRecord | None
    run_etag: str | None


@dataclass(frozen=True, slots=True)
class ReconcilerCursorRead:
    """The app-scoped durable continuation for bounded reconciliation scans."""

    app_hash: str
    continuation_token: str | None
    etag: str


@dataclass(frozen=True, slots=True)
class TableEntityPage:
    """One bounded page of raw Table entities plus an opaque continuation token."""

    entities: tuple[Mapping[str, object], ...]
    continuation_token: str | None
    service_time: datetime | None = None


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionStateStore(Protocol):
    """Async Table-backed session state store seam.

    Structurally typed (matching ``execution.backend.AgentExecutionBackend``'s
    convention in this package): :class:`AzureTableSessionStateStore` implements
    this Protocol without explicitly subclassing it.
    """

    async def ensure_table(self) -> None:
        """Create the session-state table if it does not already exist (idempotent)."""

    async def create_session(self, record: DurableSessionRecord) -> str:
        """Create a new session row. Raises :class:`RowAlreadyExistsError` if present."""

    async def get_session(self, owner_partition: OwnerPartition, session_id: str) -> SessionRead:
        """Read a session row. Raises :class:`SessionRowNotFoundError` if absent."""

    async def update_session(
        self,
        *,
        previous: DurableSessionRecord,
        updated: DurableSessionRecord,
        etag: str,
        backing_rebind: bool = False,
    ) -> str:
        """Conditionally replace a session row; returns the new ETag.

        Validates the generation transition (preserve, or strictly increase
        only when ``backing_rebind=True``) before writing.
        """

    async def create_run(self, record: DurableRunRecord) -> str:
        """Create a new run row. Raises :class:`RowAlreadyExistsError` if present."""

    async def get_run(
        self, owner_partition: OwnerPartition, session_id: str, run_id: str
    ) -> RunRead:
        """Read a run row. Raises :class:`RunRowNotFoundError` if absent."""

    async def get_operation(
        self,
        owner_partition: OwnerPartition,
        session_id: str,
        operation_id: str,
    ) -> OperationRead:
        """Read a durable operation row by the session pointer identifier."""

    async def begin_operation(
        self,
        *,
        previous: DurableSessionRecord,
        updated: DurableSessionRecord,
        operation: DurableSessionOperation,
        etag: str,
    ) -> SessionOperationFence:
        """Atomically create an operation and acquire the session operation pointer."""

    async def begin_provision_submit(
        self,
        records: ProvisionSubmitRecords,
    ) -> ProvisionSubmitOutcome:
        """Atomically reserve a new session, first run, owner claim, and operation."""

    async def resume_operation(
        self,
        *,
        owner_partition: OwnerPartition,
        session_id: str,
        token: str,
        updated_at: datetime,
    ) -> SessionOperationFence | None:
        """Rotate and return the token for the active operation when it still exists."""

    async def takeover_expired_operation(
        self,
        *,
        owner_partition: OwnerPartition,
        session_id: str,
        token: str,
        updated_at: datetime,
    ) -> SessionOperationFence | None:
        """Atomically take over only an absent-lease or expired active operation."""

    async def claim_operation_journal(
        self,
        *,
        owner_partition: OwnerPartition,
        session_id: str,
        run_id: str,
        token: str,
        updated_at: datetime,
    ) -> SessionOperationFence | None:
        """Claim one submit operation's journal launch while its lease is available."""

    async def advance_operation(
        self,
        *,
        fence: SessionOperationFence,
        phase: DurableOperationPhase,
        updated_at: datetime,
        error_code: str | None = None,
        updated_session: DurableSessionRecord | None = None,
        updated_target: SessionOperationTarget | None = None,
    ) -> SessionOperationFence:
        """Advance an active operation only while its durable token remains current."""

    async def complete_operation(
        self,
        *,
        fence: SessionOperationFence,
        updated_session: DurableSessionRecord,
        updated_at: datetime,
        terminal_run: DurableRunRecord | None = None,
    ) -> OperationOutcome:
        """Atomically release an operation pointer with an optional terminal run."""

    async def abort_operation(
        self,
        *,
        fence: SessionOperationFence,
        updated_session: DurableSessionRecord,
        error_code: str,
        updated_at: datetime,
    ) -> OperationOutcome:
        """Release an active operation pointer as an explicitly aborted operation."""

    async def admit_run(
        self,
        records: AdmissionRecords,
        *,
        expected_session_etag: str | None = None,
    ) -> AdmissionOutcome:
        """Atomically admit a new active run for a session.

        Deduplicates by idempotency key first (replay on matching payload,
        typed conflict on a mismatched payload), then enforces one-active-run
        via an entity-group transaction. A losing race raises
        :class:`ActiveRunConflictError` carrying the winner's run id after a
        safe re-read.
        """

    async def admit_operation_run(
        self,
        *,
        fence: SessionOperationFence,
        records: AdmissionRecords,
    ) -> AdmissionOutcome:
        """Atomically admit a run while advancing its active submit operation."""

    async def get_owner_idempotency(
        self,
        owner_partition: OwnerPartition,
        idempotency_hash: str,
    ) -> OwnerIdempotencyRead | None:
        """Read one owner-scoped first-session idempotency row when it exists."""

    async def delete_owner_idempotency(
        self,
        *,
        previous: DurableOwnerIdempotencyRecord,
        etag: str,
    ) -> None:
        """Conditionally delete an expired owner-scoped idempotency row."""

    async def get_idempotency(
        self,
        owner_partition: OwnerPartition,
        session_id: str,
        idempotency_hash: str,
    ) -> IdempotencyRead | None:
        """Read one session-scoped idempotency row when it exists."""

    async def delete_idempotency(
        self,
        *,
        previous: DurableIdempotencyRecord,
        etag: str,
    ) -> None:
        """Conditionally delete an expired session-scoped idempotency row."""

    async def admit_new_session_run(
        self,
        records: NewSessionAdmissionRecords,
        *,
        expected_session_etag: str | None = None,
    ) -> AdmissionOutcome:
        """Atomically admit a candidate session and elect one owner-key winner."""

    async def adopt_terminal_run(self, terminal_run: DurableRunRecord) -> AdoptionOutcome:
        """Atomically move a run to a terminal status and free the session's slot.

        Idempotent: re-adopting the same terminal outcome is a safe no-op so
        inline resubmit, opportunistic cleanup, and the reconciler timer can
        all call this without coordinating with each other.
        """

    async def invalidate_journal_run(
        self,
        *,
        owner_partition: OwnerPartition,
        session_id: str,
        run_id: str,
        updated_at: datetime,
    ) -> AdoptionOutcome:
        """Authoritatively invalidate an untrusted journal, including prior success."""

    async def get_reconciler_cursor(self, app_hash: str) -> ReconcilerCursorRead | None:
        """Read the app-scoped bounded-scan continuation."""

    async def advance_reconciler_cursor(
        self,
        *,
        app_hash: str,
        previous: ReconcilerCursorRead | None,
        continuation_token: str | None,
    ) -> ReconcilerCursorRead:
        """Conditionally persist a completed bounded-scan continuation."""

    async def delete_run(self, *, previous: DurableRunRecord, etag: str) -> None:
        """Conditionally delete a terminal run after its retention period."""

    async def delete_operation(
        self,
        *,
        previous: DurableSessionOperation,
        etag: str,
    ) -> None:
        """Conditionally delete a completed or aborted operation after retention."""

    async def delete_session(self, *, previous: DurableSessionRecord, etag: str) -> None:
        """Conditionally delete an unreferenced tombstone after retention."""

    async def tombstone_session(
        self,
        *,
        previous: DurableSessionRecord,
        etag: str,
        tombstone_reason: str,
        updated_at: datetime,
    ) -> str:
        """Conditionally tombstone a session, preserving its historical fields."""

    async def evict_run_result(
        self,
        *,
        previous: DurableRunRecord,
        etag: str,
        updated_at: datetime,
    ) -> str:
        """Conditionally mark terminal result content unavailable without deleting status."""

    async def query_entities(
        self,
        *,
        filter_expression: str,
        top: int | None = None,
        continuation_token: str | None = None,
    ) -> TableEntityPage:
        """Bounded, continuation-aware raw entity query (no reaper policy)."""


# ---------------------------------------------------------------------------
# Azure Tables implementation
# ---------------------------------------------------------------------------


class AzureTableSessionStateStore:
    """:class:`SessionStateStore` backed by a real Azure Table (or Azurite)."""

    def __init__(self, table_client: TableClient) -> None:
        self._table_client = table_client

    async def ensure_table(self) -> None:
        from azure.core.exceptions import (
            HttpResponseError,
            ResourceExistsError,
        )

        try:
            await self._table_client.create_table()
        except ResourceExistsError:
            return
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="ensure_table") from exc

    # -- session ----------------------------------------------------------

    async def create_session(self, record: DurableSessionRecord) -> str:
        from azure.core.exceptions import HttpResponseError, ResourceExistsError

        try:
            result = await self._table_client.create_entity(record.to_table_entity())
        except ResourceExistsError as exc:
            raise RowAlreadyExistsError(f"session {record.session_id!r} already exists") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="create_session") from exc
        return _etag_from_write_result(result)

    async def get_session(self, owner_partition: OwnerPartition, session_id: str) -> SessionRead:
        entity = await self._get_entity(owner_partition, str(session_row_key(session_id)))
        return SessionRead(record=_parse_session_entity(entity), etag=_etag_from_entity(entity))

    async def update_session(
        self,
        *,
        previous: DurableSessionRecord,
        updated: DurableSessionRecord,
        etag: str,
        backing_rebind: bool = False,
    ) -> str:
        if (
            previous.owner_partition.partition_key != updated.owner_partition.partition_key
            or previous.session_id != updated.session_id
        ):
            raise SessionStateStoreError("update_session requires the same session identity")
        _validate_generation_or_raise(
            previous.generation, updated.generation, backing_rebind=backing_rebind
        )
        _validate_checkpoint_expectation_transition(previous, updated)
        if (
            previous.active_operation_id != updated.active_operation_id
            or previous.operation_sequence != updated.operation_sequence
        ):
            raise SessionStateStoreError(
                "session operation pointers may only change through operation methods"
            )
        return await self._replace_entity(
            updated.to_table_entity(),
            etag=etag,
            not_found_error=SessionRowNotFoundError(
                f"session {updated.session_id!r} not found"
            ),
            context="update_session",
        )

    async def tombstone_session(
        self,
        *,
        previous: DurableSessionRecord,
        etag: str,
        tombstone_reason: str,
        updated_at: datetime,
    ) -> str:
        if previous.active_operation_id is not None:
            raise SessionStateStoreError(
                "a fenced session must be finalized through its durable operation"
            )
        tombstoned = DurableSessionRecord.create(
            owner_partition=previous.owner_partition,
            session_id=previous.session_id,
            sandbox_id=previous.sandbox_id,
            generation=previous.generation,
            digest_kind=previous.digest_kind,
            digest=previous.digest,
            protocol=previous.protocol,
            checkpoint_expectation=previous.checkpoint_expectation,
            status="tombstoned",
            last_activity_at=previous.last_activity_at,
            expires_at=previous.expires_at,
            idle_policy_armed=previous.idle_policy_armed,
            active_run_id=None,
            snapshot_ids=previous.snapshot_ids,
            region=previous.region,
            state_store_fingerprint=previous.state_store_fingerprint,
            quarantine_reason=previous.quarantine_reason,
            tombstone_reason=tombstone_reason,
            created_at=previous.created_at,
            updated_at=updated_at,
            active_operation_id=None,
            operation_sequence=previous.operation_sequence,
        )
        return await self.update_session(previous=previous, updated=tombstoned, etag=etag)

    # -- run ----------------------------------------------------------------

    async def create_run(self, record: DurableRunRecord) -> str:
        from azure.core.exceptions import HttpResponseError, ResourceExistsError

        try:
            result = await self._table_client.create_entity(record.to_table_entity())
        except ResourceExistsError as exc:
            raise RowAlreadyExistsError(f"run {record.run_id!r} already exists") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="create_run") from exc
        return _etag_from_write_result(result)

    async def get_run(
        self, owner_partition: OwnerPartition, session_id: str, run_id: str
    ) -> RunRead:
        entity = await self._get_entity(owner_partition, str(run_row_key(session_id, run_id)))
        return RunRead(record=_parse_run_entity(entity), etag=_etag_from_entity(entity))

    # -- durable operations (EGT) -----------------------------------------

    async def get_operation(
        self,
        owner_partition: OwnerPartition,
        session_id: str,
        operation_id: str,
    ) -> OperationRead:
        entity = await self._get_entity(
            owner_partition,
            str(operation_row_key(session_id, parse_operation_id(operation_id))),
            not_found_error=OperationRowNotFoundError(
                f"operation {operation_id!r} for session {session_id!r} not found"
            ),
        )
        return OperationRead(record=_parse_operation_entity(entity), etag=_etag_from_entity(entity))

    async def begin_operation(
        self,
        *,
        previous: DurableSessionRecord,
        updated: DurableSessionRecord,
        operation: DurableSessionOperation,
        etag: str,
    ) -> SessionOperationFence:
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        _validate_operation_begin(previous, updated, operation)
        try:
            await self._table_client.submit_transaction(
                [
                    _update_op(updated, etag=etag),
                    _create_op(operation),
                ]
            )
        except TableTransactionError as exc:
            if exc.index in (0, 1):
                raise ConcurrencyConflictError(
                    f"operation {operation.operation_id!r} changed concurrently"
                ) from exc
            raise _map_http_error(exc, context="begin_operation") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="begin_operation") from exc
        return SessionOperationFence.create(operation)

    async def begin_provision_submit(
        self,
        records: ProvisionSubmitRecords,
    ) -> ProvisionSubmitOutcome:
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        owner_idempotency = records.owner_idempotency
        if owner_idempotency is not None:
            existing = await self.get_owner_idempotency(
                records.session.owner_partition,
                owner_idempotency.idempotency_hash,
            )
            if existing is not None:
                replay = await self._replay_owner_idempotency(existing, owner_idempotency)
                return ProvisionSubmitOutcome(
                    run=replay.run,
                    run_etag=replay.run_etag,
                    session_etag=None,
                    fence=None,
                    replayed=True,
                )
        operations: list[_TransactionOp] = [
            _create_op(records.session),
            _create_op(records.run),
        ]
        if owner_idempotency is not None:
            operations.append(_create_op(owner_idempotency))
        operations.append(_create_op(records.operation))
        try:
            results = await self._table_client.submit_transaction(operations)
        except TableTransactionError as exc:
            if owner_idempotency is not None and exc.index in (0, 2):
                winner = await self.get_owner_idempotency(
                    records.session.owner_partition,
                    owner_idempotency.idempotency_hash,
                )
                if winner is not None:
                    replay = await self._replay_owner_idempotency(winner, owner_idempotency)
                    return ProvisionSubmitOutcome(
                        run=replay.run,
                        run_etag=replay.run_etag,
                        session_etag=None,
                        fence=None,
                        replayed=True,
                    )
            if exc.index in range(len(operations)):
                raise ConcurrencyConflictError(
                    "new-session provision reservation changed concurrently"
                ) from exc
            raise _map_http_error(exc, context="begin_provision_submit") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="begin_provision_submit") from exc
        return ProvisionSubmitOutcome(
            run=records.run,
            run_etag=_etag_from_write_result(results[1]),
            session_etag=_etag_from_write_result(results[0]),
            fence=SessionOperationFence.create(records.operation),
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
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        session = await self.get_session(owner_partition, session_id)
        operation_id = session.record.active_operation_id
        if operation_id is None:
            return None
        try:
            operation = await self.get_operation(owner_partition, session_id, operation_id)
        except OperationRowNotFoundError as exc:
            raise SessionStateStoreError(
                "session points to a missing durable operation"
            ) from exc
        _require_operation_matches_session(session.record, operation.record)
        if operation.record.state != "active":
            raise SessionStateStoreError("session points to a completed durable operation")
        resumed = _operation_with(
            operation.record,
            target=operation.record.target,
            token=token,
            phase=operation.record.phase,
            state=operation.record.state,
            attempt_count=operation.record.attempt_count + 1,
            error_code=None,
            lease_expires_at=updated_at + timedelta(seconds=_OPERATION_LEASE_SECONDS),
            next_attempt_at=None,
            updated_at=updated_at,
            finished_at=None,
        )
        try:
            await self._table_client.submit_transaction(
                [
                    _update_op(session.record, etag=session.etag),
                    _update_op(resumed, etag=operation.etag),
                ]
            )
        except TableTransactionError as exc:
            if exc.index in (0, 1):
                raise ConcurrencyConflictError(
                    f"operation {operation_id!r} changed while resuming"
                ) from exc
            raise _map_http_error(exc, context="resume_operation") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="resume_operation") from exc
        return SessionOperationFence.create(resumed)

    async def takeover_expired_operation(
        self,
        *,
        owner_partition: OwnerPartition,
        session_id: str,
        token: str,
        updated_at: datetime,
    ) -> SessionOperationFence | None:
        """Rotate an expired operation lease without preempting its live holder."""
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        session = await self.get_session(owner_partition, session_id)
        operation_id = session.record.active_operation_id
        if operation_id is None:
            return None
        try:
            operation = await self.get_operation(owner_partition, session_id, operation_id)
        except OperationRowNotFoundError as exc:
            raise SessionStateStoreError(
                "session points to a missing durable operation"
            ) from exc
        _require_operation_matches_session(session.record, operation.record)
        if operation.record.state != "active":
            raise SessionStateStoreError("session points to a completed durable operation")
        if (
            operation.record.lease_expires_at is not None
            and operation.record.lease_expires_at > updated_at
        ):
            return None
        resumed = _operation_with(
            operation.record,
            target=operation.record.target,
            token=token,
            phase=operation.record.phase,
            state=operation.record.state,
            attempt_count=operation.record.attempt_count + 1,
            error_code=None,
            lease_expires_at=updated_at + timedelta(seconds=_OPERATION_LEASE_SECONDS),
            next_attempt_at=None,
            updated_at=updated_at,
            finished_at=None,
        )
        try:
            await self._table_client.submit_transaction(
                [
                    _update_op(session.record, etag=session.etag),
                    _update_op(resumed, etag=operation.etag),
                ]
            )
        except TableTransactionError as exc:
            if exc.index in (0, 1):
                return None
            raise _map_http_error(exc, context="takeover_expired_operation") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="takeover_expired_operation") from exc
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
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        session = await self.get_session(owner_partition, session_id)
        operation_id = session.record.active_operation_id
        if operation_id is None:
            return None
        try:
            operation = await self.get_operation(owner_partition, session_id, operation_id)
        except OperationRowNotFoundError as exc:
            raise SessionStateStoreError(
                "session points to a missing durable operation"
            ) from exc
        _require_operation_matches_session(session.record, operation.record)
        if (
            operation.record.kind not in {"provision_submit", "submit_run"}
            or operation.record.target.run_id != run_id
            or session.record.active_run_id != run_id
            or operation.record.state != "active"
            or operation.record.phase
            not in {
                "provision_journal",
                "provision_launching",
                "submit_journal",
                "submit_launching",
            }
        ):
            return None
        if (
            operation.record.phase in {"provision_launching", "submit_launching"}
            and operation.record.lease_expires_at is not None
            and operation.record.lease_expires_at > updated_at
        ):
            return None
        phase: DurableOperationPhase = (
            "provision_launching"
            if operation.record.kind == "provision_submit"
            else "submit_launching"
        )
        try:
            validate_operation_phase_transition(
                operation.record.kind,
                operation.record.phase,
                phase,
            )
        except SessionStateContractError as exc:
            raise SessionStateStoreError(str(exc)) from exc
        claimed = _operation_with(
            operation.record,
            target=operation.record.target,
            token=token,
            phase=phase,
            state="active",
            attempt_count=operation.record.attempt_count + 1,
            error_code=None,
            lease_expires_at=updated_at + timedelta(seconds=_OPERATION_LEASE_SECONDS),
            next_attempt_at=None,
            updated_at=updated_at,
            finished_at=None,
        )
        try:
            await self._table_client.submit_transaction(
                [
                    _update_op(session.record, etag=session.etag),
                    _update_op(claimed, etag=operation.etag),
                ]
            )
        except TableTransactionError as exc:
            if exc.index in (0, 1):
                raise StaleOperationTokenError(
                    f"operation {operation_id!r} journal claim is stale"
                ) from exc
            raise _map_http_error(exc, context="claim_operation_journal") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="claim_operation_journal") from exc
        return SessionOperationFence.create(claimed)

    async def advance_operation(
        self,
        *,
        fence: SessionOperationFence,
        phase: DurableOperationPhase,
        updated_at: datetime,
        error_code: str | None = None,
        updated_session: DurableSessionRecord | None = None,
        updated_target: SessionOperationTarget | None = None,
    ) -> SessionOperationFence:
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        session, operation = await self._read_fenced_operation(fence)
        try:
            validate_operation_phase_transition(
                operation.record.kind,
                operation.record.phase,
                phase,
            )
        except SessionStateContractError as exc:
            raise SessionStateStoreError(str(exc)) from exc
        target = operation.record.target if updated_target is None else updated_target
        _validate_operation_target_update(operation.record.target, target)
        session_to_write = session.record if updated_session is None else updated_session
        _validate_operation_advance_session(
            session.record,
            session_to_write,
            target,
        )
        advanced = _operation_with(
            operation.record,
            target=target,
            token=operation.record.token,
            phase=phase,
            state=operation.record.state,
            attempt_count=operation.record.attempt_count,
            error_code=error_code,
            lease_expires_at=updated_at + timedelta(seconds=_OPERATION_LEASE_SECONDS),
            next_attempt_at=updated_at if error_code is not None else None,
            updated_at=updated_at,
            finished_at=None,
        )
        try:
            await self._table_client.submit_transaction(
                [
                    _update_op(session_to_write, etag=session.etag),
                    _update_op(advanced, etag=operation.etag),
                ]
            )
        except TableTransactionError as exc:
            if exc.index in (0, 1):
                raise StaleOperationTokenError(
                    f"operation {fence.operation_id!r} token is stale"
                ) from exc
            raise _map_http_error(exc, context="advance_operation") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="advance_operation") from exc
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
            terminal_run=terminal_run,
            error_code=None,
            terminal_state="completed",
            allow_active_run_abort=False,
            updated_at=updated_at,
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
            terminal_run=None,
            error_code=error_code,
            terminal_state="aborted",
            allow_active_run_abort=True,
            updated_at=updated_at,
        )

    async def _read_fenced_operation(
        self,
        fence: SessionOperationFence,
    ) -> tuple[SessionRead, OperationRead]:
        session = await self.get_session(fence.owner_partition, fence.session_id)
        try:
            operation = await self.get_operation(
                fence.owner_partition,
                fence.session_id,
                fence.operation_id,
            )
        except OperationRowNotFoundError as exc:
            raise StaleOperationTokenError(
                f"operation {fence.operation_id!r} no longer exists"
            ) from exc
        if not fence.matches(session.record, operation.record):
            raise StaleOperationTokenError(f"operation {fence.operation_id!r} token is stale")
        return session, operation

    async def _finish_operation(
        self,
        *,
        fence: SessionOperationFence,
        updated_session: DurableSessionRecord,
        terminal_run: DurableRunRecord | None,
        error_code: str | None,
        terminal_state: Literal["completed", "aborted"],
        allow_active_run_abort: bool,
        updated_at: datetime,
    ) -> OperationOutcome:
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        session, operation = await self._read_fenced_operation(fence)
        effective_terminal = terminal_run
        current_run: RunRead | None = None
        if (
            effective_terminal is None
            and terminal_state == "completed"
            and operation.record.target.run_id is not None
        ):
            current_run = await self.get_run(
                fence.owner_partition,
                fence.session_id,
                operation.record.target.run_id,
            )
            if current_run.record.status not in _TERMINAL_RUN_STATUSES:
                raise SessionStateStoreError(
                    "operation cannot complete before its target run is terminal"
                )
            effective_terminal = current_run.record
        _validate_operation_completion(
            session.record,
            operation.record,
            updated_session,
            effective_terminal,
            allow_active_run_abort=allow_active_run_abort,
        )
        completed_operation = _operation_with(
            operation.record,
            target=operation.record.target,
            token=operation.record.token,
            phase=terminal_state,
            state=terminal_state,
            attempt_count=operation.record.attempt_count,
            error_code=error_code,
            lease_expires_at=None,
            next_attempt_at=None,
            updated_at=updated_at,
            finished_at=updated_at,
        )
        run_to_write: DurableRunRecord | None = None
        if terminal_run is not None:
            current_run = await self.get_run(
                fence.owner_partition,
                fence.session_id,
                terminal_run.run_id,
            )
            run_to_write = _terminal_run_for_operation(current_run.record, terminal_run)

        operations: list[_TransactionOp] = []
        if current_run is not None and run_to_write is not None:
            operations.append(_update_op(run_to_write, etag=current_run.etag))
        operations.extend(
            [
                _update_op(updated_session, etag=session.etag),
                _update_op(completed_operation, etag=operation.etag),
            ]
        )
        try:
            results = await self._table_client.submit_transaction(operations)
        except TableTransactionError as exc:
            if exc.index in range(len(operations)):
                raise StaleOperationTokenError(
                    f"operation {fence.operation_id!r} token is stale"
                ) from exc
            raise _map_http_error(exc, context="finish_operation") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="finish_operation") from exc

        run_offset = 1 if current_run is not None and run_to_write is not None else 0
        return OperationOutcome(
            operation=completed_operation,
            operation_etag=_etag_from_write_result(results[run_offset + 1]),
            session_etag=_etag_from_write_result(results[run_offset]),
            run=run_to_write,
            run_etag=(
                None
                if run_to_write is None
                else _etag_from_write_result(results[0])
            ),
        )

    async def _get_idempotency(
        self, owner_partition: OwnerPartition, session_id: str, idempotency_hash: str
    ) -> IdempotencyRead | None:
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        # `idempotency_hash` here is ALREADY the SHA-256 digest carried on a
        # `DurableIdempotencyRecord` (matching its own `.row_key` property) --
        # unlike `identity.idempotency_row_key`, which hashes a RAW caller
        # idempotency key. Using that helper here would hash an
        # already-hashed value and look up the wrong row.
        row_key = str(IdempotencyRowKey.create(session_id, idempotency_hash))
        try:
            entity = await self._table_client.get_entity(owner_partition.partition_key, row_key)
        except ResourceNotFoundError:
            return None
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="get_idempotency") from exc
        return IdempotencyRead(
            record=_parse_idempotency_entity(entity), etag=_etag_from_entity(entity)
        )

    async def get_owner_idempotency(
        self,
        owner_partition: OwnerPartition,
        idempotency_hash: str,
    ) -> OwnerIdempotencyRead | None:
        """Read a first-session idempotency winner without treating absence as failure."""
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        row_key = str(OwnerIdempotencyRowKey.create(idempotency_hash))
        try:
            entity = await self._table_client.get_entity(owner_partition.partition_key, row_key)
        except ResourceNotFoundError:
            return None
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="get_owner_idempotency") from exc
        return OwnerIdempotencyRead(
            record=_parse_owner_idempotency_entity(entity),
            etag=_etag_from_entity(entity),
        )

    async def delete_owner_idempotency(
        self,
        *,
        previous: DurableOwnerIdempotencyRecord,
        etag: str,
    ) -> None:
        """Delete a stale owner-key row only when the read ETag still matches."""
        await self._delete_entity(
            previous.to_table_entity(),
            etag=etag,
            not_found_error=SessionStateStoreError("owner idempotency row not found"),
            context="delete_owner_idempotency",
        )

    async def get_idempotency(
        self,
        owner_partition: OwnerPartition,
        session_id: str,
        idempotency_hash: str,
    ) -> IdempotencyRead | None:
        return await self._get_idempotency(owner_partition, session_id, idempotency_hash)

    async def delete_idempotency(
        self,
        *,
        previous: DurableIdempotencyRecord,
        etag: str,
    ) -> None:
        await self._delete_entity(
            previous.to_table_entity(),
            etag=etag,
            not_found_error=SessionStateStoreError("idempotency row not found"),
            context="delete_idempotency",
        )

    # -- admission (EGT) ------------------------------------------------

    async def admit_run(
        self,
        records: AdmissionRecords,
        *,
        expected_session_etag: str | None = None,
    ) -> AdmissionOutcome:
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        partition = records.session.owner_partition
        session_id = records.session.session_id

        if records.idempotency is not None:
            replay = await self._try_replay_idempotency(partition, session_id, records.idempotency)
            if replay is not None:
                return replay

        current_session = await self.get_session(partition, session_id)
        if (
            expected_session_etag is not None
            and current_session.etag != expected_session_etag
        ):
            raise ConcurrencyConflictError("session changed concurrently before admission")
        if current_session.record.active_operation_id is not None:
            raise SessionNotAdmissibleError(
                "session has an active durable controller operation"
            )
        if current_session.record.active_run_id is not None:
            raise ActiveRunConflictError(
                f"session {session_id!r} already has an active run",
                active_run_id=current_session.record.active_run_id,
            )
        if current_session.record.status not in _STATUSES_ADMITTING_RUN:
            raise SessionNotAdmissibleError(
                "session lifecycle state cannot accept a new run"
            )
        # Admission is never a backing rebind: the freshly re-read stored
        # generation and the caller's target generation must match exactly,
        # or this is a rollback attempt (typed GenerationConflictError), not
        # a silent overwrite -- mirroring update_session/tombstone_session.
        _validate_generation_or_raise(
            current_session.record.generation,
            records.session.generation,
            backing_rebind=False,
        )

        operations: list[_TransactionOp] = [
            _update_op(records.session, etag=current_session.etag),
            _create_op(records.run),
        ]
        if records.idempotency is not None:
            operations.append(_create_op(records.idempotency))

        try:
            results = await self._table_client.submit_transaction(operations)
        except TableTransactionError as exc:
            return await self._resolve_admission_conflict(exc, partition, session_id, records)
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="admit_run") from exc

        return AdmissionOutcome(
            run=records.run,
            run_etag=_etag_from_write_result(results[1]),
            session_etag=_etag_from_write_result(results[0]),
            replayed=False,
        )

    async def admit_operation_run(
        self,
        *,
        fence: SessionOperationFence,
        records: AdmissionRecords,
    ) -> AdmissionOutcome:
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        session, operation = await self._read_fenced_operation(fence)
        if (
            operation.record.kind != "submit_run"
            or operation.record.target.run_id != records.run.run_id
            or (
                operation.record.agent_slug
                and operation.record.agent_slug != records.run.agent_slug
            )
            or session.record.active_run_id not in {None, records.run.run_id}
        ):
            raise SessionStateStoreError("submit operation does not match its admission")
        if session.record.active_run_id == records.run.run_id:
            existing = await self.get_run(
                records.run.owner_partition,
                records.run.session_id,
                records.run.run_id,
            )
            return AdmissionOutcome(
                run=existing.record,
                run_etag=existing.etag,
                session_etag=session.etag,
                replayed=True,
            )
        if operation.record.phase != "submit_admission":
            raise SessionStateStoreError(
                "submit operation must reach submit_admission before run admission"
            )
        if records.idempotency is not None:
            replay = await self._try_replay_idempotency(
                records.session.owner_partition,
                records.session.session_id,
                records.idempotency,
            )
            if replay is not None:
                return replay
        if (
            records.session.active_operation_id != fence.operation_id
            or records.session.active_run_id != records.run.run_id
            or records.session.operation_sequence != session.record.operation_sequence
        ):
            raise SessionStateStoreError("submit admission does not preserve its operation pointer")
        advanced = _operation_with(
            operation.record,
            target=operation.record.target,
            token=operation.record.token,
            phase="submit_journal",
            state="active",
            attempt_count=operation.record.attempt_count,
            error_code=None,
            lease_expires_at=records.run.updated_at
            + timedelta(seconds=_OPERATION_LEASE_SECONDS),
            next_attempt_at=None,
            updated_at=records.run.updated_at,
            finished_at=None,
        )
        operations: list[_TransactionOp] = [
            _update_op(records.session, etag=session.etag),
            _create_op(records.run),
        ]
        if records.idempotency is not None:
            operations.append(_create_op(records.idempotency))
        operations.append(_update_op(advanced, etag=operation.etag))
        try:
            results = await self._table_client.submit_transaction(operations)
        except TableTransactionError as exc:
            if records.idempotency is not None:
                replay = await self._try_replay_idempotency(
                    records.session.owner_partition,
                    records.session.session_id,
                    records.idempotency,
                )
                if replay is not None:
                    return replay
            if exc.index in range(len(operations)):
                raise StaleOperationTokenError(
                    f"operation {fence.operation_id!r} token is stale"
                ) from exc
            raise _map_http_error(exc, context="admit_operation_run") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="admit_operation_run") from exc
        return AdmissionOutcome(
            run=records.run,
            run_etag=_etag_from_write_result(results[1]),
            session_etag=_etag_from_write_result(results[0]),
            replayed=False,
        )

    async def admit_new_session_run(
        self,
        records: NewSessionAdmissionRecords,
        *,
        expected_session_etag: str | None = None,
    ) -> AdmissionOutcome:
        """Admit a candidate through an owner-key EGT distinct from session dedupe."""
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        partition = records.session.owner_partition
        owner_idempotency = records.owner_idempotency
        existing = await self.get_owner_idempotency(
            partition,
            owner_idempotency.idempotency_hash,
        )
        if existing is not None:
            return await self._replay_owner_idempotency(existing, owner_idempotency)

        current_session = await self.get_session(partition, records.session.session_id)
        if (
            expected_session_etag is not None
            and current_session.etag != expected_session_etag
        ):
            raise ConcurrencyConflictError("session changed concurrently before admission")
        if current_session.record.active_operation_id is not None:
            raise SessionNotAdmissibleError(
                "session has an active durable controller operation"
            )
        if current_session.record.active_run_id is not None:
            raise ActiveRunConflictError(
                f"session {records.session.session_id!r} already has an active run",
                active_run_id=current_session.record.active_run_id,
            )
        if current_session.record.status not in _STATUSES_ADMITTING_RUN:
            raise SessionNotAdmissibleError("session lifecycle state cannot accept a new run")
        _validate_generation_or_raise(
            current_session.record.generation,
            records.session.generation,
            backing_rebind=False,
        )

        operations: list[_TransactionOp] = [
            _update_op(records.session, etag=current_session.etag),
            _create_op(records.run),
            _create_op(owner_idempotency),
        ]
        try:
            results = await self._table_client.submit_transaction(operations)
        except TableTransactionError as exc:
            if exc.index == 1:
                raise RowAlreadyExistsError(f"run {records.run.run_id!r} already exists") from exc
            if exc.index in (0, 2):
                winner = await self.get_owner_idempotency(
                    partition,
                    owner_idempotency.idempotency_hash,
                )
                if winner is not None:
                    return await self._replay_owner_idempotency(winner, owner_idempotency)
                if exc.index == 0:
                    reread = await self.get_session(partition, records.session.session_id)
                    if reread.record.active_run_id is not None:
                        raise ActiveRunConflictError(
                            f"session {records.session.session_id!r} already has an active run",
                            active_run_id=reread.record.active_run_id,
                        ) from exc
                raise ConcurrencyConflictError(
                    "new-session owner idempotency admission changed concurrently"
                ) from exc
            raise _map_http_error(exc, context="admit_new_session_run") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="admit_new_session_run") from exc
        return AdmissionOutcome(
            run=records.run,
            run_etag=_etag_from_write_result(results[1]),
            session_etag=_etag_from_write_result(results[0]),
            replayed=False,
        )

    async def _replay_owner_idempotency(
        self,
        existing: OwnerIdempotencyRead,
        candidate: DurableOwnerIdempotencyRecord,
    ) -> AdmissionOutcome:
        """Resolve a durable owner-key winner or report a payload conflict."""
        if existing.record.request_hash != candidate.request_hash:
            raise IdempotencyConflictError(
                "idempotency key already used with a different payload",
                existing_run_id=existing.record.run_id,
            )
        run = await self.get_run(
            existing.record.owner_partition,
            existing.record.session_id,
            existing.record.run_id,
        )
        return AdmissionOutcome(
            run=run.record,
            run_etag=run.etag,
            session_etag=None,
            replayed=True,
        )

    async def _resolve_admission_conflict(
        self,
        exc: TableTransactionError,
        partition: OwnerPartition,
        session_id: str,
        records: AdmissionRecords,
    ) -> AdmissionOutcome:
        """Map a failed admission transaction to a replay or a typed conflict.

        Index 1 (run row) always means the run already exists. Index 0
        (session ETag) and index 2 (idempotency row) share one idempotency
        replay check: a same-key/same-payload race commonly lands on index 0
        too, since a winner's commit invalidates our session ETag before its
        idempotency row could independently conflict with ours.
        """
        if exc.index == 1:
            raise RowAlreadyExistsError(f"run {records.run.run_id!r} already exists") from exc
        if exc.index not in (0, 2):
            raise _map_http_error(exc, context="admit_run") from exc

        if records.idempotency is not None:
            raced = await self._try_replay_idempotency(partition, session_id, records.idempotency)
            if raced is not None:
                return raced

        if exc.index == 2:
            # The transaction reported an idempotency-row conflict, but a
            # consistent re-read found no such row -- we cannot identify
            # which run "won", so surface a retryable concurrency conflict
            # rather than fabricating existing_run_id from our own new run.
            raise ConcurrencyConflictError(
                f"idempotency row for session {session_id!r} reported a "
                "write conflict but could not be re-read; retry the admission"
            ) from exc

        reread = await self.get_session(partition, session_id)
        if reread.record.active_operation_id is not None:
            raise SessionNotAdmissibleError(
                "session has an active durable controller operation"
            ) from exc
        if reread.record.active_run_id is not None:
            raise ActiveRunConflictError(
                f"session {session_id!r} already has an active run",
                active_run_id=reread.record.active_run_id,
            ) from exc
        raise ConcurrencyConflictError(
            f"session {session_id!r} changed concurrently during admission"
        ) from exc

    async def _try_replay_idempotency(
        self,
        partition: OwnerPartition,
        session_id: str,
        idempotency: DurableIdempotencyRecord,
    ) -> AdmissionOutcome | None:
        """Return a replay :class:`AdmissionOutcome` if this key/payload was seen before.

        Raises :class:`IdempotencyConflictError` if the key was reused with a
        different payload. Returns ``None`` only when no idempotency row
        exists yet (caller should proceed to a fresh admission attempt).
        """
        existing = await self._get_idempotency(partition, session_id, idempotency.idempotency_hash)
        if existing is None:
            return None
        if existing.record.request_hash != idempotency.request_hash:
            raise IdempotencyConflictError(
                "idempotency key already used with a different payload",
                existing_run_id=existing.record.run_id,
            )
        run = await self.get_run(partition, session_id, existing.record.run_id)
        return AdmissionOutcome(
            run=run.record, run_etag=run.etag, session_etag=None, replayed=True
        )

    # -- terminal adoption ------------------------------------------------

    async def adopt_terminal_run(self, terminal_run: DurableRunRecord) -> AdoptionOutcome:
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        if terminal_run.status not in _TERMINAL_RUN_STATUSES:
            raise SessionStateStoreError(
                f"adopt_terminal_run requires a terminal status, got {terminal_run.status!r}"
            )
        partition = terminal_run.owner_partition
        session_id = terminal_run.session_id
        run_id = terminal_run.run_id

        for _attempt in range(_MAX_ADOPTION_ATTEMPTS):
            current_run = await self.get_run(partition, session_id, run_id)
            if current_run.record.status in _TERMINAL_RUN_STATUSES:
                if current_run.record.status != terminal_run.status:
                    _require_matching_terminal_outcome(current_run.record, terminal_run, run_id)
                adopted_run = (
                    terminal_run
                    if current_run.record.result_available and not terminal_run.result_available
                    else current_run.record
                )
                try:
                    session_read = await self.get_session(partition, session_id)
                except SessionRowNotFoundError:
                    session_read = None
                owns_slot = (
                    session_read is not None
                    and session_read.record.active_run_id == run_id
                    and session_read.record.status in _STATUSES_OWNING_ACTIVE_RUN
                    and session_read.record.active_operation_id is None
                )
                if owns_slot:
                    assert session_read is not None
                    released_session = _release_active_run(
                        session_read.record,
                        updated_at=adopted_run.updated_at,
                    )
                    operations: list[_TransactionOp] = [
                        _update_op(released_session, etag=session_read.etag)
                    ]
                    if adopted_run is not current_run.record:
                        operations.insert(0, _update_op(adopted_run, etag=current_run.etag))
                    try:
                        results = await self._table_client.submit_transaction(operations)
                    except TableTransactionError as exc:
                        if exc.index in (0, 1):
                            continue
                        raise _map_http_error(exc, context="adopt_terminal_run") from exc
                    except HttpResponseError as exc:
                        raise _map_http_error(exc, context="adopt_terminal_run") from exc
                    run_etag = (
                        _etag_from_write_result(results[0])
                        if adopted_run is not current_run.record
                        else current_run.etag
                    )
                    return AdoptionOutcome(
                        run=adopted_run,
                        run_etag=run_etag,
                        slot_released=True,
                    )
                if adopted_run is not current_run.record:
                    run_etag = await self._replace_entity(
                        adopted_run.to_table_entity(),
                        etag=current_run.etag,
                        not_found_error=RunRowNotFoundError(f"run {run_id!r} not found"),
                        context="adopt_terminal_run",
                    )
                    return AdoptionOutcome(
                        run=adopted_run,
                        run_etag=run_etag,
                        slot_released=False,
                    )
                return AdoptionOutcome(
                    run=current_run.record,
                    run_etag=current_run.etag,
                    slot_released=False,
                )

            active_session_read: SessionRead | None
            try:
                active_session_read = await self.get_session(partition, session_id)
            except SessionRowNotFoundError:
                active_session_read = None

            owns_slot = (
                active_session_read is not None
                and active_session_read.record.active_run_id == run_id
                and active_session_read.record.status in _STATUSES_OWNING_ACTIVE_RUN
                and active_session_read.record.active_operation_id is None
            )

            if not owns_slot:
                try:
                    run_etag = await self._replace_entity(
                        terminal_run.to_table_entity(),
                        etag=current_run.etag,
                        not_found_error=RunRowNotFoundError(f"run {run_id!r} not found"),
                        context="adopt_terminal_run",
                    )
                except ConcurrencyConflictError:
                    continue
                return AdoptionOutcome(run=terminal_run, run_etag=run_etag, slot_released=False)

            assert active_session_read is not None
            released_session = _release_active_run(
                active_session_read.record, updated_at=terminal_run.updated_at
            )
            active_operations: list[_TransactionOp] = [
                _update_op(terminal_run, etag=current_run.etag),
                _update_op(released_session, etag=active_session_read.etag),
            ]
            try:
                results = await self._table_client.submit_transaction(active_operations)
            except TableTransactionError as exc:
                if exc.index in (0, 1):
                    continue
                raise _map_http_error(exc, context="adopt_terminal_run") from exc
            except HttpResponseError as exc:
                raise _map_http_error(exc, context="adopt_terminal_run") from exc
            return AdoptionOutcome(
                run=terminal_run,
                run_etag=_etag_from_write_result(results[0]),
                slot_released=True,
            )

        raise ConcurrencyConflictError(
            f"adopt_terminal_run for {run_id!r} did not converge after "
            f"{_MAX_ADOPTION_ATTEMPTS} attempts"
        )

    async def invalidate_journal_run(
        self,
        *,
        owner_partition: OwnerPartition,
        session_id: str,
        run_id: str,
        updated_at: datetime,
    ) -> AdoptionOutcome:
        """Force the sole integrity-authorized terminal correction for one run."""
        from azure.core.exceptions import HttpResponseError
        from azure.data.tables import TableTransactionError

        for _attempt in range(_MAX_ADOPTION_ATTEMPTS):
            current_run = await self.get_run(owner_partition, session_id, run_id)
            current_session = await self.get_session(owner_partition, session_id)
            effective_updated_at = max(updated_at, current_run.record.updated_at)
            invalidated = _journal_invalidated_run(
                current_run.record,
                effective_updated_at,
            )
            run_needs_update = invalidated != current_run.record
            owns_slot = (
                current_session.record.active_run_id == run_id
                and current_session.record.status in _STATUSES_OWNING_ACTIVE_RUN
                and current_session.record.active_operation_id is None
            )
            released_session = (
                _release_active_run(
                    current_session.record,
                    updated_at=max(effective_updated_at, current_session.record.updated_at),
                )
                if owns_slot
                else None
            )
            if not run_needs_update and released_session is None:
                return AdoptionOutcome(
                    run=current_run.record,
                    run_etag=current_run.etag,
                    slot_released=False,
                )
            operations: list[_TransactionOp] = []
            if run_needs_update:
                operations.append(_update_op(invalidated, etag=current_run.etag))
            if released_session is not None:
                operations.append(_update_op(released_session, etag=current_session.etag))
            try:
                results = await self._table_client.submit_transaction(operations)
            except TableTransactionError as exc:
                if exc.index in range(len(operations)):
                    continue
                raise _map_http_error(exc, context="invalidate_journal_run") from exc
            except HttpResponseError as exc:
                raise _map_http_error(exc, context="invalidate_journal_run") from exc
            return AdoptionOutcome(
                run=invalidated,
                run_etag=(
                    _etag_from_write_result(results[0])
                    if run_needs_update
                    else current_run.etag
                ),
                slot_released=released_session is not None,
            )
        raise ConcurrencyConflictError(
            f"invalidate_journal_run for {run_id!r} did not converge after "
            f"{_MAX_ADOPTION_ATTEMPTS} attempts"
        )

    async def get_reconciler_cursor(self, app_hash: str) -> ReconcilerCursorRead | None:
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        partition_key = _reconciler_cursor_partition_key(app_hash)
        try:
            entity = await self._table_client.get_entity(
                partition_key,
                _RECONCILER_CURSOR_ROW_KEY,
            )
        except ResourceNotFoundError:
            return None
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="get_reconciler_cursor") from exc
        stored_app_hash = entity.get("cursor_app_hash")
        stored_token = entity.get("continuation_token")
        if (
            stored_app_hash != app_hash
            or not isinstance(stored_token, str)
            or len(stored_token.encode("utf-8")) > _MAX_RECONCILER_CURSOR_BYTES
        ):
            raise CorruptEntityError("stored reconciler cursor failed validation")
        return ReconcilerCursorRead(
            app_hash=app_hash,
            continuation_token=stored_token or None,
            etag=_etag_from_entity(entity),
        )

    async def advance_reconciler_cursor(
        self,
        *,
        app_hash: str,
        previous: ReconcilerCursorRead | None,
        continuation_token: str | None,
    ) -> ReconcilerCursorRead:
        from azure.core.exceptions import (
            HttpResponseError,
            ResourceExistsError,
            ResourceModifiedError,
        )
        from azure.data.tables import UpdateMode

        partition_key = _reconciler_cursor_partition_key(app_hash)
        token = continuation_token or ""
        if len(token.encode("utf-8")) > _MAX_RECONCILER_CURSOR_BYTES:
            raise SessionStateStoreError("reconciler continuation token is too large")
        entity: TableEntity = {
            "PartitionKey": partition_key,
            "RowKey": _RECONCILER_CURSOR_ROW_KEY,
            "cursor_app_hash": app_hash,
            "continuation_token": token,
        }
        if previous is None:
            try:
                result = await self._table_client.create_entity(entity)
            except ResourceExistsError as exc:
                raise ConcurrencyConflictError("reconciler cursor was created concurrently") from exc
            except HttpResponseError as exc:
                raise _map_http_error(exc, context="advance_reconciler_cursor") from exc
        else:
            if previous.app_hash != app_hash:
                raise SessionStateStoreError("reconciler cursor app hash does not match")
            try:
                result = await self._table_client.update_entity(
                    entity,
                    mode=UpdateMode.REPLACE,
                    etag=previous.etag,
                    match_condition=_if_not_modified(),
                )
            except ResourceModifiedError as exc:
                raise ConcurrencyConflictError(
                    "reconciler cursor changed concurrently"
                ) from exc
            except HttpResponseError as exc:
                raise _map_http_error(exc, context="advance_reconciler_cursor") from exc
        return ReconcilerCursorRead(
            app_hash=app_hash,
            continuation_token=continuation_token,
            etag=_etag_from_write_result(result),
        )

    async def delete_run(self, *, previous: DurableRunRecord, etag: str) -> None:
        if previous.status not in _TERMINAL_RUN_STATUSES:
            raise SessionStateStoreError("only terminal runs may be pruned")
        await self._delete_entity(
            previous.to_table_entity(),
            etag=etag,
            not_found_error=RunRowNotFoundError(f"run {previous.run_id!r} not found"),
            context="delete_run",
        )

    async def delete_operation(
        self,
        *,
        previous: DurableSessionOperation,
        etag: str,
    ) -> None:
        if previous.state == "active":
            raise SessionStateStoreError("active operations may not be pruned")
        await self._delete_entity(
            previous.to_table_entity(),
            etag=etag,
            not_found_error=OperationRowNotFoundError(
                f"operation {previous.operation_id!r} not found"
            ),
            context="delete_operation",
        )

    async def delete_session(self, *, previous: DurableSessionRecord, etag: str) -> None:
        if previous.status not in {"tombstoned", "deleted"}:
            raise SessionStateStoreError("only tombstoned sessions may be pruned")
        await self._delete_entity(
            previous.to_table_entity(),
            etag=etag,
            not_found_error=SessionRowNotFoundError(
                f"session {previous.session_id!r} not found"
            ),
            context="delete_session",
        )

    async def evict_run_result(
        self,
        *,
        previous: DurableRunRecord,
        etag: str,
        updated_at: datetime,
    ) -> str:
        """Retain terminal status while making removed sandbox result content unavailable."""
        if previous.status not in _TERMINAL_RUN_STATUSES:
            raise TerminalStateConflictError("only terminal run results may be evicted")
        if not previous.result_available:
            return etag
        evicted = DurableRunRecord.create(
            owner_partition=previous.owner_partition,
            session_id=previous.session_id,
            run_id=previous.run_id,
            generation=previous.generation,
            status=previous.status,
            result_available=False,
            status_reason=previous.status_reason,
            expires_at=previous.expires_at,
            created_at=previous.created_at,
            updated_at=updated_at,
            agent_slug=previous.agent_slug,
        )
        return await self._replace_entity(
            evicted.to_table_entity(),
            etag=etag,
            not_found_error=RunRowNotFoundError(f"run {previous.run_id!r} not found"),
            context="evict_run_result",
        )

    # -- bounded query ----------------------------------------------------

    async def query_entities(
        self,
        *,
        filter_expression: str,
        top: int | None = None,
        continuation_token: str | None = None,
    ) -> TableEntityPage:
        from azure.core.exceptions import HttpResponseError

        decoded_token = _decode_continuation_token(continuation_token)
        try:
            # The SDK's own type stub declares `by_page(continuation_token: str
            # | None)`, but Azure Table Storage's real continuation token is a
            # dict (verified empirically against a real Azurite Table
            # service). Silence this stub-only mismatch narrowly here.
            pager: Any = self._table_client.query_entities(
                query_filter=filter_expression,
                results_per_page=top,
            ).by_page(continuation_token=decoded_token)  # type: ignore[arg-type]
            page = await _fetch_first_page_or_none(pager)
            if page is None:
                return TableEntityPage(entities=(), continuation_token=None)
            entities = tuple([entity async for entity in page])
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="query_entities") from exc
        next_token = _encode_continuation_token(pager.continuation_token)
        return TableEntityPage(
            entities=entities,
            continuation_token=next_token,
            service_time=_latest_service_timestamp(entities),
        )

    # -- internals ----------------------------------------------------------

    async def _get_entity(
        self,
        owner_partition: OwnerPartition,
        row_key: str,
        *,
        not_found_error: SessionStateStoreError | None = None,
    ) -> SdkTableEntity:
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        try:
            return await self._table_client.get_entity(owner_partition.partition_key, row_key)
        except ResourceNotFoundError as exc:
            if not_found_error is not None:
                raise not_found_error from exc
            if row_key.startswith("session:"):
                raise SessionRowNotFoundError(f"row {row_key!r} not found") from exc
            if row_key.startswith("run:"):
                raise RunRowNotFoundError(f"row {row_key!r} not found") from exc
            if row_key.startswith("operation:"):
                raise OperationRowNotFoundError(f"row {row_key!r} not found") from exc
            raise SessionStateStoreError(f"row {row_key!r} not found") from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context="get_entity") from exc

    async def _replace_entity(
        self,
        entity: Mapping[str, object],
        *,
        etag: str,
        not_found_error: SessionStateStoreError,
        context: str,
    ) -> str:
        from azure.core.exceptions import (
            HttpResponseError,
            ResourceModifiedError,
            ResourceNotFoundError,
        )
        from azure.data.tables import UpdateMode

        try:
            result = await self._table_client.update_entity(
                entity,
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=_if_not_modified(),
            )
        except ResourceNotFoundError as exc:
            raise not_found_error from exc
        except ResourceModifiedError as exc:
            raise ConcurrencyConflictError(
                f"{context}: row changed concurrently (stale ETag)"
            ) from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context=context) from exc
        return _etag_from_write_result(result)

    async def _delete_entity(
        self,
        entity: Mapping[str, object],
        *,
        etag: str,
        not_found_error: SessionStateStoreError,
        context: str,
    ) -> None:
        from azure.core.exceptions import (
            HttpResponseError,
            ResourceModifiedError,
            ResourceNotFoundError,
        )

        partition_key = entity.get("PartitionKey")
        row_key = entity.get("RowKey")
        if not isinstance(partition_key, str) or not isinstance(row_key, str):
            raise SessionStateStoreError(f"{context}: durable entity has no key")
        try:
            await self._table_client.delete_entity(
                partition_key,
                row_key,
                etag=etag,
                match_condition=_if_not_modified(),
            )
        except ResourceNotFoundError as exc:
            raise not_found_error from exc
        except ResourceModifiedError as exc:
            raise ConcurrencyConflictError(
                f"{context}: row changed concurrently (stale ETag)"
            ) from exc
        except HttpResponseError as exc:
            raise _map_http_error(exc, context=context) from exc


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


async def _fetch_first_page_or_none(pager: Any) -> Any | None:
    """Return the pager's first page, or ``None`` when it has no pages left.

    Isolates the ``StopAsyncIteration`` handling in its own single-level
    try/except so the caller's ``HttpResponseError`` handling never nests a
    try inside a try.
    """
    try:
        return await pager.__anext__()
    except StopAsyncIteration:
        return None


def _if_not_modified() -> MatchConditions:
    from azure.core import MatchConditions

    return MatchConditions.IfNotModified


def _create_op(
    record: (
        DurableSessionRecord
        | DurableRunRecord
        | DurableSessionOperation
        | DurableIdempotencyRecord
        | DurableOwnerIdempotencyRecord
    ),
) -> _TransactionOp:
    return ("create", record.to_table_entity())


def _update_op(
    record: (
        DurableSessionRecord
        | DurableRunRecord
        | DurableSessionOperation
        | DurableIdempotencyRecord
        | DurableOwnerIdempotencyRecord
    ),
    *,
    etag: str,
) -> _TransactionOp:
    from azure.data.tables import UpdateMode

    return (
        "update",
        record.to_table_entity(),
        {"mode": UpdateMode.REPLACE, "etag": etag, "match_condition": _if_not_modified()},
    )


def _release_active_run(
    session: DurableSessionRecord, *, updated_at: datetime
) -> DurableSessionRecord:
    """Build the released-slot session record after a terminal run is adopted.

    Always targets ``status="ready"`` because the session model only allows
    ``active_run_id`` to be set while status is ``running`` or ``canceling``
    (:data:`_STATUSES_OWNING_ACTIVE_RUN`) -- those are the only two "from"
    states this helper is ever called with.
    """
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        checkpoint_expectation=session.checkpoint_expectation,
        status="ready",
        last_activity_at=updated_at,
        expires_at=session.expires_at,
        idle_policy_armed=session.idle_policy_armed,
        active_run_id=None,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


def _journal_invalidated_run(
    run: DurableRunRecord,
    updated_at: datetime,
) -> DurableRunRecord:
    """Build the narrowly authorized correction for an untrusted journal result."""
    return DurableRunRecord.create(
        owner_partition=run.owner_partition,
        session_id=run.session_id,
        run_id=run.run_id,
        generation=run.generation,
        status="failed",
        result_available=False,
        status_reason=_JOURNAL_INTEGRITY_FAILURE_REASON,
        expires_at=run.expires_at,
        created_at=run.created_at,
        updated_at=updated_at,
        agent_slug=run.agent_slug,
    )


def _require_matching_terminal_outcome(
    current: DurableRunRecord, terminal_run: DurableRunRecord, run_id: str
) -> None:
    """Raise if a run already terminal has a different status/result than requested.

    A no-op when the outcome matches, so re-adopting the same terminal
    result stays idempotent for callers.
    """
    if (
        current.status == terminal_run.status
        and current.result_available == terminal_run.result_available
    ):
        return
    raise TerminalStateConflictError(
        f"run {run_id!r} is already terminal as "
        f"{current.status!r}; cannot re-adopt as "
        f"{terminal_run.status!r}"
    )


def _validate_generation_or_raise(previous: int, candidate: int, *, backing_rebind: bool) -> None:
    try:
        validate_generation_transition(previous, candidate, backing_rebind=backing_rebind)
    except SessionStateContractError as exc:
        raise GenerationConflictError(str(exc)) from exc


def _operation_with(
    operation: DurableSessionOperation,
    *,
    target: SessionOperationTarget,
    token: str,
    phase: DurableOperationPhase,
    state: DurableOperationState,
    attempt_count: int,
    error_code: str | None,
    lease_expires_at: datetime | None,
    next_attempt_at: datetime | None,
    updated_at: datetime,
    finished_at: datetime | None,
) -> DurableSessionOperation:
    return DurableSessionOperation.create(
        owner_partition=operation.owner_partition,
        target=target,
        sequence=operation.sequence,
        kind=operation.kind,
        phase=phase,
        state=state,
        correlation_label=operation.correlation_label,
        token=token,
        attempt_count=attempt_count,
        error_code=error_code,
        lease_expires_at=lease_expires_at,
        next_attempt_at=next_attempt_at,
        created_at=operation.created_at,
        updated_at=updated_at,
        finished_at=finished_at,
        agent_slug=operation.agent_slug,
    )


def _require_operation_matches_session(
    session: DurableSessionRecord,
    operation: DurableSessionOperation,
) -> None:
    target = operation.target
    if (
        operation.owner_partition.partition_key != session.owner_partition.partition_key
        or target.session_id != session.session_id
        or (target.sandbox_id is not None and target.sandbox_id != session.sandbox_id)
        or target.generation != session.generation
        or target.digest_kind != session.digest_kind
        or target.digest != session.digest
    ):
        raise SessionStateStoreError("durable operation target no longer matches its session")


def _validate_operation_begin(
    previous: DurableSessionRecord,
    updated: DurableSessionRecord,
    operation: DurableSessionOperation,
) -> None:
    _require_operation_matches_session(previous, operation)
    _validate_checkpoint_expectation_transition(previous, updated)
    if previous.active_operation_id is not None:
        raise SessionStateStoreError("session already has an active durable operation")
    if operation.sequence != previous.operation_sequence + 1:
        raise SessionStateStoreError("operation sequence does not match the session counter")
    if (
        updated.owner_partition.partition_key != previous.owner_partition.partition_key
        or updated.session_id != previous.session_id
        or updated.sandbox_id != previous.sandbox_id
        or updated.generation != previous.generation
        or updated.active_run_id != previous.active_run_id
        or updated.active_operation_id != operation.operation_id
        or updated.operation_sequence != operation.sequence
    ):
        raise SessionStateStoreError("operation begin does not preserve the session binding")


def _validate_operation_target_update(
    previous: SessionOperationTarget,
    updated: SessionOperationTarget,
) -> None:
    normalized = SessionOperationTarget.create(
        session_id=updated.session_id,
        sandbox_id=updated.sandbox_id,
        generation=updated.generation,
        digest_kind=updated.digest_kind,
        digest=updated.digest,
        run_id=updated.run_id,
    )
    if (
        normalized.session_id != previous.session_id
        or normalized.generation != previous.generation
        or normalized.digest_kind != previous.digest_kind
        or normalized.digest != previous.digest
        or normalized.run_id != previous.run_id
        or (
            previous.sandbox_id is not None
            and normalized.sandbox_id != previous.sandbox_id
        )
    ):
        raise SessionStateStoreError("operation target binding may not be rewritten")


def _validate_operation_advance_session(
    previous: DurableSessionRecord,
    updated: DurableSessionRecord,
    target: SessionOperationTarget,
) -> None:
    _validate_checkpoint_expectation_transition(previous, updated)
    if (
        updated.owner_partition.partition_key != previous.owner_partition.partition_key
        or updated.session_id != previous.session_id
        or updated.generation != previous.generation
        or updated.digest_kind != previous.digest_kind
        or updated.digest != previous.digest
        or updated.active_operation_id != previous.active_operation_id
        or updated.operation_sequence != previous.operation_sequence
        or (
            target.sandbox_id is not None
            and updated.sandbox_id != target.sandbox_id
        )
    ):
        raise SessionStateStoreError("operation advance does not preserve the session binding")


def _validate_operation_completion(
    current_session: DurableSessionRecord,
    operation: DurableSessionOperation,
    updated_session: DurableSessionRecord,
    terminal_run: DurableRunRecord | None,
    *,
    allow_active_run_abort: bool,
) -> None:
    _require_operation_matches_session(current_session, operation)
    _validate_checkpoint_expectation_transition(current_session, updated_session)
    if (
        updated_session.owner_partition.partition_key
        != current_session.owner_partition.partition_key
        or updated_session.session_id != current_session.session_id
        or updated_session.sandbox_id != current_session.sandbox_id
        or updated_session.generation != current_session.generation
        or updated_session.active_operation_id is not None
        or updated_session.operation_sequence != current_session.operation_sequence
    ):
        raise SessionStateStoreError("operation completion does not preserve the session binding")
    target_run_id = operation.target.run_id
    if target_run_id is None:
        if terminal_run is not None:
            raise SessionStateStoreError("operation does not own a terminal run transition")
        return
    if terminal_run is None:
        if allow_active_run_abort and (
            updated_session.active_run_id == target_run_id
            or (
                operation.kind == "submit_run"
                and current_session.active_run_id is None
                and updated_session.active_run_id is None
            )
            or (
                operation.kind in {"provision_submit", "submit_run"}
                and updated_session.active_run_id is None
            )
        ):
            return
        raise SessionStateStoreError(
            "operation terminal transition does not match its run target"
        )
    if (
        terminal_run.owner_partition.partition_key != current_session.owner_partition.partition_key
        or terminal_run.session_id != current_session.session_id
        or terminal_run.run_id != target_run_id
        or terminal_run.generation != current_session.generation
        or terminal_run.status not in _TERMINAL_RUN_STATUSES
        or updated_session.active_run_id is not None
    ):
        raise SessionStateStoreError("operation terminal transition does not match its run target")


def _validate_checkpoint_expectation_transition(
    previous: DurableSessionRecord,
    updated: DurableSessionRecord,
) -> None:
    if (
        previous.checkpoint_expectation != updated.checkpoint_expectation
        and updated.checkpoint_expectation != "required"
    ):
        raise SessionStateStoreError("checkpoint expectation may only advance to required")


def _terminal_run_for_operation(
    current: DurableRunRecord,
    terminal: DurableRunRecord,
) -> DurableRunRecord | None:
    if current.status not in _TERMINAL_RUN_STATUSES:
        return terminal
    if current.status != terminal.status:
        _require_matching_terminal_outcome(current, terminal, current.run_id)
    if current.result_available and not terminal.result_available:
        return terminal
    if current.result_available != terminal.result_available:
        _require_matching_terminal_outcome(current, terminal, current.run_id)
    return None


def _parse_session_entity(entity: Mapping[str, object]) -> DurableSessionRecord:
    try:
        return DurableSessionRecord.from_table_entity(entity)
    except SessionStateContractError as exc:
        raise CorruptEntityError(f"stored session entity failed validation: {exc}") from exc


def _parse_run_entity(entity: Mapping[str, object]) -> DurableRunRecord:
    try:
        return DurableRunRecord.from_table_entity(entity)
    except SessionStateContractError as exc:
        raise CorruptEntityError(f"stored run entity failed validation: {exc}") from exc


def _parse_operation_entity(entity: Mapping[str, object]) -> DurableSessionOperation:
    try:
        return DurableSessionOperation.from_table_entity(entity)
    except SessionStateContractError as exc:
        raise CorruptEntityError(f"stored operation entity failed validation: {exc}") from exc


def _parse_idempotency_entity(entity: Mapping[str, object]) -> DurableIdempotencyRecord:
    try:
        return DurableIdempotencyRecord.from_table_entity(entity)
    except SessionStateContractError as exc:
        raise CorruptEntityError(f"stored idempotency entity failed validation: {exc}") from exc


def _parse_owner_idempotency_entity(
    entity: Mapping[str, object],
) -> DurableOwnerIdempotencyRecord:
    try:
        return DurableOwnerIdempotencyRecord.from_table_entity(entity)
    except SessionStateContractError as exc:
        raise CorruptEntityError(
            f"stored owner idempotency entity failed validation: {exc}"
        ) from exc


def _etag_from_write_result(result: Mapping[str, object]) -> str:
    etag = result.get("etag")
    if not isinstance(etag, str) or not etag:
        raise StateStoreUnavailableError("Table write response did not include an ETag")
    return etag


def _etag_from_entity(entity: SdkTableEntity) -> str:
    etag = entity.metadata.get("etag")
    if not isinstance(etag, str) or not etag:
        raise StateStoreUnavailableError("Table entity response did not include an ETag")
    return etag


def _map_http_error(exc: HttpResponseError, *, context: str) -> SessionStateStoreError:
    status_code = exc.status_code
    # azure-data-tables' error decoder (_error._decode_error) builds the
    # exception via `error_type(message=..., response=...)` and only attaches
    # `error_code` as a plain attribute afterward -- it is never passed
    # through `__init__` or declared on `HttpResponseError`, so `getattr` is
    # the correct way to read it.
    error_code = getattr(exc, "error_code", None)
    return StateStoreUnavailableError(
        f"Table service call failed ({context}): status={status_code} error_code={error_code}",
        status_code=status_code,
    )


def _encode_continuation_token(token: Mapping[str, str] | None) -> str | None:
    """Encode the SDK's dict-shaped continuation token as one opaque string."""
    if not token:
        return None
    return json.dumps(dict(token), sort_keys=True)


def _decode_continuation_token(token: str | None) -> Mapping[str, str] | None:
    if token is None:
        return None
    try:
        decoded: object = json.loads(token)
    except (TypeError, ValueError) as exc:
        raise SessionStateStoreError("invalid continuation token") from exc
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
    ):
        raise SessionStateStoreError("invalid continuation token")
    return decoded


def _reconciler_cursor_partition_key(app_hash: str) -> str:
    if not app_hash.startswith("a") or ":" in app_hash:
        raise SessionStateStoreError("invalid reconciler app hash")
    return f"{_RECONCILER_CURSOR_PARTITION_PREFIX}{app_hash}"


def _latest_service_timestamp(entities: tuple[Mapping[str, object], ...]) -> datetime | None:
    """Project the latest Azure Table service ``Timestamp`` from one read page."""
    timestamps = [
        timestamp
        for entity in entities
        if isinstance((timestamp := entity.get("Timestamp")), datetime)
        and timestamp.tzinfo is not None
        and timestamp.utcoffset() is not None
    ]
    return max(timestamps, default=None)


async def build_store_from_service_client(
    service_client: TableServiceClient, *, table_name: str = TABLE_NAME
) -> AzureTableSessionStateStore:
    """Build a store bound to ``table_name`` from an already-resolved service client."""
    table_client = service_client.get_table_client(table_name)
    return AzureTableSessionStateStore(table_client)
