"""ACA Sandbox implementation of the four-method execution lifecycle seam."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..config import DEFAULT_TIMEOUT
from ..controller.readiness import (
    ActivatedSession,
    SessionActivationError,
    SessionRuntimeBinding,
    activate_session,
    revalidate_before_submit,
    session_with_admitted_run,
    terminal_run,
)
from ..session_state import (
    AdmissionRecords,
    DurableRunRecord,
    DurableSessionRecord,
    OwnerContext,
    mint_run_id,
    mint_session_id,
    owner_partition,
)
from .backend import (
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
from .run_control import RunEnvelope, SandboxRunControl
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
    ) -> None:
        agent_name = binding.agent_name
        if not agent_name:
            raise ValueError("ACA Sandbox execution requires an agent identity slug")
        self._binding = binding
        self._agent_name = agent_name
        self._runtime = runtime
        self._owner = owner
        self._run_control = run_control or SandboxRunControl()

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        """Activate the session, atomically admit one run, and submit its envelope."""
        session_id = request.session_id or mint_session_id()
        run_id = mint_run_id()
        setup_budget = SetupBudget.start()
        async with self._runtime.hold_session(session_id):
            activated = await activate_session(
                self._runtime,
                self._owner,
                session_id,
                setup_budget,
                allow_create=request.session_id is None,
            )
            try:
                run = _new_run(activated.session, run_id, timeout=request.timeout)
                admitted_session = session_with_admitted_run(
                    activated.session,
                    run_id,
                    updated_at=run.updated_at,
                )
                outcome = await activated.store.admit_run(
                    AdmissionRecords.create(admitted_session, run),
                    expected_session_etag=activated.etag,
                )
                if outcome.replayed:
                    return RunHandle(
                        run_id=outcome.run.run_id,
                        session_id=outcome.run.session_id,
                        state=outcome.run.status,
                        created_at=outcome.run.created_at,
                    )

                submitted = False
                try:
                    await revalidate_before_submit(activated, outcome.run)
                    status = await self._run_control.submit(
                        activated.handle,
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
                    submitted = True
                except Exception:
                    if not submitted:
                        await _adopt_failed_submission(activated, outcome.run)
                    raise
                await _adopt_if_terminal(activated, outcome.run, status)
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
        try:
            activated = await activate_session(
                self._runtime,
                self._owner,
                context.session_id,
                SetupBudget.start(),
                allow_create=False,
            )
        except SessionActivationError:
            return _durable_status(run_read.record)
        try:
            status = await self._run_control.get_status(activated.handle, context)
            await _adopt_if_terminal(activated, run_read.record, status)
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
            finally:
                await activated.handle.close()

        return stream()

    async def cancel_run(self, context: RunContext) -> RunStatus:
        """Serialize cancellation behind activation so the current process is signaled."""
        async with self._runtime.hold_session(context.session_id):
            activated = await activate_session(
                self._runtime,
                self._owner,
                context.session_id,
                SetupBudget.start(),
                allow_create=False,
            )
            try:
                run_read = await activated.store.get_run(
                    activated.partition,
                    context.session_id,
                    context.run_id,
                )
                status = await self._run_control.cancel(activated.handle, context)
                await _adopt_if_terminal(activated, run_read.record, status)
                return status
            finally:
                await activated.handle.close()


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


async def _adopt_if_terminal(
    activated: ActivatedSession,
    run: DurableRunRecord,
    status: RunStatus,
) -> None:
    terminal = _terminal_record(run, status)
    if terminal is None:
        return
    await activated.store.adopt_terminal_run(terminal)


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
            reason="sandbox_failed",
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


def _durable_status(run: DurableRunRecord) -> RunStatus:
    error = _durable_error(run.status)
    return RunStatus(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.status,
        last_sequence=0,
        result_available=run.result_available,
        result=None,
        error=error,
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
