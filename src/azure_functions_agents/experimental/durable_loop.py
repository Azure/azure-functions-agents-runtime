"""Explicit adaptive model/tool loop with replayable local foundation semantics."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..strict_json import assert_json_value, canonical_json_bytes
from .durable_loop_activities import (
    ContextRehydrator,
    DeterministicContextCompactor,
    DeterministicContextRehydrator,
    DurableContentStore,
    DurableLoopContextOverflowError,
    DurableLoopModelError,
    DurableLoopProviderTerminalError,
    ExactBindingResumeValidator,
    OneStepModelProvider,
    OneStepModelRequest,
    ResumeContextDisposition,
    ResumeContextValidator,
    append_assistant_message,
    append_audit_assistant,
    append_audit_final,
    append_audit_model_and_tool,
    append_audit_tool_results,
    append_final_message,
    append_model_and_tool_messages,
    append_tool_result_messages,
)
from .durable_loop_config import DurableLoopSettings
from .durable_loop_observability import (
    DurableLoopOutcome,
    DurableLoopPhase,
    DurableLoopTimer,
    record_durable_loop_event,
)
from .durable_loop_protocol import (
    MAX_HUMAN_ANSWER_BYTES,
    MAX_HUMAN_CHOICE_CHARS,
    MAX_HUMAN_CHOICES,
    MAX_HUMAN_QUESTION_BYTES,
    CheckpointStateV1,
    ContentRefV1,
    DurableLoopBudgetV1,
    DurableLoopFinalResultV1,
    DurableLoopRunStatus,
    DurableLoopStatusEnvelopeV1,
    DurableRunIdentityV1,
    ErrorDisposition,
    ErrorEnvelopeV1,
    FrozenToolCatalogV1,
    FrozenToolDescriptorV1,
    HumanInputContentV1,
    HumanInputRequestV1,
    HumanInputResponseV1,
    HumanInputState,
    HumanResponseDisposition,
    MAFMessageBundleV1,
    ModelDecisionEnvelopeV1,
    ModelToolCallV1,
    ToolBehavior,
    ToolProvenance,
    ToolRequestV1,
    ToolResultStatus,
    ToolResultV1,
    UsageV1,
    WorkingContextV1,
    canonical_hash,
    deterministic_call_key,
    deterministic_human_event_name,
    deterministic_model_step_key,
    tool_request_hash,
    validate_human_response_schema,
    validate_human_response_value,
)
from .durable_loop_state import (
    AdmissionResult,
    DurableLoopCancellationConflictError,
    DurableLoopNotFoundError,
    DurableLoopRunRecord,
    DurableLoopStatePort,
    HumanResponseAcceptance,
    InMemoryActivityJournal,
)
from .durable_loop_tools import (
    REQUEST_HUMAN_INPUT_TOOL_NAME,
    DurableToolDispatchPort,
)


class DurableLoopExecutionError(RuntimeError):
    """The local durable-loop runner cannot safely continue."""


class HumanEventDeliveryDisposition(StrEnum):
    """The outbox delivery result for one accepted answer."""

    DELIVERED = "delivered"
    RETRY_PENDING = "retry_pending"
    RUN_NOT_FOUND = "run_not_found"
    RUN_TERMINAL = "run_terminal"


@runtime_checkable
class HumanEventPublisher(Protocol):
    """Durable-client seam used only after the answer fact commits."""

    async def raise_event(
        self,
        *,
        run_id: str,
        event_name: str,
        payload: Mapping[str, object],
    ) -> HumanEventDeliveryDisposition:
        """Raise one wake-up hint for an already-authoritative answer."""


@runtime_checkable
class DurableLoopFaultInjector(Protocol):
    """Optional deterministic fault hooks for replay qualification."""

    async def reach(self, point: str, run_id: str) -> None:
        """Raise at a named boundary when the test requests it."""


class NoopDurableLoopFaultInjector:
    """Default fault injector."""

    async def reach(self, point: str, run_id: str) -> None:
        del point, run_id


class InMemoryHumanEventPublisher:
    """Local event publisher that records early/duplicate delivery."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self.next_disposition = HumanEventDeliveryDisposition.DELIVERED

    async def raise_event(
        self,
        *,
        run_id: str,
        event_name: str,
        payload: Mapping[str, object],
    ) -> HumanEventDeliveryDisposition:
        disposition = self.next_disposition
        if disposition is HumanEventDeliveryDisposition.DELIVERED:
            self.events.append((run_id, event_name, dict(payload)))
        return disposition


@dataclass(frozen=True, slots=True)
class DurableLoopPlan:
    """Immutable local execution inputs carried by production orchestration."""

    instructions: str
    catalog: FrozenToolCatalogV1
    model_settings: Mapping[str, object]
    maf_core_version: str
    provider: str
    model: str
    api_version: str
    settings: DurableLoopSettings

    def validate(self) -> None:
        """Reject unbounded or non-JSON model inputs before admission."""
        if len(self.instructions.encode("utf-8")) > 64 * 1024:
            raise DurableLoopExecutionError("instructions exceed the byte limit")
        assert_json_value(self.model_settings)
        if len(canonical_json_bytes(self.model_settings)) > 32 * 1024:
            raise DurableLoopExecutionError("model settings exceed the byte limit")
        self.settings.validate()


@dataclass(frozen=True, slots=True)
class DurableLoopExecutionResult:
    """Current run projection plus optional authorized content."""

    status: DurableLoopStatusEnvelopeV1
    final_result: DurableLoopFinalResultV1 | None = None
    human_input: HumanInputContentV1 | None = None


@dataclass(frozen=True, slots=True)
class HumanSubmissionResult:
    """First-answer receipt and outbox delivery outcome."""

    acceptance: HumanResponseAcceptance
    delivery: HumanEventDeliveryDisposition


class DurableLoopRunner:
    """Locally executable implementation of the durable orchestration semantics."""

    def __init__(
        self,
        *,
        state: DurableLoopStatePort,
        content: DurableContentStore,
        model: OneStepModelProvider,
        tools: DurableToolDispatchPort,
        activity_journal: InMemoryActivityJournal | None = None,
        compactor: DeterministicContextCompactor | None = None,
        resume_validator: ResumeContextValidator | None = None,
        rehydrator: ContextRehydrator | None = None,
        event_publisher: HumanEventPublisher | None = None,
        fault_injector: DurableLoopFaultInjector | None = None,
        clock: Callable[[], datetime] | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self._state = state
        self._content = content
        self._model = model
        self._tools = tools
        self._journal = activity_journal or InMemoryActivityJournal()
        self._compactor = compactor or DeterministicContextCompactor()
        self._resume_validator = resume_validator or ExactBindingResumeValidator()
        self._rehydrator = rehydrator or DeterministicContextRehydrator()
        self._event_publisher = event_publisher or InMemoryHumanEventPublisher()
        self._faults = fault_injector or NoopDurableLoopFaultInjector()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._nonce_factory = nonce_factory or (lambda: uuid.uuid4().hex)
        self._plans: dict[str, DurableLoopPlan] = {}

    async def start(
        self,
        identity: DurableRunIdentityV1,
        plan: DurableLoopPlan,
        messages: Sequence[dict[str, object]],
    ) -> DurableLoopExecutionResult:
        """Admit or deduplicate one run, then execute to a quiescent boundary."""
        plan.validate()
        self._validate_identity_budget(identity, plan.settings)
        committed = await self._state.get_committed_context(identity.session_id)
        context = _initial_working_context(
            plan,
            _messages_with_committed_context(
                committed,
                tuple(messages),
                plan,
            ),
        )
        committed_generation = await self._state.get_session_generation(
            identity.session_id
        )
        checkpoint = CheckpointStateV1(
            identity=identity,
            status=DurableLoopRunStatus.PENDING,
            audit_bundle=context.bundle,
            working_context=context,
            audit_head_hash=context.source_audit_hash,
            completed_model_steps=0,
            completed_tool_calls=0,
            human_wait_count=0,
            next_model_step=0,
            committed_session_generation=committed_generation,
            continue_as_new_generation=0,
            checkpoints_in_generation=0,
        )
        admission: AdmissionResult = await self._state.admit(identity, checkpoint)
        self._plans[admission.identity.run_id] = plan
        if admission.replayed:
            return await self.get(admission.identity.run_id)
        record_durable_loop_event(
            DurableLoopPhase.RUN,
            DurableLoopOutcome.STARTED,
        )
        return await self._drive(identity.run_id, plan)

    async def resume(self, run_id: str) -> DurableLoopExecutionResult:
        """Resume a locally held plan after an event, retry, or worker restart."""
        try:
            plan = self._plans[run_id]
        except KeyError:
            raise DurableLoopExecutionError(
                "local runner does not hold the immutable plan for this run"
            ) from None
        return await self._drive(run_id, plan)

    async def get(self, run_id: str) -> DurableLoopExecutionResult:
        """Return content-free status plus authorized result/request content."""
        try:
            plan = self._plans[run_id]
        except KeyError:
            raise DurableLoopNotFoundError(f"run {run_id!r} was not found") from None
        record = await self._state.get_run(run_id)
        status = await self._status(run_id, plan)
        human_input = None
        if (
            record.human_request is not None
            and record.human_request.state is HumanInputState.PENDING
        ):
            human_input = await self._human_content(record.human_request)
        return DurableLoopExecutionResult(
            status=status,
            final_result=record.final_result,
            human_input=human_input,
        )

    async def cancel(self, run_id: str) -> DurableLoopExecutionResult:
        """Persist cancellation intent and drive the run to terminal state."""
        await self._state.request_cancel(run_id)
        return await self.resume(run_id)

    async def timeout_human_input(
        self,
        *,
        run_id: str,
        request_id: str,
    ) -> DurableLoopExecutionResult:
        """Close one pending request by CAS and continue from the winner."""
        await self._state.close_human_request(
            run_id,
            request_id,
            HumanInputState.TIMED_OUT,
        )
        return await self.resume(run_id)

    async def submit_human_input(
        self,
        *,
        run_id: str,
        request_id: str,
        submission_id: str,
        actor_hash: str,
        answer: object,
    ) -> HumanSubmissionResult:
        """Validate and persist the first answer before raising its wake-up event."""
        record = await self._state.get_run(run_id)
        request = record.human_request
        if request is None or request.request_id != request_id:
            raise DurableLoopNotFoundError("human-input request was not found")
        if actor_hash != request.actor_policy_hash:
            raise PermissionError("human-input actor is not authorized")
        _validate_human_answer(request, answer)
        answer_ref = await self._put_json(
            kind="human-answer",
            value=answer,
            retention_class="run",
        )
        now = self._clock()
        response = HumanInputResponseV1(
            request_id=request.request_id,
            run_id=request.run_id,
            session_id=request.session_id,
            generation=request.generation,
            call_id=request.call_id,
            submission_id_hash=canonical_hash({"submission_id": submission_id}),
            body_hash=canonical_hash({"answer": answer}),
            actor_hash=actor_hash,
            answer_ref=answer_ref,
            accepted_at=now,
            schema_valid=True,
            disposition=HumanResponseDisposition.ACCEPTED,
        )
        acceptance = await self._state.accept_human_response(response)
        delivery = await self._deliver_human_response(request, acceptance.response)
        return HumanSubmissionResult(acceptance=acceptance, delivery=delivery)

    async def retry_human_delivery(
        self,
        *,
        run_id: str,
        request_id: str,
    ) -> HumanEventDeliveryDisposition:
        """Retry the outbox side of an already-accepted response."""
        record = await self._state.get_run(run_id)
        request = record.human_request
        response = record.human_response
        if (
            request is None
            or response is None
            or request.request_id != request_id
        ):
            raise DurableLoopNotFoundError("accepted human response was not found")
        return await self._deliver_human_response(request, response)

    async def _drive(  # noqa: PLR0912, PLR0915
        self,
        run_id: str,
        plan: DurableLoopPlan,
    ) -> DurableLoopExecutionResult:
        while True:
            record = await self._state.get_run(run_id)
            if record.final_result is not None:
                return await self.get(run_id)
            checkpoint = record.checkpoint
            if checkpoint.cancellation_requested:
                await self._cancel_open_human_request(record)
                await self._terminalize(
                    checkpoint,
                    DurableLoopRunStatus.CANCELLED,
                    ErrorEnvelopeV1(
                        code="run_cancelled",
                        classification="cancellation",
                        retryable=False,
                        phase="cancellation",
                    ),
                )
                record_durable_loop_event(
                    DurableLoopPhase.CANCELLATION,
                    DurableLoopOutcome.CANCELLED,
                )
                return await self.get(run_id)
            if checkpoint.pending_human_request_id is not None:
                if not await self._resume_human_input(record, plan):
                    return await self.get(run_id)
                continue
            budget_error = _budget_error(checkpoint, plan.settings, self._clock())
            if budget_error is not None:
                await self._terminalize(
                    checkpoint,
                    DurableLoopRunStatus.FAILED,
                    budget_error,
                )
                return await self.get(run_id)
            checkpoint = await self._compact_if_needed(checkpoint, plan)
            try:
                decision = await self._run_model_step(checkpoint, plan)
            except DurableLoopProviderTerminalError as exc:
                await self._terminalize(
                    checkpoint,
                    DurableLoopRunStatus.FAILED,
                    _error(
                        exc.code,
                        "model",
                        phase="model_step",
                        step_index=checkpoint.next_model_step,
                    ),
                )
                return await self.get(run_id)
            await self._faults.reach("after_model_result", run_id)
            latest = await self._state.get_run(run_id)
            if latest.checkpoint.cancellation_requested:
                await self._terminalize(
                    latest.checkpoint,
                    DurableLoopRunStatus.CANCELLED,
                    _error(
                        "run_cancelled",
                        "cancellation",
                        phase="cancellation",
                        step_index=latest.checkpoint.next_model_step,
                    ),
                )
                return await self.get(run_id)
            checkpoint = latest.checkpoint
            checkpoint = _apply_model_usage(checkpoint, decision.usage)
            usage_error = _budget_error(checkpoint, plan.settings, self._clock())
            if usage_error is not None:
                await self._terminalize(
                    checkpoint,
                    DurableLoopRunStatus.FAILED,
                    usage_error,
                )
                return await self.get(run_id)
            if decision.final_text is not None:
                response_ref = await self._content.put_bytes(
                    kind="final-response",
                    payload=decision.final_text.encode("utf-8"),
                    media_type="text/plain; charset=utf-8",
                    retention_class="result",
                )
                final_context = append_final_message(
                    checkpoint.working_context,
                    decision,
                )
                audit_bundle = append_audit_final(
                    checkpoint.audit_bundle,
                    decision,
                )
                completed = checkpoint.model_copy(
                    update={
                        "status": DurableLoopRunStatus.COMPLETED,
                        "audit_bundle": audit_bundle,
                        "working_context": final_context,
                        "audit_head_hash": audit_bundle.bundle_hash,
                        "completed_model_steps": checkpoint.completed_model_steps + 1,
                        "next_model_step": checkpoint.next_model_step + 1,
                        "checkpoints_in_generation": checkpoint.checkpoints_in_generation
                        + 1,
                        "external_content_bytes": (
                            checkpoint.external_content_bytes
                            + response_ref.byte_length
                        ),
                    }
                )
                content_error = _budget_error(completed, plan.settings, self._clock())
                if content_error is not None:
                    await self._terminalize(
                        completed,
                        DurableLoopRunStatus.FAILED,
                        content_error,
                    )
                    return await self.get(run_id)
                await self._state.save_checkpoint(run_id, completed)
                await self._faults.reach("before_commit", run_id)
                final_result = DurableLoopFinalResultV1(
                    run_id=run_id,
                    session_id=checkpoint.identity.session_id,
                    status=DurableLoopRunStatus.COMPLETED,
                    response_ref=response_ref,
                )
                try:
                    await self._state.commit(
                        run_id,
                        expected_generation=checkpoint.committed_session_generation,
                        result=final_result,
                    )
                except DurableLoopCancellationConflictError:
                    latest = await self._state.get_run(run_id)
                    await self._terminalize(
                        latest.checkpoint,
                        DurableLoopRunStatus.CANCELLED,
                        _error(
                            "run_cancelled",
                            "cancellation",
                            phase="cancellation",
                            step_index=latest.checkpoint.next_model_step,
                        ),
                    )
                    return await self.get(run_id)
                await self._faults.reach("after_commit", run_id)
                record_durable_loop_event(
                    DurableLoopPhase.COMMIT,
                    DurableLoopOutcome.COMPLETED,
                )
                return await self.get(run_id)

            calls = decision.tool_calls
            if checkpoint.completed_tool_calls + len(calls) > plan.settings.max_tool_calls:
                await self._terminalize(
                    checkpoint,
                    DurableLoopRunStatus.FAILED,
                    _error(
                        "tool_call_budget_exceeded",
                        "budget",
                        phase="tool_step",
                        step_index=checkpoint.next_model_step,
                    ),
                )
                return await self.get(run_id)
            clarification_calls = [
                call
                for call in calls
                if call.name == REQUEST_HUMAN_INPUT_TOOL_NAME
            ]
            if clarification_calls:
                if len(calls) != 1:
                    checkpoint = await self._repair_invalid_clarification_batch(
                        checkpoint,
                        decision,
                        plan,
                    )
                    if checkpoint.last_error is not None:
                        await self._terminalize(
                            checkpoint,
                            DurableLoopRunStatus.FAILED,
                            checkpoint.last_error,
                        )
                        return await self.get(run_id)
                    continue
                waiting = await self._open_human_request(
                    checkpoint,
                    decision,
                    clarification_calls[0],
                    plan,
                )
                if waiting.last_error is not None:
                    await self._terminalize(
                        waiting,
                        DurableLoopRunStatus.FAILED,
                        waiting.last_error,
                    )
                    return await self.get(run_id)
                await self._state.save_checkpoint(run_id, waiting)
                record_durable_loop_event(
                    DurableLoopPhase.HUMAN_WAIT,
                    DurableLoopOutcome.WAITING,
                )
                return await self.get(run_id)

            tool_results = await self._dispatch_tool_calls(
                checkpoint,
                decision,
                plan,
            )
            await self._faults.reach("after_tool_result", run_id)
            next_context = append_model_and_tool_messages(
                checkpoint.working_context,
                decision,
                tool_results,
            )
            audit_bundle = append_audit_model_and_tool(
                checkpoint.audit_bundle,
                decision,
                tool_results,
            )
            next_context = next_context.model_copy(
                update={"source_audit_hash": audit_bundle.bundle_hash}
            )
            next_checkpoint = checkpoint.model_copy(
                update={
                    "status": DurableLoopRunStatus.RUNNING,
                    "audit_bundle": audit_bundle,
                    "working_context": next_context,
                    "audit_head_hash": audit_bundle.bundle_hash,
                    "completed_model_steps": checkpoint.completed_model_steps + 1,
                    "completed_tool_calls": checkpoint.completed_tool_calls
                    + len(tool_results),
                    "next_model_step": checkpoint.next_model_step + 1,
                    "checkpoints_in_generation": checkpoint.checkpoints_in_generation
                    + 1,
                }
            )
            next_checkpoint = _roll_generation_if_quiescent(next_checkpoint, plan.settings)
            await self._state.save_checkpoint(run_id, next_checkpoint)
            await self._faults.reach("after_tool_checkpoint", run_id)
            ambiguous = next(
                (
                    result
                    for result in tool_results
                    if result.status is ToolResultStatus.AMBIGUOUS
                ),
                None,
            )
            if ambiguous is not None:
                latest = await self._state.get_run(run_id)
                await self._terminalize(
                    latest.checkpoint,
                    DurableLoopRunStatus.FAILED,
                    ambiguous.error
                    or _error(
                        "tool_outcome_ambiguous",
                        "tool",
                        phase="tool_step",
                        step_index=ambiguous.step_index,
                        call_key=ambiguous.call_key,
                        disposition=ErrorDisposition.AMBIGUOUS,
                        possibly_committed=True,
                    ),
                )
                return await self.get(run_id)

    async def _run_model_step(
        self,
        checkpoint: CheckpointStateV1,
        plan: DurableLoopPlan,
    ) -> ModelDecisionEnvelopeV1:
        request = OneStepModelRequest(
            identity=checkpoint.identity,
            step_index=checkpoint.next_model_step,
            instructions=plan.instructions,
            working_context=checkpoint.working_context,
            catalog=plan.catalog,
            model_settings=plan.model_settings,
            effective_active_deadline=(
                checkpoint.identity.active_deadline
                + timedelta(seconds=checkpoint.parked_seconds)
            ),
        )
        operation_key = deterministic_model_step_key(
            checkpoint.identity.run_id,
            checkpoint.next_model_step,
        )
        timer = DurableLoopTimer(DurableLoopPhase.MODEL_STEP)

        async def invoke() -> ModelDecisionEnvelopeV1:
            return await self._model.run_one_step(request)

        decision = await self._journal.execute_once(
            operation_key,
            request.canonical_request_hash(),
            invoke,
        )
        _validate_model_decision(checkpoint, decision)
        timer.finish(DurableLoopOutcome.COMPLETED)
        return decision

    async def _dispatch_tool_calls(
        self,
        checkpoint: CheckpointStateV1,
        decision: ModelDecisionEnvelopeV1,
        plan: DurableLoopPlan,
    ) -> tuple[ToolResultV1, ...]:
        requests = build_tool_requests(
            checkpoint,
            decision,
            plan,
            self._clock(),
        )
        results: list[ToolResultV1] = []
        index = 0
        while index < len(requests):
            current = await self._state.get_run(checkpoint.identity.run_id)
            if current.checkpoint.cancellation_requested:
                results.extend(
                    _cancelled_tool_result(request)
                    for request in requests[index:]
                )
                break
            request = requests[index]
            descriptor = plan.catalog.by_name().get(request.tool_name)
            if (
                descriptor is not None
                and descriptor.behavior is ToolBehavior.READ_ONLY
                and descriptor.parallel_safe
            ):
                parallel: list[ToolRequestV1] = []
                while index < len(requests):
                    candidate = requests[index]
                    candidate_descriptor = plan.catalog.by_name().get(
                        candidate.tool_name
                    )
                    if (
                        candidate_descriptor is None
                        or candidate_descriptor.behavior
                        is not ToolBehavior.READ_ONLY
                        or not candidate_descriptor.parallel_safe
                        or len(parallel) >= plan.settings.max_parallel_reads
                    ):
                        break
                    parallel.append(candidate)
                    index += 1
                results.extend(await asyncio.gather(*(self._dispatch_one(item) for item in parallel)))
                continue
            results.append(await self._dispatch_one(request))
            index += 1
        return tuple(sorted(results, key=lambda result: result.call_ordinal))

    async def _dispatch_one(self, request: ToolRequestV1) -> ToolResultV1:
        timer = DurableLoopTimer(
            DurableLoopPhase.TOOL_STEP,
            provenance=request.provenance.value,
        )

        if (
            len(canonical_json_bytes(request.arguments))
            > request.argument_byte_limit
        ):
            return _tool_limit_result(
                request,
                code="tool_arguments_too_large",
            )

        async def invoke() -> ToolResultV1:
            result = await self._tools.dispatch(request)
            if _tool_result_size(result) > request.result_byte_limit:
                return _tool_limit_result(
                    request,
                    code="tool_result_too_large",
                )
            return result

        result = await self._journal.execute_once(
            request.call_key,
            request.request_hash,
            invoke,
        )
        timer.finish(
            DurableLoopOutcome.COMPLETED
            if result.status is ToolResultStatus.SUCCEEDED
            else (
                DurableLoopOutcome.AMBIGUOUS
                if result.status is ToolResultStatus.AMBIGUOUS
                else DurableLoopOutcome.FAILED
            )
        )
        return result

    async def _compact_if_needed(
        self,
        checkpoint: CheckpointStateV1,
        plan: DurableLoopPlan,
    ) -> CheckpointStateV1:
        current_bytes = len(canonical_json_bytes(checkpoint.working_context.bundle.messages))
        threshold = (
            plan.settings.context_max_bytes
            * plan.settings.context_compaction_percent
            // 100
        )
        if current_bytes < threshold:
            return checkpoint
        operation_key = canonical_hash(
            {
                "context_hash": checkpoint.working_context.bundle.bundle_hash,
                "generation": checkpoint.working_context.compaction_generation + 1,
                "kind": "context_compaction",
                "run_id": checkpoint.identity.run_id,
            }
        )

        async def invoke() -> WorkingContextV1:
            return await self._compactor.compact(
                checkpoint.working_context,
                maximum_bytes=plan.settings.context_max_bytes,
            )

        timer = DurableLoopTimer(DurableLoopPhase.COMPACTION)
        try:
            compacted = await self._journal.execute_once(
                operation_key,
                checkpoint.working_context.bundle.bundle_hash,
                invoke,
            )
        except DurableLoopContextOverflowError as exc:
            raise DurableLoopExecutionError("working context cannot be compacted") from exc
        timer.finish(DurableLoopOutcome.COMPLETED)
        compacted_checkpoint = checkpoint.model_copy(
            update={
                "working_context": compacted,
                "checkpoints_in_generation": checkpoint.checkpoints_in_generation + 1,
            }
        )
        compacted_checkpoint = _roll_generation_if_quiescent(
            compacted_checkpoint,
            plan.settings,
        )
        await self._state.save_checkpoint(checkpoint.identity.run_id, compacted_checkpoint)
        return compacted_checkpoint

    async def _open_human_request(
        self,
        checkpoint: CheckpointStateV1,
        decision: ModelDecisionEnvelopeV1,
        call: ModelToolCallV1,
        plan: DurableLoopPlan,
    ) -> CheckpointStateV1:
        if checkpoint.human_wait_count >= plan.settings.max_human_waits:
            return checkpoint.model_copy(
                update={
                    "last_error": _error(
                        "human_wait_budget_exceeded",
                        "budget",
                        phase="human_wait",
                        step_index=checkpoint.next_model_step,
                    )
                }
            )
        args = _parse_human_request_arguments(call.arguments)
        question_ref = await self._content.put_bytes(
            kind="human-question",
            payload=args.question.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            retention_class="run",
        )
        decision_hash = canonical_hash(decision.model_dump(mode="json"))
        call_key = deterministic_call_key(
            run_id=checkpoint.identity.run_id,
            step_index=checkpoint.next_model_step,
            call_ordinal=0,
            decision_hash=decision_hash,
            tool_name=call.name,
        )
        request_hash = tool_request_hash(
            tool_name=call.name,
            arguments=call.arguments,
            behavior=ToolBehavior.READ_ONLY,
            provenance=ToolProvenance.RUNTIME,
            argument_byte_limit=plan.settings.max_argument_bytes,
            result_byte_limit=plan.settings.max_result_bytes,
            policy_hash=plan.catalog.policy_hash,
            catalog_hash=plan.catalog.catalog_hash,
            package_hash=plan.catalog.package_hash,
        )
        request_id = f"human-{checkpoint.next_model_step}-{call_key[:16]}"
        nonce = self._nonce_factory()
        now = self._clock()
        request = HumanInputRequestV1(
            request_id=request_id,
            run_id=checkpoint.identity.run_id,
            session_id=checkpoint.identity.session_id,
            generation=checkpoint.continue_as_new_generation + 1,
            turn_index=checkpoint.committed_session_generation,
            step_index=checkpoint.next_model_step,
            call_id=call.call_id,
            call_key=call_key,
            request_hash=request_hash,
            question_ref=question_ref,
            choices=args.choices,
            allow_free_text=args.allow_free_text,
            response_schema=args.response_schema,
            actor_policy_hash=checkpoint.identity.owner_hash,
            event_name=deterministic_human_event_name(
                generation=checkpoint.continue_as_new_generation + 1,
                request_id=request_id,
                nonce=nonce,
            ),
            issued_at=now,
            expires_at=min(
                now + timedelta(seconds=plan.settings.human_wait_seconds),
                checkpoint.identity.absolute_deadline,
            ),
            record_version=1,
        )
        await self._state.put_human_request(request)
        waiting_context = append_assistant_message(
            checkpoint.working_context,
            decision,
        )
        audit_bundle = append_audit_assistant(
            checkpoint.audit_bundle,
            decision,
        )
        waiting_context = waiting_context.model_copy(
            update={"source_audit_hash": audit_bundle.bundle_hash}
        )
        return checkpoint.model_copy(
            update={
                "status": DurableLoopRunStatus.WAITING,
                "audit_bundle": audit_bundle,
                "working_context": waiting_context,
                "audit_head_hash": audit_bundle.bundle_hash,
                "completed_model_steps": checkpoint.completed_model_steps + 1,
                "human_wait_count": checkpoint.human_wait_count + 1,
                "external_content_bytes": (
                    checkpoint.external_content_bytes + question_ref.byte_length
                ),
                "next_model_step": checkpoint.next_model_step + 1,
                "pending_human_request_id": request_id,
                "checkpoints_in_generation": checkpoint.checkpoints_in_generation + 1,
            }
        )

    async def _resume_human_input(
        self,
        record: DurableLoopRunRecord,
        plan: DurableLoopPlan,
    ) -> bool:
        checkpoint = record.checkpoint
        request = record.human_request
        if request is None:
            raise DurableLoopExecutionError("checkpoint references a missing human request")
        if request.state is HumanInputState.PENDING:
            return False
        if request.state is HumanInputState.ANSWERED:
            disposition = await self._resume_validator.validate(
                checkpoint.working_context,
                maf_core_version=plan.maf_core_version,
                provider=plan.provider,
                model=plan.model,
                api_version=plan.api_version,
            )
            if disposition is ResumeContextDisposition.UNAVAILABLE:
                raise DurableLoopExecutionError(
                    "accepted human answer cannot resume on the frozen model binding"
                )
            resume_context = checkpoint.working_context
            if disposition is ResumeContextDisposition.REHYDRATE:
                resume_context = await self._rehydrator.rehydrate(
                    checkpoint.working_context,
                    maf_core_version=plan.maf_core_version,
                    provider=plan.provider,
                    model=plan.model,
                    api_version=plan.api_version,
                )
            response = await self._state.consume_human_response(
                record.identity.run_id,
                request.request_id,
            )
            answer = await self._get_json(response.answer_ref)
            answer_content_bytes = response.answer_ref.byte_length
            result = ToolResultV1(
                run_id=request.run_id,
                step_index=request.step_index,
                call_ordinal=0,
                provider_call_id=request.call_id,
                call_key=request.call_key,
                request_hash=request.request_hash,
                tool_name=REQUEST_HUMAN_INPUT_TOOL_NAME,
                status=ToolResultStatus.SUCCEEDED,
                value={
                    "answer": answer,
                    "response_id": response.submission_id_hash,
                    "status": "answered",
                },
                elapsed_ms=0.0,
            )
        else:
            resume_context = checkpoint.working_context
            answer_content_bytes = 0
            result = ToolResultV1(
                run_id=request.run_id,
                step_index=request.step_index,
                call_ordinal=0,
                provider_call_id=request.call_id,
                call_key=request.call_key,
                request_hash=request.request_hash,
                tool_name=REQUEST_HUMAN_INPUT_TOOL_NAME,
                status=(
                    ToolResultStatus.TIMED_OUT
                    if request.state is HumanInputState.TIMED_OUT
                    else ToolResultStatus.CANCELLED
                ),
                elapsed_ms=0.0,
                error=_error(
                    f"human_input_{request.state.value}",
                    "human_input",
                    phase="human_wait",
                    step_index=request.step_index,
                    call_key=request.call_key,
                ),
            )
        next_context = append_tool_result_messages(
            resume_context,
            (result,),
        )
        audit_bundle = append_audit_tool_results(
            checkpoint.audit_bundle,
            (result,),
        )
        next_context = next_context.model_copy(
            update={"source_audit_hash": audit_bundle.bundle_hash}
        )
        resumed = checkpoint.model_copy(
            update={
                "status": DurableLoopRunStatus.RUNNING,
                "audit_bundle": audit_bundle,
                "working_context": next_context,
                "audit_head_hash": audit_bundle.bundle_hash,
                "completed_tool_calls": checkpoint.completed_tool_calls + 1,
                "external_content_bytes": (
                    checkpoint.external_content_bytes + answer_content_bytes
                ),
                "pending_human_request_id": None,
                "parked_seconds": (
                    checkpoint.parked_seconds
                    + max(
                        0,
                        int(
                            (
                                self._clock()
                                - request.issued_at.astimezone(UTC)
                            ).total_seconds()
                        ),
                    )
                ),
                "checkpoints_in_generation": checkpoint.checkpoints_in_generation + 1,
            }
        )
        resumed = _roll_generation_if_quiescent(resumed, plan.settings)
        await self._state.save_checkpoint(record.identity.run_id, resumed)
        record_durable_loop_event(
            DurableLoopPhase.HUMAN_WAIT,
            DurableLoopOutcome.COMPLETED,
        )
        return True

    async def _repair_invalid_clarification_batch(
        self,
        checkpoint: CheckpointStateV1,
        decision: ModelDecisionEnvelopeV1,
        plan: DurableLoopPlan,
    ) -> CheckpointStateV1:
        if checkpoint.repair_steps_used >= plan.settings.mixed_batch_repair_steps:
            return checkpoint.model_copy(
                update={
                    "last_error": _error(
                        "invalid_clarification_batch",
                        "protocol",
                        phase="model_step",
                        step_index=checkpoint.next_model_step,
                    )
                }
            )
        results = _protocol_error_results(checkpoint, decision, plan)
        context = append_model_and_tool_messages(
            checkpoint.working_context,
            decision,
            results,
        )
        audit_bundle = append_audit_model_and_tool(
            checkpoint.audit_bundle,
            decision,
            results,
        )
        context = context.model_copy(
            update={"source_audit_hash": audit_bundle.bundle_hash}
        )
        repaired = checkpoint.model_copy(
            update={
                "status": DurableLoopRunStatus.RUNNING,
                "audit_bundle": audit_bundle,
                "working_context": context,
                "audit_head_hash": audit_bundle.bundle_hash,
                "completed_model_steps": checkpoint.completed_model_steps + 1,
                "completed_tool_calls": checkpoint.completed_tool_calls + len(results),
                "next_model_step": checkpoint.next_model_step + 1,
                "repair_steps_used": checkpoint.repair_steps_used + 1,
                "checkpoints_in_generation": checkpoint.checkpoints_in_generation + 1,
            }
        )
        await self._state.save_checkpoint(checkpoint.identity.run_id, repaired)
        return repaired

    async def _terminalize(
        self,
        checkpoint: CheckpointStateV1,
        status: DurableLoopRunStatus,
        error: ErrorEnvelopeV1,
    ) -> None:
        terminal = checkpoint.model_copy(
            update={
                "status": status,
                "pending_human_request_id": None,
                "last_error": error,
            }
        )
        await self._state.save_checkpoint(checkpoint.identity.run_id, terminal)
        await self._state.abort(
            checkpoint.identity.run_id,
            result=DurableLoopFinalResultV1(
                run_id=checkpoint.identity.run_id,
                session_id=checkpoint.identity.session_id,
                status=status,
                error=error,
            ),
        )

    async def _cancel_open_human_request(
        self,
        record: DurableLoopRunRecord,
    ) -> None:
        request = record.human_request
        if request is not None and request.state is HumanInputState.PENDING:
            await self._state.close_human_request(
                record.identity.run_id,
                request.request_id,
                HumanInputState.CANCELLED,
            )

    async def _deliver_human_response(
        self,
        request: HumanInputRequestV1,
        response: HumanInputResponseV1,
    ) -> HumanEventDeliveryDisposition:
        delivery = await self._event_publisher.raise_event(
            run_id=request.run_id,
            event_name=request.event_name,
            payload={
                "body_hash": response.body_hash,
                "request_id": request.request_id,
                "submission_id_hash": response.submission_id_hash,
            },
        )
        if delivery in {
            HumanEventDeliveryDisposition.RUN_NOT_FOUND,
            HumanEventDeliveryDisposition.RUN_TERMINAL,
        }:
            await self._state.mark_human_response_orphaned(
                request.run_id,
                request.request_id,
            )
        return delivery

    async def _human_content(
        self,
        request: HumanInputRequestV1,
    ) -> HumanInputContentV1:
        question = (await self._content.get_bytes(request.question_ref)).decode("utf-8")
        return HumanInputContentV1(
            request_id=request.request_id,
            run_id=request.run_id,
            generation=request.generation,
            question=question,
            choices=request.choices,
            allow_free_text=request.allow_free_text,
            response_schema=request.response_schema,
            expires_at=request.expires_at,
            respond_url=(
                f"/api/experimental/durable-agent-runs/{request.run_id}/"
                f"input/{request.request_id}"
            ),
        )

    async def _status(
        self,
        run_id: str,
        plan: DurableLoopPlan,
    ) -> DurableLoopStatusEnvelopeV1:
        return await self._state.status(
            run_id,
            retention_seconds=plan.settings.max_run_wait_seconds,
        )

    async def _put_json(
        self,
        *,
        kind: str,
        value: object,
        retention_class: str,
    ) -> ContentRefV1:
        payload = canonical_json_bytes(value)
        if len(payload) > MAX_HUMAN_ANSWER_BYTES:
            raise ValueError("human answer exceeds the byte limit")
        return await self._content.put_bytes(
            kind=kind,
            payload=payload,
            media_type="application/json",
            retention_class=retention_class,
        )

    async def _get_json(self, reference: object) -> object:
        from .durable_loop_protocol import ContentRefV1

        if not isinstance(reference, ContentRefV1):
            raise TypeError("answer reference is invalid")
        import json

        return json.loads(await self._content.get_bytes(reference))

    @staticmethod
    def _validate_identity_budget(
        identity: DurableRunIdentityV1,
        settings: DurableLoopSettings,
    ) -> None:
        expected = DurableLoopBudgetV1(
            max_model_steps=settings.max_model_steps,
            max_tool_calls=settings.max_tool_calls,
            max_total_tokens=settings.max_total_tokens,
            max_cost_microunits=settings.max_cost_microunits,
            input_cost_microunits_per_million_tokens=(
                settings.input_cost_microunits_per_million_tokens
            ),
            output_cost_microunits_per_million_tokens=(
                settings.output_cost_microunits_per_million_tokens
            ),
            max_external_content_bytes=settings.max_external_content_bytes,
            max_elapsed_seconds=settings.max_elapsed_seconds,
            max_human_waits=settings.max_human_waits,
            human_wait_seconds=settings.human_wait_seconds,
            max_argument_bytes=settings.max_argument_bytes,
            max_result_bytes=settings.max_result_bytes,
            context_max_bytes=settings.context_max_bytes,
            context_compaction_percent=settings.context_compaction_percent,
            max_parallel_reads=settings.max_parallel_reads,
            continue_as_new_checkpoints=settings.continue_as_new_checkpoints,
        )
        if identity.budget != expected:
            raise DurableLoopExecutionError(
                "run identity budget does not match the validated process settings"
            )


@dataclass(frozen=True, slots=True)
class _HumanRequestArguments:
    question: str
    choices: tuple[str, ...]
    allow_free_text: bool
    response_schema: dict[str, object] | None


def create_run_identity(
    *,
    session_id: str,
    request_id: str,
    request_body: object,
    owner_hash: str,
    agent_slug: str,
    agent_hash: str,
    catalog_hash: str,
    deployment_hash: str,
    tool_package_hash: str,
    policy_hash: str,
    settings: DurableLoopSettings,
    now: datetime | None = None,
    run_id: str | None = None,
) -> DurableRunIdentityV1:
    """Mint a private run identity outside deterministic orchestrator code."""
    created = now or datetime.now(UTC)
    return DurableRunIdentityV1(
        run_id=run_id or uuid.uuid4().hex,
        session_id=session_id,
        request_id_hash=canonical_hash({"request_id": request_id}),
        request_hash=canonical_hash(request_body),
        owner_hash=owner_hash,
        agent_slug=agent_slug,
        agent_hash=agent_hash,
        catalog_hash=catalog_hash,
        deployment_hash=deployment_hash,
        tool_package_hash=tool_package_hash,
        policy_hash=policy_hash,
        orchestration_version="durable_agent_turn_orchestrator_v1",
        created_at=created,
        active_deadline=created + timedelta(seconds=settings.max_elapsed_seconds),
        absolute_deadline=created + timedelta(seconds=settings.max_run_wait_seconds),
        budget=DurableLoopBudgetV1(
            max_model_steps=settings.max_model_steps,
            max_tool_calls=settings.max_tool_calls,
            max_total_tokens=settings.max_total_tokens,
            max_cost_microunits=settings.max_cost_microunits,
            input_cost_microunits_per_million_tokens=(
                settings.input_cost_microunits_per_million_tokens
            ),
            output_cost_microunits_per_million_tokens=(
                settings.output_cost_microunits_per_million_tokens
            ),
            max_external_content_bytes=settings.max_external_content_bytes,
            max_elapsed_seconds=settings.max_elapsed_seconds,
            max_human_waits=settings.max_human_waits,
            human_wait_seconds=settings.human_wait_seconds,
            max_argument_bytes=settings.max_argument_bytes,
            max_result_bytes=settings.max_result_bytes,
            context_max_bytes=settings.context_max_bytes,
            context_compaction_percent=settings.context_compaction_percent,
            max_parallel_reads=settings.max_parallel_reads,
            continue_as_new_checkpoints=settings.continue_as_new_checkpoints,
        ),
    )


def _initial_working_context(
    plan: DurableLoopPlan,
    messages: tuple[dict[str, object], ...],
) -> WorkingContextV1:
    if not messages:
        raise DurableLoopExecutionError("initial message bundle must not be empty")
    bundle = MAFMessageBundleV1.create(
        messages=messages,
        maf_core_version=plan.maf_core_version,
        provider=plan.provider,
        model=plan.model,
        api_version=plan.api_version,
    )
    return WorkingContextV1(
        bundle=bundle,
        compaction_generation=0,
        source_audit_hash=bundle.bundle_hash,
        source_start=0,
        source_end=len(messages),
        estimated_tokens=max(1, len(canonical_json_bytes(messages)) // 4),
    )


def _messages_with_committed_context(
    committed: WorkingContextV1 | None,
    messages: tuple[dict[str, object], ...],
    plan: DurableLoopPlan,
) -> tuple[dict[str, object], ...]:
    if committed is None:
        return messages
    binding = committed.bundle
    if (
        binding.maf_core_version != plan.maf_core_version
        or binding.provider != plan.provider
        or binding.model != plan.model
        or binding.api_version != plan.api_version
    ):
        raise DurableLoopExecutionError(
            "committed session context is incompatible with the requested model binding"
        )
    return (*binding.messages, *messages)


def _validate_model_decision(
    checkpoint: CheckpointStateV1,
    decision: ModelDecisionEnvelopeV1,
) -> None:
    if decision.run_id != checkpoint.identity.run_id:
        raise DurableLoopModelError("model decision run ID mismatch")
    if decision.step_index != checkpoint.next_model_step:
        raise DurableLoopModelError("model decision step index mismatch")
    if decision.model_call_key != deterministic_model_step_key(
        checkpoint.identity.run_id,
        checkpoint.next_model_step,
    ):
        raise DurableLoopModelError("model decision call key mismatch")
    if decision.deployment_hash != checkpoint.identity.deployment_hash:
        raise DurableLoopModelError("model decision deployment binding mismatch")


def build_tool_requests(
    checkpoint: CheckpointStateV1,
    decision: ModelDecisionEnvelopeV1,
    plan: DurableLoopPlan,
    now: datetime,
) -> tuple[ToolRequestV1, ...]:
    descriptors = plan.catalog.by_name()
    decision_hash = canonical_hash(decision.model_dump(mode="json"))
    requests: list[ToolRequestV1] = []
    for ordinal, call in enumerate(decision.tool_calls):
        descriptor = descriptors.get(call.name)
        if descriptor is None:
            descriptor = _unknown_tool_descriptor(call.name)
        request_hash = tool_request_hash(
            tool_name=call.name,
            arguments=call.arguments,
            behavior=descriptor.behavior,
            provenance=descriptor.provenance,
            argument_byte_limit=plan.settings.max_argument_bytes,
            result_byte_limit=plan.settings.max_result_bytes,
            policy_hash=plan.catalog.policy_hash,
            catalog_hash=plan.catalog.catalog_hash,
            package_hash=plan.catalog.package_hash,
        )
        requests.append(
            ToolRequestV1(
                run_id=checkpoint.identity.run_id,
                step_index=checkpoint.next_model_step,
                call_ordinal=ordinal,
                provider_call_id=call.call_id,
                call_key=deterministic_call_key(
                    run_id=checkpoint.identity.run_id,
                    step_index=checkpoint.next_model_step,
                    call_ordinal=ordinal,
                    decision_hash=decision_hash,
                    tool_name=call.name,
                ),
                tool_name=call.name,
                provenance=descriptor.provenance,
                behavior=descriptor.behavior,
                arguments=call.arguments,
                argument_byte_limit=plan.settings.max_argument_bytes,
                result_byte_limit=plan.settings.max_result_bytes,
                request_hash=request_hash,
                policy_hash=plan.catalog.policy_hash,
                catalog_hash=plan.catalog.catalog_hash,
                package_hash=plan.catalog.package_hash,
                deadline=min(
                    checkpoint.identity.active_deadline
                    + timedelta(seconds=checkpoint.parked_seconds),
                    now + timedelta(seconds=plan.settings.activity_timeout_seconds),
                ),
            )
        )
    return tuple(requests)


def _unknown_tool_descriptor(name: str) -> FrozenToolDescriptorV1:
    return FrozenToolDescriptorV1(
        name=name,
        description="Unknown tool placeholder used only to produce a typed failure.",
        parameters={"additionalProperties": True, "type": "object"},
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.READ_ONLY,
        parallel_safe=False,
    )


def _protocol_error_results(
    checkpoint: CheckpointStateV1,
    decision: ModelDecisionEnvelopeV1,
    plan: DurableLoopPlan,
) -> tuple[ToolResultV1, ...]:
    requests = build_tool_requests(
        checkpoint,
        decision,
        plan,
        checkpoint.identity.created_at,
    )
    return tuple(
        ToolResultV1(
            run_id=request.run_id,
            step_index=request.step_index,
            call_ordinal=request.call_ordinal,
            provider_call_id=request.provider_call_id,
            call_key=request.call_key,
            request_hash=request.request_hash,
            tool_name=request.tool_name,
            status=ToolResultStatus.FAILED,
            elapsed_ms=0.0,
            error=_error(
                "invalid_clarification_batch",
                "protocol",
                phase="model_step",
                step_index=request.step_index,
                call_key=request.call_key,
            ),
        )
        for request in requests
    )


def _cancelled_tool_result(request: ToolRequestV1) -> ToolResultV1:
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=request.call_ordinal,
        provider_call_id=request.provider_call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=request.tool_name,
        status=ToolResultStatus.CANCELLED,
        elapsed_ms=0.0,
        error=_error(
            "tool_cancelled",
            "cancellation",
            phase="tool_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )


def _tool_limit_result(
    request: ToolRequestV1,
    *,
    code: str,
) -> ToolResultV1:
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=request.call_ordinal,
        provider_call_id=request.provider_call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=request.tool_name,
        status=ToolResultStatus.FAILED,
        elapsed_ms=0.0,
        error=_error(
            code,
            "budget",
            phase="tool_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )


def _tool_result_size(result: ToolResultV1) -> int:
    if result.result_ref is not None:
        return result.result_ref.byte_length
    return len(canonical_json_bytes(result.value))


def _parse_human_request_arguments(
    arguments: Mapping[str, object],
) -> _HumanRequestArguments:
    question = arguments.get("question")
    choices = arguments.get("choices")
    allow_free_text = arguments.get("allow_free_text")
    response_schema = arguments.get("response_schema")
    if not isinstance(question, str) or not question:
        raise DurableLoopExecutionError("human clarification question is required")
    if len(question.encode("utf-8")) > MAX_HUMAN_QUESTION_BYTES:
        raise DurableLoopExecutionError("human clarification question is too large")
    if not isinstance(choices, list | tuple) or not all(
        isinstance(choice, str)
        and choice
        and len(choice) <= MAX_HUMAN_CHOICE_CHARS
        for choice in choices
    ):
        raise DurableLoopExecutionError("human clarification choices are invalid")
    if len(choices) > MAX_HUMAN_CHOICES or len(set(choices)) != len(choices):
        raise DurableLoopExecutionError("human clarification choices are invalid")
    if not isinstance(allow_free_text, bool):
        raise DurableLoopExecutionError("allow_free_text must be a boolean")
    if response_schema is not None and not isinstance(response_schema, dict):
        raise DurableLoopExecutionError("response_schema must be an object or null")
    if response_schema is not None:
        validate_human_response_schema(response_schema)
    if not allow_free_text and not choices and response_schema is None:
        raise DurableLoopExecutionError("human clarification has no valid response shape")
    return _HumanRequestArguments(
        question=question,
        choices=tuple(choices),
        allow_free_text=allow_free_text,
        response_schema=response_schema,
    )


def _validate_human_answer(request: HumanInputRequestV1, answer: object) -> None:
    assert_json_value(answer)
    if len(canonical_json_bytes(answer)) > MAX_HUMAN_ANSWER_BYTES:
        raise ValueError("human answer exceeds the byte limit")
    if request.response_schema is not None:
        validate_human_response_value(request.response_schema, answer)
        return
    if request.choices:
        if (
            (not isinstance(answer, str) or answer not in request.choices)
            and not request.allow_free_text
        ):
            raise ValueError("human answer is not one of the allowed choices")
        return
    if not request.allow_free_text:
        raise ValueError("human answer is not allowed")


def _budget_error(
    checkpoint: CheckpointStateV1,
    settings: DurableLoopSettings,
    now: datetime,
) -> ErrorEnvelopeV1 | None:
    if checkpoint.completed_model_steps >= settings.max_model_steps:
        return _error(
            "model_step_budget_exceeded",
            "budget",
            phase="model_step",
            step_index=checkpoint.next_model_step,
        )
    if checkpoint.input_tokens + checkpoint.output_tokens > settings.max_total_tokens:
        return _error(
            "model_token_budget_exceeded",
            "budget",
            phase="model_step",
            step_index=checkpoint.next_model_step,
        )
    if (
        settings.max_cost_microunits > 0
        and checkpoint.cost_microunits > settings.max_cost_microunits
    ):
        return _error(
            "model_cost_budget_exceeded",
            "budget",
            phase="model_step",
            step_index=checkpoint.next_model_step,
        )
    if checkpoint.external_content_bytes > settings.max_external_content_bytes:
        return _error(
            "external_content_budget_exceeded",
            "budget",
            phase="run",
            step_index=checkpoint.next_model_step,
        )
    if now >= checkpoint.identity.active_deadline + timedelta(
        seconds=checkpoint.parked_seconds
    ):
        return _error(
            "run_active_deadline_exceeded",
            "timeout",
            phase="run",
            step_index=checkpoint.next_model_step,
        )
    return None


def _apply_model_usage(
    checkpoint: CheckpointStateV1,
    usage: UsageV1,
) -> CheckpointStateV1:
    return checkpoint.model_copy(
        update={
            "input_tokens": checkpoint.input_tokens + usage.input_tokens,
            "output_tokens": checkpoint.output_tokens + usage.output_tokens,
            "reasoning_tokens": (
                checkpoint.reasoning_tokens + usage.reasoning_tokens
            ),
            "cost_microunits": (
                checkpoint.cost_microunits + (usage.cost_microunits or 0)
            ),
        }
    )


def _roll_generation_if_quiescent(
    checkpoint: CheckpointStateV1,
    settings: DurableLoopSettings,
) -> CheckpointStateV1:
    if (
        checkpoint.pending_human_request_id is None
        and checkpoint.checkpoints_in_generation
        >= settings.continue_as_new_checkpoints
    ):
        return checkpoint.model_copy(
            update={
                "continue_as_new_generation": checkpoint.continue_as_new_generation
                + 1,
                "checkpoints_in_generation": 0,
            }
        )
    return checkpoint


def _error(
    code: str,
    classification: str,
    *,
    phase: str,
    step_index: int | None = None,
    call_key: str | None = None,
    disposition: ErrorDisposition = ErrorDisposition.CERTAIN,
    possibly_committed: bool = False,
) -> ErrorEnvelopeV1:
    return ErrorEnvelopeV1(
        code=code,
        classification=classification,
        retryable=False,
        disposition=disposition,
        possibly_committed=possibly_committed,
        phase=phase,
        step_index=step_index,
        call_key=call_key,
    )
