"""ACA Sandbox implementation of the four-method execution lifecycle seam."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..config import DEFAULT_TIMEOUT
from ..controller.idempotency import (
    IdempotencyAttempt,
    IdempotencyResultUnavailableError,
    build_idempotency_attempt,
)
from ..controller.journal_integrity import (
    JOURNAL_CORRUPT_ERROR_CODE,
    handle_journal_corruption,
    journal_corruption_error,
    journal_corruption_status,
)
from ..controller.readiness import (
    ActivatedSession,
    SessionActivationError,
    SessionActivationGoneError,
    SessionActivationNotFoundError,
    SessionRuntimeBinding,
    _within_setup_budget,
    abort_submit_operation,
    activate_session,
    begin_submit_operation,
    disarm_submit_lifecycle,
    finalize_submit_operation,
    provision_new_session_submit,
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
    DurableRunRecord,
    DurableSessionRecord,
    IdempotencyConflictError,
    OwnerContext,
    OwnerPartition,
    mint_run_id,
    mint_session_id,
    owner_idempotency_expiry,
    owner_partition,
)
from ..transport.transport_models import SandboxFileNotFoundError
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
    RunJournalProtocolError,
    RunSubmissionDefinitiveFailureError,
    SandboxRunControl,
)
from .setup_budget import SetupBudget
from .terminal_output_validation import validate_terminal_output


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
        if not is_new_session and attempt is not None:
            replay = await _session_idempotency_replay(
                self._runtime,
                partition,
                session_id,
                attempt,
                setup_budget,
            )
            if replay is not None:
                _ensure_replay_result_available(replay)
                return await self._resume_journal_submission(
                    replay,
                    request,
                    setup_budget,
                )
        async with self._runtime.hold_session(
            partition,
            session_id,
            setup_deadline=setup_budget,
        ):
            activated: ActivatedSession | None = None
            try:
                if is_new_session:
                    provisioned = await provision_new_session_submit(
                        self._runtime,
                        self._owner,
                        session_id=session_id,
                        run_id=run_id,
                        timeout=request.timeout,
                        attempt=attempt,
                        setup_deadline=setup_budget,
                    )
                    if provisioned.outcome.replayed:
                        _ensure_replay_result_available(provisioned.outcome.run)
                        if provisioned.activated is not None:
                            await provisioned.activated.handle.close()
                        if provisioned.activated is None and (
                            provisioned.outcome.run.status not in TERMINAL_RUN_STATUSES
                        ):
                            return _run_handle(provisioned.outcome.run)
                        return await self._resume_journal_submission(
                            provisioned.outcome.run,
                            request,
                            setup_budget,
                        )
                    assert provisioned.activated is not None
                    activated = provisioned.activated
                    outcome = AdmissionOutcome(
                        run=provisioned.outcome.run,
                        run_etag=provisioned.outcome.run_etag,
                        session_etag=provisioned.outcome.session_etag,
                        replayed=False,
                    )
                else:
                    activated = await activate_session(
                        self._runtime,
                        self._owner,
                        session_id,
                        setup_budget,
                        allow_create=False,
                    )
                    run = _new_run(
                        activated.session,
                        run_id,
                        timeout=request.timeout,
                        agent_slug=self._agent_name,
                    )
                    prepared, fence = await _within_setup_budget(
                        begin_submit_operation(
                            activated,
                            run,
                            agent_slug=self._agent_name,
                        ),
                        setup_budget,
                    )
                    prepared, fence = await _within_setup_budget(
                        disarm_submit_lifecycle(self._runtime, prepared, fence),
                        setup_budget,
                    )
                    admitted_session = session_with_admitted_run(
                        prepared.session,
                        run_id,
                        updated_at=run.updated_at,
                    )
                    idempotency = (
                        None
                        if attempt is None
                        else DurableIdempotencyRecord.create(
                            owner_partition=prepared.partition,
                            session_id=run.session_id,
                            idempotency_hash=attempt.key_hash,
                            request_hash=attempt.request_hash,
                            run_id=run.run_id,
                            expires_at=owner_idempotency_expiry(
                                prepared.session.expires_at,
                                run.expires_at,
                                None,
                                run.created_at,
                            ),
                            created_at=run.created_at,
                        )
                    )
                    try:
                        outcome = await _within_setup_budget(
                            prepared.store.admit_operation_run(
                                fence=fence,
                                records=AdmissionRecords.create(
                                    admitted_session,
                                    run,
                                    idempotency,
                                ),
                            ),
                            setup_budget,
                        )
                    except Exception:
                        await abort_submit_operation(self._runtime, prepared, fence)
                        raise
                    if outcome.replayed:
                        await abort_submit_operation(self._runtime, prepared, fence)
                        _ensure_replay_result_available(outcome.run)
                        return _run_handle(outcome.run)
                    current = await _within_setup_budget(
                        prepared.store.get_session(
                            prepared.partition,
                            prepared.session.session_id,
                        ),
                        setup_budget,
                    )
                    activated = ActivatedSession.create(
                        handle=prepared.handle,
                        session=current.record,
                        etag=outcome.session_etag or current.etag,
                        partition=prepared.partition,
                        store=prepared.store,
                        checkpoint_name=prepared.checkpoint_name,
                    )

                assert activated is not None
                await _within_setup_budget(
                    revalidate_before_submit(activated, outcome.run),
                    setup_budget,
                )
                try:
                    status = await self._submit_fenced_journal(
                        activated,
                        outcome.run,
                        request,
                        setup_budget,
                    )
                except RunSubmissionDefinitiveFailureError:
                    await _adopt_failed_submission(self._runtime, activated, outcome.run)
                    raise
                await _adopt_if_terminal(
                    self._runtime,
                    activated,
                    outcome.run,
                    validate_terminal_output(self._binding, status),
                )
                return RunHandle(
                    run_id=outcome.run.run_id,
                    session_id=outcome.run.session_id,
                    state="accepted",
                    created_at=outcome.run.created_at,
                )
            finally:
                if activated is not None:
                    await activated.handle.close()

    async def _submit_fenced_journal(
        self,
        activated: ActivatedSession,
        run: DurableRunRecord,
        request: StartRunRequest,
        setup_budget: SetupBudget,
    ) -> RunStatus:
        fence = await activated.store.claim_operation_journal(
            owner_partition=activated.partition,
            session_id=run.session_id,
            run_id=run.run_id,
            token=mint_run_id(),
            updated_at=datetime.now(UTC),
        )
        if fence is None:
            try:
                return await self._run_control.get_status(
                    activated.handle,
                    RunContext(run_id=run.run_id, session_id=run.session_id),
                )
            except RunJournalProtocolError:
                await self._handle_runtime_journal_corruption(
                    activated,
                    RunContext(run_id=run.run_id, session_id=run.session_id),
                )
                raise SessionActivationNotFoundError(
                    "Session run journal cannot be trusted."
                ) from None
            except SandboxFileNotFoundError as exc:
                raise ActiveRunConflictError(
                    "another controller owns the journal launch",
                    active_run_id=run.run_id,
                ) from exc
        try:
            return await self._run_control.submit(
                activated.handle,
                run.run_id,
                RunEnvelope.create(
                    run_id=run.run_id,
                    session_id=run.session_id,
                    agent_name=self._agent_name,
                    prompt=request.prompt,
                    timeout=request.timeout,
                ),
                timeout_seconds=setup_budget.remaining_setup_seconds(),
            )
        except RunJournalProtocolError:
            await self._handle_runtime_journal_corruption(
                activated,
                RunContext(run_id=run.run_id, session_id=run.session_id),
            )
            raise SessionActivationNotFoundError(
                "Session run journal cannot be trusted."
            ) from None

    async def _handle_runtime_journal_corruption(
        self,
        activated: ActivatedSession,
        context: RunContext,
    ) -> DurableRunRecord:
        """Fail, quarantine, and finalize only the matching durable submit operation."""
        corrupted = await handle_journal_corruption(
            activated.store,
            activated.partition,
            context.session_id,
            context.run_id,
        )
        await finalize_submit_operation(
            self._runtime,
            activated,
            expected_run_id=context.run_id,
        )
        return corrupted

    async def _resume_journal_submission(
        self,
        run: DurableRunRecord,
        request: StartRunRequest,
        setup_budget: SetupBudget,
    ) -> RunHandle:
        if run.status in TERMINAL_RUN_STATUSES:
            return _run_handle(run)
        activated = await activate_session(
            self._runtime,
            self._owner,
            run.session_id,
            setup_budget,
            allow_create=False,
        )
        try:
            status = await self._submit_fenced_journal(
                activated,
                run,
                request,
                setup_budget,
            )
            await _adopt_if_terminal(
                self._runtime,
                activated,
                run,
                validate_terminal_output(self._binding, status),
            )
            return _run_handle(run)
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
            status = validate_terminal_output(self._binding, status)
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
        except RunJournalProtocolError:
            corrupted = await self._handle_runtime_journal_corruption(activated, context)
            return journal_corruption_status(corrupted)
        finally:
            await activated.handle.close()

    def read_events(
        self,
        context: RunContext,
        after_sequence: int,
    ) -> AsyncIterator[RunEvent]:
        """Tail a reachable sandbox journal without cancelling on reader disconnect."""

        async def stream() -> AsyncIterator[RunEvent]:
            try:
                activated = await activate_session(
                    self._runtime,
                    self._owner,
                    context.session_id,
                    SetupBudget.start(),
                    allow_create=False,
                )
            except SessionActivationError:
                return
            try:
                async for event in self._run_control.read_events(
                    activated.handle,
                    context,
                    after_sequence,
                ):
                    yield event
                await self.get_run(context)
            except RunJournalProtocolError:
                await self._handle_runtime_journal_corruption(activated, context)
                return
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
                try:
                    status = await self._run_control.cancel(activated.handle, context)
                except RunJournalProtocolError:
                    corrupted = await self._handle_runtime_journal_corruption(
                        activated,
                        context,
                    )
                    return journal_corruption_status(corrupted)
                validated = validate_terminal_output(self._binding, status)
                adopted = await _adopt_if_terminal(
                    self._runtime,
                    activated,
                    run_read.record,
                    validated,
                )
                if adopted is None:
                    return validated
                projection = _durable_status(adopted, error=validated.error)
                if (
                    adopted.status == "succeeded"
                    and adopted.result_available
                    and validated.result is not None
                ):
                    return RunStatus(
                        run_id=projection.run_id,
                        session_id=projection.session_id,
                        state=projection.state,
                        last_sequence=validated.last_sequence,
                        result_available=projection.result_available,
                        result=validated.result,
                        error=projection.error,
                    )
                return projection
            finally:
                await activated.handle.close()


def _run_handle(run: DurableRunRecord) -> RunHandle:
    return RunHandle(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.status,
        created_at=run.created_at,
    )


def _ensure_replay_result_available(run: DurableRunRecord) -> None:
    """Reject only an evicted successful replay before any journal work is resumed."""
    if run.status == "succeeded" and not run.result_available:
        raise IdempotencyResultUnavailableError(
            "The idempotent run completed but its result is no longer available."
        )


async def _session_idempotency_replay(
    runtime: SessionRuntimeBinding,
    partition: OwnerPartition,
    session_id: str,
    attempt: IdempotencyAttempt,
    setup_budget: SetupBudget,
) -> DurableRunRecord | None:
    state_binding = await _within_setup_budget(runtime.get_state_store(), setup_budget)
    existing = await _within_setup_budget(
        state_binding.store.get_idempotency(
            partition,
            session_id,
            attempt.key_hash,
        ),
        setup_budget,
    )
    if existing is None:
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
                session_id,
                existing.record.run_id,
            ),
            setup_budget,
        )
    ).record


def _new_run(
    session: DurableSessionRecord,
    run_id: str,
    *,
    timeout: float | None,
    agent_slug: str = "",
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
        agent_slug=agent_slug,
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
    await activated.store.adopt_terminal_run(failed)
    await finalize_submit_operation(
        runtime,
        activated,
        expected_run_id=run.run_id,
    )


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
    await finalize_submit_operation(
        runtime,
        activated,
        expected_run_id=run.run_id,
    )
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
        error=(
            _durable_error(run.status, reason=run.status_reason)
            if error is None
            else error
        ),
    )


def _durable_error(state: RunState, *, reason: str | None = None) -> RunError | None:
    if state == "failed":
        if reason == JOURNAL_CORRUPT_ERROR_CODE:
            return journal_corruption_error()
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
