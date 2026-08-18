"""ACA Sandbox implementation of the four-method execution lifecycle seam."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
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
    SessionActivationAuthorizationError,
    SessionActivationError,
    SessionActivationGoneError,
    SessionActivationNotFoundError,
    SessionActivationSetupTimeoutError,
    SessionRuntimeBinding,
    _await_admission_with_setup_timeout,
    _bounded_admission_confirmation,
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
    DurableSessionOperation,
    DurableSessionRecord,
    IdempotencyConflictError,
    OwnerContext,
    OwnerPartition,
    PreLaunchCancelOutcome,
    ProvisionSubmitOutcome,
    SessionOperationFence,
    SessionRowNotFoundError,
    SessionStateStore,
    StaleOperationTokenError,
    mint_run_id,
    mint_session_id,
    owner_idempotency_expiry,
    owner_partition,
)
from ..transport.transport_models import SandboxFileNotFoundError, SandboxFileOperationError
from .backend import (
    SESSION_TOMBSTONED_ERROR_CODE,
    AgentExecutionBackend,
    DurableAdmissionSetupTimeoutError,
    LinkedActiveRunConflictError,
    RunContext,
    RunError,
    RunEvent,
    RunHandle,
    RunPhase,
    RunState,
    RunStatus,
    StartRunRequest,
)
from .binding import AgentBinding
from .run_control import (
    EVENT_POLL_INTERVAL_SECONDS,
    RunEnvelope,
    RunJournalProtocolError,
    RunSubmissionDefinitiveFailureError,
    SandboxRunControl,
)
from .setup_budget import (
    SetupBudget,
    SetupBudgetExpiredError,
    SetupPhase,
    SetupTimeoutExceptionType,
    SetupTimeoutMetadata,
    SetupTimeoutReason,
)
from .terminal_output_validation import validate_terminal_output

_TABLE_PROGRESS_POLL_SECONDS = EVENT_POLL_INTERVAL_SECONDS
_CANCEL_JOURNAL_POLL_SECONDS = 0.1
_PRELAUNCH_SUBMIT_PHASES = frozenset(
    {
        "provision_create",
        "provision_lifecycle",
        "provision_content",
        "provision_manifest",
        "provision_journal",
        "submit_journal",
    }
)
_LAUNCHING_PHASES = frozenset({"provision_launching", "submit_launching"})
_SUBMIT_CANCEL_PHASES = _PRELAUNCH_SUBMIT_PHASES | _LAUNCHING_PHASES


@dataclass(frozen=True, slots=True)
class _DurableManagementState:
    """One owner-scoped run, session, and matching active operation read."""

    partition: OwnerPartition
    store: SessionStateStore
    run: DurableRunRecord
    session: DurableSessionRecord
    operation: DurableSessionOperation | None


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
        setup_budget = self._setup_budget or SetupBudget.start()
        if request.session_id is not None:
            await _within_setup_budget(
                self._runtime.reconcile_session(partition, request.session_id, setup_budget),
                setup_budget,
                phase=SetupPhase.PROVISION_RECONCILE,
            )
        try:
            return await self._start_run_once(request, partition, setup_budget)
        except ActiveRunConflictError:
            if request.session_id is None:
                raise
            await _within_setup_budget(
                self._runtime.reconcile_session(partition, request.session_id, setup_budget),
                setup_budget,
                phase=SetupPhase.PROVISION_RECONCILE,
            )
            try:
                return await self._start_run_once(request, partition, setup_budget)
            except ActiveRunConflictError as exc:
                linked = await self._linked_active_run_conflict(
                    request.session_id,
                    exc,
                )
                raise linked from exc

    async def _start_run_once(
        self,
        request: StartRunRequest,
        partition: OwnerPartition,
        setup_budget: SetupBudget,
    ) -> RunHandle:
        is_new_session = request.session_id is None
        session_id = request.session_id or mint_session_id()
        run_id = mint_run_id()
        outcome: AdmissionOutcome | None = None
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
        if not is_new_session:
            active_run_id = await _active_run_before_activation(
                self._runtime,
                partition,
                session_id,
                setup_budget,
            )
            if active_run_id is not None:
                raise ActiveRunConflictError(
                    "session already has an active run",
                    active_run_id=active_run_id,
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
                    if provisioned.setup_timed_out:
                        assert provisioned.timeout_metadata is not None
                        raise _durable_admission_timeout(
                            provisioned.outcome,
                            provisioned.timeout_metadata,
                        )
                    outcome = AdmissionOutcome(
                        run=provisioned.outcome.run,
                        run_etag=provisioned.outcome.run_etag,
                        session_etag=provisioned.outcome.session_etag,
                        replayed=provisioned.outcome.replayed,
                        admission=provisioned.outcome.admission,
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
                    if provisioned.activated is None:
                        return _run_handle(provisioned.outcome.run)
                    assert provisioned.activated is not None
                    activated = provisioned.activated
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
                        phase=SetupPhase.SUBMIT_ADMISSION,
                    )
                    prepared, fence = await _within_setup_budget(
                        disarm_submit_lifecycle(self._runtime, prepared, fence),
                        setup_budget,
                        phase=SetupPhase.LIFECYCLE,
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
                        outcome = await _await_admission_with_setup_timeout(
                            lambda: prepared.store.admit_operation_run(
                                fence=fence,
                                records=AdmissionRecords.create(
                                    admitted_session,
                                    run,
                                    idempotency,
                                ),
                            ),
                            setup_budget,
                            lambda: _confirm_operation_admission_after_setup_timeout(
                                prepared.store,
                                fence,
                                AdmissionRecords.create(
                                    admitted_session,
                                    run,
                                    idempotency,
                                ),
                            ),
                            lambda result: result.admission == "possibly_committed",
                            phase=SetupPhase.SUBMIT_ADMISSION,
                        )
                    except Exception:
                        await abort_submit_operation(self._runtime, prepared, fence)
                        raise
                    if outcome.admission == "not_reserved":
                        await abort_submit_operation(self._runtime, prepared, fence)
                        outcome = None
                        raise _not_reserved_admission_timeout(
                            _admission_timeout_metadata(
                                setup_budget,
                                phase=SetupPhase.SUBMIT_ADMISSION,
                            )
                        )
                    if outcome.admission == "possibly_committed":
                        raise _durable_admission_timeout(
                            outcome,
                            _admission_timeout_metadata(
                                setup_budget,
                                phase=SetupPhase.SUBMIT_ADMISSION,
                                reason=SetupTimeoutReason.PROVISION_INDETERMINATE,
                            ),
                        )
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
                        phase=SetupPhase.STATE_STORE,
                    )
                    activated = ActivatedSession.create(
                        handle=prepared.handle,
                        session=current.record,
                        etag=outcome.session_etag or current.etag,
                        partition=prepared.partition,
                        store=prepared.store,
                    )

                assert activated is not None
                assert outcome is not None
                await _within_setup_budget(
                    revalidate_before_submit(activated, outcome.run),
                    setup_budget,
                    phase=SetupPhase.PRE_SUBMIT_VALIDATION,
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
            except DurableAdmissionSetupTimeoutError:
                raise
            except (SessionActivationSetupTimeoutError, SetupBudgetExpiredError) as exc:
                if outcome is not None:
                    raise _durable_admission_timeout(outcome, exc.metadata) from None
                raise
            finally:
                if activated is not None:
                    await activated.handle.close()

    async def _linked_active_run_conflict(
        self,
        session_id: str,
        conflict: ActiveRunConflictError,
    ) -> LinkedActiveRunConflictError:
        durable = await self._read_durable_management_state(
            RunContext(run_id=conflict.active_run_id, session_id=session_id)
        )
        return LinkedActiveRunConflictError(
            "session already has an active run",
            session_id=durable.session.session_id,
            run_id=durable.run.run_id,
            status=durable.run.status,
            phase=_public_phase(durable),
        )

    async def _submit_fenced_journal(
        self,
        activated: ActivatedSession,
        run: DurableRunRecord,
        request: StartRunRequest,
        setup_budget: SetupBudget,
    ) -> RunStatus:
        context = RunContext(run_id=run.run_id, session_id=run.session_id)
        try:
            fence = await _within_setup_budget(
                activated.store.claim_operation_journal(
                    owner_partition=activated.partition,
                    session_id=run.session_id,
                    run_id=run.run_id,
                    token=mint_run_id(),
                    updated_at=datetime.now(UTC),
                ),
                setup_budget,
                phase=SetupPhase.JOURNAL,
            )
        except StaleOperationTokenError:
            durable = await self._read_durable_management_state(context)
            return _durable_status(durable.run, phase=_public_phase(durable))
        if fence is None:
            durable = await self._read_durable_management_state(context)
            if not _is_launching_submission(durable):
                return _durable_status(durable.run, phase=_public_phase(durable))
            try:
                return await _within_setup_budget(
                    self._run_control.get_status(
                        activated.handle,
                        context,
                    ),
                    setup_budget,
                    phase=SetupPhase.JOURNAL,
                )
            except RunJournalProtocolError:
                await self._handle_runtime_journal_corruption(
                    activated,
                    context,
                )
                raise SessionActivationNotFoundError(
                    "Session run journal cannot be trusted."
                ) from None
            except SandboxFileNotFoundError:
                return _durable_status(durable.run, phase=_public_phase(durable))
        try:
            return await _within_setup_budget(
                self._run_control.submit(
                    activated.handle,
                    run.run_id,
                    RunEnvelope.create(
                        run_id=run.run_id,
                        session_id=run.session_id,
                        agent_name=self._agent_name,
                        prompt=request.prompt,
                        timeout=request.timeout,
                    ),
                    timeout_seconds=setup_budget.remaining_setup_seconds(phase=SetupPhase.JOURNAL),
                ),
                setup_budget,
                phase=SetupPhase.JOURNAL,
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
        """Read durable management state before using a live journal."""
        durable = await self._read_durable_management_state(context)
        phase = _public_phase(durable)
        if _is_tombstoned_session(durable.session):
            return _tombstoned_status(durable.run, phase=phase)
        if _is_prelaunch_submission(durable):
            return _durable_status(durable.run, phase=phase)
        if durable.run.status in TERMINAL_RUN_STATUSES and (
            durable.run.status != "succeeded" or not durable.run.result_available
        ):
            return _durable_status(durable.run, phase=phase)
        try:
            activated = await activate_session(
                self._runtime,
                self._owner,
                context.session_id,
                SetupBudget.start(),
                allow_create=False,
            )
        except SessionActivationGoneError:
            await self._runtime.reconcile_session(durable.partition, context.session_id)
            refreshed = await self._read_durable_management_state(context)
            return _tombstoned_status(refreshed.run, phase=_public_phase(refreshed))
        except SessionActivationAuthorizationError:
            # Deterministic RBAC failure must reach the management 503 instead of
            # the durable-state fallback used for other activation failures.
            raise
        except (
            SessionActivationError,
            SetupBudgetExpiredError,
            SandboxFileNotFoundError,
            SandboxFileOperationError,
        ):
            return _durable_status(durable.run, phase=phase)
        try:
            status = await self._run_control.get_status(activated.handle, context)
            status = validate_terminal_output(self._binding, status)
            adopted = await _adopt_if_terminal(
                self._runtime,
                activated,
                durable.run,
                status,
            )
            if (
                adopted is not None
                and adopted.status == "succeeded"
                and not adopted.result_available
            ):
                refreshed = await self._read_durable_management_state(context)
                return _durable_status(adopted, phase=_public_phase(refreshed))
            if status.state in TERMINAL_RUN_STATUSES:
                refreshed = await self._read_durable_management_state(context)
                return _with_public_phase(status, _public_phase(refreshed))
            if status.state not in TERMINAL_RUN_STATUSES:
                await self._runtime.reconcile_session(durable.partition, context.session_id)
                refreshed = await self._read_durable_management_state(context)
                if refreshed.run.status in TERMINAL_RUN_STATUSES:
                    return _durable_status(refreshed.run, phase=_public_phase(refreshed))
            return _with_public_phase(status, _live_public_phase(durable, status))
        except RunJournalProtocolError:
            corrupted = await self._handle_runtime_journal_corruption(activated, context)
            return journal_corruption_status(corrupted)
        except (SandboxFileNotFoundError, SandboxFileOperationError):
            return _durable_status(durable.run, phase=phase)
        finally:
            await activated.handle.close()

    def read_events(
        self,
        context: RunContext,
        after_sequence: int,
    ) -> AsyncIterator[RunEvent]:
        """Tail journal events after Table-backed provisioning completes."""

        async def stream() -> AsyncIterator[RunEvent]:
            durable = await self._read_durable_management_state(context)
            while _is_prelaunch_submission(durable):
                await asyncio.sleep(_TABLE_PROGRESS_POLL_SECONDS)
                durable = await self._read_durable_management_state(context)
            if _is_rearming_terminal(durable):
                return
            try:
                activated = await activate_session(
                    self._runtime,
                    self._owner,
                    context.session_id,
                    SetupBudget.start(),
                    allow_create=False,
                )
            except SessionActivationAuthorizationError:
                # Let event preflight surface the same management 503 rather than
                # silently ending the stream through the broad fallback below.
                raise
            except (
                SessionActivationError,
                SetupBudgetExpiredError,
                SandboxFileNotFoundError,
                SandboxFileOperationError,
            ):
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
            except (SandboxFileNotFoundError, SandboxFileOperationError):
                return
            finally:
                await activated.handle.close()

        return stream()

    async def cancel_run(self, context: RunContext) -> RunStatus:
        """Cancel from durable state before activating a launched journal."""
        state_binding = await self._runtime.get_state_store()
        partition = owner_partition(self._owner)
        session = await state_binding.store.get_session(partition, context.session_id)
        if _is_tombstoned_session(session.record):
            raise SessionActivationGoneError("Session has been retired.")
        durable = await self._read_durable_management_state(context)
        if durable.run.status in TERMINAL_RUN_STATUSES:
            return _durable_status(durable.run, phase=_public_phase(durable))
        if _is_submit_cancel_candidate(durable):
            outcome = await _cancel_prelaunch_submit(durable, context)
            durable = _management_state_from_cancel_outcome(durable, outcome)
            if outcome.disposition in {"canceled_before_launch", "terminal"}:
                return _durable_status(durable.run, phase=_public_phase(durable))
            if outcome.disposition == "retry":
                durable = await self._read_durable_management_state(context)
                if _is_prelaunch_submission(durable):
                    return _durable_status(durable.run, phase=_public_phase(durable))
            if durable.run.status in TERMINAL_RUN_STATUSES:
                return _durable_status(durable.run, phase=_public_phase(durable))
        return await self._cancel_live_run(context, durable)

    async def _read_durable_management_state(
        self,
        context: RunContext,
    ) -> _DurableManagementState:
        state_binding = await self._runtime.get_state_store()
        partition = owner_partition(self._owner)
        run = (
            await state_binding.store.get_run(
                partition,
                context.session_id,
                context.run_id,
            )
        ).record
        session = await state_binding.store.get_session(partition, context.session_id)
        operation: DurableSessionOperation | None = None
        if session.record.active_operation_id is not None:
            candidate = await state_binding.store.get_operation(
                partition,
                context.session_id,
                session.record.active_operation_id,
            )
            if (
                candidate.record.state == "active"
                and candidate.record.target.session_id == context.session_id
                and candidate.record.target.run_id == context.run_id
            ):
                operation = candidate.record
        return _DurableManagementState(
            partition=partition,
            store=state_binding.store,
            run=run,
            session=session.record,
            operation=operation,
        )

    async def _cancel_live_run(
        self,
        context: RunContext,
        fallback: _DurableManagementState,
    ) -> RunStatus:
        setup_budget = SetupBudget.start()
        durable = fallback
        while True:
            try:
                async with self._runtime.hold_session(
                    durable.partition,
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
                            phase=SetupPhase.STATE_STORE,
                        )
                        if run_read.record.status in TERMINAL_RUN_STATUSES:
                            refreshed = await self._read_durable_management_state(context)
                            return _durable_status(
                                refreshed.run,
                                phase=_public_phase(refreshed),
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
                            return _with_public_phase(
                                validated,
                                _live_public_phase(durable, validated),
                            )
                        refreshed = await self._read_durable_management_state(context)
                        projection = _durable_status(
                            adopted,
                            phase=_public_phase(refreshed),
                            error=validated.error,
                        )
                        if (
                            adopted.status == "succeeded"
                            and adopted.result_available
                            and validated.result is not None
                        ):
                            return _with_public_phase(
                                RunStatus(
                                    run_id=projection.run_id,
                                    session_id=projection.session_id,
                                    state=projection.state,
                                    last_sequence=validated.last_sequence,
                                    result_available=projection.result_available,
                                    result=validated.result,
                                    error=projection.error,
                                ),
                                _public_phase(refreshed),
                            )
                        return projection
                    finally:
                        await activated.handle.close()
            except SessionActivationGoneError:
                raise
            except SessionActivationAuthorizationError:
                raise
            except (SandboxFileNotFoundError, SandboxFileOperationError):
                durable = await self._read_durable_management_state(context)
                if durable.run.status in TERMINAL_RUN_STATUSES:
                    return _durable_status(durable.run, phase=_public_phase(durable))
                if not _is_launching_submission(durable):
                    return _durable_status(durable.run, phase=_public_phase(durable))
                try:
                    await _within_setup_budget(
                        asyncio.sleep(_CANCEL_JOURNAL_POLL_SECONDS),
                        setup_budget,
                        phase=SetupPhase.JOURNAL,
                    )
                except (SessionActivationError, SetupBudgetExpiredError):
                    return _durable_status(durable.run, phase=_public_phase(durable))
            except (SessionActivationError, SetupBudgetExpiredError):
                return _durable_status(durable.run, phase=_public_phase(durable))


async def _cancel_prelaunch_submit(
    durable: _DurableManagementState,
    context: RunContext,
) -> PreLaunchCancelOutcome:
    """Race durable pre-launch cancellation against the journal claim."""
    return await durable.store.cancel_prelaunch_submit(
        owner_partition=durable.partition,
        session_id=context.session_id,
        run_id=context.run_id,
        token=mint_run_id(),
        updated_at=datetime.now(UTC),
    )


def _management_state_from_cancel_outcome(
    durable: _DurableManagementState,
    outcome: PreLaunchCancelOutcome,
) -> _DurableManagementState:
    return _DurableManagementState(
        partition=durable.partition,
        store=durable.store,
        run=outcome.run,
        session=outcome.session,
        operation=outcome.operation,
    )


def _is_prelaunch_submission(durable: _DurableManagementState) -> bool:
    operation = durable.operation
    return (
        durable.run.status == "accepted"
        and durable.session.active_run_id == durable.run.run_id
        and operation is not None
        and operation.kind in {"provision_submit", "submit_run"}
        and operation.phase in _PRELAUNCH_SUBMIT_PHASES
    )


def _is_submit_cancel_candidate(durable: _DurableManagementState) -> bool:
    operation = durable.operation
    return (
        durable.run.status == "accepted"
        and durable.session.active_run_id == durable.run.run_id
        and operation is not None
        and operation.kind in {"provision_submit", "submit_run"}
        and operation.phase in _SUBMIT_CANCEL_PHASES
    )


def _is_launching_submission(durable: _DurableManagementState) -> bool:
    operation = durable.operation
    return (
        durable.run.status == "accepted"
        and durable.session.active_run_id == durable.run.run_id
        and operation is not None
        and operation.kind in {"provision_submit", "submit_run"}
        and operation.phase in _LAUNCHING_PHASES
    )


def _is_rearming_terminal(durable: _DurableManagementState) -> bool:
    operation = durable.operation
    return (
        durable.run.status in TERMINAL_RUN_STATUSES
        and durable.session.active_run_id == durable.run.run_id
        and operation is not None
        and operation.kind in {"provision_submit", "submit_run"}
        and operation.phase in {"provision_rearm", "submit_rearm"}
    )


def _is_tombstoned_session(session: DurableSessionRecord) -> bool:
    return session.status in {"tombstoned", "deleted"}


def _public_phase(durable: _DurableManagementState) -> RunPhase | None:
    """Derive the management phase only from the matching durable evidence."""
    if durable.run.status in TERMINAL_RUN_STATUSES:
        if durable.session.active_run_id == durable.run.run_id or durable.operation is not None:
            return "settling"
        return "terminal"
    if _is_prelaunch_submission(durable):
        return "provisioning"
    if (
        durable.run.status in {"accepted", "running"}
        and durable.operation is not None
        and durable.operation.phase in _LAUNCHING_PHASES
    ):
        return "executing"
    if durable.run.status == "running":
        return "executing"
    return None


def _live_public_phase(
    durable: _DurableManagementState,
    status: RunStatus,
) -> RunPhase | None:
    phase = _public_phase(durable)
    if status.state in {"accepted", "running"} and phase != "provisioning":
        return "executing"
    return phase


def _with_public_phase(status: RunStatus, phase: RunPhase | None) -> RunStatus:
    """Attach the derived management phase to one status projection."""
    if phase is not None:
        status.phase = phase
    return status


def _run_handle(run: DurableRunRecord) -> RunHandle:
    return RunHandle(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.status,
        created_at=run.created_at,
    )


def _durable_admission_timeout(
    outcome: AdmissionOutcome | ProvisionSubmitOutcome,
    metadata: SetupTimeoutMetadata,
) -> DurableAdmissionSetupTimeoutError:
    if outcome.admission == "not_reserved":
        raise ValueError("unreserved admissions do not have a durable timeout handle")
    return DurableAdmissionSetupTimeoutError(
        outcome=outcome.admission,
        handle=_run_handle(outcome.run),
        metadata=metadata,
    )


def _not_reserved_admission_timeout(
    metadata: SetupTimeoutMetadata,
) -> SessionActivationSetupTimeoutError:
    return SessionActivationSetupTimeoutError(metadata)


def _admission_timeout_metadata(
    setup_budget: SetupBudget,
    *,
    phase: SetupPhase,
    reason: SetupTimeoutReason = SetupTimeoutReason.DEADLINE_ELAPSED,
) -> SetupTimeoutMetadata:
    return setup_budget.timeout_metadata(
        phase=phase,
        reason=reason,
        exception_type=SetupTimeoutExceptionType.SESSION_ACTIVATION_SETUP_TIMEOUT,
    )


async def _confirm_operation_admission_after_setup_timeout(
    store: SessionStateStore,
    fence: SessionOperationFence,
    records: AdmissionRecords,
) -> AdmissionOutcome:
    fallback = AdmissionOutcome(
        run=records.run,
        run_etag=None,
        session_etag=None,
        replayed=False,
        admission="possibly_committed",
    )
    return await _bounded_admission_confirmation(
        store.confirm_operation_run_admission(
            fence=fence,
            records=records,
        ),
        fallback,
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
    state_binding = await _within_setup_budget(
        runtime.get_state_store(), setup_budget, phase=SetupPhase.STATE_STORE
    )
    existing = await _within_setup_budget(
        state_binding.store.get_idempotency(
            partition,
            session_id,
            attempt.key_hash,
        ),
        setup_budget,
        phase=SetupPhase.IDEMPOTENCY_LOOKUP,
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
            phase=SetupPhase.IDEMPOTENCY_LOOKUP,
        )
    ).record


async def _active_run_before_activation(
    runtime: SessionRuntimeBinding,
    partition: OwnerPartition,
    session_id: str,
    setup_budget: SetupBudget,
) -> str | None:
    """Read the durable slot before activation can reject a provisioning session."""
    state_binding = await _within_setup_budget(
        runtime.get_state_store(),
        setup_budget,
        phase=SetupPhase.STATE_STORE,
    )
    try:
        session = await _within_setup_budget(
            state_binding.store.get_session(partition, session_id),
            setup_budget,
            phase=SetupPhase.SESSION_LOOKUP,
        )
    except SessionRowNotFoundError:
        return None
    return session.record.active_run_id


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
    phase: RunPhase | None = None,
    result_available: bool | None = None,
    error: RunError | None = None,
) -> RunStatus:
    return _with_public_phase(
        RunStatus(
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
        ),
        phase,
    )


def _tombstoned_status(run: DurableRunRecord, *, phase: RunPhase | None) -> RunStatus:
    return _durable_status(
        run,
        phase=phase,
        result_available=False,
        error=RunError(
            code=SESSION_TOMBSTONED_ERROR_CODE,
            message="Session backing is no longer available.",
            fault_domain="sandbox",
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
