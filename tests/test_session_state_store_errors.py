"""Dense pure unit tests for :mod:`azure_functions_agents.session_state.store`.

Uses a minimal in-memory fake standing in for ``azure.data.tables.aio.TableClient``
(test double only -- never imported by ``src``) so error-mapping, admission
ordering, and terminal-adoption logic can be verified deterministically and
fast, without a running Azurite instance. Genuine concurrency/CAS/EGT races
against a REAL Table service are covered separately by
``tests/endtoend/test_session_state_store_azurite.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.data.tables import TableTransactionError

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
    operation_correlation_label,
    owner_partition,
)
from azure_functions_agents.session_state.store import (
    AzureTableSessionStateStore,
    build_store_from_service_client,
)

_NOW = datetime(2026, 7, 30, 16, 0, tzinfo=UTC)
_FINGERPRINT = "s1-" + "a" * 52


class _FakeEntity(dict[str, object]):
    """Minimal stand-in for the SDK's ``TableEntity`` (dict + ``.metadata``)."""

    def __init__(self, values: Mapping[str, object], etag: str) -> None:
        super().__init__(values)
        self.metadata = {"etag": etag}


class _FakeTableClient:
    """In-memory double implementing just the calls ``store.py`` makes.

    Mimics real Azure Table semantics (existence + ETag checks) closely
    enough to exercise every branch in ``store.py`` deterministically.
    ``raise_once`` lets a test force a specific exception from the next call
    to a named method; ``transaction_failure`` lets a test force
    ``submit_transaction`` to fail atomically at a specific op index.
    """

    def __init__(self) -> None:
        self._entities: dict[tuple[str, str], dict[str, object]] = {}
        self._etags: dict[tuple[str, str], str] = {}
        self._etag_counter = 0
        self.raise_once: dict[str, Exception] = {}
        self.transaction_failure: tuple[int, Exception] | None = None
        self.transaction_options: list[dict[str, object]] = []

    def _next_etag(self) -> str:
        self._etag_counter += 1
        return f"etag-{self._etag_counter}"

    def _maybe_raise(self, method: str) -> None:
        exc = self.raise_once.pop(method, None)
        if exc is not None:
            raise exc

    async def create_table(self) -> None:
        self._maybe_raise("create_table")

    async def create_entity(self, entity: Mapping[str, object]) -> dict[str, object]:
        self._maybe_raise("create_entity")
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if key in self._entities:
            raise ResourceExistsError("entity exists")
        etag = self._next_etag()
        self._entities[key] = dict(entity)
        self._etags[key] = etag
        return {"etag": etag}

    async def get_entity(self, partition_key: str, row_key: str) -> _FakeEntity:
        self._maybe_raise("get_entity")
        key = (partition_key, row_key)
        if key not in self._entities:
            raise ResourceNotFoundError("entity not found")
        return _FakeEntity(self._entities[key], self._etags[key])

    async def update_entity(
        self,
        entity: Mapping[str, object],
        *,
        mode: Any = None,
        etag: str | None = None,
        match_condition: Any = None,
    ) -> dict[str, object]:
        del mode, match_condition
        self._maybe_raise("update_entity")
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if key not in self._entities:
            raise ResourceNotFoundError("entity not found")
        if etag is not None and self._etags[key] != etag:
            raise ResourceModifiedError("etag mismatch")
        new_etag = self._next_etag()
        self._entities[key] = dict(entity)
        self._etags[key] = new_etag
        return {"etag": new_etag}

    async def submit_transaction(
        self,
        operations: list[Any],
        **kwargs: object,
    ) -> list[dict[str, object]]:
        self.transaction_options.append(kwargs)
        self._maybe_raise("submit_transaction")
        if self.transaction_failure is not None:
            index, base_exc = self.transaction_failure
            error = TableTransactionError(message=str(base_exc))
            error.index = index
            error.status_code = getattr(base_exc, "status_code", None)
            error.error_code = getattr(base_exc, "error_code", None)
            raise error

        # Real Table transactions validate/apply atomically and surface ANY
        # failing op wrapped in one TableTransactionError carrying that op's
        # index (verified against real Azurite) -- never a bare
        # ResourceExistsError/ResourceModifiedError escaping the transaction.
        for index, (kind, entity, *rest) in enumerate(operations):
            key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
            if kind == "create" and key in self._entities:
                raise _transaction_error(index, 409, "EntityAlreadyExists")
            if kind == "update":
                if key not in self._entities:
                    raise _transaction_error(index, 404, "ResourceNotFound")
                kwargs = rest[0] if rest else {}
                expected_etag = kwargs.get("etag")
                if expected_etag is not None and self._etags[key] != expected_etag:
                    raise _transaction_error(index, 412, "UpdateConditionNotSatisfied")

        results = []
        for _kind, entity, *_rest in operations:
            key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
            etag = self._next_etag()
            self._entities[key] = dict(entity)
            self._etags[key] = etag
            results.append({"etag": etag})
        return results


def _transaction_error(index: int, status_code: int, error_code: str) -> TableTransactionError:
    error = TableTransactionError(message=f"{status_code} {error_code}")
    error.index = index
    error.status_code = status_code
    error.error_code = error_code
    return error


def _partition() -> Any:
    app = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555", site_name="agent-app"
    )
    return owner_partition(FunctionAppOwnerContext.create(app, "main"))


def _session(
    *,
    session_id: str = "session-1",
    status: str = "ready",
    active_run_id: str | None = None,
    generation: int = 1,
    quarantine_reason: str | None = None,
    tombstone_reason: str | None = None,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=_partition(),
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
        state_store_fingerprint=_FINGERPRINT,
        quarantine_reason=quarantine_reason,
        tombstone_reason=tombstone_reason,
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
) -> DurableRunRecord:
    return DurableRunRecord.create(
        owner_partition=_partition(),
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


def _fake_sha256(label: str) -> str:
    """Deterministic-looking SHA-256 hex digest for test fixtures."""
    import hashlib

    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _idempotency(
    *,
    session_id: str = "session-1",
    run_id: str = "run-1",
    request_hash: str = "c" * 64,
    idempotency_hash: str = "d" * 64,
) -> DurableIdempotencyRecord:
    return DurableIdempotencyRecord.create(
        owner_partition=_partition(),
        session_id=session_id,
        idempotency_hash=idempotency_hash,
        request_hash=request_hash,
        run_id=run_id,
        expires_at=_NOW + timedelta(hours=1),
        created_at=_NOW,
    )


def _provision_submit_records(
    *,
    session_id: str = "session-provision",
    run_id: str = "run-provision",
) -> ProvisionSubmitRecords:
    sequence = 1
    operation = DurableSessionOperation.create(
        owner_partition=_partition(),
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
        token="a" * 32,
        attempt_count=0,
        error_code=None,
        lease_expires_at=_NOW + timedelta(seconds=60),
        next_attempt_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        finished_at=None,
    )
    session = DurableSessionRecord.create(
        owner_partition=_partition(),
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
        state_store_fingerprint=_FINGERPRINT,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=_NOW,
        updated_at=_NOW,
        active_operation_id=operation.operation_id,
        operation_sequence=sequence,
    )
    run = _run(session_id=session_id, run_id=run_id, status="accepted")
    return ProvisionSubmitRecords.create(session, run, operation)


def _session_after_prelaunch_cancel(
    session: DurableSessionRecord,
    *,
    updated_at: datetime,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="ready",
        last_activity_at=updated_at,
        expires_at=session.expires_at,
        idle_policy_armed=False,
        active_run_id=None,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=None,
        operation_sequence=session.operation_sequence,
    )


# ---------------------------------------------------------------------------
# ensure_table / create_session / create_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_table_swallows_already_exists_and_maps_other_errors() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]

    await store.ensure_table()  # first call: fine

    fake.raise_once["create_table"] = ResourceExistsError("already there")
    await store.ensure_table()  # idempotent: swallowed

    fake.raise_once["create_table"] = _http_error(500, "InternalError")
    with pytest.raises(StateStoreUnavailableError) as excinfo:
        await store.ensure_table()
    assert excinfo.value.status_code == 500


@pytest.mark.asyncio
async def test_create_session_maps_exists_and_unavailable_errors() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]

    await store.create_session(_session())
    with pytest.raises(RowAlreadyExistsError):
        await store.create_session(_session())

    fake.raise_once["create_entity"] = _http_error(403, "AuthorizationFailure")
    with pytest.raises(StateStoreUnavailableError) as excinfo:
        await store.create_session(_session(session_id="other"))
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_create_run_maps_exists_and_unavailable_errors() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]

    await store.create_run(_run())
    with pytest.raises(RowAlreadyExistsError):
        await store.create_run(_run())

    fake.raise_once["create_entity"] = _http_error(429, "TooManyRequests")
    with pytest.raises(StateStoreUnavailableError) as excinfo:
        await store.create_run(_run(run_id="other"))
    assert excinfo.value.status_code == 429


# ---------------------------------------------------------------------------
# get_session / get_run / corrupt entities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_and_get_run_map_not_found() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]

    with pytest.raises(SessionRowNotFoundError):
        await store.get_session(_partition(), "missing")
    with pytest.raises(RunRowNotFoundError):
        await store.get_run(_partition(), "missing", "run-1")


@pytest.mark.asyncio
async def test_corrupt_stored_entity_fails_closed_never_coerced() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session())

    # Corrupt the row directly (bypassing the typed record contract) to
    # simulate a hand-edited/corrupted Table row.
    key = (_partition().partition_key, "session:session-1")
    fake._entities[key]["status"] = "not-a-real-status"

    with pytest.raises(CorruptEntityError, match="failed validation"):
        await store.get_session(_partition(), "session-1")


# ---------------------------------------------------------------------------
# update_session / generation / tombstone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_session_maps_stale_etag_and_missing_row() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    etag = await store.create_session(_session())

    updated = replace(_session(), digest="sha256:" + ("c" * 64))
    with pytest.raises(ConcurrencyConflictError):
        await store.update_session(previous=_session(), updated=updated, etag="stale-etag")

    new_etag = await store.update_session(previous=_session(), updated=updated, etag=etag)
    assert new_etag != etag

    missing = _session(session_id="never-created")
    with pytest.raises(SessionRowNotFoundError):
        await store.update_session(previous=missing, updated=missing, etag="whatever")


@pytest.mark.asyncio
async def test_update_session_rejects_generation_rollback_but_allows_equal() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    etag = await store.create_session(_session(generation=2))

    with pytest.raises(GenerationConflictError, match="preserve"):
        await store.update_session(
            previous=_session(generation=2),
            updated=_session(generation=1),
            etag=etag,
        )

    same_gen = replace(_session(generation=2), digest="sha256:" + ("e" * 64))
    new_etag = await store.update_session(
        previous=_session(generation=2), updated=same_gen, etag=etag
    )
    assert new_etag != etag


@pytest.mark.asyncio
async def test_tombstone_session_preserves_historical_fields() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    session = _session(status="ready")
    etag = await store.create_session(session)

    await store.tombstone_session(
        previous=session, etag=etag, tombstone_reason="owner_deleted", updated_at=_NOW
    )

    read = await store.get_session(_partition(), session.session_id)
    assert read.record.status == "tombstoned"
    assert read.record.tombstone_reason == "owner_deleted"
    assert read.record.digest == session.digest  # historical field preserved
    assert read.record.generation == session.generation


# ---------------------------------------------------------------------------
# admit_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admit_run_succeeds_when_no_active_run() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="run-1"), _run(), None
    )
    outcome = await store.admit_run(records)

    assert outcome.replayed is False
    assert outcome.run.run_id == "run-1"
    stored = await store.get_session(_partition(), "session-1")
    assert stored.record.active_run_id == "run-1"


@pytest.mark.asyncio
async def test_admit_run_accepts_a_resumed_suspended_session() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="suspended"))

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="run-1"), _run(), None
    )
    await store.admit_run(records)

    stored = await store.get_session(_partition(), "session-1")
    assert stored.record.status == "running"
    assert stored.record.active_run_id == "run-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "quarantine_reason", "tombstone_reason"),
    [
        ("creating", None, None),
        ("suspending", None, None),
        ("resuming", None, None),
        ("failed", None, None),
        ("quarantined", "sandbox_manifest_mismatch", None),
        ("tombstoned", None, "owner_deleted"),
        ("deleting", None, None),
        ("deleted", None, None),
    ],
)
async def test_admit_run_rejects_a_session_not_ready_for_a_new_run(
    status: str,
    quarantine_reason: str | None,
    tombstone_reason: str | None,
) -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    blocked = _session(
        status=status,
        quarantine_reason=quarantine_reason,
        tombstone_reason=tombstone_reason,
    )
    await store.create_session(blocked)

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="run-1"), _run(), None
    )
    with pytest.raises(SessionNotAdmissibleError):
        await store.admit_run(records)

    stored = await store.get_session(_partition(), "session-1")
    assert stored.record == blocked
    with pytest.raises(RunRowNotFoundError):
        await store.get_run(_partition(), "session-1", "run-1")


@pytest.mark.asyncio
async def test_admit_run_rejects_a_stale_expected_session_etag() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    session = _session(status="ready")
    stale_etag = await store.create_session(session)
    fresh_etag = await store.update_session(
        previous=session,
        updated=session,
        etag=stale_etag,
    )

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="run-1"), _run(), None
    )
    with pytest.raises(ConcurrencyConflictError):
        await store.admit_run(records, expected_session_etag=stale_etag)

    stored = await store.get_session(_partition(), "session-1")
    assert stored.etag == fresh_etag
    assert stored.record == session
    with pytest.raises(RunRowNotFoundError):
        await store.get_run(_partition(), "session-1", "run-1")


@pytest.mark.asyncio
async def test_admit_run_raises_active_run_conflict_when_already_active() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="running", active_run_id="existing-run"))

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="new-run"), _run(run_id="new-run"), None
    )
    with pytest.raises(ActiveRunConflictError) as excinfo:
        await store.admit_run(records)
    assert excinfo.value.active_run_id == "existing-run"


@pytest.mark.asyncio
async def test_admit_run_transaction_race_reports_winner_run_id() -> None:
    """A concurrent winner fully committing between our pre-flight read and
    our own transaction submission must be observed on the post-failure
    re-read and reported as ``ActiveRunConflictError`` naming the winner --
    not because the pre-flight read already saw it (that would just
    duplicate ``test_admit_run_raises_active_run_conflict_when_already_active``
    without ever reaching ``submit_transaction``/the index-0 handler at all).
    """
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))

    key = (_partition().partition_key, "session:session-1")
    real_submit = fake.submit_transaction

    async def _commit_winner_then_fail(operations: Any) -> Any:
        # The winner's own transaction fully commits in the narrow window
        # between our pre-flight read (already done above, saw no active
        # run) and our own submission -- then our stale-ETag op fails with
        # a 412-style conflict at index 0, exactly as real Azure Table
        # would report a lost race.
        fake._entities[key]["active_run_id"] = "winner-run"
        fake._entities[key]["status"] = "running"
        return await real_submit(operations)

    fake.submit_transaction = _commit_winner_then_fail  # type: ignore[method-assign]
    fake.transaction_failure = (0, _http_error(412, "UpdateConditionNotSatisfied"))

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="loser-run"), _run(run_id="loser-run"), None
    )

    with pytest.raises(ActiveRunConflictError) as excinfo:
        await store.admit_run(records)
    assert excinfo.value.active_run_id == "winner-run"


@pytest.mark.asyncio
async def test_admit_run_index0_race_with_matching_idempotency_replays_winner() -> None:
    """Same key + same payload race that lands on the index-0 session-CAS
    failure (not index-2) must still replay the winner, not raise
    ``ActiveRunConflictError``. In a real EGT, whichever op Azure Table
    reports first is index 0; a winning admission's session-update always
    invalidates a concurrent loser's ETag before its idempotency-row create
    would independently conflict, so this race reliably lands on index 0 --
    not the narrower index-2 window covered by
    ``test_admit_run_idempotency_row_collision_during_transaction_replays_winner``.

    The winner's run/idempotency rows and the session's ``active_run_id``
    only appear *inside* the wrapped ``submit_transaction`` -- i.e. strictly
    after our own pre-flight idempotency check and session read (both of
    which must see nothing yet, or the race would be caught earlier and
    this handler would never run) -- so this genuinely drives execution
    into the index-0 handler's own idempotency re-check, not the upfront
    one.
    """
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))

    shared_hash = _fake_sha256("shared-payload")
    key = (_partition().partition_key, "session:session-1")
    real_submit = fake.submit_transaction

    async def _commit_winner_then_fail(operations: Any) -> Any:
        winner_run = _run(run_id="winner-run")
        winner_idem = _idempotency(request_hash=shared_hash, run_id="winner-run")
        await fake.create_entity(winner_run.to_table_entity())
        await fake.create_entity(winner_idem.to_table_entity())
        fake._entities[key]["active_run_id"] = "winner-run"
        fake._entities[key]["status"] = "running"
        return await real_submit(operations)

    fake.submit_transaction = _commit_winner_then_fail  # type: ignore[method-assign]
    fake.transaction_failure = (0, _http_error(412, "UpdateConditionNotSatisfied"))

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="loser-run"),
        _run(run_id="loser-run"),
        _idempotency(request_hash=shared_hash, run_id="loser-run"),
    )
    outcome = await store.admit_run(records)

    assert outcome.replayed is True
    assert outcome.run.run_id == "winner-run"


@pytest.mark.asyncio
async def test_admit_run_index0_race_with_no_active_run_and_no_idempotency_is_retryable() -> None:
    """Index-0 (stale session ETag) with no idempotency key configured and
    a re-read that still shows no active run -- e.g. an unrelated
    concurrent write changed the session's ETag without setting
    ``active_run_id`` -- must surface a retryable ``ConcurrencyConflictError``,
    not silently succeed or misreport an active-run conflict that doesn't
    exist.
    """
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))

    key = (_partition().partition_key, "session:session-1")
    real_submit = fake.submit_transaction

    async def _unrelated_write_then_fail(operations: Any) -> Any:
        # Some other concurrent write (not an admission) bumps this row's
        # ETag without touching active_run_id -- e.g. a no-op
        # update_session -- so our own stale-ETag op still fails at
        # index 0, but the re-read finds no winner to report.
        fake._entities[key] = dict(fake._entities[key])
        fake._etags[key] = "etag-unrelated-write"
        return await real_submit(operations)

    fake.submit_transaction = _unrelated_write_then_fail  # type: ignore[method-assign]
    fake.transaction_failure = (0, _http_error(412, "UpdateConditionNotSatisfied"))

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="run-1"), _run(run_id="run-1"), None
    )
    with pytest.raises(ConcurrencyConflictError):
        await store.admit_run(records)


@pytest.mark.asyncio
async def test_admit_run_rejects_generation_rollback() -> None:
    """A caller admitting against a session generation lower than what is
    currently stored must get a typed ``GenerationConflictError``, not a
    silent overwrite -- admission is never a backing rebind, so the stored
    and target generations must match exactly (mirrors
    ``update_session``/``tombstone_session``'s rollback protection).
    """
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None, generation=2))

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="run-1", generation=1),
        _run(run_id="run-1", generation=1),
        None,
    )
    with pytest.raises(GenerationConflictError):
        await store.admit_run(records)

    stored = await store.get_session(_partition(), "session-1")
    assert stored.record.active_run_id is None
    assert stored.record.generation == 2


@pytest.mark.asyncio
async def test_admit_run_idempotency_row_collision_during_transaction_replays_winner() -> None:
    """Covers the ``exc.index == 2`` branch: the idempotency-row CREATE op
    collides *inside* the transaction even though the upfront pre-check
    (moments earlier) found nothing -- i.e. another admission using the same
    idempotency key/payload fully committed (including its idempotency row)
    in the narrow window between our pre-check and our own transaction. The
    session-update/run-create ops in the SAME transaction are NOT forced to
    fail, isolating this from the (separately tested) index-0 session race.

    The upfront check is monkeypatched to return ``None`` exactly once (the
    pre-check "not found yet") while the real winning row already exists in
    the fake's storage, so the exception handler's own re-check (unpatched)
    genuinely finds it -- deterministically reproducing a race that is too
    narrow to trigger reliably via simple two-way ``asyncio.gather`` (the
    session-row CAS collision would almost always win that race first; see
    ``test_admit_run_transaction_race_reports_winner_run_id`` for that case).
    """
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))

    shared_hash = _fake_sha256("shared-payload")
    winner_run = _run(run_id="winner-run")
    winner_idem = _idempotency(request_hash=shared_hash, run_id="winner-run")
    await fake.create_entity(winner_run.to_table_entity())
    await fake.create_entity(winner_idem.to_table_entity())

    original_try_replay = store._try_replay_idempotency
    call_count = 0

    async def _pre_check_blind_once(partition: Any, session_id: str, idempotency: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None  # upfront pre-check: simulate "not found yet"
        return await original_try_replay(partition, session_id, idempotency)

    store._try_replay_idempotency = _pre_check_blind_once  # type: ignore[method-assign]
    fake.transaction_failure = (2, _http_error(409, "EntityAlreadyExists"))

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="loser-run"),
        _run(run_id="loser-run"),
        _idempotency(request_hash=shared_hash, run_id="loser-run"),
    )
    outcome = await store.admit_run(records)

    assert outcome.replayed is True
    assert outcome.run.run_id == "winner-run"
    assert call_count == 2


@pytest.mark.asyncio
async def test_admit_run_idempotency_row_collision_during_transaction_conflicts_on_mismatch() -> (
    None
):
    """Same ``exc.index == 2`` race as above, but the row that won the race
    used a DIFFERENT payload -- the re-check must raise
    :class:`IdempotencyConflictError`, not replay a mismatched run.
    """
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))

    shared_key_hash = _fake_sha256("shared-key")
    winner_run = _run(run_id="winner-run")
    winner_idem = _idempotency(
        idempotency_hash=shared_key_hash,
        request_hash=_fake_sha256("winner-payload"),
        run_id="winner-run",
    )
    await fake.create_entity(winner_run.to_table_entity())
    await fake.create_entity(winner_idem.to_table_entity())

    original_try_replay = store._try_replay_idempotency
    call_count = 0

    async def _pre_check_blind_once(partition: Any, session_id: str, idempotency: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None
        return await original_try_replay(partition, session_id, idempotency)

    store._try_replay_idempotency = _pre_check_blind_once  # type: ignore[method-assign]
    fake.transaction_failure = (2, _http_error(409, "EntityAlreadyExists"))

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="loser-run"),
        _run(run_id="loser-run"),
        _idempotency(
            idempotency_hash=shared_key_hash,
            request_hash=_fake_sha256("loser-payload"),
            run_id="loser-run",
        ),
    )
    with pytest.raises(IdempotencyConflictError) as excinfo:
        await store.admit_run(records)
    assert excinfo.value.existing_run_id == "winner-run"
    assert call_count == 2


@pytest.mark.asyncio
async def test_admit_run_idempotency_row_collision_not_found_on_reread_is_retryable() -> None:
    """Same ``exc.index == 2`` shape, but the re-check (unpatched, real) finds
    NO idempotency row at all -- an inconsistent state where the transaction
    claimed a collision yet a consistent re-read shows nothing. There is no
    "existing run" to report here, so this must raise
    :class:`ConcurrencyConflictError` (retryable) rather than fabricate an
    :class:`IdempotencyConflictError` pointing at the caller's own new run id.
    """
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))

    # No winner run/idempotency row is ever created in `fake` -- the upfront
    # pre-check and the post-failure re-check both genuinely find nothing.
    fake.transaction_failure = (2, _http_error(409, "EntityAlreadyExists"))

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="loser-run"),
        _run(run_id="loser-run"),
        _idempotency(request_hash=_fake_sha256("payload"), run_id="loser-run"),
    )

    with pytest.raises(ConcurrencyConflictError):
        await store.admit_run(records)


@pytest.mark.asyncio
async def test_admit_run_replays_same_key_same_payload() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))
    idem = _idempotency(request_hash=_fake_sha256("payload"))
    records = AdmissionRecords.create(
        _session(status="running", active_run_id="run-1"), _run(), idem
    )
    first = await store.admit_run(records)
    assert first.replayed is False

    # A second admission attempt with the SAME idempotency key/payload, even
    # though the session now has a (different-looking) admitted run request,
    # must replay the original run rather than conflict.
    replay_records = AdmissionRecords.create(
        _session(status="running", active_run_id="run-1"),
        _run(),
        _idempotency(request_hash=_fake_sha256("payload")),
    )
    second = await store.admit_run(replay_records)
    assert second.replayed is True
    assert second.run.run_id == first.run.run_id


@pytest.mark.asyncio
async def test_admit_run_same_key_different_payload_conflicts() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))
    idem = _idempotency(request_hash=_fake_sha256("payload-A"))
    records = AdmissionRecords.create(
        _session(status="running", active_run_id="run-1"), _run(), idem
    )
    await store.admit_run(records)

    conflicting = AdmissionRecords.create(
        _session(status="running", active_run_id="run-2"),
        _run(run_id="run-2"),
        _idempotency(run_id="run-2", request_hash=_fake_sha256("payload-B")),
    )
    with pytest.raises(IdempotencyConflictError) as excinfo:
        await store.admit_run(conflicting)
    assert excinfo.value.existing_run_id == "run-1"


@pytest.mark.asyncio
async def test_admit_run_distinct_key_while_active_yields_active_conflict() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))
    await store.admit_run(
        AdmissionRecords.create(
            _session(status="running", active_run_id="run-1"),
            _run(),
            _idempotency(request_hash=_fake_sha256("key-A-payload")),
        )
    )

    with pytest.raises(ActiveRunConflictError) as excinfo:
        await store.admit_run(
            AdmissionRecords.create(
                _session(status="running", active_run_id="run-2"),
                _run(run_id="run-2"),
                DurableIdempotencyRecord.create(
                    owner_partition=_partition(),
                    session_id="session-1",
                    idempotency_hash="e" * 64,  # distinct key hash
                    request_hash=_fake_sha256("key-B-payload"),
                    run_id="run-2",
                    expires_at=_NOW + timedelta(hours=1),
                    created_at=_NOW,
                ),
            )
        )
    assert excinfo.value.active_run_id == "run-1"


@pytest.mark.asyncio
async def test_admit_run_run_row_collision_maps_to_already_exists() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))
    await store.create_run(_run())  # pre-existing run row with the same id

    records = AdmissionRecords.create(
        _session(status="running", active_run_id="run-1"), _run(), None
    )
    with pytest.raises(RowAlreadyExistsError):
        await store.admit_run(records)

    # Atomicity: the session must NOT have been updated by the failed transaction.
    stored = await store.get_session(_partition(), "session-1")
    assert stored.record.active_run_id is None


# ---------------------------------------------------------------------------
# adopt_terminal_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopt_terminal_run_releases_slot_and_is_idempotent() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))
    await store.admit_run(
        AdmissionRecords.create(_session(status="running", active_run_id="run-1"), _run(), None)
    )

    terminal = _run(status="succeeded", result_available=True)
    first = await store.adopt_terminal_run(terminal)
    assert first.slot_released is True

    stored = await store.get_session(_partition(), "session-1")
    assert stored.record.active_run_id is None
    assert stored.record.status == "ready"

    second = await store.adopt_terminal_run(terminal)
    assert second.slot_released is False  # idempotent no-op


@pytest.mark.asyncio
async def test_adopt_existing_terminal_run_releases_a_stale_active_slot() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    terminal = _run(status="succeeded", result_available=False)
    active_session = _session(status="running", active_run_id=terminal.run_id)
    await store.create_session(active_session)
    await store.create_run(terminal)

    outcome = await store.adopt_terminal_run(terminal)

    assert outcome.slot_released is True
    assert (await store.get_session(_partition(), active_session.session_id)).record.status == "ready"


@pytest.mark.asyncio
async def test_adopt_terminal_run_never_resurrects_an_evicted_result() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    evicted = _run(status="succeeded", result_available=False)
    await store.create_run(evicted)

    outcome = await store.adopt_terminal_run(
        _run(status="succeeded", result_available=True)
    )

    assert outcome.run.result_available is False
    stored = await store.get_run(_partition(), "session-1", "run-1")
    assert stored.record.result_available is False


@pytest.mark.asyncio
async def test_adopt_terminal_run_rejects_conflicting_terminal_outcome() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))
    await store.admit_run(
        AdmissionRecords.create(_session(status="running", active_run_id="run-1"), _run(), None)
    )
    await store.adopt_terminal_run(_run(status="succeeded", result_available=True))

    with pytest.raises(TerminalStateConflictError):
        await store.adopt_terminal_run(_run(status="failed", result_available=False))


@pytest.mark.asyncio
async def test_adopt_terminal_run_requires_a_terminal_status() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    with pytest.raises(SessionStateStoreError, match="terminal status"):
        await store.adopt_terminal_run(_run(status="running"))


# ---------------------------------------------------------------------------
# continuation tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_entities_rejects_malformed_continuation_tokens() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    with pytest.raises(SessionStateStoreError):
        await store.query_entities(filter_expression="true", continuation_token="not-json")


@pytest.mark.asyncio
async def test_query_entities_rejects_a_token_with_non_string_values() -> None:
    import json

    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    bad_token = json.dumps({"PartitionKey": 123})
    with pytest.raises(SessionStateStoreError):
        await store.query_entities(filter_expression="true", continuation_token=bad_token)


# ---------------------------------------------------------------------------
# Additional coverage: identity guards, corrupt run/idempotency rows, unused
# construction helper, generic unavailable mapping on less-common call sites.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_session_rejects_mismatched_session_identity() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session())

    with pytest.raises(SessionStateStoreError, match="same session identity"):
        await store.update_session(
            previous=_session(session_id="session-1"),
            updated=_session(session_id="other-session"),
            etag="whatever",
        )


@pytest.mark.asyncio
async def test_corrupt_run_and_idempotency_entities_fail_closed() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session())
    await store.create_run(_run())

    run_key = (_partition().partition_key, "run:session-1:run-1")
    fake._entities[run_key]["status"] = "not-a-real-status"
    with pytest.raises(CorruptEntityError, match="stored run entity"):
        await store.get_run(_partition(), "session-1", "run-1")

    idem = _idempotency()
    await fake.create_entity(idem.to_table_entity())
    idem_key = (_partition().partition_key, str(idem.row_key))
    fake._entities[idem_key]["run_id"] = ""  # blank out a required field
    with pytest.raises(CorruptEntityError, match="stored idempotency entity"):
        await store._get_idempotency(  # type: ignore[attr-defined]
            _partition(), "session-1", idem.idempotency_hash
        )


@pytest.mark.asyncio
async def test_get_run_and_adopt_terminal_run_map_generic_unavailable_errors() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    await store.create_session(_session(status="ready", active_run_id=None))
    await store.admit_run(
        AdmissionRecords.create(_session(status="running", active_run_id="run-1"), _run(), None)
    )

    fake.raise_once["get_entity"] = _http_error(503, "ServerBusy")
    with pytest.raises(StateStoreUnavailableError) as excinfo:
        await store.get_run(_partition(), "session-1", "run-1")
    assert excinfo.value.status_code == 503

    fake.raise_once["submit_transaction"] = _http_error(500, "InternalError")
    with pytest.raises(StateStoreUnavailableError) as excinfo2:
        await store.adopt_terminal_run(_run(status="succeeded", result_available=True))
    assert excinfo2.value.status_code == 500


@pytest.mark.asyncio
async def test_adopt_terminal_run_session_missing_still_terminalizes_run() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    # A run row can exist even if its session row was already tombstoned/
    # deleted by a different path; adoption should still terminalize the run.
    await store.create_run(_run())

    outcome = await store.adopt_terminal_run(_run(status="succeeded", result_available=True))
    assert outcome.slot_released is False


@pytest.mark.asyncio
async def test_build_store_from_service_client_binds_the_named_table() -> None:
    class _FakeServiceClient:
        def __init__(self) -> None:
            self.requested_table_name: str | None = None

        def get_table_client(self, table_name: str) -> _FakeTableClient:
            self.requested_table_name = table_name
            return _FakeTableClient()

    service_client = _FakeServiceClient()
    store = await build_store_from_service_client(
        service_client,  # type: ignore[arg-type]
        table_name="CustomTableName",
    )
    assert isinstance(store, AzureTableSessionStateStore)
    assert service_client.requested_table_name == "CustomTableName"


@pytest.mark.asyncio
async def test_provision_submit_confirmation_upgrades_an_ambiguous_acknowledgement() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    submit = fake.submit_transaction

    async def commit_then_lose_acknowledgement(
        operations: Any,
        **kwargs: Any,
    ) -> Any:
        await submit(operations, **kwargs)
        raise _http_error(503, "ServerBusy")

    fake.submit_transaction = commit_then_lose_acknowledgement  # type: ignore[method-assign]

    outcome = await store.begin_provision_submit(records)

    assert outcome.admission == "committed"
    assert outcome.replayed is False
    assert outcome.run == records.run
    assert outcome.fence is not None
    assert outcome.fence.operation_id == records.operation.operation_id


@pytest.mark.asyncio
async def test_provision_submit_unconfirmed_acknowledgement_returns_unresolved_candidate() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    fake.raise_once["submit_transaction"] = _http_error(503, "ServerBusy")

    outcome = await store.begin_provision_submit(records)

    assert outcome.admission == "possibly_committed"
    assert outcome.replayed is False
    assert outcome.run == records.run
    assert outcome.run_etag is None
    assert outcome.fence is None
    assert fake._entities == {}


@pytest.mark.asyncio
async def test_provision_submit_cancellation_before_egt_propagates() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    initial = _provision_submit_records()
    owner_idempotency = DurableOwnerIdempotencyRecord.create(
        owner_partition=initial.session.owner_partition,
        idempotency_hash="d" * 64,
        request_hash="e" * 64,
        session_id=initial.session.session_id,
        run_id=initial.run.run_id,
        expires_at=_NOW + timedelta(hours=1),
        created_at=_NOW,
    )
    records = ProvisionSubmitRecords.create(
        initial.session,
        initial.run,
        initial.operation,
        owner_idempotency,
    )
    entered = asyncio.Event()

    async def block_owner_read(*args: object) -> _FakeEntity:
        del args
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    fake.get_entity = block_owner_read  # type: ignore[method-assign]
    task = asyncio.create_task(store.begin_provision_submit(records))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake._entities == {}


@pytest.mark.asyncio
async def test_provision_submit_cancellation_during_egt_propagates() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    entered = asyncio.Event()

    async def block_transaction(
        operations: object,
        **kwargs: object,
    ) -> object:
        del operations, kwargs
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    fake.submit_transaction = block_transaction  # type: ignore[method-assign]
    task = asyncio.create_task(store.begin_provision_submit(records))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake._entities == {}


@pytest.mark.asyncio
async def test_operation_admission_confirmation_upgrades_an_ambiguous_acknowledgement() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    session = _session(status="ready")
    session_etag = await store.create_session(session)
    run = _run(run_id="run-admission", status="accepted")
    operation = DurableSessionOperation.create(
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
        phase="submit_admission",
        state="active",
        correlation_label=operation_correlation_label(session.session_id, 1),
        token="a" * 32,
        attempt_count=0,
        error_code=None,
        lease_expires_at=_NOW + timedelta(seconds=60),
        next_attempt_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        finished_at=None,
    )
    prepared = replace(
        session,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    fence = await store.begin_operation(
        previous=session,
        updated=prepared,
        operation=operation,
        etag=session_etag,
    )
    records = AdmissionRecords.create(
        replace(prepared, status="running", active_run_id=run.run_id),
        run,
        None,
    )
    submit = fake.submit_transaction

    async def commit_then_lose_acknowledgement(
        operations: Any,
        **kwargs: Any,
    ) -> Any:
        await submit(operations, **kwargs)
        raise _http_error(503, "ServerBusy")

    fake.submit_transaction = commit_then_lose_acknowledgement  # type: ignore[method-assign]

    outcome = await store.admit_operation_run(fence=fence, records=records)

    assert outcome.admission == "committed"
    assert outcome.replayed is False
    assert outcome.run == run
    stored = await store.get_session(session.owner_partition, session.session_id)
    assert stored.record.active_run_id == run.run_id


@pytest.mark.asyncio
async def test_cancel_provision_submit_terminalizes_before_launch_and_retains_fence() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    reserved = await store.begin_provision_submit(records)
    assert reserved.fence is not None
    assigned_session = replace(
        records.session,
        sandbox_id="sandbox-1",
        updated_at=_NOW + timedelta(seconds=1),
    )
    assigned_target = replace(records.operation.target, sandbox_id="sandbox-1")
    await store.advance_operation(
        fence=reserved.fence,
        phase="provision_lifecycle",
        updated_session=assigned_session,
        updated_target=assigned_target,
        updated_at=_NOW + timedelta(seconds=1),
    )

    outcome = await store.cancel_prelaunch_submit(
        owner_partition=records.session.owner_partition,
        session_id=records.session.session_id,
        run_id=records.run.run_id,
        token="b" * 32,
        updated_at=_NOW + timedelta(seconds=2),
    )

    assert outcome.disposition == "canceled_before_launch"
    assert outcome.run.status == "canceled"
    assert outcome.run.status_reason == "canceled_before_launch"
    assert outcome.fence is not None
    assert outcome.fence.token == "b" * 32
    assert outcome.session.active_run_id == records.run.run_id
    assert outcome.session.active_operation_id == records.operation.operation_id
    assert outcome.session.sandbox_id == "sandbox-1"
    assert outcome.operation is not None
    assert outcome.operation.phase == "provision_rearm"
    assert outcome.operation.target.sandbox_id == "sandbox-1"

    repeated = await store.cancel_prelaunch_submit(
        owner_partition=records.session.owner_partition,
        session_id=records.session.session_id,
        run_id=records.run.run_id,
        token="c" * 32,
        updated_at=_NOW + timedelta(seconds=2),
    )
    assert repeated.disposition == "terminal"
    adopted = await store.adopt_terminal_run(outcome.run)
    assert adopted.slot_released is False


@pytest.mark.asyncio
async def test_canceled_pre_pointer_provision_can_complete_as_tombstoned() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    reserved = await store.begin_provision_submit(records)
    assert reserved.fence is not None
    canceled = await store.cancel_prelaunch_submit(
        owner_partition=records.session.owner_partition,
        session_id=records.session.session_id,
        run_id=records.run.run_id,
        token="b" * 32,
        updated_at=_NOW + timedelta(seconds=1),
    )
    assert canceled.fence is not None
    assert canceled.session.sandbox_id is None
    tombstoned = replace(
        canceled.session,
        status="tombstoned",
        active_run_id=None,
        active_operation_id=None,
        tombstone_reason="provision_terminal_before_pointer",
        updated_at=_NOW + timedelta(seconds=2),
    )

    await store.complete_operation(
        fence=canceled.fence,
        updated_session=tombstoned,
        terminal_run=canceled.run,
        updated_at=_NOW + timedelta(seconds=2),
    )

    session = await store.get_session(records.session.owner_partition, records.session.session_id)
    run = await store.get_run(
        records.run.owner_partition,
        records.run.session_id,
        records.run.run_id,
    )
    operation = await store.get_operation(
        records.operation.owner_partition,
        records.operation.target.session_id,
        records.operation.operation_id,
    )
    assert session.record.status == "tombstoned"
    assert session.record.active_run_id is None
    assert session.record.active_operation_id is None
    assert run.record.status == "canceled"
    assert operation.record.state == "completed"


@pytest.mark.asyncio
async def test_existing_submit_cancels_before_journal_claim() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    session = _session(status="ready")
    session_etag = await store.create_session(session)
    run = _run(run_id="run-existing-cancel", status="accepted")
    operation = DurableSessionOperation.create(
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
        phase="submit_admission",
        state="active",
        correlation_label=operation_correlation_label(session.session_id, 1),
        token="a" * 32,
        attempt_count=0,
        error_code=None,
        lease_expires_at=_NOW + timedelta(seconds=60),
        next_attempt_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        finished_at=None,
    )
    prepared = replace(
        session,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )
    fence = await store.begin_operation(
        previous=session,
        updated=prepared,
        operation=operation,
        etag=session_etag,
    )
    admitted = replace(prepared, status="running", active_run_id=run.run_id)
    await store.admit_operation_run(
        fence=fence,
        records=AdmissionRecords.create(admitted, run, None),
    )

    canceled = await store.cancel_prelaunch_submit(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        run_id=run.run_id,
        token="b" * 32,
        updated_at=_NOW + timedelta(seconds=1),
    )
    claimed = await store.claim_operation_journal(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        run_id=run.run_id,
        token="c" * 32,
        updated_at=_NOW + timedelta(seconds=2),
    )

    assert canceled.disposition == "canceled_before_launch"
    assert canceled.run.status == "canceled"
    assert canceled.operation is not None
    assert canceled.operation.kind == "submit_run"
    assert canceled.operation.phase == "submit_rearm"
    assert canceled.session.active_run_id == run.run_id
    assert claimed is None


@pytest.mark.asyncio
async def test_cancel_provision_submit_reports_launch_claimed_without_terminalizing() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    reserved = await store.begin_provision_submit(records)
    assert reserved.fence is not None
    fence = reserved.fence
    for phase in (
        "provision_lifecycle",
        "provision_content",
        "provision_manifest",
        "provision_journal",
    ):
        fence = await store.advance_operation(
            fence=fence,
            phase=phase,  # type: ignore[arg-type]
            updated_at=_NOW + timedelta(seconds=1),
        )
    claimed = await store.claim_operation_journal(
        owner_partition=records.session.owner_partition,
        session_id=records.session.session_id,
        run_id=records.run.run_id,
        token="c" * 32,
        updated_at=_NOW + timedelta(seconds=2),
    )
    assert claimed is not None

    outcome = await store.cancel_prelaunch_submit(
        owner_partition=records.session.owner_partition,
        session_id=records.session.session_id,
        run_id=records.run.run_id,
        token="d" * 32,
        updated_at=_NOW + timedelta(seconds=3),
    )

    assert outcome.disposition == "launch_claimed"
    assert outcome.run.status == "accepted"
    assert outcome.fence is None
    assert outcome.operation is not None
    assert outcome.operation.phase == "provision_launching"


@pytest.mark.asyncio
async def test_cancel_provision_submit_reports_retry_for_an_unmatched_active_slot() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    await store.begin_provision_submit(records)
    session_key = (records.session.owner_partition.partition_key, str(records.session.row_key))
    fake._entities[session_key]["active_operation_id"] = ""

    outcome = await store.cancel_prelaunch_submit(
        owner_partition=records.session.owner_partition,
        session_id=records.session.session_id,
        run_id=records.run.run_id,
        token="b" * 32,
        updated_at=_NOW + timedelta(seconds=1),
    )

    assert outcome.disposition == "retry"
    assert outcome.run.status == "accepted"
    assert outcome.fence is None


@pytest.mark.asyncio
async def test_journal_claim_uses_the_accepted_run_etag_in_its_egt() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    reserved = await store.begin_provision_submit(records)
    assert reserved.fence is not None
    fence = reserved.fence
    for phase in (
        "provision_lifecycle",
        "provision_content",
        "provision_manifest",
        "provision_journal",
    ):
        fence = await store.advance_operation(
            fence=fence,
            phase=phase,  # type: ignore[arg-type]
            updated_at=_NOW + timedelta(seconds=1),
        )
    run_key = (records.run.owner_partition.partition_key, str(records.run.row_key))
    real_submit = fake.submit_transaction

    async def _change_run_etag_then_submit(operations: Any) -> Any:
        fake._etags[run_key] = "etag-run-raced"
        return await real_submit(operations)

    fake.submit_transaction = _change_run_etag_then_submit  # type: ignore[method-assign]
    with pytest.raises(StaleOperationTokenError):
        await store.claim_operation_journal(
            owner_partition=records.session.owner_partition,
            session_id=records.session.session_id,
            run_id=records.run.run_id,
            token="b" * 32,
            updated_at=_NOW + timedelta(seconds=2),
        )

    operation = await store.get_operation(
        records.session.owner_partition,
        records.session.session_id,
        records.operation.operation_id,
    )
    assert operation.record.phase == "provision_journal"


@pytest.mark.asyncio
async def test_prelaunch_cancel_completion_clears_operation_and_active_run_together() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    await store.begin_provision_submit(records)
    canceled = await store.cancel_prelaunch_submit(
        owner_partition=records.session.owner_partition,
        session_id=records.session.session_id,
        run_id=records.run.run_id,
        token="b" * 32,
        updated_at=_NOW + timedelta(seconds=1),
    )
    assert canceled.fence is not None

    completed = await store.complete_operation(
        fence=canceled.fence,
        updated_session=_session_after_prelaunch_cancel(
            canceled.session,
            updated_at=_NOW + timedelta(seconds=2),
        ),
        updated_at=_NOW + timedelta(seconds=2),
    )

    assert completed.operation.state == "completed"
    assert completed.run is not None
    assert completed.run.status == "canceled"
    settled = await store.get_session(
        records.session.owner_partition,
        records.session.session_id,
    )
    assert settled.record.active_operation_id is None
    assert settled.record.active_run_id is None


@pytest.mark.asyncio
async def test_non_canceled_terminal_submit_rearm_completes_normally() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    reserved = await store.begin_provision_submit(records)
    assert reserved.fence is not None
    assigned_session = replace(
        records.session,
        sandbox_id="sandbox-1",
        updated_at=_NOW + timedelta(seconds=1),
    )
    fence = await store.advance_operation(
        fence=reserved.fence,
        phase="provision_lifecycle",
        updated_session=assigned_session,
        updated_target=replace(records.operation.target, sandbox_id="sandbox-1"),
        updated_at=_NOW + timedelta(seconds=1),
    )
    terminal = replace(
        records.run,
        status="succeeded",
        result_available=True,
        updated_at=_NOW + timedelta(seconds=2),
    )
    adopted = await store.adopt_terminal_run(terminal)
    assert adopted.slot_released is False
    fence = await store.advance_operation(
        fence=fence,
        phase="provision_rearm",
        updated_at=_NOW + timedelta(seconds=3),
    )

    completed = await store.complete_operation(
        fence=fence,
        updated_session=_session_after_prelaunch_cancel(
            assigned_session,
            updated_at=_NOW + timedelta(seconds=4),
        ),
        terminal_run=terminal,
        updated_at=_NOW + timedelta(seconds=4),
    )

    assert completed.operation.state == "completed"
    stored_run = await store.get_run(
        records.run.owner_partition,
        records.run.session_id,
        records.run.run_id,
    )
    assert stored_run.record.status == "succeeded"


@pytest.mark.asyncio
async def test_missing_run_submit_rearm_can_abort_and_release_its_slot() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    reserved = await store.begin_provision_submit(records)
    assert reserved.fence is not None
    assigned_session = replace(
        records.session,
        sandbox_id="sandbox-1",
        updated_at=_NOW + timedelta(seconds=1),
    )
    fence = await store.advance_operation(
        fence=reserved.fence,
        phase="provision_lifecycle",
        updated_session=assigned_session,
        updated_target=replace(records.operation.target, sandbox_id="sandbox-1"),
        updated_at=_NOW + timedelta(seconds=1),
    )
    run_key = (records.run.owner_partition.partition_key, str(records.run.row_key))
    del fake._entities[run_key]
    del fake._etags[run_key]
    fence = await store.advance_operation(
        fence=fence,
        phase="provision_rearm",
        updated_at=_NOW + timedelta(seconds=2),
    )

    aborted = await store.abort_operation(
        fence=fence,
        updated_session=_session_after_prelaunch_cancel(
            assigned_session,
            updated_at=_NOW + timedelta(seconds=3),
        ),
        error_code="provision_run_missing",
        updated_at=_NOW + timedelta(seconds=3),
    )

    assert aborted.operation.state == "aborted"
    settled = await store.get_session(
        records.session.owner_partition,
        records.session.session_id,
    )
    assert settled.record.active_operation_id is None
    assert settled.record.active_run_id is None


@pytest.mark.asyncio
async def test_terminal_adoption_does_not_release_an_active_operation_slot() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    await store.begin_provision_submit(records)

    outcome = await store.adopt_terminal_run(
        DurableRunRecord.create(
            owner_partition=records.run.owner_partition,
            session_id=records.run.session_id,
            run_id=records.run.run_id,
            generation=records.run.generation,
            status="canceled",
            result_available=False,
            status_reason="canceled_before_launch",
            expires_at=records.run.expires_at,
            created_at=records.run.created_at,
            updated_at=_NOW + timedelta(seconds=1),
        )
    )

    assert outcome.run.status == "canceled"
    assert outcome.slot_released is False


@pytest.mark.asyncio
async def test_journal_invalidation_does_not_release_an_active_operation_slot() -> None:
    fake = _FakeTableClient()
    store = AzureTableSessionStateStore(fake)  # type: ignore[arg-type]
    records = _provision_submit_records()
    await store.begin_provision_submit(records)

    outcome = await store.invalidate_journal_run(
        owner_partition=records.session.owner_partition,
        session_id=records.session.session_id,
        run_id=records.run.run_id,
        updated_at=_NOW + timedelta(seconds=1),
    )

    assert outcome.run.status == "failed"
    assert outcome.slot_released is False


def _http_error(status_code: int, error_code: str) -> HttpResponseError:
    error = HttpResponseError(message=f"{status_code} {error_code}")
    error.status_code = status_code
    error.error_code = error_code
    return error
