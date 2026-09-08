"""Refs-only Durable Functions registration for the private agent loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import azure.durable_functions as df
import azure.functions as func

from .._logger import logger
from ..client_manager import get_client_manager
from ..strict_json import canonical_json_bytes
from .durable_loop import DurableLoopPlan, build_tool_requests
from .durable_loop_activities import (
    BackgroundModelProvider,
    BlobDurableContentStore,
    DeterministicContextCompactor,
    DurableContentStore,
    DurableLoopProviderTerminalError,
    HumanEventDeliveryPort,
    OneStepModelProvider,
    OneStepModelRequest,
    append_audit_final,
    append_audit_model_and_tool,
    append_final_message,
    append_model_and_tool_messages,
    append_tool_result_messages,
    get_protocol_model,
    put_protocol_model,
)
from .durable_loop_config import DurableLoopSettings
from .durable_loop_execution import DurableLoopExecutionBinding
from .durable_loop_protocol import (
    CheckpointStateV1,
    ContentRefV1,
    DurableFaultProfile,
    DurableLoopPlanDocumentV1,
    DurableLoopRunStatus,
    DurableOrchestrationInputV1,
    DurableRunDocumentV1,
    ErrorDisposition,
    ErrorEnvelopeV1,
    HumanEventDeliveryResultV1,
    HumanEventDeliveryStatus,
    HumanInputRequestV1,
    HumanInputResponseV1,
    HumanWaitActivityResultV1,
    ModelDecisionEnvelopeV1,
    ModelOperationStatus,
    ModelOperationV1,
    ModelStepActivityResultV1,
    SandboxExecutionProfile,
    ToolBehavior,
    ToolDispatchRefV1,
    ToolRequestV1,
    ToolResultRefV1,
    ToolResultStatus,
    ToolResultV1,
    WorkingContextV1,
    WorkspaceArtifactV1,
    canonical_hash,
    deterministic_human_event_name,
    deterministic_model_step_key,
)
from .durable_loop_tools import (
    REQUEST_HUMAN_INPUT_TOOL_NAME,
    DurableToolCleanupPort,
    DurableToolDispatchPort,
)

DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME = "durable_agent_admission_v1"
DURABLE_LOOP_ORCHESTRATOR_NAME = "durable_agent_turn_orchestrator_v1"
DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME = "durable_agent_human_input_outbox_v1"
DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME = (
    "durable_agent_human_delivery_outbox_v1"
)
DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME = (
    "durable_agent_cancel_delivery_outbox_v1"
)
DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME = "durable_agent_control_v1"
DURABLE_LOOP_SESSION_ENTITY_NAME = "durable_agent_session_entity_v1"
DURABLE_LOOP_MODEL_ACTIVITY_NAME = "durable_agent_model_step_v1"
DURABLE_LOOP_TOOL_ACTIVITY_NAME = "durable_agent_tool_step_v1"
DURABLE_LOOP_APPEND_ACTIVITY_NAME = "durable_agent_append_results_v1"
DURABLE_LOOP_HUMAN_ACTIVITY_NAME = "durable_agent_human_request_v1"
DURABLE_LOOP_HUMAN_RESULT_ACTIVITY_NAME = "durable_agent_human_result_v1"
DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME = "durable_agent_human_delivery_v1"
DURABLE_LOOP_COMPACTION_ACTIVITY_NAME = "durable_agent_compact_context_v1"
DURABLE_LOOP_MODEL_POLL_ACTIVITY_NAME = "durable_agent_model_poll_v1"
DURABLE_LOOP_MODEL_CANCEL_ACTIVITY_NAME = "durable_agent_model_cancel_v1"
DURABLE_LOOP_CLEANUP_ACTIVITY_NAME = "durable_agent_cleanup_v1"
DURABLE_LOOP_FAULT_ACTIVITY_NAME = "durable_agent_fault_v1"
DURABLE_LOOP_CANCEL_EVENT_NAME = "durable_agent_cancel_v1"

_MAX_ENTITY_IDEMPOTENCY_RECEIPTS = 128
_MAX_ENTITY_HUMAN_INPUT_RECEIPTS = 64
_MAX_ENTITY_ABORT_RECEIPTS = 64


class _UnavailableToolDispatcher:
    async def dispatch(self, request: ToolRequestV1) -> ToolResultV1:
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
            error=ErrorEnvelopeV1(
                code="tool_transport_unconfigured",
                classification="configuration",
                retryable=False,
                phase="tool_step",
                step_index=request.step_index,
                call_key=request.call_key,
            ),
        )


class _UnavailableHumanEventDelivery:
    async def deliver(
        self,
        *,
        run_id: str,
        event_name: str,
        event_data: Mapping[str, object],
    ) -> HumanEventDeliveryResultV1:
        del run_id, event_name, event_data
        return HumanEventDeliveryResultV1(
            status=HumanEventDeliveryStatus.RETRY,
            retry_after_seconds=30.0,
        )


@dataclass(frozen=True, slots=True)
class DurableLoopActivityRuntime:
    """Adapters constructed identically on every worker."""

    model: OneStepModelProvider
    tools: DurableToolDispatchPort
    compactor: DeterministicContextCompactor
    content: DurableContentStore
    background_model: BackgroundModelProvider | None = None
    faults: Any | None = None
    human_events: HumanEventDeliveryPort = field(
        default_factory=_UnavailableHumanEventDelivery
    )


type ActivityRuntimeFactory = Callable[[], DurableLoopActivityRuntime]


def _default_activity_runtime() -> DurableLoopActivityRuntime:
    global _default_runtime_instance
    if _default_runtime_instance is None:
        from .durable_loop_apim import ApimMafResponsesProvider
        from .durable_loop_execution import (
            build_durable_execution_plane,
            durable_model_control_base_url,
        )
        from .durable_loop_receipts import (
            BlobDurableKeyedDocumentStore,
            DurableOneShotFaults,
        )
        from .hybrid_apim import HybridApimClientManager

        settings = DurableLoopSettings.from_environment()
        if settings is None:
            raise RuntimeError("durable-loop activity runtime is not enabled")
        content = BlobDurableContentStore.from_environment()
        receipts = BlobDurableKeyedDocumentStore.from_environment()
        manager = get_client_manager()
        if not isinstance(manager, HybridApimClientManager):
            raise RuntimeError("durable-loop APIM client manager is unavailable")
        tools = build_durable_execution_plane(
            client_manager=manager,
            settings=settings,
            content=content,
            receipts=receipts,
            binding=_execution_binding,
        )
        model = ApimMafResponsesProvider(
            manager,
            content=content,
            receipts=receipts,
            settings=settings,
            control_base_url=durable_model_control_base_url(),
        )
        faults = DurableOneShotFaults(
            receipts,
            enabled=settings.fault_injection_enabled,
        )
        _default_runtime_instance = DurableLoopActivityRuntime(
            model=model,
            background_model=model if settings.background_model_enabled else None,
            tools=tools,
            compactor=DeterministicContextCompactor(),
            content=content,
            faults=faults,
            human_events=_UnavailableHumanEventDelivery(),
        )
    return _default_runtime_instance


_default_runtime_instance: DurableLoopActivityRuntime | None = None
_activity_runtime_factory: ActivityRuntimeFactory = _default_activity_runtime

_execution_binding = DurableLoopExecutionBinding(
    enabled_mcp_names=(),
    local_tools_enabled=True,
)


def configure_durable_loop_execution_binding(
    *,
    enabled_mcp_names: Sequence[str],
    local_tools_enabled: bool,
) -> None:
    """Configure the app-scoped filters consumed by worker runtime factories."""
    global _execution_binding, _default_runtime_instance
    _execution_binding = DurableLoopExecutionBinding(
        enabled_mcp_names=tuple(enabled_mcp_names),
        local_tools_enabled=local_tools_enabled,
    )
    _default_runtime_instance = None


def get_durable_loop_activity_runtime() -> DurableLoopActivityRuntime:
    """Return the configured private runtime adapters."""
    return _activity_runtime_factory()


def set_durable_loop_activity_runtime_factory(
    factory: ActivityRuntimeFactory,
) -> None:
    """Override activity adapters for deterministic local tests."""
    global _activity_runtime_factory
    _activity_runtime_factory = factory


def reset_durable_loop_activity_runtime_factory() -> None:
    """Restore exact-MAF, Blob-content, and unconfigured-tool adapters."""
    global _activity_runtime_factory, _default_runtime_instance
    _default_runtime_instance = None
    _activity_runtime_factory = _default_activity_runtime


def apply_session_entity_operation(  # noqa: PLR0912, PLR0915
    state: Mapping[str, object] | None,
    operation: str,
    payload: Mapping[str, object] | None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Apply one deterministic session admission, mailbox, or commit transition."""
    current = dict(
        state
        or {
            "aborted_runs": {},
            "active_run_id": None,
            "cancelled_runs": {},
            "committed_context_ref": None,
            "committed_generation": 0,
            "human_inputs": {},
            "idempotency": {},
            "last_commit": None,
            "open_human_request": None,
            "schema_version": "1",
        }
    )
    data = dict(payload or {})
    if operation == "admit":
        return _entity_admit(current, data)
    if operation == "complete":
        return _entity_complete(current, data)
    if operation == "abort":
        return _entity_abort(current, data)
    if operation == "mark_running":
        run_id = _required_string(data, "run_id")
        if current.get("active_run_id") != run_id:
            return current, {"disposition": "stale"}
        cancelled = _mapping(current.get("cancelled_runs"), "cancelled_runs")
        if cancelled.get(run_id) is True:
            return current, {"disposition": "cancelled"}
        _update_idempotency_lifecycle(
            current,
            run_id,
            lifecycle="running",
        )
        return current, {"disposition": "running"}
    if operation == "release_admission":
        run_id = _required_string(data, "run_id")
        if current.get("active_run_id") != run_id:
            return current, {"disposition": "stale"}
        idempotency = _mapping(current.get("idempotency"), "idempotency")
        for key, value in tuple(idempotency.items()):
            if isinstance(value, Mapping) and value.get("run_id") == run_id:
                idempotency.pop(key)
        current["active_run_id"] = None
        current["idempotency"] = idempotency
        return current, {"disposition": "released"}
    if operation == "cancel":
        run_id = _required_string(data, "run_id")
        if current.get("active_run_id") != run_id:
            return current, {"disposition": "stale"}
        open_request = current.get("open_human_request")
        if isinstance(open_request, Mapping):
            request_id = open_request.get("request_id")
            human_inputs = _mapping(current.get("human_inputs"), "human_inputs")
            if isinstance(request_id, str) and isinstance(
                human_inputs.get(request_id),
                Mapping,
            ):
                return current, {"disposition": "answer_won"}
        cancelled = _mapping(current.get("cancelled_runs"), "cancelled_runs")
        cancelled[run_id] = True
        current["cancelled_runs"] = cancelled
        return current, {"disposition": "accepted"}
    if operation == "is_cancelled":
        run_id = _required_string(data, "run_id")
        cancelled = _mapping(current.get("cancelled_runs"), "cancelled_runs")
        return current, {
            "cancelled": cancelled.get(run_id) is True,
            "disposition": "found",
        }
    if operation == "open_human":
        if current.get("active_run_id") != _required_string(data, "run_id"):
            return current, {"disposition": "stale"}
        existing = current.get("open_human_request")
        if isinstance(existing, Mapping):
            if existing == data:
                return current, {"disposition": "replayed"}
            return current, {"disposition": "busy"}
        current["open_human_request"] = data
        return current, {"disposition": "opened"}
    if operation == "accept_human":
        return _entity_accept_human(current, data)
    if operation == "reserve_human":
        return _entity_reserve_human(current, data)
    if operation == "close_human":
        return _entity_close_human(current, data)
    if operation == "consume_human":
        request_id = _required_string(data, "request_id")
        request = current.get("open_human_request")
        if isinstance(request, Mapping) and request.get("request_id") == request_id:
            human_inputs = _mapping(current.get("human_inputs"), "human_inputs")
            response = human_inputs.get(request_id)
            if isinstance(response, Mapping):
                human_inputs[request_id] = {
                    **response,
                    "disposition": "consumed",
                }
                current["human_inputs"] = human_inputs
            current["open_human_request"] = None
            return current, {"disposition": "consumed"}
        return current, {"disposition": "gone"}
    if operation == "orphan_human":
        request_id = _required_string(data, "request_id")
        human_inputs = _mapping(current.get("human_inputs"), "human_inputs")
        response = human_inputs.get(request_id)
        if not isinstance(response, Mapping):
            return current, {"disposition": "gone"}
        orphaned = {**response, "disposition": "orphaned"}
        human_inputs[request_id] = orphaned
        current["human_inputs"] = human_inputs
        return current, orphaned
    if operation == "mark_human_delivery":
        request_id = _required_string(data, "request_id")
        human_inputs = _mapping(current.get("human_inputs"), "human_inputs")
        response = human_inputs.get(request_id)
        if not isinstance(response, Mapping):
            return current, {"disposition": "gone"}
        delivery = _required_string(data, "delivery")
        updated = {**response, "delivery": delivery}
        human_inputs[request_id] = updated
        current["human_inputs"] = human_inputs
        return current, updated
    if operation == "get_human":
        request_id = _required_string(data, "request_id")
        request = current.get("open_human_request")
        if not isinstance(request, Mapping) or request.get("request_id") != request_id:
            return current, {"disposition": "not_found"}
        human_inputs = _mapping(current.get("human_inputs"), "human_inputs")
        response = human_inputs.get(request_id)
        return current, (
            dict(response)
            if isinstance(response, Mapping)
            else {"disposition": "pending"}
        )
    if operation == "get":
        return current, {
            "active_run_id": current.get("active_run_id"),
            "committed_context_ref": current.get("committed_context_ref"),
            "committed_generation": current.get("committed_generation"),
            "disposition": "found",
        }
    raise ValueError(f"unsupported durable-loop entity operation {operation!r}")


def register_durable_loop_blueprint(app: func.FunctionApp) -> None:
    """Register versioned refs-only entity, orchestrators, and activities."""
    blueprint = df.Blueprint()

    @blueprint.entity_trigger(  # type: ignore[untyped-decorator]
        context_name="context",
        entity_name=DURABLE_LOOP_SESSION_ENTITY_NAME,
    )
    def durable_agent_session_entity_v1(context: df.DurableEntityContext) -> None:
        state, result = apply_session_entity_operation(
            context.get_state(lambda: None),
            context.operation_name,
            context.get_input(),
        )
        context.set_state(state)
        context.set_result(result)

    _register_activities(blueprint)
    _register_short_orchestrators(blueprint)

    @blueprint.orchestration_trigger(  # type: ignore[untyped-decorator]
        context_name="context",
        orchestration=DURABLE_LOOP_ORCHESTRATOR_NAME,
    )
    def durable_agent_turn_orchestrator_v1(
        context: df.DurableOrchestrationContext,
    ) -> Any:
        payload = DurableOrchestrationInputV1.model_validate_json(
            canonical_json_bytes(context.get_input())
        )
        entity = df.EntityId(
            DURABLE_LOOP_SESSION_ENTITY_NAME,
            payload.session_entity_key,
        )
        try:
            return (yield from _run_durable_loop(context, entity, payload))
        except Exception:
            yield from _cleanup_execution_plane(context, payload)
            yield context.call_entity(
                entity,
                "abort",
                {
                    "context_ref": payload.run_document_ref.model_dump(mode="json"),
                    "error": "orchestration_failed",
                    "run_id": payload.identity.run_id,
                    "status": DurableLoopRunStatus.FAILED.value,
                },
            )
            raise

    app.register_blueprint(blueprint)


def _register_activities(blueprint: df.Blueprint) -> None:  # noqa: PLR0915
    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_LOOP_MODEL_ACTIVITY_NAME,
    )
    async def durable_agent_model_step_v1(
        payload: dict,  # type: ignore[type-arg]
    ) -> dict[str, object]:
        runtime = get_durable_loop_activity_runtime()
        reference = ContentRefV1.model_validate(payload["run_document_ref"])
        document = await get_protocol_model(
            runtime.content,
            reference,
            DurableRunDocumentV1,
        )
        plan = _plan_from_document(document.plan)
        checkpoint = document.checkpoint
        request = OneStepModelRequest(
            identity=checkpoint.identity,
            step_index=checkpoint.next_model_step,
            instructions=plan.instructions,
            working_context=checkpoint.working_context,
            catalog=plan.catalog,
            model_settings=plan.model_settings,
            fault_profile=plan.fault_profile,
            effective_active_deadline=(
                checkpoint.identity.active_deadline
                + timedelta(seconds=checkpoint.parked_seconds)
            ),
        )
        background_written_bytes = 0
        try:
            async with asyncio.timeout(plan.settings.activity_timeout_seconds):
                if runtime.background_model is None:
                    decision = await runtime.model.run_one_step(request)
                else:
                    started = await runtime.background_model.start(request)
                    background_written_bytes = started.written_bytes
                    if started.operation is not None:
                        operation_ref = await put_protocol_model(
                            runtime.content,
                            kind="model-operation",
                            model=started.operation,
                        )
                        return ModelStepActivityResultV1(
                            run_document_ref=reference,
                            step_index=checkpoint.next_model_step,
                            background_operation_ref=operation_ref,
                            poll_after_seconds=plan.settings.poll_initial_seconds,
                            written_bytes=(
                                started.written_bytes
                                or operation_ref.byte_length
                            ),
                        ).model_dump(mode="json")
                    if started.error is not None:
                        return await _persist_model_error(
                            runtime,
                            document,
                            started.error,
                            extra_written_bytes=started.written_bytes,
                        )
                    if started.decision is None:
                        raise RuntimeError(
                            "background model start returned no outcome"
                        )
                    decision = started.decision
        except TimeoutError:
            raise RuntimeError("durable model activity timed out") from None
        except DurableLoopProviderTerminalError as exc:
            error = ErrorEnvelopeV1(
                code=exc.code,
                classification="model",
                retryable=False,
                phase="model_step",
                step_index=checkpoint.next_model_step,
            )
            failed = document.model_copy(
                update={
                    "checkpoint": _checkpoint_with_usage(
                        checkpoint,
                        exc.usage,
                    ).model_copy(
                        update={
                            "last_error": error,
                            "status": DurableLoopRunStatus.FAILED,
                        }
                    )
                }
            )
            failed_ref = await put_protocol_model(
                runtime.content,
                kind="run-document",
                model=failed,
            )
            return ModelStepActivityResultV1(
                run_document_ref=failed_ref,
                step_index=checkpoint.next_model_step,
                error=error,
                usage=exc.usage,
                written_bytes=failed_ref.byte_length,
            ).model_dump(mode="json")
        return await _persist_model_decision(
            runtime,
            document,
            decision,
            extra_written_bytes=(
                background_written_bytes
            ),
        )

    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_LOOP_MODEL_POLL_ACTIVITY_NAME,
    )
    async def durable_agent_model_poll_v1(
        payload: dict,  # type: ignore[type-arg]
    ) -> dict[str, object]:
        runtime = get_durable_loop_activity_runtime()
        if runtime.background_model is None:
            raise RuntimeError("background model provider is unavailable")
        document = await get_protocol_model(
            runtime.content,
            ContentRefV1.model_validate(payload["run_document_ref"]),
            DurableRunDocumentV1,
        )
        operation = await get_protocol_model(
            runtime.content,
            ContentRefV1.model_validate(payload["operation_ref"]),
            ModelOperationV1,
        )
        plan = _plan_from_document(document.plan)
        polled = await runtime.background_model.poll(operation)
        if polled.operation is not None:
            operation_ref = await put_protocol_model(
                runtime.content,
                kind="model-operation",
                model=polled.operation,
            )
            return ModelStepActivityResultV1(
                run_document_ref=ContentRefV1.model_validate(
                    payload["run_document_ref"]
                ),
                step_index=operation.step_index,
                background_operation_ref=operation_ref,
                poll_after_seconds=min(
                    plan.settings.poll_max_seconds,
                    plan.settings.poll_initial_seconds
                    * (2 ** min(polled.operation.poll_count, 8)),
                ),
                written_bytes=(
                    polled.written_bytes
                    or operation_ref.byte_length
                ),
            ).model_dump(mode="json")
        if polled.error is not None:
            return await _persist_model_error(
                runtime,
                document,
                polled.error,
                extra_written_bytes=polled.written_bytes,
            )
        if polled.decision is None:
            raise RuntimeError("background model poll returned no outcome")
        return await _persist_model_decision(
            runtime,
            document,
            polled.decision,
            extra_written_bytes=polled.written_bytes,
        )

    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_LOOP_MODEL_CANCEL_ACTIVITY_NAME,
    )
    async def durable_agent_model_cancel_v1(
        payload: dict,  # type: ignore[type-arg]
    ) -> dict[str, object]:
        runtime = get_durable_loop_activity_runtime()
        if runtime.background_model is None:
            return {"status": ModelOperationStatus.CANCELLED.value}
        operation_ref = ContentRefV1.model_validate(payload["operation_ref"])
        operation = await get_protocol_model(
            runtime.content,
            operation_ref,
            ModelOperationV1,
        )
        cancelled = await runtime.background_model.cancel(operation)
        cancelled_ref = await put_protocol_model(
            runtime.content,
            kind="model-operation",
            model=cancelled,
        )
        return {
            "operation_ref": cancelled_ref.model_dump(mode="json"),
            "status": cancelled.status.value,
            "written_bytes": cancelled_ref.byte_length,
        }

    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_LOOP_CLEANUP_ACTIVITY_NAME,
    )
    async def durable_agent_cleanup_v1(
        payload: dict,  # type: ignore[type-arg]
    ) -> dict[str, object]:
        runtime = get_durable_loop_activity_runtime()
        if isinstance(runtime.tools, DurableToolCleanupPort):
            run_id = _required_string(payload, "run_id")
            session_id = _required_string(payload, "session_id")
            sandbox_profile = SandboxExecutionProfile(
                _required_string(payload, "sandbox_profile")
            )
            fault_profile = DurableFaultProfile(
                _required_string(payload, "fault_profile")
            )
            try:
                await runtime.tools.cleanup(
                    run_id=run_id,
                    session_id=session_id,
                    sandbox_profile=sandbox_profile,
                    fault_profile=fault_profile,
                )
            except Exception as exc:
                if fault_profile is DurableFaultProfile.CLEANUP_FAILURE_ONCE:
                    try:
                        await runtime.tools.cleanup(
                            run_id=run_id,
                            session_id=session_id,
                            sandbox_profile=sandbox_profile,
                            fault_profile=fault_profile,
                        )
                    except Exception as retry_exc:
                        logger.error(
                            "Durable-loop cleanup retry failed "
                            "(error_type=%s).",
                            type(retry_exc).__name__,
                        )
                        return {"cleaned": False}
                else:
                    logger.error(
                        "Durable-loop cleanup failed (error_type=%s).",
                        type(exc).__name__,
                    )
                    return {"cleaned": False}
        return {"cleaned": True}

    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_LOOP_FAULT_ACTIVITY_NAME,
    )
    async def durable_agent_fault_v1(
        payload: dict,  # type: ignore[type-arg]
    ) -> dict[str, object]:
        runtime = get_durable_loop_activity_runtime()
        injected = False
        if runtime.faults is not None:
            injected = await runtime.faults.consume(
                DurableFaultProfile(_required_string(payload, "fault_profile")),
                run_id=_required_string(payload, "run_id"),
                point=_required_string(payload, "point"),
            )
        return {"injected": injected}

    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_LOOP_TOOL_ACTIVITY_NAME,
    )
    async def durable_agent_tool_step_v1(
        payload: dict,  # type: ignore[type-arg]
    ) -> dict[str, object]:
        runtime = get_durable_loop_activity_runtime()
        request_ref = ContentRefV1.model_validate(payload["request_ref"])
        request = await get_protocol_model(
            runtime.content,
            request_ref,
            ToolRequestV1,
        )
        remaining = (
            request.deadline.astimezone(UTC) - datetime.now(UTC)
        ).total_seconds()
        try:
            if (
                len(canonical_json_bytes(request.arguments))
                > request.argument_byte_limit
            ):
                result = _tool_limit_result(
                    request,
                    code="tool_arguments_too_large",
                )
            else:
                async with asyncio.timeout(max(0.001, remaining)):
                    result = await runtime.tools.dispatch(request)
                if _tool_result_size(result) > request.result_byte_limit:
                    result = _tool_limit_result(
                        request,
                        code="tool_result_too_large",
                    )
        except TimeoutError:
            result = ToolResultV1(
                run_id=request.run_id,
                step_index=request.step_index,
                call_ordinal=request.call_ordinal,
                provider_call_id=request.provider_call_id,
                call_key=request.call_key,
                request_hash=request.request_hash,
                tool_name=request.tool_name,
                status=ToolResultStatus.TIMED_OUT,
                elapsed_ms=max(0.0, remaining * 1000.0),
                error=ErrorEnvelopeV1(
                    code="tool_timeout",
                    classification="timeout",
                    retryable=request.behavior is ToolBehavior.READ_ONLY,
                    phase="tool_step",
                    step_index=request.step_index,
                    call_key=request.call_key,
                ),
            )
        if runtime.faults is not None and await runtime.faults.consume(
            request.fault_profile,
            run_id=request.run_id,
            point="tool_activity_ack_loss",
        ):
            if (
                request.behavior is ToolBehavior.MUTATING
                and result.status is ToolResultStatus.SUCCEEDED
            ):
                result = ToolResultV1(
                    run_id=request.run_id,
                    step_index=request.step_index,
                    call_ordinal=request.call_ordinal,
                    provider_call_id=request.provider_call_id,
                    call_key=request.call_key,
                    request_hash=request.request_hash,
                    tool_name=request.tool_name,
                    status=ToolResultStatus.AMBIGUOUS,
                    elapsed_ms=result.elapsed_ms,
                    error=ErrorEnvelopeV1(
                        code="tool_acknowledgement_lost",
                        classification="tool",
                        retryable=False,
                        disposition=ErrorDisposition.AMBIGUOUS,
                        possibly_committed=True,
                        phase="tool_step",
                        step_index=request.step_index,
                        call_key=request.call_key,
                    ),
                )
            else:
                result = await runtime.tools.dispatch(request)
        result_ref = await put_protocol_model(
            runtime.content,
            kind="tool-result",
            model=result,
        )
        written_bytes = result_ref.byte_length
        if result.workspace_ref is not None:
            workspace = await get_protocol_model(
                runtime.content,
                result.workspace_ref,
                WorkspaceArtifactV1,
            )
            written_bytes += (
                result.workspace_ref.byte_length
                + workspace.archive_ref.byte_length
            )
        return ToolResultRefV1(
            result_ref=result_ref,
            call_ordinal=result.call_ordinal,
            call_key=result.call_key,
            request_hash=result.request_hash,
            tool_name=result.tool_name,
            status=result.status,
            written_bytes=written_bytes,
        ).model_dump(mode="json")

    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_LOOP_APPEND_ACTIVITY_NAME,
    )
    async def durable_agent_append_results_v1(
        payload: dict,  # type: ignore[type-arg]
    ) -> dict[str, object]:
        runtime = get_durable_loop_activity_runtime()
        document_ref = ContentRefV1.model_validate(payload["run_document_ref"])
        document = await get_protocol_model(
            runtime.content,
            document_ref,
            DurableRunDocumentV1,
        )
        decision = document.pending_decision
        if decision is None:
            raise ValueError("run document has no pending model decision")
        if payload.get("protocol_error") is True:
            request_refs = [
                ContentRefV1.model_validate(item)
                for item in _sequence(payload.get("request_refs"), "request_refs")
            ]
            requests = [
                await get_protocol_model(
                    runtime.content,
                    item,
                    ToolRequestV1,
                )
                for item in request_refs
            ]
            results = [_protocol_error_result(item) for item in requests]
        else:
            result_refs = [
                ToolResultRefV1.model_validate_json(canonical_json_bytes(item))
                for item in _sequence(payload.get("result_refs"), "result_refs")
            ]
            results = [
                await get_protocol_model(
                    runtime.content,
                    item.result_ref,
                    ToolResultV1,
                )
                for item in result_refs
            ]
            cancelled_request_refs = [
                ContentRefV1.model_validate(item)
                for item in _sequence(
                    payload.get("cancelled_request_refs", ()),
                    "cancelled_request_refs",
                )
            ]
            for item in cancelled_request_refs:
                request = await get_protocol_model(
                    runtime.content,
                    item,
                    ToolRequestV1,
                )
                results.append(_cancelled_tool_result(request))
        working = append_model_and_tool_messages(
            document.checkpoint.working_context,
            decision,
            results,
        )
        audit = append_audit_model_and_tool(
            document.checkpoint.audit_bundle,
            decision,
            results,
        )
        working = working.model_copy(
            update={"source_audit_hash": audit.bundle_hash}
        )
        checkpoint = document.checkpoint.model_copy(
            update={
                "audit_bundle": audit,
                "audit_head_hash": audit.bundle_hash,
                "checkpoints_in_generation": (
                    document.checkpoint.checkpoints_in_generation + 1
                ),
                "completed_model_steps": (
                    document.checkpoint.completed_model_steps + 1
                ),
                "completed_tool_calls": (
                    document.checkpoint.completed_tool_calls + len(results)
                ),
                "next_model_step": document.checkpoint.next_model_step + 1,
                "status": DurableLoopRunStatus.RUNNING,
                "working_context": working,
                "workspace_ref": next(
                    (
                        result.workspace_ref
                        for result in sorted(
                            results,
                            key=lambda item: item.call_ordinal,
                            reverse=True,
                        )
                        if result.workspace_ref is not None
                    ),
                    document.checkpoint.workspace_ref,
                ),
            }
        )
        updated = document.model_copy(
            update={"checkpoint": checkpoint, "pending_decision": None}
        )
        next_ref = await put_protocol_model(
            runtime.content,
            kind="run-document",
            model=updated,
        )
        return {
            "run_document_ref": next_ref.model_dump(mode="json"),
            "written_bytes": next_ref.byte_length,
            "working_context_bytes": len(
                canonical_json_bytes(working.bundle.messages)
            ),
        }

    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_LOOP_HUMAN_ACTIVITY_NAME,
    )
    async def durable_agent_human_request_v1(
        payload: dict,  # type: ignore[type-arg]
    ) -> dict[str, object]:
        runtime = get_durable_loop_activity_runtime()
        document_ref = ContentRefV1.model_validate(payload["run_document_ref"])
        request_ref = ContentRefV1.model_validate(payload["request_ref"])
        document = await get_protocol_model(
            runtime.content,
            document_ref,
            DurableRunDocumentV1,
        )
        request = await get_protocol_model(
            runtime.content,
            request_ref,
            ToolRequestV1,
        )
        arguments = request.arguments
        question = arguments.get("question")
        if not isinstance(question, str) or not question:
            raise ValueError("human clarification question is required")
        choices = arguments.get("choices") or []
        if not isinstance(choices, list) or not all(
            isinstance(item, str) for item in choices
        ):
            raise ValueError("human clarification choices are invalid")
        allow_free_text = arguments.get("allow_free_text")
        if not isinstance(allow_free_text, bool):
            raise ValueError("allow_free_text must be a boolean")
        response_schema = arguments.get("response_schema")
        if response_schema is not None and not isinstance(response_schema, dict):
            raise ValueError("response_schema must be an object")
        question_ref = await runtime.content.put_bytes(
            kind="human-question",
            payload=question.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            retention_class="run",
        )
        generation = document.checkpoint.continue_as_new_generation + 1
        request_id = (
            f"human-{document.checkpoint.next_model_step}-"
            f"{request.call_key[:16]}"
        )
        event_name = deterministic_human_event_name(
            generation=generation,
            request_id=request_id,
            nonce=f"{document.checkpoint.identity.run_id}:{request.provider_call_id}",
        )
        human = HumanInputRequestV1(
            request_id=request_id,
            run_id=document.checkpoint.identity.run_id,
            session_id=document.checkpoint.identity.session_id,
            generation=generation,
            turn_index=document.checkpoint.committed_session_generation,
            step_index=document.checkpoint.next_model_step,
            call_id=request.provider_call_id,
            call_key=request.call_key,
            request_hash=request.request_hash,
            question_ref=question_ref,
            choices=tuple(choices),
            allow_free_text=allow_free_text,
            response_schema=response_schema,
            actor_policy_hash=document.checkpoint.identity.owner_hash,
            event_name=event_name,
            issued_at=_datetime_value(payload["issued_at"], "issued_at"),
            expires_at=_datetime_value(payload["expires_at"], "expires_at"),
            record_version=1,
        )
        human_ref = await put_protocol_model(
            runtime.content,
            kind="human-request",
            model=human,
        )
        return HumanWaitActivityResultV1(
            request_ref=human_ref,
            request_id=request_id,
            run_id=human.run_id,
            generation=generation,
            call_key=human.call_key,
            event_name=event_name,
            question_ref=question_ref,
            issued_at=human.issued_at,
            expires_at=human.expires_at,
            written_bytes=question_ref.byte_length + human_ref.byte_length,
        ).model_dump(mode="json")

    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_LOOP_HUMAN_RESULT_ACTIVITY_NAME,
    )
    async def durable_agent_human_result_v1(
        payload: dict,  # type: ignore[type-arg]
    ) -> dict[str, object]:
        runtime = get_durable_loop_activity_runtime()
        document_ref = ContentRefV1.model_validate(payload["run_document_ref"])
        request_ref = ContentRefV1.model_validate(payload["request_ref"])
        document = await get_protocol_model(
            runtime.content,
            document_ref,
            DurableRunDocumentV1,
        )
        human = await get_protocol_model(
            runtime.content,
            request_ref,
            HumanInputRequestV1,
        )
        response_ref_data = payload.get("response_ref")
        if response_ref_data is None:
            result = _timed_out_human_result(human)
        else:
            response = await get_protocol_model(
                runtime.content,
                ContentRefV1.model_validate(response_ref_data),
                HumanInputResponseV1,
            )
            answer = await runtime.content.get_bytes(response.answer_ref)
            result = _answered_human_result(human, answer)
        working = append_tool_result_messages(
            _append_pending_assistant(document),
            (result,),
        )
        decision = document.pending_decision
        if decision is None:
            raise ValueError("run document has no pending decision")
        audit = append_audit_model_and_tool(
            document.checkpoint.audit_bundle,
            decision,
            (result,),
        )
        working = working.model_copy(
            update={"source_audit_hash": audit.bundle_hash}
        )
        checkpoint = document.checkpoint.model_copy(
            update={
                "audit_bundle": audit,
                "audit_head_hash": audit.bundle_hash,
                "checkpoints_in_generation": (
                    document.checkpoint.checkpoints_in_generation + 1
                ),
                "completed_model_steps": (
                    document.checkpoint.completed_model_steps + 1
                ),
                "completed_tool_calls": (
                    document.checkpoint.completed_tool_calls + 1
                ),
                "human_wait_count": document.checkpoint.human_wait_count + 1,
                "next_model_step": document.checkpoint.next_model_step + 1,
                "parked_seconds": (
                    document.checkpoint.parked_seconds
                    + _nonnegative_int(
                        payload.get("parked_seconds", 0),
                        "parked_seconds",
                    )
                ),
                "status": DurableLoopRunStatus.RUNNING,
                "working_context": working,
            }
        )
        updated = document.model_copy(
            update={"checkpoint": checkpoint, "pending_decision": None}
        )
        next_ref = await put_protocol_model(
            runtime.content,
            kind="run-document",
            model=updated,
        )
        return {
            "run_document_ref": next_ref.model_dump(mode="json"),
            "written_bytes": (
                next_ref.byte_length
                + (
                    0
                    if response_ref_data is None
                    else ContentRefV1.model_validate(response_ref_data).byte_length
                    + response.answer_ref.byte_length
                )
            ),
            "working_context_bytes": len(
                canonical_json_bytes(working.bundle.messages)
            ),
        }

    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
    )
    @blueprint.durable_client_input(  # type: ignore[untyped-decorator]
        client_name="client"
    )
    async def durable_agent_human_delivery_v1(
        payload: dict,  # type: ignore[type-arg]
        client: df.DurableOrchestrationClient,
    ) -> dict[str, object]:
        run_id = _required_string(payload, "run_id")
        event_name = _required_string(payload, "event_name")
        event_data = _mapping(payload.get("event_data"), "event_data")
        if client is None:
            result = await get_durable_loop_activity_runtime().human_events.deliver(
                run_id=run_id,
                event_name=event_name,
                event_data=event_data,
            )
        else:
            result = await _deliver_event_with_durable_client(
                client,
                run_id=run_id,
                event_name=event_name,
                event_data=event_data,
            )
        return result.model_dump(mode="json")

    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_LOOP_COMPACTION_ACTIVITY_NAME,
    )
    async def durable_agent_compact_context_v1(
        payload: dict,  # type: ignore[type-arg]
    ) -> dict[str, object]:
        runtime = get_durable_loop_activity_runtime()
        reference = ContentRefV1.model_validate(payload["run_document_ref"])
        document = await get_protocol_model(
            runtime.content,
            reference,
            DurableRunDocumentV1,
        )
        settings = _plan_from_document(document.plan).settings
        try:
            async with asyncio.timeout(settings.activity_timeout_seconds):
                compacted = await runtime.compactor.compact(
                    document.checkpoint.working_context,
                    maximum_bytes=_nonnegative_int(
                        payload["maximum_bytes"],
                        "maximum_bytes",
                    ),
                )
        except TimeoutError:
            raise RuntimeError("durable compaction activity timed out") from None
        updated = document.model_copy(
            update={
                "checkpoint": document.checkpoint.model_copy(
                    update={"working_context": compacted}
                )
            }
        )
        next_ref = await put_protocol_model(
            runtime.content,
            kind="run-document",
            model=updated,
        )
        return {
            "run_document_ref": next_ref.model_dump(mode="json"),
            "written_bytes": next_ref.byte_length,
            "working_context_bytes": len(
                canonical_json_bytes(compacted.bundle.messages)
            ),
        }


def _register_short_orchestrators(blueprint: df.Blueprint) -> None:
    @blueprint.orchestration_trigger(  # type: ignore[untyped-decorator]
        context_name="context",
        orchestration=DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
    )
    def durable_agent_admission_v1(
        context: df.DurableOrchestrationContext,
    ) -> Any:
        return (yield from _call_session_entity(context, "admit"))

    @blueprint.orchestration_trigger(  # type: ignore[untyped-decorator]
        context_name="context",
        orchestration=DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME,
    )
    def durable_agent_human_input_outbox_v1(
        context: df.DurableOrchestrationContext,
    ) -> Any:
        return (yield from _call_session_entity(context, "accept_human"))

    @blueprint.orchestration_trigger(  # type: ignore[untyped-decorator]
        context_name="context",
        orchestration=DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME,
    )
    def durable_agent_human_delivery_outbox_v1(
        context: df.DurableOrchestrationContext,
    ) -> Any:
        payload = _mapping(context.get_input(), "human_delivery")
        entity = df.EntityId(
            DURABLE_LOOP_SESSION_ENTITY_NAME,
            _required_string(payload, "session_entity_key"),
        )
        request_id = _required_string(payload, "request_id")
        expires_at = _datetime_value(payload["expires_at"], "expires_at")
        attempt = _nonnegative_int(payload.get("attempt", 0), "attempt")
        while context.current_utc_datetime < expires_at:
            receipt = yield context.call_entity(
                entity,
                "get_human",
                {"request_id": request_id},
            )
            if receipt.get("delivery") in {"delivered", "orphaned"}:
                return receipt
            result_data = yield context.call_activity(
                DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
                {
                    "event_data": _mapping(
                        payload.get("event_data"),
                        "event_data",
                    ),
                    "event_name": _required_string(payload, "event_name"),
                    "run_id": _required_string(payload, "run_id"),
                },
            )
            result = HumanEventDeliveryResultV1.model_validate_json(
                canonical_json_bytes(result_data)
            )
            if result.status is HumanEventDeliveryStatus.DELIVERED:
                return (
                    yield context.call_entity(
                        entity,
                        "mark_human_delivery",
                        {
                            "delivery": "delivered",
                            "request_id": request_id,
                        },
                    )
                )
            if result.status is HumanEventDeliveryStatus.TERMINAL:
                return (
                    yield context.call_entity(
                        entity,
                        "orphan_human",
                        {"request_id": request_id},
                    )
                )
            attempt += 1
            if attempt % 20 == 0:
                context.continue_as_new({**payload, "attempt": attempt})
                return None
            yield context.create_timer(
                min(
                    expires_at,
                    context.current_utc_datetime
                    + timedelta(seconds=max(1.0, result.retry_after_seconds)),
                )
            )
        return {"delivery": "retry_expired", "disposition": "accepted"}

    @blueprint.orchestration_trigger(  # type: ignore[untyped-decorator]
        context_name="context",
        orchestration=DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME,
    )
    def durable_agent_cancel_delivery_outbox_v1(
        context: df.DurableOrchestrationContext,
    ) -> Any:
        payload = _mapping(context.get_input(), "cancel_delivery")
        expires_at = _datetime_value(payload["expires_at"], "expires_at")
        attempt = _nonnegative_int(payload.get("attempt", 0), "attempt")
        while context.current_utc_datetime < expires_at:
            result_data = yield context.call_activity(
                DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
                {
                    "event_data": _mapping(
                        payload.get("event_data"),
                        "event_data",
                    ),
                    "event_name": _required_string(payload, "event_name"),
                    "run_id": _required_string(payload, "run_id"),
                },
            )
            result = HumanEventDeliveryResultV1.model_validate_json(
                canonical_json_bytes(result_data)
            )
            if result.status in {
                HumanEventDeliveryStatus.DELIVERED,
                HumanEventDeliveryStatus.TERMINAL,
            }:
                return result.model_dump(mode="json")
            attempt += 1
            if attempt % 20 == 0:
                context.continue_as_new({**payload, "attempt": attempt})
                return None
            yield context.create_timer(
                min(
                    expires_at,
                    context.current_utc_datetime
                    + timedelta(seconds=max(1.0, result.retry_after_seconds)),
                )
            )
        return {"status": HumanEventDeliveryStatus.TERMINAL.value}

    @blueprint.orchestration_trigger(  # type: ignore[untyped-decorator]
        context_name="context",
        orchestration=DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
    )
    def durable_agent_control_v1(
        context: df.DurableOrchestrationContext,
    ) -> Any:
        payload = _mapping(context.get_input(), "control")
        operation = _required_string(payload, "operation")
        if operation not in {
            "abort",
            "cancel",
            "get",
            "mark_human_delivery",
            "orphan_human",
            "release_admission",
            "reserve_human",
        }:
            raise ValueError("unsupported durable-loop control operation")
        return (yield from _call_session_entity(context, operation))


def _run_durable_loop(  # noqa: PLR0912, PLR0915
    context: df.DurableOrchestrationContext,
    entity: df.EntityId,
    payload: DurableOrchestrationInputV1,
) -> Any:
    admission = yield context.call_entity(
        entity,
        "admit",
        {
            "request_hash": payload.identity.request_hash,
            "request_id_hash": payload.identity.request_id_hash,
            "run_id": payload.identity.run_id,
        },
    )
    if admission.get("disposition") not in {"admitted", "replayed"}:
        return {
            "error": admission.get("disposition"),
            "status": DurableLoopRunStatus.FAILED.value,
        }
    current = payload
    running = yield context.call_entity(
        entity,
        "mark_running",
        {"run_id": current.identity.run_id},
    )
    if running.get("disposition") not in {"running", "replayed"}:
        return {
            "error": "session_fence_lost",
            "status": DurableLoopRunStatus.FAILED.value,
        }
    while True:
        cancelled = yield context.call_entity(
            entity,
            "is_cancelled",
            {"run_id": current.identity.run_id},
        )
        if cancelled.get("cancelled") is True:
            yield from _cleanup_execution_plane(context, current)
            yield context.call_entity(
                entity,
                "abort",
                {
                    "context_ref": current.run_document_ref.model_dump(mode="json"),
                    "run_id": current.identity.run_id,
                    "status": DurableLoopRunStatus.CANCELLED.value,
                },
            )
            return {"status": DurableLoopRunStatus.CANCELLED.value}
        compaction_threshold = (
            current.identity.budget.context_max_bytes
            * current.identity.budget.context_compaction_percent
            // 100
        )
        if (
            current.working_context_bytes >= compaction_threshold
            and current.last_compacted_step != current.next_model_step
        ):
            compacted = yield context.call_activity(
                DURABLE_LOOP_COMPACTION_ACTIVITY_NAME,
                {
                    "maximum_bytes": current.identity.budget.context_max_bytes,
                    "run_document_ref": current.run_document_ref.model_dump(
                        mode="json"
                    ),
                },
            )
            current = current.model_copy(
                update={
                    "checkpoints_in_generation": (
                        current.checkpoints_in_generation + 1
                    ),
                    "last_compacted_step": current.next_model_step,
                    "external_content_bytes": (
                        current.external_content_bytes
                        + _nonnegative_int(
                            compacted.get("written_bytes", 0),
                            "written_bytes",
                        )
                    ),
                    "run_document_ref": ContentRefV1.model_validate(
                        compacted["run_document_ref"]
                    ),
                    "working_context_bytes": _nonnegative_int(
                        compacted.get("working_context_bytes"),
                        "working_context_bytes",
                    ),
                }
            )
        budget_error = _orchestration_budget_error(current, context)
        if budget_error is not None:
            yield from _cleanup_execution_plane(context, current)
            yield context.call_entity(
                entity,
                "abort",
                {
                    "context_ref": current.run_document_ref.model_dump(mode="json"),
                    "error": budget_error,
                    "run_id": current.identity.run_id,
                    "status": DurableLoopRunStatus.FAILED.value,
                },
            )
            return {
                "error": budget_error,
                "status": DurableLoopRunStatus.FAILED.value,
            }
        context.set_custom_status(
            {
                "cost_microunits": current.cost_microunits,
                "external_content_bytes": current.external_content_bytes,
                "input_tokens": current.input_tokens,
                "output_tokens": current.output_tokens,
                "parked_seconds": current.parked_seconds,
                "phase": "model_step",
                "reasoning_tokens": current.reasoning_tokens,
                "status": DurableLoopRunStatus.RUNNING.value,
                "step_index": current.next_model_step,
            }
        )
        model_result_data = yield context.call_activity(
            DURABLE_LOOP_MODEL_ACTIVITY_NAME,
            {
                "run_document_ref": current.run_document_ref.model_dump(
                    mode="json"
                )
            },
        )
        model_result = ModelStepActivityResultV1.model_validate_json(
            canonical_json_bytes(model_result_data)
        )
        current = current.model_copy(
            update={
                "cost_microunits": (
                    current.cost_microunits
                    + (model_result.usage.cost_microunits or 0)
                ),
                "input_tokens": (
                    current.input_tokens + model_result.usage.input_tokens
                ),
                "output_tokens": (
                    current.output_tokens + model_result.usage.output_tokens
                ),
                "reasoning_tokens": (
                    current.reasoning_tokens
                    + model_result.usage.reasoning_tokens
                ),
                "external_content_bytes": (
                    current.external_content_bytes + model_result.written_bytes
                ),
                "run_document_ref": model_result.run_document_ref,
            }
        )
        while model_result.background_operation_ref is not None:
            context.set_custom_status(
                {
                    "phase": "background_poll",
                    "status": DurableLoopRunStatus.WAITING.value,
                    "step_index": current.next_model_step,
                }
            )
            timer = context.create_timer(
                min(
                    current.identity.active_deadline
                    + timedelta(seconds=current.parked_seconds),
                    context.current_utc_datetime
                    + timedelta(seconds=model_result.poll_after_seconds or 1.0),
                )
            )
            cancel_event = context.wait_for_external_event(
                DURABLE_LOOP_CANCEL_EVENT_NAME
            )
            winner = yield context.task_any([timer, cancel_event])
            if winner == cancel_event:
                timer.cancel()
                yield context.call_activity(
                    DURABLE_LOOP_MODEL_CANCEL_ACTIVITY_NAME,
                    {
                        "operation_ref": (
                            model_result.background_operation_ref.model_dump(
                                mode="json"
                            )
                        )
                    },
                )
                yield from _cleanup_execution_plane(context, current)
                yield context.call_entity(
                    entity,
                    "abort",
                    {
                        "context_ref": current.run_document_ref.model_dump(
                            mode="json"
                        ),
                        "run_id": current.identity.run_id,
                        "status": DurableLoopRunStatus.CANCELLED.value,
                    },
                )
                return {"status": DurableLoopRunStatus.CANCELLED.value}
            polled_data = yield context.call_activity(
                DURABLE_LOOP_MODEL_POLL_ACTIVITY_NAME,
                {
                    "operation_ref": (
                        model_result.background_operation_ref.model_dump(
                            mode="json"
                        )
                    ),
                    "run_document_ref": current.run_document_ref.model_dump(
                        mode="json"
                    ),
                },
            )
            model_result = ModelStepActivityResultV1.model_validate_json(
                canonical_json_bytes(polled_data)
            )
            current = current.model_copy(
                update={
                    "cost_microunits": (
                        current.cost_microunits
                        + (model_result.usage.cost_microunits or 0)
                    ),
                    "input_tokens": (
                        current.input_tokens + model_result.usage.input_tokens
                    ),
                    "output_tokens": (
                        current.output_tokens + model_result.usage.output_tokens
                    ),
                    "reasoning_tokens": (
                        current.reasoning_tokens
                        + model_result.usage.reasoning_tokens
                    ),
                    "external_content_bytes": (
                        current.external_content_bytes
                        + model_result.written_bytes
                    ),
                    "run_document_ref": model_result.run_document_ref,
                }
            )
        cancelled_after_model = yield context.call_entity(
            entity,
            "is_cancelled",
            {"run_id": current.identity.run_id},
        )
        if cancelled_after_model.get("cancelled") is True:
            yield from _cleanup_execution_plane(context, current)
            yield context.call_entity(
                entity,
                "abort",
                {
                    "context_ref": current.run_document_ref.model_dump(mode="json"),
                    "run_id": current.identity.run_id,
                    "status": DurableLoopRunStatus.CANCELLED.value,
                },
            )
            return {"status": DurableLoopRunStatus.CANCELLED.value}
        if model_result.error is not None:
            yield from _cleanup_execution_plane(context, current)
            yield context.call_entity(
                entity,
                "abort",
                {
                    "disposition": model_result.error.disposition.value,
                    "error": model_result.error.code,
                    "possibly_committed": model_result.error.possibly_committed,
                    "run_id": current.identity.run_id,
                    "status": DurableLoopRunStatus.FAILED.value,
                },
            )
            return {
                "error": model_result.error.code,
                "status": DurableLoopRunStatus.FAILED.value,
            }
        usage_error = _orchestration_budget_error(current, context)
        if usage_error is not None:
            yield from _cleanup_execution_plane(context, current)
            yield context.call_entity(
                entity,
                "abort",
                {
                    "error": usage_error,
                    "run_id": current.identity.run_id,
                    "status": DurableLoopRunStatus.FAILED.value,
                },
            )
            return {
                "error": usage_error,
                "status": DurableLoopRunStatus.FAILED.value,
            }
        if model_result.final_response_ref is not None:
            commit_key = canonical_hash(
                {
                    "context_ref": model_result.run_document_ref.model_dump(
                        mode="json"
                    ),
                    "expected_generation": current.committed_session_generation,
                    "request_hash": current.identity.request_hash,
                    "response_ref": model_result.final_response_ref.model_dump(
                        mode="json"
                    ),
                    "run_id": current.identity.run_id,
                }
            )
            completion_payload = {
                "commit_key": commit_key,
                "context_ref": model_result.run_document_ref.model_dump(
                    mode="json"
                ),
                "expected_generation": current.committed_session_generation,
                "request_hash": current.identity.request_hash,
                "response_ref": model_result.final_response_ref.model_dump(
                    mode="json"
                ),
                "run_id": current.identity.run_id,
            }
            completion = yield context.call_entity(
                entity,
                "complete",
                completion_payload,
            )
            if completion.get("disposition") == "cancelled":
                yield from _cleanup_execution_plane(context, current)
                yield context.call_entity(
                    entity,
                    "abort",
                    {
                        "context_ref": current.run_document_ref.model_dump(mode="json"),
                        "run_id": current.identity.run_id,
                        "status": DurableLoopRunStatus.CANCELLED.value,
                    },
                )
                return {"status": DurableLoopRunStatus.CANCELLED.value}
            if completion.get("disposition") != "completed":
                raise RuntimeError("session commit did not match the active fence")
            if (
                current.fault_profile
                is DurableFaultProfile.COMMIT_ACK_LOSS_ONCE
            ):
                fault = yield context.call_activity(
                    DURABLE_LOOP_FAULT_ACTIVITY_NAME,
                    {
                        "fault_profile": current.fault_profile.value,
                        "point": "commit_ack_loss",
                        "run_id": current.identity.run_id,
                    },
                )
            else:
                fault = {"injected": False}
            if fault.get("injected") is True:
                completion = yield context.call_entity(
                    entity,
                    "complete",
                    completion_payload,
                )
                if completion.get("disposition") != "completed":
                    raise RuntimeError(
                        "session commit receipt was not idempotent"
                    )
            return {
                "committed_generation": completion.get("committed_generation"),
                "cost_microunits": current.cost_microunits,
                "external_content_bytes": current.external_content_bytes,
                "human_waits": current.human_wait_count,
                "input_tokens": current.input_tokens,
                "model_steps": current.completed_model_steps + 1,
                "output_tokens": current.output_tokens,
                "parked_seconds": current.parked_seconds,
                "phase": "completed",
                "reasoning_tokens": current.reasoning_tokens,
                "response_ref": model_result.final_response_ref.model_dump(
                    mode="json"
                ),
                "status": DurableLoopRunStatus.COMPLETED.value,
                "step_index": current.next_model_step + 1,
                "tool_calls": current.completed_tool_calls,
            }

        clarification = [
            item
            for item in model_result.tool_calls
            if item.tool_name == REQUEST_HUMAN_INPUT_TOOL_NAME
        ]
        if clarification:
            if len(model_result.tool_calls) != 1:
                if current.repair_steps_used >= 1:
                    yield from _cleanup_execution_plane(context, current)
                    yield context.call_entity(
                        entity,
                        "abort",
                        {
                            "context_ref": current.run_document_ref.model_dump(mode="json"),
                            "error": "invalid_clarification_batch",
                            "run_id": current.identity.run_id,
                            "status": DurableLoopRunStatus.FAILED.value,
                        },
                    )
                    return {
                        "error": "invalid_clarification_batch",
                        "status": DurableLoopRunStatus.FAILED.value,
                    }
                repaired = yield context.call_activity(
                    DURABLE_LOOP_APPEND_ACTIVITY_NAME,
                    {
                        "protocol_error": True,
                        "request_refs": [
                            item.request_ref.model_dump(mode="json")
                            for item in model_result.tool_calls
                        ],
                        "run_document_ref": current.run_document_ref.model_dump(
                            mode="json"
                        ),
                    },
                )
                current = _advance_cursor(
                    current,
                    ContentRefV1.model_validate(
                        repaired["run_document_ref"]
                    ),
                    model_steps=1,
                    tool_calls=len(model_result.tool_calls),
                    working_context_bytes=_nonnegative_int(
                        repaired.get("working_context_bytes"),
                        "working_context_bytes",
                    ),
                ).model_copy(
                    update={"repair_steps_used": current.repair_steps_used + 1}
                )
                continue
            if current.human_wait_count >= current.identity.budget.max_human_waits:
                yield from _cleanup_execution_plane(context, current)
                yield context.call_entity(
                    entity,
                    "abort",
                    {
                        "context_ref": current.run_document_ref.model_dump(mode="json"),
                        "error": "human_wait_budget_exceeded",
                        "run_id": current.identity.run_id,
                        "status": DurableLoopRunStatus.FAILED.value,
                    },
                )
                return {
                    "error": "human_wait_budget_exceeded",
                    "status": DurableLoopRunStatus.FAILED.value,
                }
            human_data = yield context.call_activity(
                DURABLE_LOOP_HUMAN_ACTIVITY_NAME,
                {
                    "request_ref": clarification[0].request_ref.model_dump(
                        mode="json"
                    ),
                    "run_document_ref": current.run_document_ref.model_dump(
                        mode="json"
                    ),
                    "issued_at": context.current_utc_datetime.isoformat(),
                    "expires_at": min(
                        context.current_utc_datetime
                        + timedelta(
                            seconds=current.identity.budget.human_wait_seconds
                        ),
                        current.identity.absolute_deadline,
                    ).isoformat(),
                },
            )
            human = HumanWaitActivityResultV1.model_validate_json(
                canonical_json_bytes(human_data)
            )
            current = current.model_copy(
                update={
                    "external_content_bytes": (
                        current.external_content_bytes + human.written_bytes
                    )
                }
            )
            content_error = _orchestration_budget_error(current, context)
            if content_error is not None:
                yield from _cleanup_execution_plane(context, current)
                yield context.call_entity(
                    entity,
                    "abort",
                    {
                        "error": content_error,
                        "run_id": current.identity.run_id,
                        "status": DurableLoopRunStatus.FAILED.value,
                    },
                )
                return {
                    "error": content_error,
                    "status": DurableLoopRunStatus.FAILED.value,
                }
            opened = yield context.call_entity(
                entity,
                "open_human",
                {
                    **human.model_dump(mode="json"),
                    "owner_hash": current.identity.owner_hash,
                },
            )
            if opened.get("disposition") not in {"opened", "replayed"}:
                raise RuntimeError("human request could not be opened")
            context.set_custom_status(
                {
                    "cost_microunits": current.cost_microunits,
                    "external_content_bytes": current.external_content_bytes,
                    "input_tokens": current.input_tokens,
                    "output_tokens": current.output_tokens,
                    "parked_seconds": current.parked_seconds,
                    "phase": "human_wait",
                    "question_ref": human.question_ref.model_dump(mode="json"),
                    "reasoning_tokens": current.reasoning_tokens,
                    "request_id": human.request_id,
                    "request_ref": human.request_ref.model_dump(mode="json"),
                    "status": DurableLoopRunStatus.WAITING.value,
                }
            )
            answer_task = context.wait_for_external_event(human.event_name)
            cancel_task = context.wait_for_external_event(
                DURABLE_LOOP_CANCEL_EVENT_NAME
            )
            timer = context.create_timer(
                min(
                    context.current_utc_datetime
                    + timedelta(seconds=current.identity.budget.human_wait_seconds),
                    current.identity.absolute_deadline,
                )
            )
            winner = yield context.task_any([answer_task, cancel_task, timer])
            if winner is cancel_task:
                if not timer.is_completed:
                    timer.cancel()
                cancel_receipt = yield context.call_entity(
                    entity,
                    "cancel",
                    {"run_id": current.identity.run_id},
                )
                if cancel_receipt.get("disposition") != "answer_won":
                    continue
                response = yield context.call_entity(
                    entity,
                    "get_human",
                    {"request_id": human.request_id},
                )
            elif winner is timer:
                response = yield context.call_entity(
                    entity,
                    "close_human",
                    {
                        "request_id": human.request_id,
                        "run_id": current.identity.run_id,
                    },
                )
            else:
                if not timer.is_completed:
                    timer.cancel()
                response = yield context.call_entity(
                    entity,
                    "get_human",
                    {"request_id": human.request_id},
                )
            append_payload: dict[str, object] = {
                "parked_seconds": max(
                    0,
                    int(
                        (
                            context.current_utc_datetime
                            - (
                                human.issued_at
                                or context.current_utc_datetime
                            ).astimezone(UTC)
                        ).total_seconds()
                    ),
                ),
                "request_ref": human.request_ref.model_dump(mode="json"),
                "run_document_ref": current.run_document_ref.model_dump(
                    mode="json"
                ),
            }
            if isinstance(response.get("response_ref"), Mapping):
                append_payload["response_ref"] = response["response_ref"]
            appended = yield context.call_activity(
                DURABLE_LOOP_HUMAN_RESULT_ACTIVITY_NAME,
                append_payload,
            )
            current = _advance_cursor(
                current,
                ContentRefV1.model_validate(appended["run_document_ref"]),
                model_steps=1,
                tool_calls=1,
                human_waits=1,
                external_content_bytes=_nonnegative_int(
                    appended.get("written_bytes", 0),
                    "written_bytes",
                ),
                parked_seconds=_nonnegative_int(
                    append_payload["parked_seconds"],
                    "parked_seconds",
                ),
                working_context_bytes=_nonnegative_int(
                    appended.get("working_context_bytes"),
                    "working_context_bytes",
                ),
            )
            yield context.call_entity(
                entity,
                "consume_human",
                {"request_id": human.request_id},
            )
            continue

        if (
            current.completed_tool_calls + len(model_result.tool_calls)
            > current.identity.budget.max_tool_calls
        ):
            yield from _cleanup_execution_plane(context, current)
            yield context.call_entity(
                entity,
                "abort",
                {
                    "context_ref": current.run_document_ref.model_dump(mode="json"),
                    "error": "tool_call_budget_exceeded",
                    "run_id": current.identity.run_id,
                    "status": DurableLoopRunStatus.FAILED.value,
                },
            )
            return {
                "error": "tool_call_budget_exceeded",
                "status": DurableLoopRunStatus.FAILED.value,
            }
        result_refs, cancelled_calls = yield from _schedule_tool_refs(
            context,
            entity,
            current.identity.run_id,
            model_result.tool_calls,
            current.identity.budget.max_parallel_reads,
        )
        appended = yield context.call_activity(
            DURABLE_LOOP_APPEND_ACTIVITY_NAME,
            {
                "cancelled_request_refs": [
                    item.request_ref.model_dump(mode="json")
                    for item in cancelled_calls
                ],
                "result_refs": [
                    item.model_dump(mode="json") for item in result_refs
                ],
                "run_document_ref": current.run_document_ref.model_dump(
                    mode="json"
                ),
            },
        )
        current = _advance_cursor(
            current,
            ContentRefV1.model_validate(appended["run_document_ref"]),
            model_steps=1,
            tool_calls=len(model_result.tool_calls),
            external_content_bytes=(
                sum(item.written_bytes for item in result_refs)
                + _nonnegative_int(
                    appended.get("written_bytes", 0),
                    "written_bytes",
                )
            ),
            working_context_bytes=_nonnegative_int(
                appended.get("working_context_bytes"),
                "working_context_bytes",
            ),
        )
        if any(item.status is ToolResultStatus.AMBIGUOUS for item in result_refs):
            yield from _cleanup_execution_plane(context, current)
            yield context.call_entity(
                entity,
                "abort",
                {
                    "context_ref": current.run_document_ref.model_dump(mode="json"),
                    "disposition": ErrorDisposition.AMBIGUOUS.value,
                    "error": "tool_outcome_ambiguous",
                    "possibly_committed": True,
                    "run_id": current.identity.run_id,
                    "status": DurableLoopRunStatus.FAILED.value,
                },
            )
            return {
                "disposition": ErrorDisposition.AMBIGUOUS.value,
                "error": "tool_outcome_ambiguous",
                "possibly_committed": True,
                "status": DurableLoopRunStatus.FAILED.value,
            }
        cancelled_after_tools = {"cancelled": bool(cancelled_calls)}
        if not cancelled_calls:
            cancelled_after_tools = yield context.call_entity(
                entity,
                "is_cancelled",
                {"run_id": current.identity.run_id},
            )
        if cancelled_after_tools.get("cancelled") is True:
            yield from _cleanup_execution_plane(context, current)
            yield context.call_entity(
                entity,
                "abort",
                {
                    "context_ref": current.run_document_ref.model_dump(mode="json"),
                    "run_id": current.identity.run_id,
                    "status": DurableLoopRunStatus.CANCELLED.value,
                },
            )
            return {"status": DurableLoopRunStatus.CANCELLED.value}
        if (
            current.checkpoints_in_generation
            >= current.identity.budget.continue_as_new_checkpoints
        ):
            context.continue_as_new(
                current.model_copy(
                    update={
                        "checkpoints_in_generation": 0,
                        "continue_as_new_generation": (
                            current.continue_as_new_generation + 1
                        ),
                    }
                ).model_dump(mode="json")
            )
            return None


def _call_session_entity(
    context: df.DurableOrchestrationContext,
    operation: str,
) -> Any:
    payload = _mapping(context.get_input(), operation)
    entity = df.EntityId(
        DURABLE_LOOP_SESSION_ENTITY_NAME,
        _required_string(payload, "session_entity_key"),
    )
    operation_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"operation", "session_entity_key"}
    }
    return (yield context.call_entity(entity, operation, operation_payload))


def _cleanup_execution_plane(
    context: df.DurableOrchestrationContext,
    current: DurableOrchestrationInputV1,
) -> Any:
    """Schedule idempotent terminal cleanup before releasing session ownership."""
    if current.sandbox_profile is SandboxExecutionProfile.PER_CALL:
        return None
    return (
        yield context.call_activity(
            DURABLE_LOOP_CLEANUP_ACTIVITY_NAME,
            {
                "fault_profile": current.fault_profile.value,
                "run_id": current.identity.run_id,
                "sandbox_profile": current.sandbox_profile.value,
                "session_id": current.identity.session_id,
            },
        )
    )


def _schedule_tool_refs(
    context: df.DurableOrchestrationContext,
    entity: df.EntityId,
    run_id: str,
    calls: tuple[ToolDispatchRefV1, ...],
    max_parallel_reads: int,
) -> Any:
    results: list[ToolResultRefV1] = []
    index = 0
    while index < len(calls):
        cancelled = yield context.call_entity(
            entity,
            "is_cancelled",
            {"run_id": run_id},
        )
        if cancelled.get("cancelled") is True:
            return tuple(sorted(results, key=lambda item: item.call_ordinal)), calls[index:]
        call = calls[index]
        if call.behavior is ToolBehavior.READ_ONLY and call.parallel_safe:
            batch: list[ToolDispatchRefV1] = []
            while (
                index < len(calls)
                and calls[index].behavior is ToolBehavior.READ_ONLY
                and calls[index].parallel_safe
                and len(batch) < max_parallel_reads
            ):
                batch.append(calls[index])
                index += 1
            raw = yield context.task_all(
                [
                    context.call_activity(
                        DURABLE_LOOP_TOOL_ACTIVITY_NAME,
                        {"request_ref": item.request_ref.model_dump(mode="json")},
                    )
                    for item in batch
                ]
            )
            results.extend(
                ToolResultRefV1.model_validate_json(canonical_json_bytes(item))
                for item in raw
            )
            continue
        raw = yield context.call_activity(
            DURABLE_LOOP_TOOL_ACTIVITY_NAME,
            {"request_ref": call.request_ref.model_dump(mode="json")},
        )
        results.append(
            ToolResultRefV1.model_validate_json(canonical_json_bytes(raw))
        )
        index += 1
    return tuple(sorted(results, key=lambda item: item.call_ordinal)), ()


def _entity_admit(
    current: dict[str, object],
    data: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    run_id = _required_string(data, "run_id")
    request_id_hash = _required_string(data, "request_id_hash")
    request_hash = _required_string(data, "request_hash")
    idempotency = _mapping(current.get("idempotency"), "idempotency")
    previous = idempotency.get(request_id_hash)
    if isinstance(previous, Mapping):
        if previous.get("request_hash") != request_hash:
            return current, {"disposition": "conflict"}
        return current, {
            "disposition": "replayed",
            "lifecycle": previous.get("lifecycle"),
            "run_id": previous.get("run_id"),
            "terminal_context_ref": previous.get("terminal_context_ref"),
            "terminal_disposition": previous.get("terminal_disposition"),
            "terminal_error": previous.get("terminal_error"),
            "terminal_possibly_committed": previous.get(
                "terminal_possibly_committed"
            ),
            "terminal_response_ref": previous.get("terminal_response_ref"),
            "terminal_status": previous.get("terminal_status"),
            "committed_context_ref": current.get("committed_context_ref"),
            "committed_generation": current.get("committed_generation"),
        }
    active_run_id = current.get("active_run_id")
    if isinstance(active_run_id, str) and active_run_id:
        return current, {
            "active_run_id": active_run_id,
            "disposition": "busy",
        }
    now = _optional_datetime(data.get("now"))
    if len(idempotency) >= _MAX_ENTITY_IDEMPOTENCY_RECEIPTS:
        _evict_terminal_idempotency_receipts(idempotency, now)
    if len(idempotency) >= _MAX_ENTITY_IDEMPOTENCY_RECEIPTS:
        return current, {"disposition": "idempotency_capacity_exceeded"}
    idempotency[request_id_hash] = {
        "expires_at": data.get("expires_at"),
        "lifecycle": "admitted",
        "request_hash": request_hash,
        "run_id": run_id,
    }
    current["active_run_id"] = run_id
    current["idempotency"] = idempotency
    return current, {
        "committed_context_ref": current.get("committed_context_ref"),
        "committed_generation": current.get("committed_generation"),
        "disposition": "admitted",
        "lifecycle": "admitted",
        "run_id": run_id,
    }


def _entity_complete(
    current: dict[str, object],
    data: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    commit_key = _required_string(data, "commit_key")
    expected_commit_key = canonical_hash(
        {
            "context_ref": data.get("context_ref"),
            "expected_generation": data.get("expected_generation"),
            "request_hash": data.get("request_hash"),
            "response_ref": data.get("response_ref"),
            "run_id": data.get("run_id"),
        }
    )
    if commit_key != expected_commit_key:
        return current, {"disposition": "commit_key_mismatch"}
    previous = current.get("last_commit")
    if isinstance(previous, Mapping) and previous.get("commit_key") == commit_key:
        return current, dict(previous)
    run_id = _required_string(data, "run_id")
    if current.get("active_run_id") != run_id:
        return current, {"disposition": "stale"}
    cancelled = _mapping(current.get("cancelled_runs"), "cancelled_runs")
    if cancelled.get(run_id) is True:
        return current, {"disposition": "cancelled"}
    expected = _nonnegative_int(data.get("expected_generation"), "expected_generation")
    generation = _nonnegative_int(
        current.get("committed_generation"),
        "committed_generation",
    )
    if expected != generation:
        return current, {"disposition": "generation_conflict"}
    receipt: dict[str, object] = {
        "commit_key": commit_key,
        "committed_generation": generation + 1,
        "context_ref": data.get("context_ref"),
        "disposition": "completed",
        "request_hash": _required_string(data, "request_hash"),
        "response_ref": data.get("response_ref"),
        "run_id": run_id,
    }
    current["active_run_id"] = None
    current["committed_context_ref"] = data.get("context_ref")
    current["committed_generation"] = generation + 1
    current["last_commit"] = receipt
    current["open_human_request"] = None
    cancelled.pop(run_id, None)
    current["cancelled_runs"] = cancelled
    _update_idempotency_lifecycle(
        current,
        run_id,
        lifecycle="completed",
        terminal_context_ref=data.get("context_ref"),
        terminal_response_ref=data.get("response_ref"),
        terminal_status=DurableLoopRunStatus.COMPLETED.value,
    )
    return current, receipt


def _entity_abort(
    current: dict[str, object],
    data: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    run_id = _required_string(data, "run_id")
    aborted = _mapping(current.get("aborted_runs"), "aborted_runs")
    if aborted.get(run_id) is True:
        return current, {
            "committed_generation": current.get("committed_generation"),
            "disposition": "replayed",
        }
    if current.get("active_run_id") != run_id:
        return current, {"disposition": "stale"}
    if len(aborted) >= _MAX_ENTITY_ABORT_RECEIPTS:
        aborted.pop(next(iter(aborted)))
    aborted[run_id] = True
    current["aborted_runs"] = aborted
    current["active_run_id"] = None
    current["open_human_request"] = None
    cancelled = _mapping(current.get("cancelled_runs"), "cancelled_runs")
    cancelled.pop(run_id, None)
    current["cancelled_runs"] = cancelled
    terminal_status = data.get("status", DurableLoopRunStatus.FAILED.value)
    if terminal_status not in {
        DurableLoopRunStatus.CANCELLED.value,
        DurableLoopRunStatus.FAILED.value,
    }:
        terminal_status = DurableLoopRunStatus.FAILED.value
    terminal_error = data.get("error")
    terminal_disposition = data.get("disposition")
    terminal_possibly_committed = data.get("possibly_committed") is True
    _update_idempotency_lifecycle(
        current,
        run_id,
        lifecycle="aborted",
        terminal_context_ref=data.get("context_ref"),
        terminal_error=terminal_error,
        terminal_disposition=terminal_disposition,
        terminal_possibly_committed=terminal_possibly_committed,
        terminal_status=terminal_status,
    )
    return current, {
        "committed_generation": current.get("committed_generation"),
        "disposition": "aborted",
        "error": terminal_error,
        "possibly_committed": terminal_possibly_committed,
        "status": terminal_status,
    }


def _entity_reserve_human(
    current: dict[str, object],
    data: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    request = current.get("open_human_request")
    if not isinstance(request, Mapping):
        return current, {"disposition": "gone"}
    required = ("request_id", "run_id", "generation", "call_key", "owner_hash")
    if any(request.get(field) != data.get(field) for field in required):
        return current, {"disposition": "stale"}
    request_id = _required_string(data, "request_id")
    human_inputs = _mapping(current.get("human_inputs"), "human_inputs")
    previous = human_inputs.get(request_id)
    if isinstance(previous, Mapping):
        if (
            previous.get("body_hash") == data.get("body_hash")
            and previous.get("submission_id_hash") == data.get("submission_id_hash")
        ):
            return current, dict(previous)
        return current, {"disposition": "conflict"}
    now = _optional_datetime(data.get("now"))
    if len(human_inputs) >= _MAX_ENTITY_HUMAN_INPUT_RECEIPTS:
        _evict_terminal_human_receipts(current, human_inputs, now)
    if len(human_inputs) >= _MAX_ENTITY_HUMAN_INPUT_RECEIPTS:
        return current, {"disposition": "human_input_capacity_exceeded"}
    receipt = {
        "accepted_at": data.get("accepted_at"),
        "body_hash": _required_string(data, "body_hash"),
        "delivery": "pending",
        "disposition": "reserved",
        "expires_at": data.get("expires_at"),
        "submission_id_hash": _required_string(data, "submission_id_hash"),
    }
    human_inputs[request_id] = receipt
    current["human_inputs"] = human_inputs
    return current, receipt


def _entity_accept_human(
    current: dict[str, object],
    data: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    request_id = _required_string(data, "request_id")
    human_inputs = _mapping(current.get("human_inputs"), "human_inputs")
    previous = human_inputs.get(request_id)
    if not isinstance(previous, Mapping):
        return current, {"disposition": "gone"}
    if (
        previous.get("body_hash") != data.get("body_hash")
        or previous.get("submission_id_hash") != data.get("submission_id_hash")
    ):
        return current, {"disposition": "conflict"}
    if previous.get("response_ref") is not None:
        return current, dict(previous)
    if previous.get("disposition") != "reserved":
        return current, {"disposition": "gone"}
    accepted = {
        **previous,
        "disposition": "accepted",
        "response_ref": data.get("response_ref"),
    }
    human_inputs[request_id] = accepted
    current["human_inputs"] = human_inputs
    return current, accepted


def _update_idempotency_lifecycle(
    current: dict[str, object],
    run_id: str,
    *,
    lifecycle: str,
    terminal_context_ref: object = None,
    terminal_disposition: object = None,
    terminal_error: object = None,
    terminal_possibly_committed: bool = False,
    terminal_response_ref: object = None,
    terminal_status: object = None,
) -> None:
    idempotency = _mapping(current.get("idempotency"), "idempotency")
    for key, value in tuple(idempotency.items()):
        if not isinstance(value, Mapping) or value.get("run_id") != run_id:
            continue
        updated = {**value, "lifecycle": lifecycle}
        if terminal_context_ref is not None:
            updated["terminal_context_ref"] = terminal_context_ref
        if terminal_disposition is not None:
            updated["terminal_disposition"] = terminal_disposition
        if terminal_error is not None:
            updated["terminal_error"] = terminal_error
        if terminal_possibly_committed:
            updated["terminal_possibly_committed"] = True
        if terminal_response_ref is not None:
            updated["terminal_response_ref"] = terminal_response_ref
        if terminal_status is not None:
            updated["terminal_status"] = terminal_status
        idempotency[key] = updated
        current["idempotency"] = idempotency
        return


def _evict_terminal_idempotency_receipts(
    idempotency: dict[str, object],
    now: datetime | None,
) -> None:
    if now is None:
        return
    for key, value in tuple(idempotency.items()):
        if (
            isinstance(value, Mapping)
            and value.get("lifecycle") in {"aborted", "completed"}
            and _receipt_expired(value, now)
        ):
            idempotency.pop(key)
            return


def _evict_terminal_human_receipts(
    current: Mapping[str, object],
    human_inputs: dict[str, object],
    now: datetime | None,
) -> None:
    if now is None:
        return
    open_request = current.get("open_human_request")
    open_request_id = (
        open_request.get("request_id")
        if isinstance(open_request, Mapping)
        else None
    )
    for key, value in tuple(human_inputs.items()):
        if key == open_request_id or not isinstance(value, Mapping):
            continue
        if value.get("disposition") in {
            "consumed",
            "orphaned",
            "timed_out",
        } and _receipt_expired(value, now):
            human_inputs.pop(key)
            return


def _receipt_expired(value: Mapping[str, object], now: datetime) -> bool:
    expires_at = _optional_datetime(value.get("expires_at"))
    return expires_at is not None and expires_at <= now


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _datetime_value(value, "timestamp")


def _entity_close_human(
    current: dict[str, object],
    data: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    request_id = _required_string(data, "request_id")
    human_inputs = _mapping(current.get("human_inputs"), "human_inputs")
    previous = human_inputs.get(request_id)
    if isinstance(previous, Mapping):
        return current, dict(previous)
    request = current.get("open_human_request")
    if (
        not isinstance(request, Mapping)
        or request.get("request_id") != request_id
        or request.get("run_id") != data.get("run_id")
    ):
        return current, {"disposition": "gone"}
    receipt: dict[str, object] = {"disposition": "timed_out"}
    human_inputs[request_id] = receipt
    current["human_inputs"] = human_inputs
    return current, receipt


def _advance_cursor(
    current: DurableOrchestrationInputV1,
    reference: ContentRefV1,
    *,
    model_steps: int,
    tool_calls: int,
    working_context_bytes: int,
    human_waits: int = 0,
    parked_seconds: int = 0,
    external_content_bytes: int = 0,
) -> DurableOrchestrationInputV1:
    return current.model_copy(
        update={
            "checkpoints_in_generation": current.checkpoints_in_generation + 1,
            "completed_model_steps": current.completed_model_steps + model_steps,
            "completed_tool_calls": current.completed_tool_calls + tool_calls,
            "human_wait_count": current.human_wait_count + human_waits,
            "next_model_step": current.next_model_step + model_steps,
            "parked_seconds": current.parked_seconds + parked_seconds,
            "external_content_bytes": (
                current.external_content_bytes + external_content_bytes
            ),
            "run_document_ref": reference,
            "working_context_bytes": working_context_bytes,
        }
    )


def _orchestration_budget_error(
    current: DurableOrchestrationInputV1,
    context: df.DurableOrchestrationContext,
) -> str | None:
    if current.completed_model_steps >= current.identity.budget.max_model_steps:
        return "model_step_budget_exceeded"
    if current.completed_tool_calls > current.identity.budget.max_tool_calls:
        return "tool_call_budget_exceeded"
    if (
        current.input_tokens + current.output_tokens
        > current.identity.budget.max_total_tokens
    ):
        return "model_token_budget_exceeded"
    if (
        current.identity.budget.max_cost_microunits > 0
        and current.cost_microunits
        > current.identity.budget.max_cost_microunits
    ):
        return "model_cost_budget_exceeded"
    if (
        current.external_content_bytes
        > current.identity.budget.max_external_content_bytes
    ):
        return "external_content_budget_exceeded"
    if context.current_utc_datetime >= current.identity.active_deadline + timedelta(
        seconds=current.parked_seconds
    ):
        return "run_active_deadline_exceeded"
    return None


def _plan_from_document(document: DurableLoopPlanDocumentV1) -> DurableLoopPlan:
    return DurableLoopPlan(
        instructions=document.instructions,
        catalog=document.catalog,
        model_settings=document.model_settings,
        maf_core_version=document.maf_core_version,
        provider=document.provider,
        model=document.model,
        api_version=document.api_version,
        settings=DurableLoopSettings(**document.settings),  # type: ignore[arg-type]
        sandbox_profile=document.sandbox_profile,
        fault_profile=document.fault_profile,
    )


async def _persist_model_error(
    runtime: DurableLoopActivityRuntime,
    document: DurableRunDocumentV1,
    error: ErrorEnvelopeV1,
    *,
    extra_written_bytes: int = 0,
) -> dict[str, object]:
    failed = document.model_copy(
        update={
            "checkpoint": document.checkpoint.model_copy(
                update={
                    "last_error": error,
                    "status": DurableLoopRunStatus.FAILED,
                }
            )
        }
    )
    failed_ref = await put_protocol_model(
        runtime.content,
        kind="run-document",
        model=failed,
    )
    return ModelStepActivityResultV1(
        run_document_ref=failed_ref,
        step_index=document.checkpoint.next_model_step,
        error=error,
        written_bytes=failed_ref.byte_length + extra_written_bytes,
    ).model_dump(mode="json")


async def _persist_model_decision(
    runtime: DurableLoopActivityRuntime,
    document: DurableRunDocumentV1,
    decision: ModelDecisionEnvelopeV1,
    *,
    extra_written_bytes: int = 0,
) -> dict[str, object]:
    checkpoint = document.checkpoint
    plan = _plan_from_document(document.plan)
    if decision.usage.cost_microunits is None:
        decision = decision.model_copy(
            update={
                "usage": decision.usage.model_copy(
                    update={
                        "cost_microunits": (
                            (
                                decision.usage.input_tokens
                                * plan.settings.input_cost_microunits_per_million_tokens
                                + decision.usage.output_tokens
                                * plan.settings.output_cost_microunits_per_million_tokens
                            )
                            // 1_000_000
                        )
                    }
                )
            }
        )
    _validate_decision(checkpoint, decision)
    checkpoint = _checkpoint_with_usage(checkpoint, decision.usage)
    if decision.final_text is not None:
        response_ref = await runtime.content.put_bytes(
            kind="final-response",
            payload=decision.final_text.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            retention_class="result",
        )
        working = append_final_message(checkpoint.working_context, decision)
        audit = append_audit_final(checkpoint.audit_bundle, decision)
        working = working.model_copy(update={"source_audit_hash": audit.bundle_hash})
        updated = document.model_copy(
            update={
                "checkpoint": checkpoint.model_copy(
                    update={
                        "audit_bundle": audit,
                        "audit_head_hash": audit.bundle_hash,
                        "completed_model_steps": checkpoint.completed_model_steps + 1,
                        "next_model_step": checkpoint.next_model_step + 1,
                        "status": DurableLoopRunStatus.COMPLETED,
                        "working_context": working,
                    }
                ),
                "pending_decision": None,
            }
        )
        document_ref = await put_protocol_model(
            runtime.content,
            kind="run-document",
            model=updated,
        )
        return ModelStepActivityResultV1(
            run_document_ref=document_ref,
            step_index=decision.step_index,
            final_response_ref=response_ref,
            usage=decision.usage,
            written_bytes=(
                document_ref.byte_length
                + response_ref.byte_length
                + extra_written_bytes
            ),
        ).model_dump(mode="json")

    tool_refs: list[ToolDispatchRefV1] = []
    for request_item in build_tool_requests(
        checkpoint,
        decision,
        plan,
        datetime.now(UTC),
    ):
        request_ref = await put_protocol_model(
            runtime.content,
            kind="tool-request",
            model=request_item,
        )
        descriptor = plan.catalog.by_name().get(request_item.tool_name)
        tool_refs.append(
            ToolDispatchRefV1(
                request_ref=request_ref,
                call_ordinal=request_item.call_ordinal,
                call_key=request_item.call_key,
                request_hash=request_item.request_hash,
                tool_name=request_item.tool_name,
                provenance=request_item.provenance,
                behavior=request_item.behavior,
                parallel_safe=bool(
                    descriptor is not None and descriptor.parallel_safe
                ),
            )
        )
    pending = document.model_copy(update={"pending_decision": decision})
    document_ref = await put_protocol_model(
        runtime.content,
        kind="run-document",
        model=pending,
    )
    return ModelStepActivityResultV1(
        run_document_ref=document_ref,
        step_index=decision.step_index,
        tool_calls=tuple(tool_refs),
        usage=decision.usage,
        written_bytes=(
            document_ref.byte_length
            + sum(item.request_ref.byte_length for item in tool_refs)
            + extra_written_bytes
        ),
    ).model_dump(mode="json")


def _validate_decision(
    checkpoint: CheckpointStateV1,
    decision: ModelDecisionEnvelopeV1,
) -> None:
    if (
        decision.run_id != checkpoint.identity.run_id
        or decision.step_index != checkpoint.next_model_step
        or decision.model_call_key
        != deterministic_model_step_key(
            checkpoint.identity.run_id,
            checkpoint.next_model_step,
        )
        or decision.deployment_hash != checkpoint.identity.deployment_hash
    ):
        raise ValueError("model decision identity binding mismatch")


def _checkpoint_with_usage(
    checkpoint: CheckpointStateV1,
    usage: Any,
) -> CheckpointStateV1:
    return checkpoint.model_copy(
        update={
            "cost_microunits": (
                checkpoint.cost_microunits + (usage.cost_microunits or 0)
            ),
            "input_tokens": checkpoint.input_tokens + usage.input_tokens,
            "output_tokens": checkpoint.output_tokens + usage.output_tokens,
            "reasoning_tokens": (
                checkpoint.reasoning_tokens + usage.reasoning_tokens
            ),
        }
    )


def _append_pending_assistant(
    document: DurableRunDocumentV1,
) -> WorkingContextV1:
    decision = document.pending_decision
    if decision is None:
        raise ValueError("run document has no pending decision")
    messages = (
        *document.checkpoint.working_context.bundle.messages,
        decision.assistant_message,
    )
    bundle = document.checkpoint.working_context.bundle.create(
        messages=messages,
        maf_core_version=document.plan.maf_core_version,
        provider=document.plan.provider,
        model=document.plan.model,
        api_version=document.plan.api_version,
    )
    return document.checkpoint.working_context.model_copy(update={"bundle": bundle})


def _answered_human_result(
    request: HumanInputRequestV1,
    answer_json: bytes,
) -> ToolResultV1:
    import json

    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=0,
        provider_call_id=request.call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=REQUEST_HUMAN_INPUT_TOOL_NAME,
        status=ToolResultStatus.SUCCEEDED,
        value={
            "answer": json.loads(answer_json),
            "status": "answered",
        },
        elapsed_ms=0.0,
    )


def _timed_out_human_result(request: HumanInputRequestV1) -> ToolResultV1:
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=0,
        provider_call_id=request.call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=REQUEST_HUMAN_INPUT_TOOL_NAME,
        status=ToolResultStatus.TIMED_OUT,
        elapsed_ms=0.0,
        error=ErrorEnvelopeV1(
            code="human_input_timed_out",
            classification="human_input",
            retryable=False,
            phase="human_wait",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )


def _protocol_error_result(request: ToolRequestV1) -> ToolResultV1:
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
        error=ErrorEnvelopeV1(
            code="invalid_clarification_batch",
            classification="protocol",
            retryable=False,
            phase="model_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
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
        error=ErrorEnvelopeV1(
            code="tool_cancelled",
            classification="cancellation",
            retryable=False,
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
        error=ErrorEnvelopeV1(
            code=code,
            classification="budget",
            retryable=False,
            phase="tool_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )


def _tool_result_size(result: ToolResultV1) -> int:
    if result.result_ref is not None:
        return result.result_ref.byte_length
    return len(canonical_json_bytes(result.value))


async def _deliver_event_with_durable_client(
    client: Any,
    *,
    run_id: str,
    event_name: str,
    event_data: Mapping[str, object],
) -> HumanEventDeliveryResultV1:
    try:
        await client.raise_event(run_id, event_name, dict(event_data))
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if not isinstance(status_code, int):
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
        if status_code in {404, 410}:
            return HumanEventDeliveryResultV1(
                status=HumanEventDeliveryStatus.TERMINAL,
                terminal_status_code=404 if status_code == 404 else 410,
            )
        return HumanEventDeliveryResultV1(
            status=HumanEventDeliveryStatus.RETRY,
            retry_after_seconds=5.0,
        )
    return HumanEventDeliveryResultV1(
        status=HumanEventDeliveryStatus.DELIVERED
    )


def _mapping(value: object, field_name: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{field_name} must be an object")
    return dict(value)


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{field_name} must be an array")
    return value


def _required_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _datetime_value(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    else:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)
