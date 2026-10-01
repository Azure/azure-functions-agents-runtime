from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from azure_functions_agents.experimental.durable_loop_activities import (
    InMemoryDurableContentStore,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    DURABLE_LOOP_ORCHESTRATOR_V4_NAME,
    DurableLoopBudgetV1,
    DurableLoopBudgetV2,
    DurableLoopRunStatus,
    DurablePublicLinksV1,
    DurablePublicStatusV1,
    DurableRetentionPolicyV1,
    DurableRunIdentityV1,
    DurableRunIdentityV2,
)
from azure_functions_agents.experimental.durable_loop_receipts import (
    InMemoryDurableKeyedDocumentStore,
)
from azure_functions_agents.experimental.durable_retention import (
    DurableAdmissionReceiptState,
    DurableRetentionExpiredError,
    DurableRetentionLegacyExcludedError,
    DurableRetentionManager,
    DurableRunVisibility,
    DurableSessionExpiredError,
    retained_identifier_hash,
)

_HASH = "a" * 64
_NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


def _budget_v1() -> DurableLoopBudgetV1:
    return DurableLoopBudgetV1(
        max_model_steps=8,
        max_tool_calls=16,
        max_elapsed_seconds=4 * 60 * 60,
        max_human_waits=2,
        human_wait_seconds=60 * 60,
        max_argument_bytes=64 * 1024,
        max_result_bytes=1024 * 1024,
        context_max_bytes=1024 * 1024,
        context_compaction_percent=75,
        max_parallel_reads=4,
        continue_as_new_checkpoints=10,
    )


def _budget_v2() -> DurableLoopBudgetV2:
    return DurableLoopBudgetV2.model_validate(
        {
            **_budget_v1().model_dump(),
            "schema_version": "2",
        }
    )


def _policy() -> DurableRetentionPolicyV1:
    return DurableRetentionPolicyV1(
        event_result_seconds=60 * 60,
        receipt_seconds=2 * 60 * 60,
        skill_grace_seconds=24 * 60 * 60,
        human_content_seconds=30 * 60,
        session_seconds=24 * 60 * 60,
        tombstone_seconds=3 * 60 * 60,
        trigger_admission_deadline_seconds=5 * 60,
    )


def _identity(
    run_id: str,
    *,
    session_id: str = "session-1",
    request_id_hash: str = _HASH,
) -> DurableRunIdentityV2:
    suffix = hashlib.sha256(run_id.encode()).hexdigest()
    return DurableRunIdentityV2(
        run_id=run_id,
        session_id=session_id,
        request_id_hash=request_id_hash,
        request_hash=suffix,
        owner_hash="b" * 64,
        agent_slug="main",
        agent_hash="c" * 64,
        catalog_hash="d" * 64,
        deployment_hash="e" * 64,
        tool_package_hash="f" * 64,
        policy_hash="1" * 64,
        orchestration_version=DURABLE_LOOP_ORCHESTRATOR_V4_NAME,
        created_at=_NOW,
        active_deadline=_NOW + timedelta(hours=4),
        absolute_deadline=_NOW + timedelta(days=7),
        budget=_budget_v2(),
        skill_catalog_hash="2" * 64,
        access_namespace_hash="3" * 64,
        retention_policy=_policy(),
    )


def _legacy_identity() -> DurableRunIdentityV1:
    return DurableRunIdentityV1(
        run_id="legacy-run",
        session_id="legacy-session",
        request_id_hash=_HASH,
        request_hash="b" * 64,
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash="e" * 64,
        deployment_hash="f" * 64,
        tool_package_hash="1" * 64,
        policy_hash="2" * 64,
        orchestration_version="durable_agent_turn_orchestrator_v1",
        created_at=_NOW,
        active_deadline=_NOW + timedelta(hours=4),
        absolute_deadline=_NOW + timedelta(days=7),
        budget=_budget_v1(),
    )


def _terminal_projection(
    run_id: str,
    *,
    terminal_at: datetime,
) -> DurablePublicStatusV1:
    return DurablePublicStatusV1(
        run_id=run_id,
        session_id="session-1",
        status=DurableLoopRunStatus.COMPLETED,
        phase="completed",
        created_at=_NOW,
        updated_at=terminal_at,
        completed_model_steps=1,
        completed_tool_calls=0,
        human_waits=0,
        result_available=True,
        links=DurablePublicLinksV1(
            status=f"/durable/runs/{run_id}",
            events=f"/durable/runs/{run_id}/events",
            result=f"/durable/runs/{run_id}/result",
            cancel=f"/durable/runs/{run_id}/cancel",
        ),
    )


def _manager(
    *,
    pending_safety_seconds: int = 60,
) -> tuple[
    DurableRetentionManager,
    InMemoryDurableKeyedDocumentStore,
    InMemoryDurableContentStore,
]:
    documents = InMemoryDurableKeyedDocumentStore()
    content = InMemoryDurableContentStore()
    return (
        DurableRetentionManager(
            documents,
            content,
            pending_safety_seconds=pending_safety_seconds,
        ),
        documents,
        content,
    )


@pytest.mark.asyncio
async def test_pending_reference_fences_delete_until_safety_window_expires() -> None:
    manager, _, content = _manager()
    payload = b"pending"
    reference = await content.put_bytes(
        kind="result",
        payload=payload,
        media_type="text/plain",
        retention_class="result",
    )
    run_hash = retained_identifier_hash("run", "run-pending")
    await manager.acquire_pending_reference(
        object_kind="result",
        object_digest=reference.sha256,
        run_id_hash=run_hash,
        retention_class="result",
        pending_until=_NOW + timedelta(seconds=60),
        now=_NOW,
    )

    released, deleted = await manager.cleanup_object_reference(
        reference,
        run_id_hash=run_hash,
        retention_class="result",
        now=_NOW + timedelta(seconds=30),
    )
    assert (released, deleted) == (False, False)
    assert await content.get_bytes(reference) == payload

    released, deleted = await manager.cleanup_object_reference(
        reference,
        run_id_hash=run_hash,
        retention_class="result",
        now=_NOW + timedelta(seconds=61),
    )
    assert (released, deleted) == (True, True)
    assert content.object_count == 0


@pytest.mark.asyncio
async def test_shared_object_survives_until_every_run_reference_expires() -> None:
    manager, _, content = _manager()
    await manager.admit_run_resource(_identity("run-1"))
    await manager.admit_run_resource(
        _identity("run-2", request_id_hash="4" * 64)
    )
    payload = b"shared"
    first = await manager.tracked_put(
        run_id="run-1",
        kind="result",
        payload=payload,
        media_type="text/plain",
        retention_class="result",
        expires_at=_NOW + timedelta(hours=1),
        now=_NOW,
    )
    second = await manager.tracked_put(
        run_id="run-2",
        kind="result",
        payload=payload,
        media_type="text/plain",
        retention_class="result",
        expires_at=_NOW + timedelta(hours=2),
        now=_NOW,
    )
    assert first.object_id == second.object_id
    assert content.object_count == 1

    first_cleanup = await manager.cleanup_run(
        "run-1",
        now=_NOW + timedelta(hours=1, seconds=1),
    )
    assert first_cleanup.released_references == 1
    assert first_cleanup.deleted_objects == 0
    assert await content.get_bytes(second) == payload

    second_cleanup = await manager.cleanup_run(
        "run-2",
        now=_NOW + timedelta(hours=2, seconds=1),
    )
    assert second_cleanup.released_references == 1
    assert second_cleanup.deleted_objects == 1
    assert content.object_count == 0


@pytest.mark.asyncio
async def test_tracked_put_retry_reuses_one_bounded_artifact_page() -> None:
    manager, _, _ = _manager()
    await manager.admit_run_resource(_identity("run-retry"))
    expires_at = _NOW + timedelta(hours=1)

    first = await manager.tracked_put(
        run_id="run-retry",
        kind="result",
        payload=b"stable",
        media_type="text/plain",
        retention_class="result",
        expires_at=expires_at,
        now=_NOW,
    )
    replay = await manager.tracked_put(
        run_id="run-retry",
        kind="result",
        payload=b"stable",
        media_type="text/plain",
        retention_class="result",
        expires_at=expires_at,
        now=_NOW + timedelta(seconds=1),
    )
    header, _ = (await manager.get_run_resource("run-retry")) or pytest.fail()

    assert replay == first
    assert header.artifact_page_count == 1
    records = await manager.read_cleanup_bucket(expires_at)
    assert any(record.content_ref == first for record in records)


@pytest.mark.asyncio
async def test_cleanup_deletes_only_the_indexed_keyed_document() -> None:
    manager, documents, _ = _manager()
    await manager.admit_run_resource(_identity("run-keyed"))
    assert await documents.create("test/exact", b"exact")
    assert await documents.create("test/neighbor", b"neighbor")
    await manager.append_keyed_artifact(
        run_id="run-keyed",
        kind="tool-receipt",
        retention_class="receipt",
        document_key="test/exact",
        expires_at=_NOW + timedelta(hours=1),
        now=_NOW,
    )

    await manager.cleanup_run(
        "run-keyed",
        now=_NOW + timedelta(hours=1, seconds=1),
    )

    assert await documents.get("test/exact") is None
    assert await documents.get("test/neighbor") is not None


@pytest.mark.asyncio
async def test_time_bucket_cleanup_processes_only_due_exact_object() -> None:
    manager, _, content = _manager()
    await manager.admit_run_resource(_identity("run-bucket"))
    expires_at = _NOW + timedelta(hours=1)
    await manager.tracked_put(
        run_id="run-bucket",
        kind="result",
        payload=b"bucketed",
        media_type="text/plain",
        retention_class="result",
        expires_at=expires_at,
        now=_NOW,
    )

    outcome = await manager.cleanup_bucket(
        expires_at,
        now=expires_at + timedelta(seconds=1),
    )

    assert outcome.released_references == 1
    assert outcome.deleted_objects == 1
    assert content.object_count == 0


@pytest.mark.asyncio
async def test_cleanup_cursor_catches_up_old_exact_buckets_without_scanning() -> None:
    manager, _, _ = _manager()
    first = await manager.due_cleanup_buckets(now=_NOW, limit=3)
    assert first == (
        _NOW - timedelta(hours=24),
        _NOW - timedelta(hours=23),
        _NOW - timedelta(hours=22),
    )
    for bucket in first:
        await manager.advance_cleanup_cursor(bucket, now=_NOW)

    after_outage = _NOW + timedelta(hours=100)
    resumed = await manager.due_cleanup_buckets(now=after_outage, limit=2)
    assert resumed == (
        _NOW - timedelta(hours=21),
        _NOW - timedelta(hours=20),
    )
    with pytest.raises(
        DurableRetentionConflictError,
        match="cannot skip",
    ):
        await manager.advance_cleanup_cursor(resumed[1], now=after_outage)


@pytest.mark.asyncio
async def test_session_expiry_waits_for_active_run_and_leaves_tombstone() -> None:
    manager, _, content = _manager()
    session_hash = retained_identifier_hash("session", "session-1")
    run_hash = retained_identifier_hash("run", "run-1")
    resource = await manager.put_session_context(
        session_id_hash=session_hash,
        owner_hash="b" * 64,
        access_namespace_hash="3" * 64,
        active_run_id_hash=run_hash,
        committed_generation=1,
        payload=b"context",
        media_type="application/json",
        committed_at=_NOW,
        retention_seconds=24 * 60 * 60,
        tombstone_seconds=3 * 60 * 60,
    )
    acquired = await manager.acquire_session_admission(
        session_id_hash=session_hash,
        owner_hash=resource.owner_hash,
        access_namespace_hash=resource.access_namespace_hash,
        run_id_hash=run_hash,
        now=_NOW + timedelta(hours=1),
    )
    assert acquired is not None
    assert acquired.active_run_id_hash == run_hash

    expired = await manager.expire_session(
        session_hash,
        now=_NOW + timedelta(days=1, seconds=1),
        tombstone_seconds=3 * 60 * 60,
    )
    assert expired is False
    await manager.release_session_admission(
        session_id_hash=session_hash,
        run_id_hash=run_hash,
    )
    assert await manager.expire_session(
        session_hash,
        now=_NOW + timedelta(days=1, seconds=1),
        tombstone_seconds=3 * 60 * 60,
    )
    assert content.object_count == 0
    with pytest.raises(DurableSessionExpiredError, match="session_expired"):
        await manager.acquire_session_admission(
            session_id_hash=session_hash,
            owner_hash=resource.owner_hash,
            access_namespace_hash=resource.access_namespace_hash,
            run_id_hash="5" * 64,
            now=_NOW + timedelta(days=1, minutes=1),
        )


@pytest.mark.asyncio
async def test_terminal_projection_and_admission_receipt_outlive_public_result() -> None:
    manager, _, content = _manager()
    identity = _identity("run-terminal")
    _, admission = await manager.admit_run_resource(identity)
    terminal_at = _NOW + timedelta(minutes=5)
    header = await manager.terminalize_run(
        run_id=identity.run_id,
        status=DurableLoopRunStatus.COMPLETED,
        terminal_at=terminal_at,
        projection=_terminal_projection(
            identity.run_id,
            terminal_at=terminal_at,
        ),
    )
    replayed_header = await manager.terminalize_run(
        run_id=identity.run_id,
        status=DurableLoopRunStatus.COMPLETED,
        terminal_at=terminal_at,
        projection=_terminal_projection(
            identity.run_id,
            terminal_at=terminal_at,
        ),
    )

    projection = await manager.read_terminal_projection(
        identity.run_id,
        now=terminal_at + timedelta(minutes=30),
    )
    assert replayed_header == header
    assert projection.resource_expiry == header.expiry
    assert await manager.run_visibility(
        identity.run_id,
        now=terminal_at + timedelta(minutes=30),
    ) is DurableRunVisibility.TERMINAL
    receipt = await manager.read_admission_receipt(
        owner_hash=identity.owner_hash,
        request_id_hash=identity.request_id_hash,
    )
    assert admission.replayed is False
    assert receipt is not None
    assert receipt[0].state is DurableAdmissionReceiptState.TERMINAL
    assert receipt[0].terminal_status is DurableLoopRunStatus.COMPLETED

    expired_at = terminal_at + timedelta(hours=1, seconds=1)
    with pytest.raises(DurableRetentionExpiredError, match="result_expired"):
        await manager.read_terminal_projection(identity.run_id, now=expired_at)
    assert await manager.run_visibility(
        identity.run_id,
        now=expired_at,
    ) is DurableRunVisibility.EXPIRED
    cleanup = await manager.cleanup_run(identity.run_id, now=expired_at)
    assert cleanup.deleted_objects == 1
    assert content.object_count == 0
    assert await manager.read_admission_receipt(
        owner_hash=identity.owner_hash,
        request_id_hash=identity.request_id_hash,
    ) is not None


@pytest.mark.asyncio
async def test_access_record_and_purge_gate_survive_instance_history() -> None:
    manager, _, _ = _manager()
    identity = _identity("run-access")
    await manager.admit_run_resource(identity)
    active = await manager.get_run_access_record(identity.run_id, now=_NOW)
    assert active is not None
    assert active.header is not None
    assert active.owner_hash == identity.owner_hash
    assert active.access_namespace_hash == identity.access_namespace_hash
    assert active.visibility is DurableRunVisibility.ACTIVE

    terminal_at = _NOW + timedelta(minutes=5)
    header = await manager.terminalize_run(
        run_id=identity.run_id,
        status=DurableLoopRunStatus.COMPLETED,
        terminal_at=terminal_at,
        projection=_terminal_projection(
            identity.run_id,
            terminal_at=terminal_at,
        ),
    )
    assert header.expiry is not None
    expired = await manager.get_run_access_record(
        identity.run_id,
        now=header.expiry.result_expires_at + timedelta(seconds=1),
    )
    assert expired is not None
    assert expired.owner_hash == identity.owner_hash
    assert expired.access_namespace_hash == identity.access_namespace_hash
    assert expired.visibility is DurableRunVisibility.EXPIRED

    await manager.cleanup_run(
        identity.run_id,
        now=header.expiry.tombstone_expires_at,
    )
    assert (
        await manager.get_run_access_record(
            identity.run_id,
            now=header.expiry.tombstone_expires_at,
        )
        is None
    )
    assert await manager.get_run_resource(identity.run_id) is not None

    purge_at = max(
        header.expiry.receipts_expire_at,
        header.expiry.skills_expire_at,
        header.expiry.tombstone_expires_at,
    )
    assert (
        await manager.purgeable_run_ids(
            purge_at,
            now=purge_at - timedelta(seconds=1),
        )
        == ()
    )
    assert await manager.purgeable_run_ids(
        purge_at,
        now=purge_at,
    ) == (identity.run_id,)


@pytest.mark.asyncio
async def test_legacy_v1_is_never_inferred_or_deleted() -> None:
    manager, _, content = _manager()
    reference = await content.put_bytes(
        kind="legacy",
        payload=b"legacy",
        media_type="application/octet-stream",
        retention_class="run",
    )

    with pytest.raises(DurableRetentionLegacyExcludedError, match="legacy"):
        await manager.admit_run_resource(_legacy_identity())
    cleanup = await manager.cleanup_run(
        "legacy-run",
        now=_NOW + timedelta(days=365),
    )
    assert cleanup.legacy_excluded is True
    assert await content.get_bytes(reference) == b"legacy"
