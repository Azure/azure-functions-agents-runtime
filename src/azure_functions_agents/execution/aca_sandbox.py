"""ACA Sandbox implementation of the four-method execution lifecycle seam."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Never, TypeIs

from .._logger import logger
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
    SessionActivationBindingError,
    SessionActivationConflictError,
    SessionActivationError,
    SessionActivationGoneError,
    SessionActivationNotFoundError,
    SessionActivationSetupTimeoutError,
    SessionActivationTransientError,
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
from ..sandbox_runtime_limits import RESULT_HOLD_SECONDS
from ..session_state import (
    TERMINAL_RUN_STATUSES,
    ActiveRunConflictError,
    AdmissionDisposition,
    AdmissionOutcome,
    AdmissionRecords,
    DurableIdempotencyRecord,
    DurableRunRecord,
    DurableSessionOperation,
    DurableSessionRecord,
    IdempotencyConflictError,
    OwnerContext,
    OwnerPartition,
    PreLaunchCancelDisposition,
    PreLaunchCancelOutcome,
    ProvisionSubmitOutcome,
    SessionOperationFence,
    SessionRowNotFoundError,
    SessionStateStore,
    SessionStateStoreError,
    StaleOperationTokenError,
    mint_run_id,
    mint_session_id,
    owner_idempotency_expiry,
    owner_partition,
)
from ..transport.transport_models import (
    SANDBOX_GROUP_AUTHORIZATION_MESSAGE,
    SandboxFileNotFoundError,
    SandboxFileOperationError,
    SandboxGroupAuthorizationError,
    SandboxGroupBindingError,
    SandboxGroupTransientError,
    SandboxInvalidStateError,
    SandboxNotFoundError,
    SandboxTransportError,
)
from .backend import (
    SESSION_TOMBSTONED_ERROR_CODE,
    AgentExecutionBackend,
    DurableAdmissionIndeterminateError,
    DurableAdmissionOutcome,
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
    RunControlTimeoutError,
    RunEnvelope,
    RunJournalProtocolError,
    RunSubmissionDefinitiveFailureError,
    RunSubmissionIndeterminateError,
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
_HANDLE_PROVISIONING_PHASES = _PRELAUNCH_SUBMIT_PHASES | frozenset(
    {"provision_reconcile", "submit_admission", "submit_disarm"}
)
_ADMISSION_NOT_RESERVED: Final[AdmissionDisposition] = "not_reserved"
_ADMISSION_POSSIBLY_COMMITTED: Final[AdmissionDisposition] = "possibly_committed"
_PRELAUNCH_CANCEL_COMPLETE_DISPOSITIONS: Final[frozenset[PreLaunchCancelDisposition]] = frozenset(
    {"canceled_before_launch", "terminal"}
)
_PRELAUNCH_CANCEL_RETRY: Final[PreLaunchCancelDisposition] = "retry"

type _AdmissionOutcome = AdmissionOutcome | ProvisionSubmitOutcome


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
        attempt = build_idempotency_attempt(
            agent_slug=self._agent_name,
            prompt=request.prompt,
            timeout=request.timeout,
            idempotency_key=request.idempotency_key,
        )
        if request.session_id is not None:
            replay = await self._replay_start_request(
                request,
                partition,
                request.session_id,
                False,
                attempt,
                setup_budget,
            )
            if replay is not None:
                return replay
            await _within_setup_budget(
                self._runtime.reconcile_session(partition, request.session_id, setup_budget),
                setup_budget,
                phase=SetupPhase.PROVISION_RECONCILE,
            )
        try:
            return await self._start_run_once(
                request,
                partition,
                setup_budget,
                attempt,
                replay_checked=request.session_id is not None,
            )
        except ActiveRunConflictError:
            if request.session_id is None:
                raise
            await _within_setup_budget(
                self._runtime.reconcile_session(partition, request.session_id, setup_budget),
                setup_budget,
                phase=SetupPhase.PROVISION_RECONCILE,
            )
            try:
                return await self._start_run_once(request, partition, setup_budget, attempt)
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
        attempt: IdempotencyAttempt | None,
        *,
        replay_checked: bool = False,
    ) -> RunHandle:
        is_new_session = request.session_id is None
        session_id = request.session_id or mint_session_id()
        run_id = mint_run_id()
        if not replay_checked:
            replay = await self._replay_start_request(
                request,
                partition,
                session_id,
                is_new_session,
                attempt,
                setup_budget,
            )
            if replay is not None:
                return replay
        if not is_new_session:
            await self._ensure_existing_session_is_idle(
                partition,
                session_id,
                setup_budget,
            )
        async with self._runtime.hold_session(
            partition,
            session_id,
            setup_deadline=setup_budget,
        ):
            if is_new_session:
                return await self._start_provisioned_run(
                    request,
                    session_id,
                    run_id,
                    attempt,
                    setup_budget,
                )
            return await self._start_existing_session_run(
                request,
                session_id,
                run_id,
                attempt,
                setup_budget,
            )

    async def _replay_start_request(
        self,
        request: StartRunRequest,
        partition: OwnerPartition,
        session_id: str,
        is_new_session: bool,
        attempt: IdempotencyAttempt | None,
        setup_budget: SetupBudget,
    ) -> RunHandle | None:
        if is_new_session or attempt is None:
            return None
        replay = await _session_idempotency_replay(
            self._runtime,
            partition,
            session_id,
            attempt,
            setup_budget,
        )
        if replay is None:
            return None
        _ensure_replay_result_available(replay)
        return await self._resume_journal_submission(replay, request, setup_budget)

    async def _ensure_existing_session_is_idle(
        self,
        partition: OwnerPartition,
        session_id: str,
        setup_budget: SetupBudget,
    ) -> None:
        active_run_id = await _active_run_before_activation(
            self._runtime,
            partition,
            session_id,
            setup_budget,
        )
        if active_run_id is not None:
            durable = await self._read_durable_management_state(
                RunContext(run_id=active_run_id, session_id=session_id)
            )
            if durable.run.status in TERMINAL_RUN_STATUSES:
                raise LinkedActiveRunConflictError(
                    "session already has a settling run",
                    session_id=durable.session.session_id,
                    run_id=durable.run.run_id,
                    status=durable.run.status,
                    phase=_public_phase(durable),
                )
            raise ActiveRunConflictError(
                "session already has an active run",
                active_run_id=active_run_id,
            )

    async def _start_provisioned_run(
        self,
        request: StartRunRequest,
        session_id: str,
        run_id: str,
        attempt: IdempotencyAttempt | None,
        setup_budget: SetupBudget,
    ) -> RunHandle:
        activated: ActivatedSession | None = None
        outcome: ProvisionSubmitOutcome | None = None
        try:
            provisioned = await provision_new_session_submit(
                self._runtime,
                self._owner,
                session_id=session_id,
                run_id=run_id,
                timeout=request.timeout,
                attempt=attempt,
                setup_deadline=setup_budget,
            )
            outcome = provisioned.outcome
            if provisioned.setup_timed_out:
                assert provisioned.timeout_metadata is not None
                raise await self._durable_admission_timeout(
                    outcome,
                    provisioned.timeout_metadata,
                )
            if outcome.replayed:
                return await self._resume_replayed_provision(
                    provisioned.activated,
                    outcome.run,
                    request,
                    setup_budget,
                )
            activated = provisioned.activated
            if activated is None:
                return await self._run_handle_from_durable_evidence(outcome.run)
            return await self._submit_admitted_run(
                activated,
                outcome.run,
                request,
                setup_budget,
            )
        except DurableAdmissionSetupTimeoutError:
            raise
        except (SessionActivationSetupTimeoutError, SetupBudgetExpiredError) as exc:
            if outcome is None:
                raise
            raise await self._durable_admission_timeout(outcome, exc.metadata) from None
        finally:
            if activated is not None:
                await activated.handle.close()

    async def _resume_replayed_provision(
        self,
        activated: ActivatedSession | None,
        run: DurableRunRecord,
        request: StartRunRequest,
        setup_budget: SetupBudget,
    ) -> RunHandle:
        _ensure_replay_result_available(run)
        if activated is not None:
            await activated.handle.close()
        if activated is None and run.status not in TERMINAL_RUN_STATUSES:
            return await self._run_handle_from_durable_evidence(run)
        return await self._resume_journal_submission(run, request, setup_budget)

    async def _start_existing_session_run(
        self,
        request: StartRunRequest,
        session_id: str,
        run_id: str,
        attempt: IdempotencyAttempt | None,
        setup_budget: SetupBudget,
    ) -> RunHandle:
        activated = await activate_session(
            self._runtime,
            self._owner,
            session_id,
            setup_budget,
            allow_create=False,
        )
        outcome: AdmissionOutcome | None = None
        try:
            prepared, fence, outcome = await self._admit_existing_session_run(
                activated,
                run_id,
                request,
                attempt,
                setup_budget,
            )
            if outcome.replayed:
                await abort_submit_operation(self._runtime, prepared, fence)
                _ensure_replay_result_available(outcome.run)
                return await self._run_handle_from_durable_evidence(outcome.run)
            current = await _within_setup_budget(
                prepared.store.get_session(
                    prepared.partition,
                    prepared.session.session_id,
                ),
                setup_budget,
                phase=SetupPhase.STATE_STORE,
            )
            submitted = ActivatedSession.create(
                handle=prepared.handle,
                session=current.record,
                etag=outcome.session_etag or current.etag,
                partition=prepared.partition,
                store=prepared.store,
            )
            return await self._submit_admitted_run(
                submitted,
                outcome.run,
                request,
                setup_budget,
            )
        except DurableAdmissionSetupTimeoutError:
            raise
        except (SessionActivationSetupTimeoutError, SetupBudgetExpiredError) as exc:
            if outcome is None:
                raise
            raise await self._durable_admission_timeout(outcome, exc.metadata) from None
        finally:
            await activated.handle.close()

    async def _admit_existing_session_run(
        self,
        activated: ActivatedSession,
        run_id: str,
        request: StartRunRequest,
        attempt: IdempotencyAttempt | None,
        setup_budget: SetupBudget,
    ) -> tuple[ActivatedSession, SessionOperationFence, AdmissionOutcome]:
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
        records = AdmissionRecords.create(
            session_with_admitted_run(
                prepared.session,
                run_id,
                updated_at=run.updated_at,
            ),
            run,
            _idempotency_record(prepared, run, attempt),
        )
        try:
            outcome = await _await_admission_with_setup_timeout(
                lambda: prepared.store.admit_operation_run(
                    fence=fence,
                    records=records,
                ),
                setup_budget,
                lambda: _confirm_operation_admission_after_setup_timeout(
                    prepared.store,
                    fence,
                    records,
                ),
                phase=SetupPhase.SUBMIT_ADMISSION,
            )
        except Exception:
            await abort_submit_operation(self._runtime, prepared, fence)
            raise
        if _has_admission_disposition(outcome, _ADMISSION_NOT_RESERVED):
            await abort_submit_operation(self._runtime, prepared, fence)
            raise _not_reserved_admission_timeout(
                _admission_timeout_metadata(
                    setup_budget,
                    phase=SetupPhase.SUBMIT_ADMISSION,
                )
            )
        if _has_admission_disposition(outcome, _ADMISSION_POSSIBLY_COMMITTED):
            raise await self._durable_admission_timeout(
                outcome,
                _admission_timeout_metadata(
                    setup_budget,
                    phase=SetupPhase.SUBMIT_ADMISSION,
                    reason=SetupTimeoutReason.PROVISION_INDETERMINATE,
                ),
            )
        return prepared, fence, outcome

    async def _submit_admitted_run(
        self,
        activated: ActivatedSession,
        run: DurableRunRecord,
        request: StartRunRequest,
        setup_budget: SetupBudget,
    ) -> RunHandle:
        await _within_setup_budget(
            revalidate_before_submit(activated, run),
            setup_budget,
            phase=SetupPhase.PRE_SUBMIT_VALIDATION,
        )
        try:
            status = await self._submit_fenced_journal(
                activated,
                run,
                request,
                setup_budget,
            )
        except RunSubmissionDefinitiveFailureError:
            await _adopt_failed_submission(self._runtime, activated, run)
            raise
        except RunSubmissionIndeterminateError as exc:
            logger.warning(
                "Indeterminate journal acceptance after committed admission; "
                "deferring to reconciliation (stage=submit_admission, "
                "reason=launch_indeterminate)",
            )
            raise DurableAdmissionIndeterminateError(
                handle=_run_handle(run, phase="executing"),
            ) from exc
        await _adopt_if_terminal(
            self._runtime,
            activated,
            run,
            validate_terminal_output(self._binding, status),
        )
        return await self._run_handle_from_durable_evidence(run)

    async def _run_handle_from_durable_evidence(
        self,
        run: DurableRunRecord,
    ) -> RunHandle:
        durable = await self._read_durable_management_state(
            RunContext(run_id=run.run_id, session_id=run.session_id)
        )
        if not _matches_run_evidence(run, durable.run):
            raise SessionStateStoreError("Durable run evidence does not match the requested run.")
        return _run_handle(durable.run, phase=_run_handle_phase(durable))

    async def _durable_admission_timeout(
        self,
        outcome: _AdmissionOutcome,
        metadata: SetupTimeoutMetadata,
    ) -> DurableAdmissionSetupTimeoutError:
        admission = outcome.admission
        if not _is_durable_admission_disposition(admission):
            raise ValueError("unreserved admissions do not have a durable timeout handle")
        return DurableAdmissionSetupTimeoutError(
            outcome=admission,
            handle=await self._admission_run_handle(outcome),
            metadata=metadata,
        )

    async def _admission_run_handle(self, outcome: _AdmissionOutcome) -> RunHandle:
        fallback = _admission_fallback_handle(outcome)
        try:
            return await _bounded_admission_confirmation(
                self._run_handle_from_durable_evidence(outcome.run),
                fallback,
            )
        except SessionStateStoreError:
            return fallback

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
            except SandboxFileOperationError as exc:
                if exc.status_code in {401, 403}:
                    raise SessionActivationAuthorizationError(
                        SANDBOX_GROUP_AUTHORIZATION_MESSAGE,
                        status_code=exc.status_code,
                    ) from None
                raise RunSubmissionIndeterminateError(
                    "Existing run state could not be confirmed after journal claim."
                ) from exc
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
            return await self._run_handle_from_durable_evidence(run)
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
            return await self._run_handle_from_durable_evidence(run)
        finally:
            await activated.handle.close()

    async def get_run(self, context: RunContext) -> RunStatus:
        """Read durable management state before using a live journal."""
        durable = await self._read_durable_management_state(context)
        durable_status = _management_status(durable)
        if durable_status is not None:
            return durable_status
        return await self._get_live_run(context, durable)

    async def _get_live_run(
        self,
        context: RunContext,
        durable: _DurableManagementState,
    ) -> RunStatus:
        phase = _public_phase(durable)
        activated, fallback = await self._activate_for_get_run(context, durable, phase)
        if fallback is not None:
            return fallback
        assert activated is not None
        try:
            return await self._read_live_run_status(context, durable, activated)
        except RunJournalProtocolError:
            corrupted = await self._handle_runtime_journal_corruption(activated, context)
            return journal_corruption_status(corrupted)
        except SandboxFileNotFoundError:
            return _durable_status(durable.run, phase=phase)
        except SandboxFileOperationError as exc:
            if _is_launching_submission(durable):
                return _durable_status(durable.run, phase=phase)
            _raise_file_operation_activation_error(exc)
        finally:
            await activated.handle.close()

    async def _activate_for_get_run(
        self,
        context: RunContext,
        durable: _DurableManagementState,
        phase: RunPhase | None,
    ) -> tuple[ActivatedSession | None, RunStatus | None]:
        try:
            return (
                await activate_session(
                    self._runtime,
                    self._owner,
                    context.session_id,
                    SetupBudget.start(),
                    allow_create=False,
                ),
                None,
            )
        except SessionActivationGoneError:
            await self._runtime.reconcile_session(durable.partition, context.session_id)
            refreshed = await self._read_durable_management_state(context)
            return None, _tombstoned_status(refreshed.run, phase=_public_phase(refreshed))
        except (
            SessionActivationAuthorizationError,
            SessionActivationBindingError,
            SessionActivationConflictError,
            SessionActivationTransientError,
        ):
            raise
        except (
            SessionActivationError,
            SetupBudgetExpiredError,
            SandboxFileNotFoundError,
            SandboxFileOperationError,
        ):
            return None, _durable_status(durable.run, phase=phase)

    async def _read_live_run_status(
        self,
        context: RunContext,
        durable: _DurableManagementState,
        activated: ActivatedSession,
    ) -> RunStatus:
        status = await self._run_control.get_status(activated.handle, context)
        status = validate_terminal_output(self._binding, status)
        adopted = await _adopt_if_terminal(
            self._runtime,
            activated,
            durable.run,
            status,
        )
        if _is_unavailable_adopted_success(adopted):
            refreshed = await self._read_durable_management_state(context)
            return _durable_status(adopted, phase=_public_phase(refreshed))
        if status.state in TERMINAL_RUN_STATUSES:
            refreshed = await self._read_durable_management_state(context)
            return _with_public_phase(status, _public_phase(refreshed))
        reconciled = await self._reconciled_terminal_status(context, durable)
        if reconciled is not None:
            return reconciled
        return _with_public_phase(status, _live_public_phase(durable, status))

    async def _reconciled_terminal_status(
        self,
        context: RunContext,
        durable: _DurableManagementState,
    ) -> RunStatus | None:
        await self._runtime.reconcile_session(durable.partition, context.session_id)
        refreshed = await self._read_durable_management_state(context)
        if refreshed.run.status not in TERMINAL_RUN_STATUSES:
            return None
        return _durable_status(refreshed.run, phase=_public_phase(refreshed))

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
            except (
                SessionActivationAuthorizationError,
                SessionActivationBindingError,
                SessionActivationConflictError,
                SessionActivationTransientError,
            ):
                # Let event preflight surface the same structured management
                # response rather than silently ending through the fallback below.
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
            except SandboxFileNotFoundError:
                return
            except SandboxFileOperationError as exc:
                if _is_launching_submission(durable):
                    return
                _raise_file_operation_activation_error(exc)
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
            durable, canceled = await self._cancel_submit_candidate(context, durable)
            if canceled is not None:
                return canceled
        return await self._cancel_live_run(context, durable)

    async def _cancel_submit_candidate(
        self,
        context: RunContext,
        durable: _DurableManagementState,
    ) -> tuple[_DurableManagementState, RunStatus | None]:
        outcome = await _cancel_prelaunch_submit(durable, context)
        durable = _management_state_from_cancel_outcome(durable, outcome)
        if _is_completed_prelaunch_cancel(outcome):
            return durable, _durable_status(durable.run, phase=_public_phase(durable))
        if _should_refresh_prelaunch_cancel(outcome):
            durable = await self._read_durable_management_state(context)
            if _is_prelaunch_submission(durable):
                return durable, _durable_status(durable.run, phase=_public_phase(durable))
        if durable.run.status in TERMINAL_RUN_STATUSES:
            return durable, _durable_status(durable.run, phase=_public_phase(durable))
        return durable, None

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
                return await self._cancel_live_run_once(context, durable, setup_budget)
            except SessionActivationGoneError:
                raise
            except (
                SessionActivationAuthorizationError,
                SessionActivationBindingError,
                SessionActivationConflictError,
                SessionActivationNotFoundError,
                SessionActivationTransientError,
            ):
                raise
            except (RunControlTimeoutError, SandboxTransportError) as exc:
                durable, fallback_status = await self._cancel_transport_error_status(
                    context,
                    setup_budget,
                    exc,
                )
                if fallback_status is not None:
                    return fallback_status
            except (SessionActivationError, SetupBudgetExpiredError):
                return _durable_status(durable.run, phase=_public_phase(durable))

    async def _cancel_live_run_once(
        self,
        context: RunContext,
        durable: _DurableManagementState,
        setup_budget: SetupBudget,
    ) -> RunStatus:
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
                return await self._cancel_activated_run(
                    activated,
                    run_read.record,
                    context,
                    durable,
                )
            finally:
                await activated.handle.close()

    async def _cancel_activated_run(
        self,
        activated: ActivatedSession,
        run: DurableRunRecord,
        context: RunContext,
        durable: _DurableManagementState,
    ) -> RunStatus:
        try:
            status = await self._run_control.cancel(activated.handle, context)
        except RunJournalProtocolError:
            corrupted = await self._handle_runtime_journal_corruption(activated, context)
            return journal_corruption_status(corrupted)
        validated = validate_terminal_output(self._binding, status)
        adopted = await _adopt_if_terminal(
            self._runtime,
            activated,
            run,
            validated,
        )
        if adopted is None:
            return _with_public_phase(validated, _live_public_phase(durable, validated))
        refreshed = await self._read_durable_management_state(context)
        return _cancel_terminal_projection(validated, adopted, refreshed)

    async def _cancel_file_error_status(
        self,
        context: RunContext,
        setup_budget: SetupBudget,
    ) -> tuple[_DurableManagementState, RunStatus | None]:
        durable = await self._read_durable_management_state(context)
        if durable.run.status in TERMINAL_RUN_STATUSES or not _is_launching_submission(durable):
            return durable, _durable_status(durable.run, phase=_public_phase(durable))
        try:
            await _within_setup_budget(
                asyncio.sleep(_CANCEL_JOURNAL_POLL_SECONDS),
                setup_budget,
                phase=SetupPhase.JOURNAL,
            )
        except (SessionActivationError, SetupBudgetExpiredError):
            return durable, _durable_status(durable.run, phase=_public_phase(durable))
        return durable, None

    async def _cancel_transport_error_status(
        self,
        context: RunContext,
        setup_budget: SetupBudget,
        error: RunControlTimeoutError | SandboxTransportError,
    ) -> tuple[_DurableManagementState, RunStatus | None]:
        if isinstance(error, SandboxFileOperationError) and error.status_code in {401, 403}:
            raise SessionActivationAuthorizationError(
                SANDBOX_GROUP_AUTHORIZATION_MESSAGE,
                status_code=error.status_code,
            ) from None
        if isinstance(error, SandboxGroupAuthorizationError):
            raise SessionActivationAuthorizationError(
                SANDBOX_GROUP_AUTHORIZATION_MESSAGE,
                status_code=error.status_code,
            ) from None
        if isinstance(error, SandboxGroupBindingError):
            raise SessionActivationBindingError(str(error)) from None
        if isinstance(error, SandboxGroupTransientError):
            raise SessionActivationTransientError(str(error)) from None
        if isinstance(error, SandboxInvalidStateError):
            raise SessionActivationConflictError(str(error)) from None
        if isinstance(error, SandboxNotFoundError):
            raise SessionActivationGoneError(
                "Session backing sandbox is unavailable."
            ) from None
        if isinstance(error, SandboxFileOperationError):
            durable = await self._read_durable_management_state(context)
            if (
                durable.run.status not in TERMINAL_RUN_STATUSES
                and not _is_launching_submission(durable)
            ):
                _raise_file_operation_activation_error(error)
            return await self._cancel_file_error_status(context, setup_budget)
        if isinstance(error, SandboxFileNotFoundError):
            return await self._cancel_file_error_status(context, setup_budget)
        refreshed = await self._read_durable_management_state(context)
        return refreshed, _durable_status(refreshed.run, phase=_public_phase(refreshed))


def _raise_file_operation_activation_error(error: SandboxFileOperationError) -> Never:
    """Project a live journal transport failure into the controller's typed boundary."""
    if error.status_code in {401, 403}:
        raise SessionActivationAuthorizationError(
            SANDBOX_GROUP_AUTHORIZATION_MESSAGE,
            status_code=error.status_code,
        ) from None
    if error.status_code == 409:
        raise SessionActivationConflictError(
            "Sandbox journal state does not permit this operation."
        ) from None
    if (
        error.status_code is None
        or error.status_code in {408, 429}
        or 500 <= error.status_code < 600
    ):
        raise SessionActivationTransientError(
            "Sandbox journal transport is temporarily unavailable."
        ) from None
    raise SessionActivationBindingError("Sandbox journal transport request was rejected.") from None


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


def _is_completed_prelaunch_cancel(outcome: PreLaunchCancelOutcome) -> bool:
    return outcome.disposition in _PRELAUNCH_CANCEL_COMPLETE_DISPOSITIONS


def _should_refresh_prelaunch_cancel(outcome: PreLaunchCancelOutcome) -> bool:
    return outcome.disposition == _PRELAUNCH_CANCEL_RETRY


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


def _management_status(durable: _DurableManagementState) -> RunStatus | None:
    phase = _public_phase(durable)
    if _is_tombstoned_session(durable.session):
        return _tombstoned_status(durable.run, phase=phase)
    if _is_prelaunch_submission(durable):
        return _durable_status(durable.run, phase=phase)
    if (
        durable.run.status in TERMINAL_RUN_STATUSES
        and (durable.run.status != "succeeded" or not durable.run.result_available)
    ):
        return _durable_status(durable.run, phase=phase)
    return None


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


def _run_handle_phase(durable: _DurableManagementState) -> RunPhase:
    phase = _public_phase(durable)
    if phase is not None:
        return phase
    if (
        durable.operation is not None
        and durable.operation.phase in _HANDLE_PROVISIONING_PHASES
    ):
        return "provisioning"
    return "executing"


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


def _matches_run_evidence(expected: DurableRunRecord, actual: DurableRunRecord) -> bool:
    return (
        expected.owner_partition == actual.owner_partition
        and expected.session_id == actual.session_id
        and expected.run_id == actual.run_id
        and expected.generation == actual.generation
    )


def _run_handle(run: DurableRunRecord, *, phase: RunPhase) -> RunHandle:
    return RunHandle(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.status,
        created_at=run.created_at,
        phase=phase,
    )


def _admission_fallback_handle(outcome: _AdmissionOutcome) -> RunHandle:
    phase: RunPhase = (
        "terminal" if outcome.run.status in TERMINAL_RUN_STATUSES else "provisioning"
    )
    return _run_handle(outcome.run, phase=phase)


def _has_admission_disposition(
    outcome: _AdmissionOutcome,
    disposition: AdmissionDisposition,
) -> bool:
    return outcome.admission == disposition


def _is_durable_admission_disposition(
    disposition: AdmissionDisposition,
) -> TypeIs[DurableAdmissionOutcome]:
    return disposition != _ADMISSION_NOT_RESERVED


def _is_unavailable_adopted_success(
    adopted: DurableRunRecord | None,
) -> TypeIs[DurableRunRecord]:
    return adopted is not None and adopted.status == "succeeded" and not adopted.result_available


def _cancel_terminal_projection(
    validated: RunStatus,
    adopted: DurableRunRecord,
    refreshed: _DurableManagementState,
) -> RunStatus:
    projection = _durable_status(
        adopted,
        phase=_public_phase(refreshed),
        error=validated.error,
    )
    if (
        adopted.status != "succeeded"
        or not adopted.result_available
        or validated.result is None
    ):
        return projection
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
        admission=_ADMISSION_POSSIBLY_COMMITTED,
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


def _idempotency_record(
    activated: ActivatedSession,
    run: DurableRunRecord,
    attempt: IdempotencyAttempt | None,
) -> DurableIdempotencyRecord | None:
    if attempt is None:
        return None
    return DurableIdempotencyRecord.create(
        owner_partition=activated.partition,
        session_id=run.session_id,
        idempotency_hash=attempt.key_hash,
        request_hash=attempt.request_hash,
        run_id=run.run_id,
        expires_at=owner_idempotency_expiry(
            activated.session.expires_at,
            run.expires_at,
            None,
            run.created_at,
        ),
        created_at=run.created_at,
    )


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
    outcome = await activated.store.adopt_terminal_run(
        terminal,
        minimum_session_expires_at=(
            terminal.updated_at + timedelta(seconds=RESULT_HOLD_SECONDS)
            if terminal.result_available
            else None
        ),
    )
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
