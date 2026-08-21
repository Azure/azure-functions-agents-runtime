"""Real Azurite Table-service tests for the session state store.

Unlike ``tests/test_session_state_store_errors.py`` (a fast, deterministic
fake-backed suite), these tests exercise the ACTUAL optimistic-concurrency
(ETag) and entity-group-transaction (EGT) guarantees of a real Azure Table
service. A fake cannot prove server-enforced atomicity/races -- only a real
Table service (Azurite locally, the same service in Azure) can.

Requires Azurite's Table service reachable at ``127.0.0.1:10002`` (the same
endpoint/credentials ``tests/endtoend/_storage_probe.py`` already uses for
blob/queue E2E tests). Marked ``e2e`` (excluded from the default unit run;
the existing E2E pipeline -- ``eng/templates/official/jobs/e2e-tests.yml`` --
already starts Azurite with ``--tableHost 0.0.0.0`` on port 10002, so this
suite runs there with no additional CI wiring). Locally: start Azurite
(``azurite --tableHost 127.0.0.1``) then run
``pytest -m e2e tests/endtoend/test_session_state_store_azurite.py``.
Skips cleanly (rather than failing) if Azurite's Table port is unreachable.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from azure_functions_agents.controller.reconciler import SessionReconciler
from azure_functions_agents.session_state import (
    ActiveRunConflictError,
    AdmissionRecords,
    AppIdentity,
    ConcurrencyConflictError,
    CorruptEntityError,
    DurableIdempotencyRecord,
    DurableOwnerIdempotencyRecord,
    DurableRunRecord,
    DurableSessionOperation,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    GenerationConflictError,
    IdempotencyConflictError,
    OperationRowNotFoundError,
    OwnerPartition,
    ProvisionSubmitRecords,
    RowAlreadyExistsError,
    RunRowNotFoundError,
    SessionNotAdmissibleError,
    SessionOperationTarget,
    SessionRowNotFoundError,
    SessionStateStoreError,
    StaleOperationTokenError,
    StateStoreUnavailableError,
    TerminalStateConflictError,
    compute_state_store_fingerprint,
    operation_correlation_label,
    operation_id_for_sequence,
    owner_partition,
)
from azure_functions_agents.session_state.session_models import (
    DurableProviderRunMapping,
    DurableProviderSessionBinding,
)
from azure_functions_agents.session_state.store import AzureTableSessionStateStore
from azure_functions_agents.transport.transport_models import SandboxSummary
from tests.endtoend._storage_probe import DEV_CONNECTION_STRING


def _azurite_table_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 10002), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _azurite_table_reachable(),
        reason="Azurite Table service not reachable at 127.0.0.1:10002",
    ),
]

_NOW = datetime(2026, 7, 30, 16, 0, tzinfo=UTC)
_BAD_KEY_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=" + ("A" * 88) + ";"
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
)


def _unique_table_name() -> str:
    return f"SessionStateTest{uuid4().hex[:20]}"


def _new_table_client(table_name: str, *, connection_string: str = DEV_CONNECTION_STRING):  # type: ignore[no-untyped-def]
    from azure.data.tables.aio import TableClient

    return TableClient.from_connection_string(connection_string, table_name=table_name)


@contextlib.asynccontextmanager
async def _one_store() -> AsyncIterator[AzureTableSessionStateStore]:
    """One store bound to a freshly created, uniquely named table."""
    table_name = _unique_table_name()
    client = _new_table_client(table_name)
    store = AzureTableSessionStateStore(client)
    await store.ensure_table()
    try:
        yield store
    finally:
        with contextlib.suppress(Exception):
            await client.delete_table()
        await client.close()


@contextlib.asynccontextmanager
async def _two_controller_stores() -> (
    AsyncIterator[tuple[AzureTableSessionStateStore, AzureTableSessionStateStore]]
):
    """Two INDEPENDENTLY constructed stores/clients bound to the SAME table.

    Simulates two separate controller processes racing against one real
    Table service -- neither shares a Python client instance nor a cache.
    """
    table_name = _unique_table_name()
    client_a = _new_table_client(table_name)
    client_b = _new_table_client(table_name)
    store_a = AzureTableSessionStateStore(client_a)
    store_b = AzureTableSessionStateStore(client_b)
    await store_a.ensure_table()
    try:
        yield store_a, store_b
    finally:
        with contextlib.suppress(Exception):
            await client_a.delete_table()
        await client_a.close()
        await client_b.close()


def _partition(*, site_name: str = "agent-app") -> OwnerPartition:
    app = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555", site_name=site_name
    )
    return owner_partition(FunctionAppOwnerContext.create(app, "main"))


def _session(
    *,
    session_id: str = "session-1",
    status: str = "ready",
    active_run_id: str | None = None,
    generation: int = 1,
    partition: OwnerPartition | None = None,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=partition or _partition(),
        session_id=session_id,
        sandbox_id=None,
        generation=generation,
        digest_kind="funcs_zip",
        digest="sha256:" + ("b" * 64),
        protocol="1",
        status=status,  # type: ignore[arg-type]
        last_activity_at=_NOW,
        expires_at=_NOW + timedelta(hours=24),
        idle_policy_armed=False,
        active_run_id=active_run_id,
        snapshot_ids=(),
        region="westus2",
        state_store_fingerprint="s1-" + "a" * 52,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=_NOW,
        updated_at=_NOW,
        active_operation_id=None,
        operation_sequence=0,
    )


def _run(
    *,
    session_id: str = "session-1",
    run_id: str = "run-1",
    status: str = "running",
    generation: int = 1,
    result_available: bool = False,
    partition: OwnerPartition | None = None,
) -> DurableRunRecord:
    return DurableRunRecord.create(
        owner_partition=partition or _partition(),
        session_id=session_id,
        run_id=run_id,
        generation=generation,
        status=status,  # type: ignore[arg-type]
        result_available=result_available,
        status_reason=None,
        expires_at=_NOW + timedelta(minutes=15),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _pending_provider_mapping(
    *,
    partition: OwnerPartition,
    session_id: str = "session-1",
    run_id: str = "run-1",
) -> DurableProviderRunMapping:
    return DurableProviderRunMapping.create(
        owner_partition=partition,
        session_id=session_id,
        run_id=run_id,
        response_state="pending",
        provider_response_id=None,
        max_public_event_sequence=0,
        indeterminate_reason=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _admitted_session(session: DurableSessionRecord, run_id: str) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="running",
        last_activity_at=session.last_activity_at,
        expires_at=session.expires_at,
        idle_policy_armed=session.idle_policy_armed,
        active_run_id=run_id,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=session.updated_at,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


def _operation(
    session: DurableSessionRecord,
    *,
    kind: str,
    active_run_id: str | None,
    token: str,
) -> DurableSessionOperation:
    sequence = session.operation_sequence + 1
    return DurableSessionOperation.create(
        owner_partition=session.owner_partition,
        target=SessionOperationTarget.create(
            session_id=session.session_id,
            sandbox_id=session.sandbox_id,
            generation=session.generation,
            digest_kind=session.digest_kind,
            digest=session.digest,
            run_id=active_run_id,
        ),
        sequence=sequence,
        kind=kind,  # type: ignore[arg-type]
        phase=(
            "reclaim_fenced" if kind == "reclaim_backing" else "submit_admission"
        ),  # type: ignore[arg-type]
        state="active",
        correlation_label=operation_correlation_label(
            session.session_id,
            sequence,
        ),
        token=token,
        attempt_count=0,
        error_code=None,
        lease_expires_at=_NOW + timedelta(seconds=60),
        next_attempt_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        finished_at=None,
    )


def _session_with_operation(
    session: DurableSessionRecord,
    operation: DurableSessionOperation,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status=session.status,
        last_activity_at=session.last_activity_at,
        expires_at=session.expires_at,
        idle_policy_armed=False,
        active_run_id=session.active_run_id,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=_NOW,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )


def _session_after_operation(
    session: DurableSessionRecord,
    *,
    status: str = "ready",
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status=status,  # type: ignore[arg-type]
        last_activity_at=_NOW,
        expires_at=session.expires_at,
        idle_policy_armed=status == "ready",
        active_run_id=None,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=_NOW,
        active_operation_id=None,
        operation_sequence=session.operation_sequence,
    )


def _session_before_submit_rearm(session: DurableSessionRecord) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="ready",
        last_activity_at=_NOW,
        expires_at=session.expires_at,
        idle_policy_armed=False,
        active_run_id=None,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=_NOW,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


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
        updated_at=_NOW,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


def _idempotency(
    *,
    session_id: str = "session-1",
    run_id: str = "run-1",
    idempotency_hash: str,
    request_hash: str,
    partition: OwnerPartition | None = None,
) -> DurableIdempotencyRecord:
    return DurableIdempotencyRecord.create(
        owner_partition=partition or _partition(),
        session_id=session_id,
        idempotency_hash=idempotency_hash,
        request_hash=request_hash,
        run_id=run_id,
        expires_at=_NOW + timedelta(hours=1),
        created_at=_NOW,
    )


def _provision_submit_records(
    *,
    partition: OwnerPartition,
    session_id: str = "session-provision",
    run_id: str = "run-provision",
) -> ProvisionSubmitRecords:
    sequence = 1
    operation = DurableSessionOperation.create(
        owner_partition=partition,
        target=SessionOperationTarget.create(
            session_id=session_id,
            sandbox_id=None,
            generation=1,
            digest_kind="funcs_zip",
            digest="sha256:" + ("b" * 64),
            run_id=run_id,
        ),
        sequence=sequence,
        kind="provision_submit",
        phase="provision_create",
        state="active",
        correlation_label=operation_correlation_label(session_id, sequence),
        token="f" * 32,
        attempt_count=0,
        error_code=None,
        lease_expires_at=_NOW + timedelta(seconds=60),
        next_attempt_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        finished_at=None,
    )
    session = DurableSessionRecord.create(
        owner_partition=partition,
        session_id=session_id,
        sandbox_id=None,
        generation=1,
        digest_kind="funcs_zip",
        digest="sha256:" + ("b" * 64),
        protocol="1",
        status="creating",
        last_activity_at=_NOW,
        expires_at=_NOW + timedelta(hours=24),
        idle_policy_armed=False,
        active_run_id=run_id,
        snapshot_ids=(),
        region="westus2",
        state_store_fingerprint="s1-" + "a" * 52,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=_NOW,
        updated_at=_NOW,
        active_operation_id=operation_id_for_sequence(sequence),
        operation_sequence=sequence,
    )
    run = _run(
        partition=partition,
        session_id=session_id,
        run_id=run_id,
        status="accepted",
    )
    owner_idempotency = DurableOwnerIdempotencyRecord.create(
        owner_partition=partition,
        idempotency_hash=_hash("provision-key"),
        request_hash=_hash("provision-payload"),
        session_id=session_id,
        run_id=run_id,
        expires_at=session.expires_at,
        created_at=_NOW,
    )
    return ProvisionSubmitRecords.create(
        session,
        run,
        operation,
        owner_idempotency,
    )


def _hash(label: str) -> str:
    import hashlib

    return hashlib.sha256(label.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Round trips + table/partition/row key shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_run_idempotency_round_trip_and_key_shape() -> None:
    async with _one_store() as store:
        partition = _partition()
        session = _session(partition=partition)
        session_etag = await store.create_session(session)
        assert session_etag

        run = _run(partition=partition)
        await store.create_run(run)

        idem = _idempotency(
            idempotency_hash=_hash("raw-key"), request_hash=_hash("payload"), partition=partition
        )

        read_session = await store.get_session(partition, session.session_id)
        assert read_session.record == session
        read_run = await store.get_run(partition, session.session_id, run.run_id)
        assert read_run.record == run

        # Table partition/row key expectations after base32 conversion.
        assert partition.partition_key.startswith("o1:a1-")
        assert ":function_app:o1-" in partition.partition_key
        payload = partition.partition_key.split(":")[1].removeprefix("a1-")
        assert len(payload) == 52

        # Idempotency row round trip via the raw table client (store has no
        # public get for it directly outside admit_run, so verify via the
        # underlying client used to create it).
        raw_client = store._table_client  # type: ignore[attr-defined]
        await raw_client.create_entity(idem.to_table_entity())
        raw_entity = await raw_client.get_entity(partition.partition_key, str(idem.row_key))
        assert DurableIdempotencyRecord.from_table_entity(raw_entity) == idem


@pytest.mark.asyncio
async def test_durable_operation_lifecycle_and_cursor_use_real_egt_guards() -> None:
    async with _one_store() as store:
        partition = _partition()
        run = _run(partition=partition)
        session = _admitted_session(
            replace(
                _session(partition=partition),
                sandbox_id="sandbox-1",
            ),
            run.run_id,
        )
        await store.create_session(session)
        await store.create_run(run)
        session_read = await store.get_session(partition, session.session_id)
        operation = _operation(
            session_read.record,
            kind="reclaim_backing",
            active_run_id=run.run_id,
            token="a" * 32,
        )
        fenced = _session_with_operation(session_read.record, operation)
        fence = await store.begin_operation(
            previous=session_read.record,
            updated=fenced,
            operation=operation,
            etag=session_read.etag,
        )

        stored = await store.get_session(partition, session.session_id)
        assert stored.record.status == "running"
        assert stored.record.active_operation_id == operation.operation_id
        resumed = await store.resume_operation(
            owner_partition=partition,
            session_id=session.session_id,
            token="b" * 32,
            updated_at=_NOW,
        )
        assert resumed is not None
        with pytest.raises(StaleOperationTokenError):
            await store.advance_operation(
                fence=fence,
                phase="reclaim_deleting",
                updated_at=_NOW,
            )
        resumed = await store.advance_operation(
            fence=resumed,
            phase="reclaim_rearm",
            updated_at=_NOW,
        )
        assert (
            await store.get_operation(
                partition,
                session.session_id,
                operation.operation_id,
            )
        ).record.phase == "reclaim_rearm"
        terminal = replace(run, status="succeeded", result_available=False)
        completed = await store.complete_operation(
            fence=resumed,
            updated_session=_session_after_operation(
                (await store.get_session(partition, session.session_id)).record
            ),
            terminal_run=terminal,
            updated_at=_NOW,
        )
        assert completed.operation.state == "completed"
        assert (await store.get_session(partition, session.session_id)).record.status == "ready"

        first = await store.advance_reconciler_cursor(
            app_hash=partition.app_hash,
            previous=None,
            continuation_token='{"next":"one"}',
        )
        second = await store.advance_reconciler_cursor(
            app_hash=partition.app_hash,
            previous=first,
            continuation_token=None,
        )
        assert second.continuation_token is None
        with pytest.raises(ConcurrencyConflictError):
            await store.advance_reconciler_cursor(
                app_hash=partition.app_hash,
                previous=first,
                continuation_token="stale",
            )


@pytest.mark.asyncio
async def test_durable_operation_abort_and_pruning_use_real_etags() -> None:
    async with _one_store() as store:
        partition = _partition()
        session = replace(_session(partition=partition), sandbox_id="sandbox-1")
        await store.create_session(session)
        session_read = await store.get_session(partition, session.session_id)
        operation = _operation(
            session_read.record,
            kind="reclaim_backing",
            active_run_id=None,
            token="c" * 32,
        )
        fence = await store.begin_operation(
            previous=session_read.record,
            updated=_session_with_operation(session_read.record, operation),
            operation=operation,
            etag=session_read.etag,
        )
        aborted = await store.abort_operation(
            fence=fence,
            updated_session=_session_after_operation(
                (await store.get_session(partition, session.session_id)).record
            ),
            error_code="lifecycle_policy_apply_failed",
            updated_at=_NOW,
        )
        assert aborted.operation.state == "aborted"
        await store.delete_operation(
            previous=aborted.operation,
            etag=aborted.operation_etag,
        )
        with pytest.raises(OperationRowNotFoundError):
            await store.get_operation(
                partition,
                session.session_id,
                aborted.operation.operation_id,
            )


@pytest.mark.asyncio
async def test_provision_submit_reserves_owner_claim_run_and_operation_in_one_egt() -> None:
    async with _one_store() as store:
        partition = _partition()
        records = _provision_submit_records(partition=partition)

        reserved = await store.begin_provision_submit(records)

        assert reserved.replayed is False
        assert reserved.fence is not None
        session = await store.get_session(partition, records.session.session_id)
        run = await store.get_run(
            partition,
            records.session.session_id,
            records.run.run_id,
        )
        operation = await store.get_operation(
            partition,
            records.session.session_id,
            records.operation.operation_id,
        )
        owner = await store.get_owner_idempotency(
            partition,
            records.owner_idempotency.idempotency_hash,  # type: ignore[union-attr]
        )
        assert session.record.active_operation_id == records.operation.operation_id
        assert session.record.active_run_id == records.run.run_id
        assert run.record.status == "accepted"
        assert operation.record.phase == "provision_create"
        assert owner is not None

        replay = await store.begin_provision_submit(records)
        assert replay.replayed is True
        assert replay.run.run_id == records.run.run_id


@pytest.mark.asyncio
async def test_submit_admission_advances_the_existing_operation_in_one_egt() -> None:
    async with _one_store() as store:
        partition = _partition()
        session = replace(_session(partition=partition), sandbox_id="sandbox-1")
        await store.create_session(session)
        read = await store.get_session(partition, session.session_id)
        run = _run(partition=partition, run_id="run-submit", status="accepted")
        operation = _operation(
            read.record,
            kind="submit_run",
            active_run_id=run.run_id,
            token="g" * 32,
        )
        fence = await store.begin_operation(
            previous=read.record,
            updated=_session_with_operation(read.record, operation),
            operation=operation,
            etag=read.etag,
        )
        admitted = _admitted_session(
            (await store.get_session(partition, session.session_id)).record,
            run.run_id,
        )

        outcome = await store.admit_operation_run(
            fence=fence,
            records=AdmissionRecords.create(admitted, run),
        )

        assert outcome.replayed is False
        stored = await store.get_session(partition, session.session_id)
        stored_operation = await store.get_operation(
            partition,
            session.session_id,
            operation.operation_id,
        )
        assert stored.record.active_run_id == run.run_id
        assert stored.record.active_operation_id == operation.operation_id
        assert stored_operation.record.phase == "submit_journal"


@pytest.mark.asyncio
async def test_submit_admission_rejects_wrong_predecessor_phase() -> None:
    async with _one_store() as store:
        partition = _partition()
        session = replace(_session(partition=partition), sandbox_id="sandbox-1")
        await store.create_session(session)
        read = await store.get_session(partition, session.session_id)
        run = _run(partition=partition, run_id="run-wrong-phase", status="accepted")
        operation = replace(
            _operation(
                read.record,
                kind="submit_run",
                active_run_id=run.run_id,
                token="n" * 32,
            ),
            phase="submit_disarm",
        )
        fence = await store.begin_operation(
            previous=read.record,
            updated=_session_with_operation(read.record, operation),
            operation=operation,
            etag=read.etag,
        )
        admitted = _admitted_session(
            (await store.get_session(partition, session.session_id)).record,
            run.run_id,
        )

        with pytest.raises(SessionStateStoreError, match="submit_admission"):
            await store.admit_operation_run(
                fence=fence,
                records=AdmissionRecords.create(admitted, run),
            )


@pytest.mark.asyncio
async def test_terminal_submit_rearm_keeps_second_controller_non_admissible() -> None:
    async with _two_controller_stores() as (store_a, store_b):
        partition = _partition()
        session = replace(_session(partition=partition), sandbox_id="sandbox-1")
        await store_a.create_session(session)
        read = await store_a.get_session(partition, session.session_id)
        run = _run(partition=partition, run_id="run-terminal", status="accepted")
        operation = _operation(
            read.record,
            kind="submit_run",
            active_run_id=run.run_id,
            token="h" * 32,
        )
        fence = await store_a.begin_operation(
            previous=read.record,
            updated=_session_with_operation(read.record, operation),
            operation=operation,
            etag=read.etag,
        )
        admitted = _admitted_session(
            (await store_a.get_session(partition, session.session_id)).record,
            run.run_id,
        )
        await store_a.admit_operation_run(
            fence=fence,
            records=AdmissionRecords.create(admitted, run),
        )
        await store_a.adopt_terminal_run(replace(run, status="succeeded"))
        current = await store_a.get_session(partition, session.session_id)
        fence = await store_a.advance_operation(
            fence=fence,
            phase="submit_rearm",
            updated_at=_NOW,
            updated_session=_session_before_submit_rearm(current.record),
        )
        contender_run = _run(
            partition=partition,
            run_id="run-contender",
            status="accepted",
        )
        contender_session = _admitted_session(
            (await store_b.get_session(partition, session.session_id)).record,
            contender_run.run_id,
        )

        with pytest.raises(SessionNotAdmissibleError):
            await store_b.admit_run(AdmissionRecords.create(contender_session, contender_run))

        assert fence.operation_id == operation.operation_id


@pytest.mark.asyncio
async def test_journal_invalidation_overrides_a_prior_terminal_success() -> None:
    async with _two_controller_stores() as (store_a, store_b):
        partition = _partition()
        session = _session(partition=partition)
        await store_a.create_session(session)
        run = _run(partition=partition, status="accepted")
        admitted = _admitted_session(session, run.run_id)
        await store_a.admit_run(AdmissionRecords.create(admitted, run))
        succeeded = _run(partition=partition, status="succeeded", result_available=True)
        await store_a.adopt_terminal_run(succeeded)

        first = await store_b.invalidate_journal_run(
            owner_partition=partition,
            session_id=run.session_id,
            run_id=run.run_id,
            updated_at=_NOW + timedelta(seconds=1),
        )
        repeated = await store_a.invalidate_journal_run(
            owner_partition=partition,
            session_id=run.session_id,
            run_id=run.run_id,
            updated_at=_NOW + timedelta(seconds=2),
        )
        stored = await store_a.get_run(partition, run.session_id, run.run_id)

        assert first.run.status == "failed"
        assert not first.run.result_available
        assert repeated.run.status == "failed"
        assert stored.record.status == "failed"
        assert stored.record.status_reason == "journal_corrupt"
        assert not stored.record.result_available


@pytest.mark.asyncio
async def test_two_controllers_take_over_one_expired_operation_lease() -> None:
    async with _two_controller_stores() as (store_a, store_b):
        partition = _partition()
        session = _admitted_session(
            replace(_session(partition=partition), sandbox_id="sandbox-1"),
            "run-1",
        )
        run = _run(partition=partition, status="accepted")
        await store_a.create_session(session)
        await store_a.create_run(run)
        session_read = await store_a.get_session(partition, session.session_id)
        operation = replace(
            _operation(
                session_read.record,
                kind="reclaim_backing",
                active_run_id=run.run_id,
                token="a" * 32,
            ),
            lease_expires_at=_NOW - timedelta(seconds=1),
        )
        fenced = _session_with_operation(session_read.record, operation)
        original = await store_a.begin_operation(
            previous=session_read.record,
            updated=fenced,
            operation=operation,
            etag=session_read.etag,
        )

        first, second = await asyncio.gather(
            store_a.takeover_expired_operation(
                owner_partition=partition,
                session_id=session.session_id,
                token="b" * 32,
                updated_at=_NOW,
            ),
            store_b.takeover_expired_operation(
                owner_partition=partition,
                session_id=session.session_id,
                token="c" * 32,
                updated_at=_NOW,
            ),
        )

        winners = [fence for fence in (first, second) if fence is not None]
        assert len(winners) == 1
        with pytest.raises(StaleOperationTokenError):
            await store_a.advance_operation(
                fence=original,
                phase="reclaim_deleting",
                updated_at=_NOW,
            )


@pytest.mark.asyncio
async def test_two_controllers_journal_claim_and_takeover_reject_stale_token() -> None:
    async with _two_controller_stores() as (store_a, store_b):
        partition = _partition()
        session = replace(_session(partition=partition), sandbox_id="sandbox-1")
        await store_a.create_session(session)
        read = await store_a.get_session(partition, session.session_id)
        run = _run(partition=partition, run_id="run-claim", status="accepted")
        operation = _operation(
            read.record,
            kind="submit_run",
            active_run_id=run.run_id,
            token="j" * 32,
        )
        fence = await store_a.begin_operation(
            previous=read.record,
            updated=_session_with_operation(read.record, operation),
            operation=operation,
            etag=read.etag,
        )
        admitted = _admitted_session(
            (await store_a.get_session(partition, session.session_id)).record,
            run.run_id,
        )
        await store_a.admit_operation_run(
            fence=fence,
            records=AdmissionRecords.create(admitted, run),
        )

        first = await store_a.claim_operation_journal(
            owner_partition=partition,
            session_id=session.session_id,
            run_id=run.run_id,
            token="k" * 32,
            updated_at=_NOW,
        )
        blocked = await store_b.claim_operation_journal(
            owner_partition=partition,
            session_id=session.session_id,
            run_id=run.run_id,
            token="l" * 32,
            updated_at=_NOW,
        )
        assert first is not None
        assert blocked is None

        takeover = await store_b.claim_operation_journal(
            owner_partition=partition,
            session_id=session.session_id,
            run_id=run.run_id,
            token="m" * 32,
            updated_at=_NOW + timedelta(seconds=61),
        )
        assert takeover is not None
        with pytest.raises(StaleOperationTokenError):
            await store_a.advance_operation(
                fence=first,
                phase="submit_launching",
                updated_at=_NOW + timedelta(seconds=61),
            )


@pytest.mark.asyncio
async def test_reclaimer_resumes_a_crashed_durable_operation_with_real_egt() -> None:
    class Provider:
        def __init__(self, sandbox: SandboxSummary) -> None:
            self.sandbox = sandbox
            self.deleted: list[str] = []

        async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[SandboxSummary, ...]:
            if labels.get("app_hash") != partition.app_hash:
                return ()
            return (self.sandbox,)

        async def list_snapshots(self) -> tuple[object, ...]:
            return ()

        async def delete_sandbox(self, sandbox_id: str) -> None:
            self.deleted.append(sandbox_id)

        async def delete_snapshot(self, snapshot_id: str) -> None:
            del snapshot_id

    async with _one_store() as store:
        partition = _partition()
        run = _run(partition=partition)
        session = _admitted_session(
            replace(_session(partition=partition), sandbox_id="sandbox-1"),
            run.run_id,
        )
        await store.create_session(session)
        await store.create_run(run)
        session_read = await store.get_session(partition, session.session_id)
        operation = _operation(
            session_read.record,
            kind="reclaim_backing",
            active_run_id=run.run_id,
            token="e" * 32,
        )
        await store.begin_operation(
            previous=session_read.record,
            updated=_session_with_operation(session_read.record, operation),
            operation=operation,
            etag=session_read.etag,
        )
        provider = Provider(
            SandboxSummary.create(
                sandbox_id="sandbox-1",
                labels={
                    "app_hash": partition.app_hash,
                    "owner_hash": partition.owner_hash,
                    "session_id": session.session_id,
                },
            )
        )

        report = await SessionReconciler(
            store=store,
            provider=provider,  # type: ignore[arg-type]
            app_hash=partition.app_hash,
            now=lambda: _NOW + timedelta(seconds=61),
        ).reconcile_session(partition, session.session_id)

        assert report.abandoned_runs == 1
        assert report.tombstoned_sessions == 1
        assert provider.deleted == ["sandbox-1"]
        assert (await store.get_session(partition, session.session_id)).record.status == "tombstoned"
        assert (
            await store.get_operation(partition, session.session_id, operation.operation_id)
        ).record.state == "completed"


@pytest.mark.asyncio
async def test_table_creation_races_are_idempotent() -> None:
    table_name = _unique_table_name()
    client_a = _new_table_client(table_name)
    client_b = _new_table_client(table_name)
    store_a = AzureTableSessionStateStore(client_a)
    store_b = AzureTableSessionStateStore(client_b)
    try:
        # Two controllers racing to ensure the table exists must both succeed.
        await asyncio.gather(store_a.ensure_table(), store_b.ensure_table())
        await store_a.ensure_table()  # a third, later call is still a no-op
    finally:
        with contextlib.suppress(Exception):
            await client_a.delete_table()
        await client_a.close()
        await client_b.close()


# ---------------------------------------------------------------------------
# One-active-run admission races
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_controllers_race_exactly_one_admits() -> None:
    async with _two_controller_stores() as (store_a, store_b):
        partition = _partition()
        await store_a.create_session(_session(partition=partition, status="ready"))

        records_a = AdmissionRecords.create(
            _session(partition=partition, status="running", active_run_id="run-a"),
            _run(partition=partition, run_id="run-a"),
            None,
        )
        records_b = AdmissionRecords.create(
            _session(partition=partition, status="running", active_run_id="run-b"),
            _run(partition=partition, run_id="run-b"),
            None,
        )

        results = await asyncio.gather(
            store_a.admit_run(records_a), store_b.admit_run(records_b), return_exceptions=True
        )

        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(successes) == 1, results
        assert len(failures) == 1, results
        assert isinstance(failures[0], ActiveRunConflictError)

        winner_run_id = successes[0].run.run_id  # type: ignore[union-attr]
        assert failures[0].active_run_id == winner_run_id  # type: ignore[union-attr]

        stored = await store_a.get_session(partition, "session-1")
        assert stored.record.active_run_id == winner_run_id


@pytest.mark.asyncio
async def test_two_controllers_race_rearm_operation_against_admission() -> None:
    async with _two_controller_stores() as (store_a, store_b):
        partition = _partition()
        session = replace(_session(partition=partition), sandbox_id="sandbox-1")
        await store_a.create_session(session)
        current = await store_a.get_session(partition, session.session_id)
        run = _run(partition=partition, run_id="run-admission")
        operation = _operation(
            current.record,
            kind="submit_run",
            active_run_id=run.run_id,
            token="d" * 32,
        )
        operation_session = _session_with_operation(current.record, operation)
        admitted_session = _admitted_session(current.record, run.run_id)

        results = await asyncio.gather(
            store_a.begin_operation(
                previous=current.record,
                updated=operation_session,
                operation=operation,
                etag=current.etag,
            ),
            store_b.admit_run(AdmissionRecords.create(admitted_session, run)),
            return_exceptions=True,
        )

        successes = [result for result in results if not isinstance(result, BaseException)]
        failures = [result for result in results if isinstance(result, BaseException)]
        assert len(successes) == 1, results
        assert len(failures) == 1, results
        assert isinstance(failures[0], (ConcurrencyConflictError, SessionNotAdmissibleError))

        stored = await store_a.get_session(partition, session.session_id)
        assert (stored.record.active_operation_id is not None) != (
            stored.record.active_run_id is not None
        )


@pytest.mark.asyncio
async def test_active_run_conflict_when_session_already_has_active_run() -> None:
    async with _one_store() as store:
        partition = _partition()
        await store.create_session(
            _session(partition=partition, status="running", active_run_id="run-1")
        )

        records = AdmissionRecords.create(
            _session(partition=partition, status="running", active_run_id="run-2"),
            _run(partition=partition, run_id="run-2"),
            None,
        )
        with pytest.raises(ActiveRunConflictError) as excinfo:
            await store.admit_run(records)
        assert excinfo.value.active_run_id == "run-1"


@pytest.mark.asyncio
async def test_idempotency_replay_conflict_and_distinct_key_active_conflict() -> None:
    async with _one_store() as store:
        partition = _partition()
        await store.create_session(_session(partition=partition, status="ready"))

        key_hash = _hash("caller-idempotency-key")
        payload_hash = _hash("payload-v1")
        idem = _idempotency(
            idempotency_hash=key_hash, request_hash=payload_hash, partition=partition
        )
        records = AdmissionRecords.create(
            _session(partition=partition, status="running", active_run_id="run-1"),
            _run(partition=partition, run_id="run-1"),
            idem,
        )
        first = await store.admit_run(records)
        assert first.replayed is False

        # Same key + same payload -> replay of the existing run.
        replay_idem = _idempotency(
            idempotency_hash=key_hash, request_hash=payload_hash, partition=partition
        )
        replay_records = AdmissionRecords.create(
            _session(partition=partition, status="running", active_run_id="run-1"),
            _run(partition=partition, run_id="run-1"),
            replay_idem,
        )
        replay = await store.admit_run(replay_records)
        assert replay.replayed is True
        assert replay.run.run_id == "run-1"

        # Same key + DIFFERENT payload -> typed idempotency conflict.
        mismatched_idem = _idempotency(
            idempotency_hash=key_hash,
            request_hash=_hash("payload-v2-different"),
            partition=partition,
            run_id="run-2",
        )
        mismatched_records = AdmissionRecords.create(
            _session(partition=partition, status="running", active_run_id="run-2"),
            _run(partition=partition, run_id="run-2"),
            mismatched_idem,
        )
        with pytest.raises(IdempotencyConflictError) as excinfo:
            await store.admit_run(mismatched_records)
        assert excinfo.value.existing_run_id == "run-1"

        # Distinct key while a run is active -> active-run conflict, not
        # idempotency conflict.
        distinct_idem = _idempotency(
            idempotency_hash=_hash("a-totally-different-caller-key"),
            request_hash=_hash("payload-v3"),
            partition=partition,
            run_id="run-3",
        )
        distinct_records = AdmissionRecords.create(
            _session(partition=partition, status="running", active_run_id="run-3"),
            _run(partition=partition, run_id="run-3"),
            distinct_idem,
        )
        with pytest.raises(ActiveRunConflictError) as excinfo2:
            await store.admit_run(distinct_records)
        assert excinfo2.value.active_run_id == "run-1"


@pytest.mark.asyncio
async def test_admission_transaction_is_atomic_on_run_collision() -> None:
    async with _one_store() as store:
        partition = _partition()
        await store.create_session(_session(partition=partition, status="ready"))
        await store.create_run(_run(partition=partition, run_id="run-1"))  # pre-existing

        records = AdmissionRecords.create(
            _session(partition=partition, status="running", active_run_id="run-1"),
            _run(partition=partition, run_id="run-1"),
            _idempotency(
                idempotency_hash=_hash("key"), request_hash=_hash("payload"), partition=partition
            ),
        )
        with pytest.raises(RowAlreadyExistsError):
            await store.admit_run(records)

        # Atomic rollback: session must still show no active run, and the
        # idempotency row must not have been created either.
        stored = await store.get_session(partition, "session-1")
        assert stored.record.active_run_id is None
        idem_lookup = await store._get_idempotency(  # type: ignore[attr-defined]
            partition, "session-1", _hash("key")
        )
        assert idem_lookup is None


@pytest.mark.asyncio
async def test_admission_does_not_resurrect_a_quarantined_session() -> None:
    async with _two_controller_stores() as (store_a, store_b):
        partition = _partition()
        await store_a.create_session(_session(partition=partition, status="ready"))
        activated = await store_a.get_session(partition, "session-1")
        quarantined = _quarantined_session(activated.record)
        await store_b.update_session(
            previous=activated.record,
            updated=quarantined,
            etag=activated.etag,
        )

        run = _run(partition=partition, run_id="run-after-quarantine")
        records = AdmissionRecords.create(
            _admitted_session(activated.record, run.run_id),
            run,
        )
        with pytest.raises(ConcurrencyConflictError):
            await store_a.admit_run(
                records,
                expected_session_etag=activated.etag,
            )

        stored = await store_a.get_session(partition, "session-1")
        assert stored.record.status == "quarantined"
        assert stored.record.quarantine_reason == "sandbox_manifest_mismatch"
        assert stored.record.active_run_id is None
        with pytest.raises(RunRowNotFoundError):
            await store_a.get_run(partition, "session-1", run.run_id)


@pytest.mark.asyncio
async def test_admission_does_not_resurrect_a_tombstoned_session() -> None:
    async with _two_controller_stores() as (store_a, store_b):
        partition = _partition()
        await store_a.create_session(_session(partition=partition, status="ready"))
        activated = await store_a.get_session(partition, "session-1")
        await store_b.tombstone_session(
            previous=activated.record,
            etag=activated.etag,
            tombstone_reason="owner_deleted",
            updated_at=_NOW,
        )

        run = _run(partition=partition, run_id="run-after-tombstone")
        records = AdmissionRecords.create(
            _admitted_session(activated.record, run.run_id),
            run,
        )
        with pytest.raises(ConcurrencyConflictError):
            await store_a.admit_run(
                records,
                expected_session_etag=activated.etag,
            )

        stored = await store_a.get_session(partition, "session-1")
        assert stored.record.status == "tombstoned"
        assert stored.record.tombstone_reason == "owner_deleted"
        assert stored.record.active_run_id is None
        with pytest.raises(RunRowNotFoundError):
            await store_a.get_run(partition, "session-1", run.run_id)


# ---------------------------------------------------------------------------
# ETag / generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_etag_rejected_then_converges_after_reread() -> None:
    async with _one_store() as store:
        partition = _partition()
        session = _session(partition=partition, status="ready")
        etag = await store.create_session(session)

        bumped = DurableSessionRecord.create(
            owner_partition=partition,
            session_id=session.session_id,
            sandbox_id="sandbox-123",
            generation=1,
            digest_kind=session.digest_kind,
            digest=session.digest,
            protocol=session.protocol,
            status="ready",
            last_activity_at=_NOW,
            expires_at=session.expires_at,
            idle_policy_armed=False,
            active_run_id=None,
            snapshot_ids=(),
            region=session.region,
            state_store_fingerprint=session.state_store_fingerprint,
            quarantine_reason=None,
            tombstone_reason=None,
            created_at=session.created_at,
            updated_at=_NOW,
            active_operation_id=session.active_operation_id,
            operation_sequence=session.operation_sequence,
        )
        fresh_etag = await store.update_session(previous=session, updated=bumped, etag=etag)

        # Our stale etag must now be rejected.
        with pytest.raises(ConcurrencyConflictError):
            await store.update_session(previous=session, updated=bumped, etag=etag)

        # Re-reading and retrying with the fresh etag succeeds.
        reread = await store.get_session(partition, session.session_id)
        assert reread.etag == fresh_etag
        again = DurableSessionRecord.create(
            owner_partition=partition,
            session_id=session.session_id,
            sandbox_id="sandbox-456",
            generation=1,
            digest_kind=session.digest_kind,
            digest=session.digest,
            protocol=session.protocol,
            status="ready",
            last_activity_at=_NOW,
            expires_at=session.expires_at,
            idle_policy_armed=False,
            active_run_id=None,
            snapshot_ids=(),
            region=session.region,
            state_store_fingerprint=session.state_store_fingerprint,
            quarantine_reason=None,
            tombstone_reason=None,
            created_at=session.created_at,
            updated_at=_NOW,
            active_operation_id=session.active_operation_id,
            operation_sequence=session.operation_sequence,
        )
        newest_etag = await store.update_session(
            previous=reread.record, updated=again, etag=reread.etag
        )
        assert newest_etag != fresh_etag


@pytest.mark.asyncio
async def test_lower_generation_rejected_equal_generation_legal() -> None:
    async with _one_store() as store:
        partition = _partition()
        session = _session(partition=partition, generation=3)
        etag = await store.create_session(session)

        lower = DurableSessionRecord.create(
            owner_partition=partition,
            session_id=session.session_id,
            sandbox_id=None,
            generation=2,
            digest_kind=session.digest_kind,
            digest=session.digest,
            protocol=session.protocol,
            status="ready",
            last_activity_at=_NOW,
            expires_at=session.expires_at,
            idle_policy_armed=False,
            active_run_id=None,
            snapshot_ids=(),
            region=session.region,
            state_store_fingerprint=session.state_store_fingerprint,
            quarantine_reason=None,
            tombstone_reason=None,
            created_at=session.created_at,
            updated_at=_NOW,
            active_operation_id=session.active_operation_id,
            operation_sequence=session.operation_sequence,
        )
        with pytest.raises(GenerationConflictError):
            await store.update_session(previous=session, updated=lower, etag=etag)

        equal = DurableSessionRecord.create(
            owner_partition=partition,
            session_id=session.session_id,
            sandbox_id="new-sandbox",
            generation=3,
            digest_kind=session.digest_kind,
            digest=session.digest,
            protocol=session.protocol,
            status="ready",
            last_activity_at=_NOW,
            expires_at=session.expires_at,
            idle_policy_armed=False,
            active_run_id=None,
            snapshot_ids=(),
            region=session.region,
            state_store_fingerprint=session.state_store_fingerprint,
            quarantine_reason=None,
            tombstone_reason=None,
            created_at=session.created_at,
            updated_at=_NOW,
            active_operation_id=session.active_operation_id,
            operation_sequence=session.operation_sequence,
        )
        new_etag = await store.update_session(previous=session, updated=equal, etag=etag)
        assert new_etag != etag


# ---------------------------------------------------------------------------
# Terminal adoption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_atomic_terminal_adoption_releases_slot_and_status() -> None:
    async with _one_store() as store:
        partition = _partition()
        await store.create_session(_session(partition=partition, status="ready"))
        await store.admit_run(
            AdmissionRecords.create(
                _session(partition=partition, status="running", active_run_id="run-1"),
                _run(partition=partition),
                None,
            )
        )

        outcome = await store.adopt_terminal_run(
            _run(partition=partition, status="succeeded", result_available=True)
        )
        assert outcome.slot_released is True

        stored = await store.get_session(partition, "session-1")
        assert stored.record.active_run_id is None
        assert stored.record.status == "ready"


@pytest.mark.asyncio
async def test_two_controllers_race_terminal_adoption_converges_without_error() -> None:
    async with _two_controller_stores() as (store_a, store_b):
        partition = _partition()
        await store_a.create_session(_session(partition=partition, status="ready"))
        await store_a.admit_run(
            AdmissionRecords.create(
                _session(partition=partition, status="running", active_run_id="run-1"),
                _run(partition=partition),
                None,
            )
        )

        terminal = _run(partition=partition, status="succeeded", result_available=True)
        results = await asyncio.gather(
            store_a.adopt_terminal_run(terminal),
            store_b.adopt_terminal_run(terminal),
            return_exceptions=True,
        )
        # Both calls must converge WITHOUT error (idempotent terminal
        # adoption): exactly one releases the slot, the other is a safe no-op.
        for result in results:
            assert not isinstance(result, BaseException), results
        released = [r.slot_released for r in results]  # type: ignore[union-attr]
        assert released.count(True) == 1
        assert released.count(False) == 1

        stored = await store_a.get_session(partition, "session-1")
        assert stored.record.active_run_id is None
        assert stored.record.status == "ready"


@pytest.mark.asyncio
async def test_terminal_adoption_rejects_conflicting_outcome() -> None:
    async with _one_store() as store:
        partition = _partition()
        await store.create_session(_session(partition=partition, status="ready"))
        await store.admit_run(
            AdmissionRecords.create(
                _session(partition=partition, status="running", active_run_id="run-1"),
                _run(partition=partition),
                None,
            )
        )
        await store.adopt_terminal_run(
            _run(partition=partition, status="succeeded", result_available=True)
        )

        with pytest.raises(TerminalStateConflictError):
            await store.adopt_terminal_run(
                _run(partition=partition, status="failed", result_available=False)
            )


# ---------------------------------------------------------------------------
# Tombstone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_persists_and_is_readable_afterward() -> None:
    async with _one_store() as store:
        partition = _partition()
        session = _session(partition=partition, status="ready")
        etag = await store.create_session(session)

        await store.tombstone_session(
            previous=session, etag=etag, tombstone_reason="sandbox_lost", updated_at=_NOW
        )

        read = await store.get_session(partition, session.session_id)
        assert read.record.status == "tombstoned"
        assert read.record.tombstone_reason == "sandbox_lost"
        assert read.record.active_run_id is None
        # Historical fields remain readable (future 410-mapping needs this).
        assert read.record.digest == session.digest
        assert read.record.sandbox_id == session.sandbox_id


# ---------------------------------------------------------------------------
# Pagination / continuation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bounded_query_pagination_covers_all_rows_without_duplicates() -> None:
    async with _one_store() as store:
        partition = _partition()
        for index in range(10):
            await store.create_session(
                _session(partition=partition, session_id=f"session-{index:02d}")
            )

        seen: set[str] = set()
        token: str | None = None
        pages = 0
        while True:
            page = await store.query_entities(
                filter_expression=f"PartitionKey eq '{partition.partition_key}'",
                top=3,
                continuation_token=token,
            )
            pages += 1
            for entity in page.entities:
                seen.add(str(entity["RowKey"]))
            token = page.continuation_token
            if token is None:
                break
            assert pages < 20  # safety bound against an infinite loop

        assert seen == {f"session:session-{index:02d}" for index in range(10)}
        assert pages >= 4  # 10 rows at 3/page requires at least 4 pages


# ---------------------------------------------------------------------------
# Fail-closed: unavailable / corrupt entity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_credentials_fail_closed_as_unavailable() -> None:
    table_name = _unique_table_name()
    good_client = _new_table_client(table_name)
    bad_client = _new_table_client(table_name, connection_string=_BAD_KEY_CONNECTION_STRING)
    good_store = AzureTableSessionStateStore(good_client)
    bad_store = AzureTableSessionStateStore(bad_client)
    try:
        await good_store.ensure_table()
        with pytest.raises(StateStoreUnavailableError) as excinfo:
            await bad_store.create_session(_session())
        assert excinfo.value.status_code == 403
    finally:
        with contextlib.suppress(Exception):
            await good_client.delete_table()
        await good_client.close()
        await bad_client.close()


@pytest.mark.asyncio
async def test_get_session_and_run_not_found_map_to_typed_errors() -> None:
    async with _one_store() as store:
        partition = _partition()
        with pytest.raises(SessionRowNotFoundError):
            await store.get_session(partition, "does-not-exist")
        with pytest.raises(RunRowNotFoundError):
            await store.get_run(partition, "does-not-exist", "run-1")


@pytest.mark.asyncio
async def test_corrupt_stored_entity_fails_closed_never_coerced() -> None:
    async with _one_store() as store:
        partition = _partition()
        session = _session(partition=partition)
        await store.create_session(session)

        # Directly corrupt the row via the raw client, bypassing the typed
        # record contract, to simulate a hand-edited/corrupted real row.
        from azure.data.tables import UpdateMode

        raw_client = store._table_client  # type: ignore[attr-defined]
        entity = session.to_table_entity()
        entity["status"] = "not-a-real-status"
        await raw_client.update_entity(entity, mode=UpdateMode.REPLACE)

        with pytest.raises(CorruptEntityError, match="failed validation"):
            await store.get_session(partition, session.session_id)


# ---------------------------------------------------------------------------
# No secrets in identity/logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_and_errors_never_leak_the_connection_string_or_key() -> None:
    async with _one_store() as store:
        partition = _partition()
        await store.create_session(_session(partition=partition))

        from azure.data.tables.aio import TableServiceClient

        service_client = TableServiceClient.from_connection_string(DEV_CONNECTION_STRING)
        try:
            fingerprint = compute_state_store_fingerprint(service_client)
        finally:
            await service_client.close()
        assert "AccountKey" not in fingerprint
        assert "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq" not in fingerprint

        try:
            await store.create_session(_session(partition=partition))  # duplicate -> error
        except Exception as exc:
            assert "AccountKey" not in str(exc)
            assert "DefaultEndpointsProtocol" not in str(exc)


# ---------------------------------------------------------------------------
# Private Foundry Responses mapping rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_mapping_lifecycle_uses_real_etags_and_retains_terminal_replay() -> None:
    async with _one_store() as store:
        partition = _partition()
        await store.create_session(_session(partition=partition, status="ready"))
        await store.admit_run(
            AdmissionRecords.create(
                _session(partition=partition, status="running", active_run_id="run-1"),
                _run(partition=partition),
                None,
            )
        )
        mapping = _pending_provider_mapping(partition=partition)
        mapping_etag = await store.create_provider_run_mapping(mapping)
        binding = DurableProviderSessionBinding.create(
            owner_partition=partition,
            session_id=mapping.session_id,
            provider_session_id="agent-session_123",
            created_at=_NOW,
            updated_at=_NOW,
        )
        await store.create_provider_session_binding(binding)
        assert (await store.get_provider_session_binding(partition, mapping.session_id)) is not None

        pending = await store.get_provider_run_mapping(
            partition,
            mapping.session_id,
            mapping.run_id,
        )
        assert pending is not None
        assert pending.etag == mapping_etag
        await store.mark_provider_submission_issued(
            previous=pending.record,
            etag=pending.etag,
            updated_at=_NOW + timedelta(milliseconds=500),
        )
        submitting = await store.get_provider_run_mapping(
            mapping.owner_partition,
            mapping.session_id,
            mapping.run_id,
        )
        assert submitting is not None
        await store.bind_provider_response_id(
            previous=submitting.record,
            etag=submitting.etag,
            provider_response_id="caresp_0123456789",
            updated_at=_NOW + timedelta(seconds=1),
        )
        bound = await store.get_provider_run_mapping(
            partition,
            mapping.session_id,
            mapping.run_id,
        )
        assert bound is not None
        assert bound.record.response_state == "bound"
        with pytest.raises(ConcurrencyConflictError):
            await store.advance_provider_event_watermark(
                previous=bound.record,
                etag=pending.etag,
                max_public_event_sequence=4,
                updated_at=_NOW + timedelta(seconds=2),
            )
        watermark_etag = await store.advance_provider_event_watermark(
            previous=bound.record,
            etag=bound.etag,
            max_public_event_sequence=4,
            updated_at=_NOW + timedelta(seconds=2),
        )
        watermarked = await store.get_provider_run_mapping(
            partition,
            mapping.session_id,
            mapping.run_id,
        )
        assert watermarked is not None
        assert watermarked.etag == watermark_etag
        assert watermarked.record.max_public_event_sequence == 4

        outcome = await store.adopt_provider_terminal_run(
            _run(partition=partition, status="succeeded", result_available=True)
        )
        terminal = await store.get_provider_run_mapping(
            partition,
            mapping.session_id,
            mapping.run_id,
        )
        assert outcome.slot_released is True
        assert terminal is not None
        assert terminal.record.response_state == "terminal"
        assert terminal.record.provider_response_id == "caresp_0123456789"
        await store.clear_provider_run_mapping(previous=terminal.record, etag=terminal.etag)
        assert (
            await store.get_provider_run_mapping(partition, mapping.session_id, mapping.run_id)
        ) is None


@pytest.mark.asyncio
async def test_provider_indeterminate_transition_quarantines_without_provider_lookup() -> None:
    async with _two_controller_stores() as (store_a, store_b):
        partition = _partition()
        await store_a.create_session(_session(partition=partition, status="ready"))
        await store_a.admit_run(
            AdmissionRecords.create(
                _session(partition=partition, status="running", active_run_id="run-1"),
                _run(partition=partition),
                None,
            )
        )
        mapping = _pending_provider_mapping(partition=partition)
        await store_a.create_provider_run_mapping(mapping)
        pending = await store_a.get_provider_run_mapping(
            partition,
            mapping.session_id,
            mapping.run_id,
        )
        assert pending is not None
        await store_a.mark_provider_submission_issued(
            previous=pending.record,
            etag=pending.etag,
            updated_at=_NOW + timedelta(milliseconds=500),
        )
        submitting = await store_a.get_provider_run_mapping(
            mapping.owner_partition,
            mapping.session_id,
            mapping.run_id,
        )
        assert submitting is not None
        await store_a.bind_provider_response_id(
            previous=submitting.record,
            etag=submitting.etag,
            provider_response_id="caresp_0123456789",
            updated_at=_NOW,
        )

        outcomes = await asyncio.gather(
            store_a.mark_provider_run_indeterminate(
                owner_partition=partition,
                session_id=mapping.session_id,
                run_id=mapping.run_id,
                reason="provider_termination_indeterminate",
                updated_at=_NOW + timedelta(seconds=1),
            ),
            store_b.mark_provider_run_indeterminate(
                owner_partition=partition,
                session_id=mapping.session_id,
                run_id=mapping.run_id,
                reason="provider_termination_indeterminate",
                updated_at=_NOW + timedelta(seconds=1),
            ),
        )

        assert {outcome.run.status for outcome in outcomes} == {"abandoned"}
        stored_session = await store_a.get_session(partition, mapping.session_id)
        stored_run = await store_a.get_run(partition, mapping.session_id, mapping.run_id)
        stored_mapping = await store_a.get_provider_run_mapping(
            partition,
            mapping.session_id,
            mapping.run_id,
        )
        assert stored_session.record.status == "quarantined"
        assert stored_session.record.active_run_id is None
        assert stored_run.record.status == "abandoned"
        assert stored_run.record.status_reason == "provider_termination_indeterminate"
        assert stored_mapping is not None
        assert stored_mapping.record.response_state == "indeterminate"
        assert stored_mapping.record.provider_response_id == "caresp_0123456789"


@pytest.mark.asyncio
async def test_aca_reconciler_scan_ignores_private_provider_mapping_rows() -> None:
    async with _one_store() as store:
        partition = _partition()
        await store.create_session(_session(partition=partition, status="ready"))
        await store.admit_run(
            AdmissionRecords.create(
                _session(partition=partition, status="running", active_run_id="run-1"),
                _run(partition=partition),
                None,
            )
        )
        mapping = _pending_provider_mapping(partition=partition)
        await store.create_provider_run_mapping(mapping)
        await store.create_provider_session_binding(
            DurableProviderSessionBinding.create(
                owner_partition=partition,
                session_id=mapping.session_id,
                provider_session_id="agent-session_123",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )

        reconciler = SessionReconciler(
            store=store,
            provider=object(),  # type: ignore[arg-type]
            app_hash=partition.app_hash,
        )
        sessions, runs, idempotencies, operations, _service_time = (
            await reconciler._load_working_set()  # type: ignore[attr-defined]
        )

        assert [session.session_id for session in sessions] == [mapping.session_id]
        assert [run.run_id for run in runs] == [mapping.run_id]
        assert idempotencies == ()
        assert operations == ()
