"""Session fencing, activity receipts, and human-input CAS ports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from .durable_loop_protocol import (
    CheckpointStateV1,
    DurableLoopFinalResultV1,
    DurableLoopRunStatus,
    DurableLoopStatusEnvelopeV1,
    DurableRunIdentityV1,
    HumanInputRequestV1,
    HumanInputResponseV1,
    HumanInputState,
    HumanResponseDisposition,
    WorkingContextV1,
)


class DurableLoopStateError(RuntimeError):
    """Base class for durable-loop coordination failures."""


class DurableLoopSessionBusyError(DurableLoopStateError):
    """The session already owns another active durable run."""


class DurableLoopIdempotencyConflictError(DurableLoopStateError):
    """An idempotency key was reused with different request content."""


class DurableLoopGenerationConflictError(DurableLoopStateError):
    """A stale run attempted to update or commit a session."""


class DurableLoopCancellationConflictError(DurableLoopStateError):
    """Cancellation won the authoritative race against final commit."""


class DurableLoopMixedModeError(DurableLoopStateError):
    """One session identifier was claimed by incompatible execution modes."""


class DurableLoopHumanInputConflictError(DurableLoopStateError):
    """A human-input request already accepted a different submission."""


class DurableLoopHumanInputGoneError(DurableLoopStateError):
    """A human-input request is consumed or terminal."""


class DurableLoopNotFoundError(DurableLoopStateError):
    """The requested durable run or human request does not exist."""


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """One new or deduplicated session admission."""

    identity: DurableRunIdentityV1
    replayed: bool


@dataclass(frozen=True, slots=True)
class HumanResponseAcceptance:
    """The authoritative answer acceptance outcome."""

    response: HumanInputResponseV1
    replayed: bool


@dataclass(frozen=True, slots=True)
class DurableLoopRunRecord:
    """Local adapter record mirroring the entity/CAS contract."""

    identity: DurableRunIdentityV1
    checkpoint: CheckpointStateV1
    created_at: datetime
    updated_at: datetime
    final_result: DurableLoopFinalResultV1 | None = None
    human_request: HumanInputRequestV1 | None = None
    human_response: HumanInputResponseV1 | None = None
    committed_generation: int | None = None


@runtime_checkable
class DurableLoopStatePort(Protocol):
    """Cross-instance state contract implemented by an entity or CAS store."""

    async def admit(
        self,
        identity: DurableRunIdentityV1,
        checkpoint: CheckpointStateV1,
    ) -> AdmissionResult:
        """Claim one active durable turn or return its idempotent replay."""

    async def get_run(self, run_id: str) -> DurableLoopRunRecord:
        """Read one run."""

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: CheckpointStateV1,
    ) -> None:
        """Persist a forward checkpoint for the active run."""

    async def request_cancel(self, run_id: str) -> CheckpointStateV1:
        """Persist cooperative cancellation intent."""

    async def commit(
        self,
        run_id: str,
        *,
        expected_generation: int,
        result: DurableLoopFinalResultV1,
    ) -> int:
        """Atomically publish a final result and release the session."""

    async def abort(
        self,
        run_id: str,
        *,
        result: DurableLoopFinalResultV1,
    ) -> None:
        """Terminalize without advancing committed session history."""

    async def put_human_request(self, request: HumanInputRequestV1) -> None:
        """Persist one pending clarification request."""

    async def accept_human_response(
        self,
        response: HumanInputResponseV1,
    ) -> HumanResponseAcceptance:
        """CAS the first valid response into the request record."""

    async def close_human_request(
        self,
        run_id: str,
        request_id: str,
        state: HumanInputState,
    ) -> HumanInputRequestV1:
        """Race timeout/cancellation against first-answer acceptance."""

    async def consume_human_response(
        self,
        run_id: str,
        request_id: str,
    ) -> HumanInputResponseV1:
        """Mark one accepted response consumed by the orchestration."""

    async def mark_human_response_orphaned(
        self,
        run_id: str,
        request_id: str,
    ) -> HumanInputResponseV1:
        """Record terminal event-delivery failure without losing the receipt."""

    async def status(
        self,
        run_id: str,
        *,
        retention_seconds: int,
    ) -> DurableLoopStatusEnvelopeV1:
        """Build the bounded polling projection."""

    async def get_committed_context(
        self,
        session_id: str,
    ) -> WorkingContextV1 | None:
        """Read the latest successfully committed session context."""

    async def get_session_generation(self, session_id: str) -> int:
        """Read the current committed generation for admission fencing."""


class InMemoryDurableLoopStateStore:
    """Deterministic local-test implementation of the entity/CAS port.

    The lock exists only to make this fake faithfully exercise CAS races in one
    process. Production registration uses the Durable Entity contract.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._runs: dict[str, DurableLoopRunRecord] = {}
        self._run_by_request: dict[tuple[str, str], str] = {}
        self._active_by_session: dict[str, str] = {}
        self._session_mode: dict[str, str] = {}
        self._session_generation: dict[str, int] = {}
        self._committed_context: dict[str, WorkingContextV1] = {}

    async def claim_session_mode(self, session_id: str, mode: str) -> None:
        """Reject durable/legacy reuse of one session identifier."""
        async with self._lock:
            existing = self._session_mode.get(session_id)
            if existing is not None and existing != mode:
                raise DurableLoopMixedModeError(
                    f"session {session_id!r} is already bound to {existing!r} mode"
                )
            self._session_mode[session_id] = mode

    async def admit(
        self,
        identity: DurableRunIdentityV1,
        checkpoint: CheckpointStateV1,
    ) -> AdmissionResult:
        async with self._lock:
            existing_mode = self._session_mode.get(identity.session_id)
            if existing_mode not in {None, "durable"}:
                raise DurableLoopMixedModeError(
                    f"session {identity.session_id!r} is already bound to legacy mode"
                )
            self._session_mode[identity.session_id] = "durable"

            request_key = (identity.session_id, identity.request_id_hash)
            existing_run_id = self._run_by_request.get(request_key)
            if existing_run_id is not None:
                existing = self._runs[existing_run_id]
                if existing.identity.request_hash != identity.request_hash:
                    raise DurableLoopIdempotencyConflictError(
                        "request ID was reused with different content"
                    )
                return AdmissionResult(identity=existing.identity, replayed=True)

            active_run_id = self._active_by_session.get(identity.session_id)
            if active_run_id is not None:
                raise DurableLoopSessionBusyError(
                    f"session {identity.session_id!r} already has an active run"
                )
            now = identity.created_at.astimezone(UTC)
            self._runs[identity.run_id] = DurableLoopRunRecord(
                identity=identity,
                checkpoint=checkpoint,
                created_at=now,
                updated_at=now,
            )
            self._run_by_request[request_key] = identity.run_id
            self._active_by_session[identity.session_id] = identity.run_id
            self._session_generation.setdefault(identity.session_id, 0)
            return AdmissionResult(identity=identity, replayed=False)

    async def get_run(self, run_id: str) -> DurableLoopRunRecord:
        async with self._lock:
            try:
                return self._runs[run_id]
            except KeyError:
                raise DurableLoopNotFoundError(f"run {run_id!r} was not found") from None

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: CheckpointStateV1,
    ) -> None:
        async with self._lock:
            current = self._require_active(run_id)
            if checkpoint.identity != current.identity:
                raise DurableLoopGenerationConflictError(
                    "checkpoint identity does not match the admitted run"
                )
            if checkpoint.committed_session_generation != self._session_generation[
                current.identity.session_id
            ]:
                raise DurableLoopGenerationConflictError(
                    "checkpoint session generation is stale"
                )
            if current.checkpoint.cancellation_requested:
                checkpoint = checkpoint.model_copy(
                    update={"cancellation_requested": True}
                )
            self._runs[run_id] = replace(
                current,
                checkpoint=checkpoint,
                updated_at=datetime.now(UTC),
            )

    async def request_cancel(self, run_id: str) -> CheckpointStateV1:
        async with self._lock:
            current = self._require_active(run_id)
            checkpoint = current.checkpoint.model_copy(
                update={"cancellation_requested": True}
            )
            self._runs[run_id] = replace(
                current,
                checkpoint=checkpoint,
                updated_at=datetime.now(UTC),
            )
            return checkpoint

    async def commit(
        self,
        run_id: str,
        *,
        expected_generation: int,
        result: DurableLoopFinalResultV1,
    ) -> int:
        async with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise DurableLoopNotFoundError(f"run {run_id!r} was not found")
            if current.final_result is not None:
                if current.final_result != result:
                    raise DurableLoopGenerationConflictError(
                        "commit retry does not match the recorded receipt"
                    )
                assert current.committed_generation is not None
                return current.committed_generation
            session_id = current.identity.session_id
            if self._active_by_session.get(session_id) != run_id:
                raise DurableLoopGenerationConflictError(
                    "run no longer owns the active session slot"
                )
            if self._session_generation[session_id] != expected_generation:
                raise DurableLoopGenerationConflictError(
                    "session generation does not match the commit fence"
                )
            if current.checkpoint.cancellation_requested:
                raise DurableLoopCancellationConflictError(
                    "cancellation won before final commit"
                )
            committed_generation = expected_generation + 1
            self._session_generation[session_id] = committed_generation
            self._active_by_session.pop(session_id, None)
            if result.status is DurableLoopRunStatus.COMPLETED:
                self._committed_context[session_id] = current.checkpoint.working_context
            self._runs[run_id] = replace(
                current,
                final_result=result.model_copy(
                    update={"committed_generation": committed_generation}
                ),
                committed_generation=committed_generation,
                updated_at=datetime.now(UTC),
            )
            return committed_generation

    async def abort(
        self,
        run_id: str,
        *,
        result: DurableLoopFinalResultV1,
    ) -> None:
        async with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise DurableLoopNotFoundError(f"run {run_id!r} was not found")
            if current.final_result is not None:
                if current.final_result != result:
                    raise DurableLoopGenerationConflictError(
                        "terminal retry does not match the recorded result"
                    )
                return
            session_id = current.identity.session_id
            if self._active_by_session.get(session_id) != run_id:
                raise DurableLoopGenerationConflictError(
                    "run no longer owns the active session slot"
                )
            self._active_by_session.pop(session_id, None)
            self._runs[run_id] = replace(
                current,
                final_result=result,
                updated_at=datetime.now(UTC),
            )

    async def put_human_request(self, request: HumanInputRequestV1) -> None:
        async with self._lock:
            current = self._require_active(request.run_id)
            existing = current.human_request
            if existing is not None:
                if existing != request:
                    raise DurableLoopHumanInputConflictError(
                        "another human-input request is already open"
                    )
                return
            self._runs[request.run_id] = replace(
                current,
                human_request=request,
                updated_at=datetime.now(UTC),
            )

    async def accept_human_response(
        self,
        response: HumanInputResponseV1,
    ) -> HumanResponseAcceptance:
        async with self._lock:
            current = self._require_run(response.run_id)
            request = current.human_request
            if request is None or request.request_id != response.request_id:
                raise DurableLoopNotFoundError("human-input request was not found")
            existing = current.human_response
            if existing is not None:
                if (
                    existing.submission_id_hash == response.submission_id_hash
                    and existing.body_hash == response.body_hash
                ):
                    return HumanResponseAcceptance(response=existing, replayed=True)
                raise DurableLoopHumanInputConflictError(
                    "human-input request already accepted a different response"
                )
            if request.state is not HumanInputState.PENDING:
                raise DurableLoopHumanInputGoneError(
                    f"human-input request is {request.state.value}"
                )
            answered = request.model_copy(
                update={
                    "state": HumanInputState.ANSWERED,
                    "record_version": request.record_version + 1,
                }
            )
            self._runs[response.run_id] = replace(
                current,
                human_request=answered,
                human_response=response,
                updated_at=datetime.now(UTC),
            )
            return HumanResponseAcceptance(response=response, replayed=False)

    async def close_human_request(
        self,
        run_id: str,
        request_id: str,
        state: HumanInputState,
    ) -> HumanInputRequestV1:
        if state not in {HumanInputState.TIMED_OUT, HumanInputState.CANCELLED}:
            raise ValueError("human request may close only as timed_out or cancelled")
        async with self._lock:
            current = self._require_run(run_id)
            request = current.human_request
            if request is None or request.request_id != request_id:
                raise DurableLoopNotFoundError("human-input request was not found")
            if request.state is HumanInputState.ANSWERED:
                return request
            if request.state is not HumanInputState.PENDING:
                return request
            closed = request.model_copy(
                update={
                    "state": state,
                    "record_version": request.record_version + 1,
                }
            )
            self._runs[run_id] = replace(
                current,
                human_request=closed,
                updated_at=datetime.now(UTC),
            )
            return closed

    async def consume_human_response(
        self,
        run_id: str,
        request_id: str,
    ) -> HumanInputResponseV1:
        async with self._lock:
            current = self._require_active(run_id)
            request = current.human_request
            response = current.human_response
            if (
                request is None
                or response is None
                or request.request_id != request_id
            ):
                raise DurableLoopNotFoundError("accepted human response was not found")
            if response.disposition is HumanResponseDisposition.ORPHANED:
                raise DurableLoopHumanInputGoneError("human response is orphaned")
            consumed = response.model_copy(
                update={"disposition": HumanResponseDisposition.CONSUMED}
            )
            self._runs[run_id] = replace(
                current,
                human_response=consumed,
                updated_at=datetime.now(UTC),
            )
            return consumed

    async def mark_human_response_orphaned(
        self,
        run_id: str,
        request_id: str,
    ) -> HumanInputResponseV1:
        async with self._lock:
            current = self._require_run(run_id)
            response = current.human_response
            if response is None or response.request_id != request_id:
                raise DurableLoopNotFoundError("accepted human response was not found")
            orphaned = response.model_copy(
                update={"disposition": HumanResponseDisposition.ORPHANED}
            )
            self._runs[run_id] = replace(
                current,
                human_response=orphaned,
                updated_at=datetime.now(UTC),
            )
            return orphaned

    async def status(self, run_id: str, *, retention_seconds: int) -> DurableLoopStatusEnvelopeV1:
        """Build the content-free polling projection."""
        current = await self.get_run(run_id)
        checkpoint = current.checkpoint
        return DurableLoopStatusEnvelopeV1(
            run_id=current.identity.run_id,
            session_id=current.identity.session_id,
            status=(
                current.final_result.status
                if current.final_result is not None
                else checkpoint.status
            ),
            phase=_phase_for_checkpoint(checkpoint, current.final_result),
            created_at=current.created_at,
            updated_at=current.updated_at,
            model_steps=checkpoint.completed_model_steps,
            tool_calls=checkpoint.completed_tool_calls,
            human_waits=checkpoint.human_wait_count,
            step_index=checkpoint.next_model_step,
            input_tokens=checkpoint.input_tokens,
            output_tokens=checkpoint.output_tokens,
            reasoning_tokens=checkpoint.reasoning_tokens,
            cost_microunits=checkpoint.cost_microunits,
            external_content_bytes=checkpoint.external_content_bytes,
            parked_seconds=checkpoint.parked_seconds,
            pending_human_request_id=checkpoint.pending_human_request_id,
            error=(
                current.final_result.error
                if current.final_result is not None
                else checkpoint.last_error
            ),
            result_available=(
                current.final_result is not None
                and current.final_result.status is DurableLoopRunStatus.COMPLETED
            ),
            expires_at=current.updated_at + timedelta(seconds=retention_seconds),
        )

    async def get_committed_context(
        self,
        session_id: str,
    ) -> WorkingContextV1 | None:
        """Read the latest successful context without exposing failed attempts."""
        async with self._lock:
            return self._committed_context.get(session_id)

    async def get_session_generation(self, session_id: str) -> int:
        """Read the current generation; new sessions start at generation zero."""
        async with self._lock:
            return self._session_generation.get(session_id, 0)

    def _require_run(self, run_id: str) -> DurableLoopRunRecord:
        try:
            return self._runs[run_id]
        except KeyError:
            raise DurableLoopNotFoundError(f"run {run_id!r} was not found") from None

    def _require_active(self, run_id: str) -> DurableLoopRunRecord:
        current = self._require_run(run_id)
        if self._active_by_session.get(current.identity.session_id) != run_id:
            raise DurableLoopGenerationConflictError("run does not own the session")
        return current


class InMemoryActivityJournal:
    """Durable-history simulator keyed by deterministic operation and request hash."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._results: dict[str, tuple[str, object]] = {}
        self._inflight: dict[str, tuple[str, asyncio.Future[object]]] = {}
        self.executions: dict[str, int] = {}

    async def execute_once[ResultT](
        self,
        operation_key: str,
        request_hash: str,
        operation: ProtocolOperation[ResultT],
    ) -> ResultT:
        """Execute once, replay the receipt, and reject key/hash corruption."""
        async with self._lock:
            recorded = self._results.get(operation_key)
            if recorded is not None:
                recorded_hash, result = recorded
                if recorded_hash != request_hash:
                    raise DurableLoopIdempotencyConflictError(
                        "operation key was reused with a different request hash"
                    )
                return result  # type: ignore[return-value]
            inflight = self._inflight.get(operation_key)
            if inflight is not None:
                inflight_hash, future = inflight
                if inflight_hash != request_hash:
                    raise DurableLoopIdempotencyConflictError(
                        "operation key was reused with a different request hash"
                    )
                waiter = future
                creator = False
            else:
                waiter = asyncio.get_running_loop().create_future()
                waiter.add_done_callback(_consume_future_exception)
                self._inflight[operation_key] = (request_hash, waiter)
                creator = True
        if not creator:
            return await waiter  # type: ignore[return-value]
        try:
            result = await operation()
        except BaseException as exc:
            async with self._lock:
                self._inflight.pop(operation_key, None)
                if not waiter.done():
                    waiter.set_exception(exc)
            raise
        async with self._lock:
            self._results[operation_key] = (request_hash, result)
            self.executions[operation_key] = self.executions.get(operation_key, 0) + 1
            self._inflight.pop(operation_key, None)
            if not waiter.done():
                waiter.set_result(result)
        return result


@runtime_checkable
class ProtocolOperation[ResultT](Protocol):
    """One async operation accepted by the local activity journal."""

    async def __call__(self) -> ResultT:
        """Execute the operation."""


def _consume_future_exception(future: asyncio.Future[object]) -> None:
    if not future.cancelled():
        future.exception()


def _phase_for_checkpoint(
    checkpoint: CheckpointStateV1,
    result: DurableLoopFinalResultV1 | None,
) -> str:
    if result is not None:
        return "terminal"
    if checkpoint.pending_human_request_id is not None:
        return "human_wait"
    if checkpoint.cancellation_requested:
        return "cancellation"
    return "model_step"
