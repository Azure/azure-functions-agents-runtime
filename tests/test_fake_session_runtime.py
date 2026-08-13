from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from azure_functions_agents.session_state import (
    AppIdentity,
    DurableSessionOperation,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    SessionOperationTarget,
    operation_correlation_label,
    owner_partition,
)
from tests.doubles.fake_session_runtime import FakeSessionStateStore

_NOW = datetime(2026, 8, 13, tzinfo=UTC)


def _session() -> DurableSessionRecord:
    partition = owner_partition(
        FunctionAppOwnerContext.create(
            AppIdentity.create(
                subscription_id="11111111-2222-3333-4444-555555555555",
                site_name="agent-app",
            ),
            "main",
        )
    )
    return DurableSessionRecord.create(
        owner_partition=partition,
        session_id="session-1",
        sandbox_id=None,
        generation=1,
        digest_kind="funcs_zip",
        digest="sha256:" + ("a" * 64),
        protocol="1",
        status="creating",
        last_activity_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        idle_policy_armed=False,
        active_run_id=None,
        snapshot_ids=(),
        region="westus2",
        state_store_fingerprint="s1-" + ("a" * 52),
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=_NOW,
        updated_at=_NOW,
        active_operation_id=None,
        operation_sequence=0,
    )


def _operation(session: DurableSessionRecord, kind: str) -> DurableSessionOperation:
    phase = {
        "provision_submit": "provision_create",
        "submit_run": "submit_admission",
        "reclaim_backing": "reclaim_fenced",
    }[kind]
    return DurableSessionOperation.create(
        owner_partition=session.owner_partition,
        target=SessionOperationTarget.create(
            session_id=session.session_id,
            sandbox_id=None,
            generation=session.generation,
            digest_kind=session.digest_kind,
            digest=session.digest,
            run_id="run-1" if kind != "reclaim_backing" else None,
        ),
        sequence=1,
        kind=kind,  # type: ignore[arg-type]
        phase=phase,  # type: ignore[arg-type]
        state="active",
        correlation_label=operation_correlation_label(session.session_id, 1),
        token="a" * 32,
        attempt_count=0,
        error_code=None,
        lease_expires_at=_NOW + timedelta(seconds=1),
        next_attempt_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        finished_at=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "lease_seconds"),
    [("provision_submit", 120), ("submit_run", 60), ("reclaim_backing", 60)],
)
async def test_fake_operation_writes_select_lease_by_persisted_kind(
    kind: str,
    lease_seconds: int,
) -> None:
    session = _session()
    operation = _operation(session, kind)
    store = FakeSessionStateStore(session)
    updated = replace(
        session,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )

    fence = await store.begin_operation(
        previous=session,
        updated=updated,
        operation=operation,
        etag=store.etag,
    )
    assert store.durable_operations[fence.operation_id].lease_expires_at == _NOW + timedelta(
        seconds=lease_seconds
    )

    resumed_at = _NOW + timedelta(seconds=2)
    resumed = await store.resume_operation(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        token="b" * 32,
        updated_at=resumed_at,
    )
    assert resumed is not None
    assert store.durable_operations[resumed.operation_id].lease_expires_at == resumed_at + timedelta(
        seconds=lease_seconds
    )

    expired = replace(store.durable_operations[resumed.operation_id], lease_expires_at=_NOW)
    store.durable_operations[expired.operation_id] = expired
    takeover_at = _NOW + timedelta(seconds=3)
    takeover = await store.takeover_expired_operation(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        token="c" * 32,
        updated_at=takeover_at,
    )
    assert takeover is not None
    assert store.durable_operations[takeover.operation_id].lease_expires_at == takeover_at + timedelta(
        seconds=lease_seconds
    )

    advanced_at = _NOW + timedelta(seconds=4)
    advanced = await store.advance_operation(
        fence=takeover,
        phase=store.durable_operations[takeover.operation_id].phase,
        updated_at=advanced_at,
    )
    assert store.durable_operations[advanced.operation_id].lease_expires_at == advanced_at + timedelta(
        seconds=lease_seconds
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "lease_seconds"),
    [("provision_submit", 120), ("submit_run", 60)],
)
async def test_fake_journal_claim_renews_the_kind_selected_lease(
    kind: str,
    lease_seconds: int,
) -> None:
    session = _session()
    operation = _operation(session, kind)
    store = FakeSessionStateStore(session)
    updated = replace(
        session,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
        active_run_id="run-1",
    )
    await store.begin_operation(
        previous=session,
        updated=updated,
        operation=operation,
        etag=store.etag,
    )

    claimed_at = _NOW + timedelta(seconds=5)
    claimed = await store.claim_operation_journal(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        run_id="run-1",
        token="d" * 32,
        updated_at=claimed_at,
    )

    assert claimed is not None
    assert store.durable_operations[claimed.operation_id].lease_expires_at == claimed_at + timedelta(
        seconds=lease_seconds
    )
