from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from azure_functions_agents.session_state import (
    MAX_SNAPSHOT_IDS,
    ROW_SCHEMA_VERSION,
    TABLE_NAME,
    AdmissionRecords,
    AppIdentity,
    DurableIdempotencyRecord,
    DurableRunRecord,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    OwnerPartition,
    SessionStateContractError,
    decode_snapshot_ids,
    encode_snapshot_ids,
    owner_partition,
    validate_generation,
    validate_generation_transition,
)

_NOW = datetime(2026, 7, 30, 16, 0, tzinfo=UTC)
_SESSION_ID = "session-1"
_RUN_ID = "run-1"
_STATE_FINGERPRINT = "s1-" + "a" * 52


def _partition(*, site_name: str = "agent-app") -> OwnerPartition:
    app = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name=site_name,
    )
    return owner_partition(FunctionAppOwnerContext.create(app, "main"))


def _session(
    *,
    partition: OwnerPartition | None = None,
    active_run_id: str | None = _RUN_ID,
    status: str = "running",
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=partition or _partition(),
        session_id=_SESSION_ID,
        sandbox_id="sandbox-1",
        generation=1,
        digest_kind="funcs_zip",
        digest="sha256:" + ("b" * 64),
        protocol="1",
        status=status,  # type: ignore[arg-type]
        last_activity_at=_NOW,
        expires_at=_NOW + timedelta(hours=24),
        idle_policy_armed=False,
        active_run_id=active_run_id,
        snapshot_ids=("snapshot-1", "snapshot-\N{SNOWMAN}"),
        region="WestUS2",
        state_store_fingerprint=_STATE_FINGERPRINT,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _run(*, partition: OwnerPartition | None = None) -> DurableRunRecord:
    return DurableRunRecord.create(
        owner_partition=partition or _partition(),
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        generation=1,
        status="running",
        result_available=False,
        status_reason=None,
        expires_at=_NOW + timedelta(minutes=15),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _idempotency(
    *,
    partition: OwnerPartition | None = None,
) -> DurableIdempotencyRecord:
    return DurableIdempotencyRecord.create(
        owner_partition=partition or _partition(),
        session_id=_SESSION_ID,
        idempotency_hash="c" * 64,
        request_hash="d" * 64,
        run_id=_RUN_ID,
        expires_at=_NOW + timedelta(hours=1),
        created_at=_NOW,
    )


def test_durable_table_name_and_session_entity_schema_are_exact() -> None:
    record = _session()
    entity = record.to_table_entity()

    assert TABLE_NAME == "AzureFunctionsAgentsSessions"
    assert entity == {
        "PartitionKey": record.owner_partition.partition_key,
        "RowKey": "session:session-1",
        "schema_version": 1,
        "owner_hash_version": "o1",
        "app_hash": record.owner_partition.app_hash,
        "sandbox_id": "sandbox-1",
        "generation": 1,
        "digest_kind": "funcs_zip",
        "digest": "sha256:" + ("b" * 64),
        "protocol": "1",
        "status": "running",
        "last_activity_at": _NOW,
        "expires_at": _NOW + timedelta(hours=24),
        "idle_policy_armed": False,
        "active_run_id": "run-1",
        "snapshot_ids": '["snapshot-1","snapshot-\N{SNOWMAN}"]',
        "region": "westus2",
        "state_store_fingerprint": _STATE_FINGERPRINT,
        "quarantine_reason": "",
        "tombstone_reason": "",
        "reclaim_fence_token": "",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    assert DurableSessionRecord.from_table_entity(entity) == record


def test_session_rows_without_new_scope_or_fence_properties_remain_readable() -> None:
    entity = _session().to_table_entity()
    entity.pop("app_hash")
    entity.pop("reclaim_fence_token")

    assert DurableSessionRecord.from_table_entity(entity) == _session()


def test_run_and_idempotency_entities_round_trip_without_raw_key_material() -> None:
    run = _run()
    idempotency = _idempotency()

    run_entity = run.to_table_entity()
    idempotency_entity = idempotency.to_table_entity()

    assert run_entity["RowKey"] == "run:session-1:run-1"
    assert run_entity["generation"] == 1
    assert run_entity["status_reason"] == ""
    assert DurableRunRecord.from_table_entity(run_entity) == run
    assert idempotency_entity["RowKey"] == f"idem:session-1:{'c' * 64}"
    assert set(idempotency_entity) == {
        "PartitionKey",
        "RowKey",
        "schema_version",
        "owner_hash_version",
        "app_hash",
        "request_hash",
        "run_id",
        "expires_at",
        "created_at",
    }
    assert DurableIdempotencyRecord.from_table_entity(idempotency_entity) == idempotency


def test_admission_rows_share_one_partition_and_locator_contract() -> None:
    records = AdmissionRecords.create(_session(), _run(), _idempotency())
    partition_keys = {
        records.session.to_table_entity()["PartitionKey"],
        records.run.to_table_entity()["PartitionKey"],
        records.idempotency.to_table_entity()["PartitionKey"],
    }

    assert partition_keys == {records.session.owner_partition.partition_key}


def test_admission_rejects_cross_partition_or_inconsistent_rows() -> None:
    with pytest.raises(SessionStateContractError, match="share one owner partition"):
        AdmissionRecords.create(_session(), _run(partition=_partition(site_name="other-app")))
    with pytest.raises(SessionStateContractError, match="active_run_id"):
        # Running sessions cannot be constructed without active_run_id; admission also
        # rejects a mismatched pointer when the session points at a different run.
        AdmissionRecords.create(_session(active_run_id="other-run"), _run())
    with pytest.raises(SessionStateContractError, match="share session_id"):
        AdmissionRecords.create(_session(), replace(_run(), session_id="other"))
    with pytest.raises(SessionStateContractError, match="share generation"):
        AdmissionRecords.create(_session(), replace(_run(), generation=2))
    with pytest.raises(SessionStateContractError, match="idempotency admission row"):
        AdmissionRecords.create(
            _session(),
            _run(),
            _idempotency(partition=_partition(site_name="other-app")),
        )
    with pytest.raises(SessionStateContractError, match="share session_id"):
        AdmissionRecords.create(
            _session(),
            _run(),
            replace(_idempotency(), session_id="other"),
        )
    with pytest.raises(SessionStateContractError, match="identify the admitted run"):
        AdmissionRecords.create(
            _session(),
            _run(),
            replace(_idempotency(), run_id="other"),
        )


def test_snapshot_ids_use_deterministic_bounded_json() -> None:
    encoded = encode_snapshot_ids(("snapshot-1", "caf\u00e9"))

    assert encoded == '["snapshot-1","caf\u00e9"]'
    assert decode_snapshot_ids(encoded) == ("snapshot-1", "caf\u00e9")
    with pytest.raises(SessionStateContractError, match="canonically"):
        decode_snapshot_ids('[ "snapshot-1" ]')
    with pytest.raises(SessionStateContractError, match="item limit"):
        encode_snapshot_ids(tuple(f"snapshot-{index}" for index in range(MAX_SNAPSHOT_IDS + 1)))
    with pytest.raises(SessionStateContractError, match="serialized UTF-8 byte limit"):
        encode_snapshot_ids(tuple(("x" * 200) + str(index) for index in range(64)))
    with pytest.raises(SessionStateContractError, match="snapshot_id"):
        encode_snapshot_ids(("x" * 257,))
    with pytest.raises(SessionStateContractError, match="not valid JSON"):
        decode_snapshot_ids("nope")
    with pytest.raises(SessionStateContractError, match="array of strings"):
        decode_snapshot_ids('{"snapshot":"one"}')
    with pytest.raises(SessionStateContractError, match="array of strings"):
        decode_snapshot_ids("[1,2]")


@pytest.mark.parametrize("generation", [0, -1, True, 1.5])
def test_generation_must_start_at_one(generation: object) -> None:
    with pytest.raises(SessionStateContractError, match="integer >= 1"):
        validate_generation(generation)  # type: ignore[arg-type]


def test_generation_is_preserved_normally_and_strictly_increases_only_for_rebind() -> None:
    validate_generation_transition(1, 1, backing_rebind=False)
    validate_generation_transition(1, 2, backing_rebind=True)

    with pytest.raises(SessionStateContractError, match="preserve"):
        validate_generation_transition(1, 2, backing_rebind=False)
    with pytest.raises(SessionStateContractError, match="strictly increase"):
        validate_generation_transition(2, 2, backing_rebind=True)
    with pytest.raises(SessionStateContractError, match="strictly increase"):
        validate_generation_transition(2, 1, backing_rebind=True)


def test_reason_fields_are_bounded_codes_and_required_for_terminal_session_states() -> None:
    with pytest.raises(SessionStateContractError, match="tombstone_reason"):
        _session(status="tombstoned", active_run_id=None)
    with pytest.raises(SessionStateContractError, match="quarantine_reason"):
        _session(status="quarantined", active_run_id=None)
    with pytest.raises(SessionStateContractError, match="reason code"):
        DurableSessionRecord.create(
            owner_partition=_partition(),
            session_id=_SESSION_ID,
            sandbox_id="sandbox-1",
            generation=1,
            digest_kind="funcs_zip",
            digest="sha256:" + ("b" * 64),
            protocol="1",
            status="tombstoned",
            last_activity_at=_NOW,
            expires_at=_NOW + timedelta(hours=24),
            idle_policy_armed=False,
            active_run_id=None,
            snapshot_ids=(),
            region="westus2",
            state_store_fingerprint=_STATE_FINGERPRINT,
            quarantine_reason=None,
            tombstone_reason="raw user claim: secret",
            created_at=_NOW,
            updated_at=_NOW,
        )


def test_session_status_enforces_active_run_id_lifecycle_invariants() -> None:
    with pytest.raises(SessionStateContractError, match="running sessions require active_run_id"):
        _session(status="running", active_run_id=None)
    with pytest.raises(SessionStateContractError, match="canceling sessions require active_run_id"):
        _session(status="canceling", active_run_id=None)
    with pytest.raises(
        SessionStateContractError,
        match="ready sessions require active_run_id to be unset",
    ):
        _session(status="ready", active_run_id=_RUN_ID)
    with pytest.raises(
        SessionStateContractError,
        match="deleted sessions require active_run_id to be unset",
    ):
        _session(status="deleted", active_run_id=_RUN_ID)

    ready = _session(status="ready", active_run_id=None)
    canceling = _session(status="canceling", active_run_id=_RUN_ID)
    assert ready.active_run_id is None
    assert canceling.active_run_id == _RUN_ID


def test_result_availability_is_consistent_with_run_status() -> None:
    with pytest.raises(SessionStateContractError, match="succeeded"):
        DurableRunRecord.create(
            owner_partition=_partition(),
            session_id=_SESSION_ID,
            run_id=_RUN_ID,
            generation=1,
            status="running",
            result_available=True,
            status_reason=None,
            expires_at=_NOW + timedelta(minutes=15),
            created_at=_NOW,
            updated_at=_NOW,
        )

    succeeded = DurableRunRecord.create(
        owner_partition=_partition(),
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        generation=1,
        status="succeeded",
        result_available=True,
        status_reason=None,
        expires_at=_NOW + timedelta(minutes=15),
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert succeeded.result_available is True


def test_row_boolean_fields_are_strict_at_construction() -> None:
    with pytest.raises(SessionStateContractError, match="idle_policy_armed"):
        DurableSessionRecord.create(
            owner_partition=_partition(),
            session_id=_SESSION_ID,
            sandbox_id="sandbox-1",
            generation=1,
            digest_kind="funcs_zip",
            digest="sha256:" + ("b" * 64),
            protocol="1",
            status="running",
            last_activity_at=_NOW,
            expires_at=_NOW + timedelta(hours=24),
            idle_policy_armed=1,  # type: ignore[arg-type]
            active_run_id=_RUN_ID,
            snapshot_ids=("snapshot-1",),
            region="westus2",
            state_store_fingerprint=_STATE_FINGERPRINT,
            quarantine_reason=None,
            tombstone_reason=None,
            created_at=_NOW,
            updated_at=_NOW,
        )
    with pytest.raises(SessionStateContractError, match="result_available"):
        DurableRunRecord.create(
            owner_partition=_partition(),
            session_id=_SESSION_ID,
            run_id=_RUN_ID,
            generation=1,
            status="running",
            result_available=1,  # type: ignore[arg-type]
            status_reason=None,
            expires_at=_NOW + timedelta(minutes=15),
            created_at=_NOW,
            updated_at=_NOW,
        )


def test_row_schema_deserialization_fails_closed_on_version_or_partition_drift() -> None:
    entity = _session().to_table_entity()
    entity["schema_version"] = ROW_SCHEMA_VERSION + 1
    with pytest.raises(SessionStateContractError, match="schema_version"):
        DurableSessionRecord.from_table_entity(entity)

    entity = _session().to_table_entity()
    entity["owner_hash_version"] = "o2"
    with pytest.raises(SessionStateContractError, match="owner_hash_version"):
        DurableSessionRecord.from_table_entity(entity)


def test_rows_are_immutable_and_state_fingerprint_cannot_hold_credentials() -> None:
    record = _session()
    with pytest.raises(FrozenInstanceError):
        record.generation = 2  # type: ignore[misc]
    with pytest.raises(SessionStateContractError, match="state_store_fingerprint"):
        DurableSessionRecord.create(
            owner_partition=_partition(),
            session_id=_SESSION_ID,
            sandbox_id="sandbox-1",
            generation=1,
            digest_kind="funcs_zip",
            digest="sha256:" + ("b" * 64),
            protocol="1",
            status="running",
            last_activity_at=_NOW,
            expires_at=_NOW + timedelta(hours=24),
            idle_policy_armed=False,
            active_run_id=_RUN_ID,
            snapshot_ids=(),
            region="westus2",
            state_store_fingerprint=(
                "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=secret"
            ),
            quarantine_reason=None,
            tombstone_reason=None,
            created_at=_NOW,
            updated_at=_NOW,
        )


def test_row_timestamps_require_awareness_and_normalize_to_utc() -> None:
    with pytest.raises(SessionStateContractError, match="timezone-aware"):
        DurableSessionRecord.create(
            owner_partition=_partition(),
            session_id=_SESSION_ID,
            sandbox_id="sandbox-1",
            generation=1,
            digest_kind="funcs_zip",
            digest="sha256:" + ("b" * 64),
            protocol="1",
            status="running",
            last_activity_at=_NOW,
            expires_at=_NOW + timedelta(hours=24),
            idle_policy_armed=False,
            active_run_id=_RUN_ID,
            snapshot_ids=(),
            region="westus2",
            state_store_fingerprint=_STATE_FINGERPRINT,
            quarantine_reason=None,
            tombstone_reason=None,
            created_at=datetime(2026, 7, 30, 16, 0),
            updated_at=_NOW,
        )

    plus_five = timezone(timedelta(hours=5))
    local_time = datetime(2026, 7, 30, 21, 0, tzinfo=plus_five)
    record = DurableSessionRecord.create(
        owner_partition=_partition(),
        session_id=_SESSION_ID,
        sandbox_id="sandbox-1",
        generation=1,
        digest_kind="funcs_zip",
        digest="sha256:" + ("b" * 64),
        protocol="1",
        status="running",
        last_activity_at=local_time,
        expires_at=local_time + timedelta(hours=24),
        idle_policy_armed=False,
        active_run_id=_RUN_ID,
        snapshot_ids=(),
        region="westus2",
        state_store_fingerprint=_STATE_FINGERPRINT,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=local_time,
        updated_at=local_time,
    )

    assert record.created_at == _NOW
    assert record.created_at.tzinfo is UTC
    assert record.to_table_entity()["last_activity_at"] == _NOW
