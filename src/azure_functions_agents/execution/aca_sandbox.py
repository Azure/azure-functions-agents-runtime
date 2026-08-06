"""ACA Sandbox implementation of the four-method execution lifecycle seam."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .._logger import logger
from ..config import DEFAULT_TIMEOUT
from ..controller.idempotency import (
    IdempotencyAttempt,
    IdempotencyResultUnavailableError,
    build_idempotency_attempt,
)
from ..controller.readiness import (
    ActivatedSession,
    SessionActivationError,
    SessionActivationGoneError,
    SessionRuntimeBinding,
    _within_setup_budget,
    activate_session,
    disarm_idle_lifecycle,
    rearm_idle_lifecycle,
    restore_idle_lifecycle_if_unowned,
    revalidate_before_submit,
    session_with_admitted_run,
    terminal_run,
)
from ..session_state import (
    TERMINAL_RUN_STATUSES,
    ActiveRunConflictError,
    AdmissionOutcome,
    AdmissionRecords,
    DurableIdempotencyRecord,
    DurableOwnerIdempotencyRecord,
    DurableRunRecord,
    DurableSessionRecord,
    IdempotencyConflictError,
    NewSessionAdmissionRecords,
    OwnerContext,
    OwnerPartition,
    mint_run_id,
    mint_session_id,
    owner_partition,
)
from .backend import (
    SESSION_TOMBSTONED_ERROR_CODE,
    AgentExecutionBackend,
    RunContext,
    RunError,
    RunEvent,
    RunHandle,
    RunState,
    RunStatus,
    StartRunRequest,
)
from .binding import AgentBinding
from .run_control import (
    RunEnvelope,
    RunSubmissionDefinitiveFailureError,
    SandboxRunControl,
)
from .setup_budget import SetupBudget


class AcaSandboxExecutionBackend:
    """Route each lifecycle call through a durable session and live sandbox journal."""

    def __init__(
        self,
        binding: AgentBinding,
        *,
        runtime: SessionRuntimeBinding,
        owner: OwnerContext,
        run_control: SandboxRunControl | None = None,
        setup_budget: SetupBudget | None = None,
    ) -> None:
        agent_name = binding.agent_name
        if not agent_name:
            raise ValueError("ACA Sandbox execution requires an agent identity slug")
        self._binding = binding
        self._agent_name = agent_name
        self._runtime = runtime
        self._owner = owner
        self._run_control = run_control or SandboxRunControl()
        self._setup_budget = setup_budget

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        """Activate the session, atomically admit one run, and submit its envelope."""
        partition = owner_partition(self._owner)
        if request.session_id is not None:
            await self._runtime.reconcile_session(partition, request.session_id)
        try:
            return await self._start_run_once(request, partition)
        except ActiveRunConflictError:
            if request.session_id is None:
                raise
            await self._runtime.reconcile_session(partition, request.session_id)
            return await self._start_run_once(request, partition)

    async def _start_run_once(
        self,
        request: StartRunRequest,
        partition: OwnerPartition,
    ) -> RunHandle:
        is_new_session = request.session_id is None
        session_id = request.session_id or mint_session_id()
        run_id = mint_run_id()
        setup_budget = self._setup_budget or SetupBudget.start()
        attempt = build_idempotency_attempt(
            agent_slug=self._agent_name,
            prompt=request.prompt,
            timeout=request.timeout,
            idempotency_key=request.idempotency_key,
        )
        if is_new_session and attempt is not None:
            replay = await _preflight_new_session_replay(
                self._runtime,
                partition,
                attempt,
                setup_budget,
            )
            if replay is not None:
                return _run_handle(replay)
        async with self._runtime.hold_session(
            partition,
            session_id,
            setup_deadline=setup_budget,
        ):
            activated = await activate_session(
                self._runtime,
                self._owner,
                session_id,
                setup_budget,
                allow_create=request.session_id is None,
            )
            try:
                prepared = await _within_setup_budget(
                    disarm_idle_lifecycle(self._runtime, activated),
                    setup_budget,
                )
                run = _new_run(prepared.session, run_id, timeout=request.timeout)
                admitted_session = session_with_admitted_run(
                    prepared.session,
                    run_id,
                    updated_at=run.updated_at,
                )
                try:
                    outcome = await _admit_run(
                        prepared,
                        run,
                        admitted_session,
                        attempt=attempt,
                        is_new_session=is_new_session,
                        setup_budget=setup_budget,
                    )
                except Exception:
                    await _restore_after_unadmitted(self._runtime, prepared)
                    raise
                if outcome.replayed:
                    if is_new_session:
                        await _retire_losing_new_session_candidate(prepared, self._runtime)
                    await _restore_after_unadmitted(self._runtime, prepared)
                    if outcome.run.status == "succeeded" and not outcome.run.result_available:
                        raise IdempotencyResultUnavailableError(
                            "The idempotent run completed but its result is no longer available."
                        )
                    return _run_handle(outcome.run)

                try:
                    await _within_setup_budget(
                        revalidate_before_submit(prepared, outcome.run),
                        setup_budget,
                    )
                except Exception:
                    await _restore_after_unadmitted(self._runtime, prepared)
                    raise
                try:
                    status = await self._run_control.submit(
                        prepared.handle,
                        run_id,
                        RunEnvelope.create(
                            run_id=run_id,
                            session_id=session_id,
                            agent_name=self._agent_name,
                            prompt=request.prompt,
                            timeout=request.timeout,
                        ),
                        timeout_seconds=setup_budget.remaining_setup_seconds(),
                    )
                except RunSubmissionDefinitiveFailureError:
                    await _adopt_failed_submission(self._runtime, prepared, outcome.run)
                    raise
                await _adopt_if_terminal(
                    self._runtime,
                    prepared,
                    outcome.run,
                    _validate_terminal_output(self._binding, status),
                )
                return RunHandle(
                    run_id=outcome.run.run_id,
                    session_id=outcome.run.session_id,
                    state="accepted",
                    created_at=outcome.run.created_at,
                )
            finally:
                await activated.handle.close()

    async def get_run(self, context: RunContext) -> RunStatus:
        """Prefer a reachable live journal and retain the Table row as fallback."""
        state_binding = await self._runtime.get_state_store()
        partition = owner_partition(self._owner)
        run_read = await state_binding.store.get_run(
            partition,
            context.session_id,
            context.run_id,
        )
        if run_read.record.status == "succeeded" and not run_read.record.result_available:
            return _durable_status(run_read.record)
        try:
            activated = await activate_session(
                self._runtime,
                self._owner,
                context.session_id,
                SetupBudget.start(),
                allow_create=False,
            )
        except SessionActivationGoneError:
            await self._runtime.reconcile_session(partition, context.session_id)
            refreshed = await state_binding.store.get_run(
                partition,
                context.session_id,
                context.run_id,
            )
            return _durable_status(
                refreshed.record,
                result_available=False,
                error=RunError(
                    code=SESSION_TOMBSTONED_ERROR_CODE,
                    message="Session backing is no longer available.",
                    fault_domain="sandbox",
                ),
            )
        except SessionActivationError:
            await self._runtime.reconcile_session(partition, context.session_id)
            refreshed = await state_binding.store.get_run(
                partition,
                context.session_id,
                context.run_id,
            )
            return _durable_status(refreshed.record)
        try:
            status = await self._run_control.get_status(activated.handle, context)
            status = _validate_terminal_output(self._binding, status)
            adopted = await _adopt_if_terminal(
                self._runtime,
                activated,
                run_read.record,
                status,
            )
            if (
                adopted is not None
                and adopted.status == "succeeded"
                and not adopted.result_available
            ):
                return _durable_status(adopted)
            if status.state not in TERMINAL_RUN_STATUSES:
                await self._runtime.reconcile_session(partition, context.session_id)
                refreshed = await state_binding.store.get_run(
                    partition,
                    context.session_id,
                    context.run_id,
                )
                if refreshed.record.status in TERMINAL_RUN_STATUSES:
                    return _durable_status(refreshed.record)
            return status
        finally:
            await activated.handle.close()

    def read_events(
        self,
        context: RunContext,
        after_sequence: int,
    ) -> AsyncIterator[RunEvent]:
        """Tail a reachable sandbox journal without cancelling on reader disconnect."""

        async def stream() -> AsyncIterator[RunEvent]:
            activated = await activate_session(
                self._runtime,
                self._owner,
                context.session_id,
                SetupBudget.start(),
                allow_create=False,
            )
            try:
                async for event in self._run_control.read_events(
                    activated.handle,
                    context,
                    after_sequence,
                ):
                    yield event
                await self.get_run(context)
            finally:
                await activated.handle.close()

        return stream()

    async def cancel_run(self, context: RunContext) -> RunStatus:
        """Serialize cancellation behind activation so the current process is signaled."""
        setup_budget = SetupBudget.start()
        partition = owner_partition(self._owner)
        async with self._runtime.hold_session(
            partition,
            context.session_id,
            setup_deadline=setup_budget,
        ):
            activated = await activate_session(
                self._runtime,
                self._owner,
                context.session_id,
                setup_budget,
                allow_create=False,
            )
            try:
                run_read = await _within_setup_budget(
                    activated.store.get_run(
                        activated.partition,
                        context.session_id,
                        context.run_id,
                    ),
                    setup_budget,
                )
                status = await self._run_control.cancel(activated.handle, context)
                await _adopt_if_terminal(
                    self._runtime,
                    activated,
                    run_read.record,
                    _validate_terminal_output(self._binding, status),
                )
                return status
            finally:
                await activated.handle.close()


async def _restore_after_unadmitted(
    runtime: SessionRuntimeBinding,
    activated: ActivatedSession,
) -> None:
    """Best-effort re-arm after an admission path leaves no active run owner."""
    try:
        await restore_idle_lifecycle_if_unowned(runtime, activated)
    except Exception:
        logger.exception("Could not restore sandbox idle policy after admission did not complete")


async def _preflight_new_session_replay(
    runtime: SessionRuntimeBinding,
    partition: OwnerPartition,
    attempt: IdempotencyAttempt,
    setup_budget: SetupBudget,
) -> DurableRunRecord | None:
    """Return a durable owner-key winner before creating a competing sandbox."""
    state_binding = await _within_setup_budget(runtime.get_state_store(), setup_budget)
    existing = await _within_setup_budget(
        state_binding.store.get_owner_idempotency(partition, attempt.key_hash),
        setup_budget,
    )
    if existing is None:
        return None
    if existing.record.expires_at <= datetime.now(UTC):
        await _within_setup_budget(
            state_binding.store.delete_owner_idempotency(
                previous=existing.record,
                etag=existing.etag,
            ),
            setup_budget,
        )
        return None
    if existing.record.request_hash != attempt.request_hash:
        raise IdempotencyConflictError(
            "idempotency key already used with a different payload",
            existing_run_id=existing.record.run_id,
        )
    return (
        await _within_setup_budget(
            state_binding.store.get_run(
                partition,
                existing.record.session_id,
                existing.record.run_id,
            ),
            setup_budget,
        )
    ).record


async def _admit_run(
    activated: ActivatedSession,
    run: DurableRunRecord,
    admitted_session: DurableSessionRecord,
    *,
    attempt: IdempotencyAttempt | None,
    is_new_session: bool,
    setup_budget: SetupBudget,
) -> AdmissionOutcome:
    """Choose the session or owner idempotency EGT without mixing their invariants."""
    if is_new_session and attempt is not None:
        owner_idempotency = DurableOwnerIdempotencyRecord.create(
            owner_partition=activated.partition,
            idempotency_hash=attempt.key_hash,
            request_hash=attempt.request_hash,
            session_id=run.session_id,
            run_id=run.run_id,
            expires_at=activated.session.expires_at,
            created_at=run.created_at,
        )
        return await _within_setup_budget(
            activated.store.admit_new_session_run(
                NewSessionAdmissionRecords.create(
                    admitted_session,
                    run,
                    owner_idempotency,
                ),
                expected_session_etag=activated.etag,
            ),
            setup_budget,
        )
    idempotency = (
        None
        if attempt is None
        else DurableIdempotencyRecord.create(
            owner_partition=activated.partition,
            session_id=run.session_id,
            idempotency_hash=attempt.key_hash,
            request_hash=attempt.request_hash,
            run_id=run.run_id,
            expires_at=activated.session.expires_at,
            created_at=run.created_at,
        )
    )
    return await _within_setup_budget(
        activated.store.admit_run(
            AdmissionRecords.create(admitted_session, run, idempotency),
            expected_session_etag=activated.etag,
        ),
        setup_budget,
    )


async def _retire_losing_new_session_candidate(
    activated: ActivatedSession,
    runtime: SessionRuntimeBinding,
) -> None:
    """Leave a durable deleting trail before removing a first-call race loser."""
    try:
        current = await activated.store.get_session(
            activated.partition,
            activated.session.session_id,
        )
        if current.record.active_run_id is not None or current.record.status not in {
            "creating",
            "ready",
            "suspended",
            "deleting",
        }:
            return
        deleting = (
            current.record
            if current.record.status == "deleting"
            else _session_with_status(
                current.record,
                status="deleting",
                updated_at=datetime.now(UTC),
            )
        )
        if current.record.status != "deleting":
            await activated.store.update_session(
                previous=current.record,
                updated=deleting,
                etag=current.etag,
            )
        await activated.handle.delete()
        provider = await runtime.get_provider()
        for snapshot_id in deleting.snapshot_ids:
            await provider.delete_snapshot(snapshot_id)
        reread = await activated.store.get_session(
            activated.partition,
            activated.session.session_id,
        )
        if reread.record.status == "deleting" and reread.record.active_run_id is None:
            await activated.store.tombstone_session(
                previous=reread.record,
                etag=reread.etag,
                tombstone_reason="new_session_idempotency_loser",
                updated_at=datetime.now(UTC),
            )
    except Exception:
            logger.exception("Could not fully retire a losing new-session idempotency candidate")


def _session_with_status(
    session: DurableSessionRecord,
    *,
    status: str,
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
        status=status,  # type: ignore[arg-type]
        last_activity_at=session.last_activity_at,
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
    )


def _run_handle(run: DurableRunRecord) -> RunHandle:
    return RunHandle(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.status,
        created_at=run.created_at,
    )


def _new_run(
    session: DurableSessionRecord,
    run_id: str,
    *,
    timeout: float | None,
) -> DurableRunRecord:
    now = datetime.now(UTC)
    return DurableRunRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        run_id=run_id,
        generation=session.generation,
        status="accepted",
        result_available=False,
        status_reason=None,
        expires_at=now + timedelta(seconds=timeout if timeout is not None else DEFAULT_TIMEOUT),
        created_at=now,
        updated_at=now,
    )


async def _adopt_failed_submission(
    runtime: SessionRuntimeBinding,
    activated: ActivatedSession,
    run: DurableRunRecord,
) -> None:
    failed = terminal_run(
        run,
        status="failed",
        result_available=False,
        reason="submission_failed",
        updated_at=datetime.now(UTC),
    )
    outcome = await activated.store.adopt_terminal_run(failed)
    if outcome.slot_released:
        await rearm_idle_lifecycle(runtime, activated)


async def _adopt_if_terminal(
    runtime: SessionRuntimeBinding,
    activated: ActivatedSession,
    run: DurableRunRecord,
    status: RunStatus,
) -> DurableRunRecord | None:
    terminal = _terminal_record(run, status)
    if terminal is None:
        return None
    outcome = await activated.store.adopt_terminal_run(terminal)
    if outcome.slot_released:
        await rearm_idle_lifecycle(runtime, activated)
    return outcome.run


def _terminal_record(
    run: DurableRunRecord,
    status: RunStatus,
) -> DurableRunRecord | None:
    if status.state == "succeeded":
        return terminal_run(
            run,
            status="succeeded",
            result_available=status.result_available,
            reason=None,
            updated_at=datetime.now(UTC),
        )
    if status.state == "failed":
        return terminal_run(
            run,
            status="failed",
            result_available=False,
            reason=status.error.code if status.error is not None else "sandbox_failed",
            updated_at=datetime.now(UTC),
        )
    if status.state == "canceled":
        return terminal_run(
            run,
            status="canceled",
            result_available=False,
            reason="sandbox_canceled",
            updated_at=datetime.now(UTC),
        )
    if status.state == "timed_out":
        return terminal_run(
            run,
            status="timed_out",
            result_available=False,
            reason="sandbox_timed_out",
            updated_at=datetime.now(UTC),
        )
    if status.state == "abandoned":
        return terminal_run(
            run,
            status="abandoned",
            result_available=False,
            reason="sandbox_abandoned",
            updated_at=datetime.now(UTC),
        )
    return None


def _validate_terminal_output(binding: AgentBinding, status: RunStatus) -> RunStatus:
    """Turn a failed controller-side output contract into a durable failed run."""
    if status.state != "succeeded" or status.result is None or binding.output_validator is None:
        return status
    error = binding.output_validator(status.result)
    if error is None:
        return status
    return RunStatus(
        run_id=status.run_id,
        session_id=status.session_id,
        state="failed",
        last_sequence=status.last_sequence,
        result_available=False,
        error=error,
    )


def _durable_status(
    run: DurableRunRecord,
    *,
    result_available: bool | None = None,
    error: RunError | None = None,
) -> RunStatus:
    return RunStatus(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.status,
        last_sequence=0,
        result_available=run.result_available if result_available is None else result_available,
        result=None,
        error=_durable_error(run.status) if error is None else error,
    )


def _durable_error(state: RunState) -> RunError | None:
    if state == "failed":
        return RunError(code="run_failed", message="Run failed in the sandbox.", fault_domain="sandbox")
    if state == "timed_out":
        return RunError(
            code="run_timed_out",
            message="Run timed out in the sandbox.",
            fault_domain="sandbox",
        )
    if state == "abandoned":
        return RunError(
            code="run_abandoned",
            message="Run is no longer available in the sandbox.",
            fault_domain="sandbox",
        )
    return None


if TYPE_CHECKING:

    def _assert_backend_conformance(
        binding: AgentBinding,
        runtime: SessionRuntimeBinding,
        owner: OwnerContext,
    ) -> None:
        backend: AgentExecutionBackend = AcaSandboxExecutionBackend(
            binding,
            runtime=runtime,
            owner=owner,
        )
        del backend
