"""Fail-closed handling for untrusted sandbox run journals."""

from __future__ import annotations

from datetime import UTC, datetime

from .._logger import logger
from .._observability import current_span
from ..execution.backend import RunError, RunStatus
from ..session_state import (
    ConcurrencyConflictError,
    DurableRunRecord,
    DurableSessionRecord,
    OwnerPartition,
    SessionStateStore,
)

JOURNAL_CORRUPT_ERROR_CODE = "journal_corrupt"
_JOURNAL_CORRUPT_MESSAGE = "Run journal is invalid."
_MAX_CORRUPTION_RETRIES = 3


async def handle_journal_corruption(
    store: SessionStateStore,
    owner_partition: OwnerPartition,
    session_id: str,
    run_id: str,
    *,
    updated_at: datetime | None = None,
) -> DurableRunRecord:
    """Terminalize the affected run before quarantining its matching session."""
    now = updated_at or datetime.now(UTC)
    for _ in range(_MAX_CORRUPTION_RETRIES):
        try:
            await store.invalidate_journal_run(
                owner_partition=owner_partition,
                session_id=session_id,
                run_id=run_id,
                updated_at=now,
            )

            current_session = await store.get_session(owner_partition, session_id)
            if (
                current_session.record.status not in {"tombstoned", "deleting", "deleted"}
                and current_session.record.active_run_id in {None, run_id}
                and (
                    current_session.record.status != "quarantined"
                    or current_session.record.quarantine_reason
                    != JOURNAL_CORRUPT_ERROR_CODE
                )
            ):
                await store.update_session(
                    previous=current_session.record,
                    updated=_journal_corrupt_session(
                        current_session.record,
                        max(now, current_session.record.updated_at),
                    ),
                    etag=current_session.etag,
                )
                _record_journal_corruption_event()
            return (
                await store.get_run(owner_partition, session_id, run_id)
            ).record
        except ConcurrencyConflictError:
            continue
    return (await store.get_run(owner_partition, session_id, run_id)).record


def journal_corruption_status(
    run: DurableRunRecord,
    *,
    last_sequence: int = 0,
) -> RunStatus:
    """Render a redacted terminal state after a journal integrity violation."""
    return RunStatus(
        run_id=run.run_id,
        session_id=run.session_id,
        state="failed",
        last_sequence=last_sequence,
        result_available=False,
        error=journal_corruption_error(),
    )


def journal_corruption_error() -> RunError:
    """Return the stable redacted error shared by all journal-integrity paths."""
    return RunError(
        code=JOURNAL_CORRUPT_ERROR_CODE,
        message=_JOURNAL_CORRUPT_MESSAGE,
        fault_domain="sandbox",
    )


def _journal_corrupt_session(
    session: DurableSessionRecord,
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
        status="quarantined",
        last_activity_at=session.last_activity_at,
        expires_at=session.expires_at,
        idle_policy_armed=session.idle_policy_armed,
        active_run_id=None,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=JOURNAL_CORRUPT_ERROR_CODE,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


def _record_journal_corruption_event() -> None:
    logger.warning("Sandbox run journal rejected: reason=%s", JOURNAL_CORRUPT_ERROR_CODE)
    current_span().add_event(
        "af.session.journal_rejected",
        {"af.session.reason": JOURNAL_CORRUPT_ERROR_CODE},
    )
