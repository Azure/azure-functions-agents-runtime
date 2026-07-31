"""Real Azurite Table-service tests for P3b's session state store.

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
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from azure_functions_agents.session_state import (
    ActiveRunConflictError,
    AdmissionRecords,
    AppIdentity,
    ConcurrencyConflictError,
    CorruptEntityError,
    DurableIdempotencyRecord,
    DurableRunRecord,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    GenerationConflictError,
    IdempotencyConflictError,
    OwnerPartition,
    RowAlreadyExistsError,
    RunRowNotFoundError,
    SessionRowNotFoundError,
    StateStoreUnavailableError,
    TerminalStateConflictError,
    compute_state_store_fingerprint,
    owner_partition,
)
from azure_functions_agents.session_state.store import AzureTableSessionStateStore
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
    return f"P3bTest{uuid4().hex[:20]}"


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
