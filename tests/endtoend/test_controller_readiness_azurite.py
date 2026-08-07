"""Real Table-service coverage for the activation gate's post-admission check."""

from __future__ import annotations

import contextlib
import socket
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from azure_functions_agents.controller.readiness import (
    ActivatedSession,
    SessionBindingChangedError,
    revalidate_before_submit,
    session_with_admitted_run,
)
from azure_functions_agents.session_state import (
    AdmissionRecords,
    AppIdentity,
    DurableRunRecord,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    owner_partition,
)
from azure_functions_agents.session_state.store import AzureTableSessionStateStore
from tests.doubles.fake_session_runtime import FakeSandboxSessionHandle
from tests.endtoend._storage_probe import DEV_CONNECTION_STRING

_FINGERPRINT = "s1-" + ("a" * 52)


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


def _table_name() -> str:
    return f"ReadinessGate{uuid4().hex[:20]}"


def _partition():
    app = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name="agent-app",
    )
    return owner_partition(FunctionAppOwnerContext.create(app, "main"))


def _session() -> DurableSessionRecord:
    now = datetime.now(UTC)
    return DurableSessionRecord.create(
        owner_partition=_partition(),
        session_id="session-1",
        sandbox_id="sandbox-1",
        generation=1,
        digest_kind="funcs_zip",
        digest="sha256:" + ("a" * 64),
        protocol="1",
        status="ready",
        last_activity_at=now,
        expires_at=now + timedelta(hours=24),
        idle_policy_armed=True,
        active_run_id=None,
        snapshot_ids=(),
        region="westus2",
        state_store_fingerprint=_FINGERPRINT,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=now,
        updated_at=now,
        active_operation_id=None,
        operation_sequence=0,
    )


def _run(session: DurableSessionRecord) -> DurableRunRecord:
    now = datetime.now(UTC)
    return DurableRunRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        run_id="run-1",
        generation=session.generation,
        status="accepted",
        result_available=False,
        status_reason=None,
        expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_recheck_releases_the_slot_before_quarantining_a_repointed_binding() -> None:
    from azure.data.tables.aio import TableClient

    client = TableClient.from_connection_string(DEV_CONNECTION_STRING, table_name=_table_name())
    store = AzureTableSessionStateStore(client)
    await store.ensure_table()
    try:
        session = _session()
        initial_etag = await store.create_session(session)
        run = _run(session)
        admitted_session = session_with_admitted_run(
            session,
            run.run_id,
            updated_at=run.updated_at,
        )
        outcome = await store.admit_run(AdmissionRecords.create(admitted_session, run))
        assert outcome.replayed is False

        admitted = await store.get_session(session.owner_partition, session.session_id)
        repointed = DurableSessionRecord.create(
            owner_partition=admitted.record.owner_partition,
            session_id=admitted.record.session_id,
            sandbox_id="repointed-sandbox",
            generation=admitted.record.generation,
            digest_kind=admitted.record.digest_kind,
            digest=admitted.record.digest,
            protocol=admitted.record.protocol,
            status="running",
            last_activity_at=admitted.record.last_activity_at,
            expires_at=admitted.record.expires_at,
            idle_policy_armed=admitted.record.idle_policy_armed,
            active_run_id=run.run_id,
            snapshot_ids=admitted.record.snapshot_ids,
            region=admitted.record.region,
            state_store_fingerprint=admitted.record.state_store_fingerprint,
            quarantine_reason=None,
            tombstone_reason=None,
            created_at=admitted.record.created_at,
            updated_at=datetime.now(UTC),
            active_operation_id=admitted.record.active_operation_id,
            operation_sequence=admitted.record.operation_sequence,
        )
        await store.update_session(
            previous=admitted.record,
            updated=repointed,
            etag=admitted.etag,
        )
        activated = ActivatedSession.create(
            handle=FakeSandboxSessionHandle(),
            session=session,
            etag=initial_etag,
            partition=session.owner_partition,
            store=store,
        )

        with pytest.raises(SessionBindingChangedError, match="sandbox_id"):
            await revalidate_before_submit(activated, outcome.run)

        stored_session = await store.get_session(session.owner_partition, session.session_id)
        stored_run = await store.get_run(session.owner_partition, session.session_id, run.run_id)
        assert stored_run.record.status == "failed"
        assert stored_session.record.status == "quarantined"
        assert stored_session.record.active_run_id is None
    finally:
        with contextlib.suppress(Exception):
            await client.delete_table()
        await client.close()
