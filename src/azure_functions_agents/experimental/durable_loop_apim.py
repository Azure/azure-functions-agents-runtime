"""APIM Responses adapters for durable model steps and remote control."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from agent_framework import Content, Message

from ..strict_json import canonical_json_bytes
from .durable_loop_activities import (
    BackgroundModelProvider,
    DurableContentStore,
    DurableLoopModelError,
    MafOneStepModelProvider,
    OneStepModelProvider,
    OneStepModelRequest,
    get_protocol_model,
    put_protocol_model,
)
from .durable_loop_config import DurableLoopSettings
from .durable_loop_observability import (
    DurableLoopOutcome,
    DurableLoopPhase,
    DurableLoopTimer,
    record_durable_loop_event,
)
from .durable_loop_protocol import (
    DURABLE_LOOP_ORCHESTRATOR_V3_NAME,
    BackgroundPollResultV1,
    BackgroundStartDisposition,
    BackgroundStartResultV1,
    DurableFaultProfile,
    ErrorDisposition,
    ErrorEnvelopeV1,
    ModelDecisionEnvelopeV1,
    ModelOperationStatus,
    ModelOperationV1,
    ModelToolCallV1,
    UsageV1,
    canonical_hash,
    deterministic_model_step_key,
)
from .durable_loop_receipts import (
    ActivityReceiptStatus,
    ActivityReceiptV1,
    DurableKeyedDocumentStore,
    DurableOneShotFaults,
    create_activity_receipt,
    read_activity_receipt,
    replace_activity_receipt,
)
from .hybrid_apim import HybridApimClientManager

_PROVIDER_RESPONSE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MODEL_OPERATION_ID = re.compile(r"^op-[0-9a-f]{32}$")
_DEMO_FAULT_HEADER = "x-af-demo-fault"
_DEMO_MODEL_429_VALUE = "model-429-once"
_MAX_PROVIDER_BODY_BYTES = 8 * 1024 * 1024
_TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class ApimResponsesError(RuntimeError):
    """A sanitized APIM Responses operation failure."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.ambiguous = ambiguous


@runtime_checkable
class ApimResponsesTransport(Protocol):
    """Content-blind HTTP boundary used by deterministic APIM tests."""

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> tuple[int, Mapping[str, str], Mapping[str, object]]:
        """Send one bounded request and return status, headers, and JSON."""


@runtime_checkable
class MafAgentResponseAdapter(Protocol):
    """Public MAF one-step methods used by foreground and background paths."""

    async def run_one_step(
        self,
        request: OneStepModelRequest,
        *,
        client_kwargs: Mapping[str, object] | None = None,
    ) -> ModelDecisionEnvelopeV1:
        """Run one terminal foreground step."""

    async def run_agent_response(
        self,
        request: OneStepModelRequest,
        *,
        background: bool = False,
        client_kwargs: Mapping[str, object] | None = None,
    ) -> Any:
        """Return one public Agent response, including continuation state."""

    def parse_agent_response(
        self,
        request: OneStepModelRequest,
        response: Any,
    ) -> ModelDecisionEnvelopeV1:
        """Parse one terminal public Agent response."""


class AiohttpApimResponsesTransport:
    """Default APIM transport with response-body logging disabled."""

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> tuple[int, Mapping[str, str], Mapping[str, object]]:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.request(
                    method,
                    url,
                    headers=dict(headers),
                ) as response,
            ):
                body = await response.read()
                if len(body) > _MAX_PROVIDER_BODY_BYTES:
                    raise ApimResponsesError("model_response_too_large")
                try:
                    payload = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ApimResponsesError(
                        "model_response_invalid_json",
                        status_code=response.status,
                    ) from None
                if not isinstance(payload, Mapping):
                    raise ApimResponsesError(
                        "model_response_invalid_shape",
                        status_code=response.status,
                    )
                return response.status, dict(response.headers), dict(payload)
        except TimeoutError:
            raise ApimResponsesError(
                "model_transport_timeout",
                ambiguous=method == "POST",
            ) from None
        except aiohttp.ClientError as exc:
            raise ApimResponsesError(
                "model_transport_failed",
                ambiguous=method == "POST",
            ) from exc


class ApimMafResponsesProvider(OneStepModelProvider, BackgroundModelProvider):
    """MAF one-step foreground plus APIM-affine background Responses."""

    def __init__(
        self,
        manager: HybridApimClientManager,
        *,
        content: DurableContentStore,
        receipts: DurableKeyedDocumentStore,
        settings: DurableLoopSettings,
        control_base_url: str,
        transport: ApimResponsesTransport | None = None,
        faults: DurableOneShotFaults | None = None,
        foreground: MafAgentResponseAdapter | None = None,
    ) -> None:
        self._manager = manager
        self._foreground = foreground or MafOneStepModelProvider(manager)
        self._content = content
        self._receipts = receipts
        self._settings = settings
        self._control_base_url = _validate_control_base(control_base_url)
        self._transport = transport or AiohttpApimResponsesTransport()
        self._faults = faults or DurableOneShotFaults(
            receipts,
            enabled=settings.fault_injection_enabled,
        )
        self._backend_binding_hash = canonical_hash(
            {
                "control_base_url": self._control_base_url,
                "model_base_url": manager.model_base_url.rstrip("/"),
            }
        )

    async def run_one_step(
        self,
        request: OneStepModelRequest,
    ) -> ModelDecisionEnvelopeV1:
        """Run one synchronous Responses call with deadline-bounded retries."""
        timer = DurableLoopTimer(DurableLoopPhase.MODEL_STEP, provenance="apim")
        try:
            for attempt in range(1, 4):
                client_kwargs: Mapping[str, object] = {}
                try:
                    await self._inject_model_fault(request, attempt)
                    client_kwargs = await self._model_client_kwargs(
                        request,
                        attempt,
                    )
                    decision = await self._foreground.run_one_step(
                        request,
                        client_kwargs=client_kwargs,
                    )
                except Exception as raw_exc:
                    exc = _normalized_model_exception(raw_exc)
                    if (
                        _cross_activity_429(request, client_kwargs, exc)
                        or not _retryable_exception(exc)
                        or attempt == 3
                    ):
                        raise exc from None
                    await self._retry_delay(
                        request,
                        attempt,
                        (
                            exc.retry_after_seconds
                            if isinstance(exc, ApimResponsesError)
                            else None
                        ),
                    )
                    continue
                timer.finish(DurableLoopOutcome.COMPLETED)
                return decision.model_copy(update={"attempts": attempt})
        except BaseException:
            timer.finish(DurableLoopOutcome.FAILED)
            raise
        raise AssertionError("bounded model retry loop did not return")

    async def start(
        self,
        request: OneStepModelRequest,
    ) -> BackgroundStartResultV1:
        """Start one background response or replay its external receipt."""
        operation_key = deterministic_model_step_key(
            request.identity.run_id,
            request.step_index,
        )
        receipt_key = _receipt_key("model-start", operation_key)
        request_hash = request.canonical_request_hash()
        replay = await read_activity_receipt(self._receipts, receipt_key)
        if replay is not None:
            receipt, _revision = replay
            _validate_receipt(receipt, request_hash)
            return await self._start_result_from_receipt(receipt)
        started = ActivityReceiptV1(
            operation_key=operation_key,
            request_hash=request_hash,
            kind="model_start",
            status=ActivityReceiptStatus.STARTED,
            attempt=1,
            updated_at=datetime.now(UTC),
        )
        if not await create_activity_receipt(
            self._receipts,
            receipt_key,
            started,
        ):
            replay = await read_activity_receipt(self._receipts, receipt_key)
            if replay is None:
                raise ApimResponsesError("model_start_receipt_unavailable")
            return await self._start_result_from_receipt(replay[0])

        timer = DurableLoopTimer(DurableLoopPhase.MODEL_START, provenance="apim")
        try:
            response = None
            for attempt in range(1, 4):
                client_kwargs: Mapping[str, object] = {}
                try:
                    await self._inject_model_fault(request, attempt)
                    client_kwargs = await self._model_client_kwargs(
                        request,
                        attempt,
                    )
                    response = await self._foreground.run_agent_response(
                        request,
                        background=True,
                        client_kwargs=client_kwargs,
                    )
                    break
                except Exception as raw_exc:
                    exc = _normalized_model_exception(raw_exc)
                    if (
                        _cross_activity_429(request, client_kwargs, exc)
                        or not _retryable_exception(exc)
                        or attempt == 3
                    ):
                        raise exc from None
                    await self._retry_delay(
                        request,
                        attempt,
                        (
                            exc.retry_after_seconds
                            if isinstance(exc, ApimResponsesError)
                            else None
                        ),
                    )
            response = _require_background_response(response)
            raw_body_ref = await self._persist_raw_response(response)
            if response.continuation_token is None:
                decision = self._foreground.parse_agent_response(
                    request,
                    response,
                ).model_copy(update={"attempts": attempt})
                decision_ref = await put_protocol_model(
                    self._content,
                    kind="model-decision",
                    model=decision,
                )
                await self._advance_receipt(
                    receipt_key,
                    started,
                    status=ActivityReceiptStatus.SUCCEEDED,
                    result_ref=decision_ref,
                    operation_ref=raw_body_ref,
                )
                timer.finish(DurableLoopOutcome.COMPLETED)
                return BackgroundStartResultV1(
                    disposition=BackgroundStartDisposition.TERMINAL,
                    decision=decision,
                    written_bytes=(
                        raw_body_ref.byte_length + decision_ref.byte_length
                    ),
                )
            provider_response_id = _continuation_response_id(
                response.continuation_token
            )
            provider_response_ref = await self._content.put_bytes(
                kind="provider-response-id",
                payload=provider_response_id.encode("ascii"),
                media_type="text/plain",
                retention_class="run",
            )
            token_ref = await self._content.put_bytes(
                kind="provider-continuation-token",
                payload=canonical_json_bytes(response.continuation_token),
                media_type="application/json",
                retention_class="run",
            )
            operation = ModelOperationV1(
                operation_key=operation_key,
                run_id=request.identity.run_id,
                step_index=request.step_index,
                status=ModelOperationStatus.QUEUED,
                backend_binding_hash=self._backend_binding_hash,
                deployment_hash=request.identity.deployment_hash,
                provider_response_ref=provider_response_ref,
                continuation_token_ref=token_ref,
                accepted_at=datetime.now(UTC),
                deadline=request.effective_active_deadline
                or request.identity.active_deadline,
                terminal_retrieval_expires_at=min(
                    request.identity.absolute_deadline,
                    datetime.now(UTC) + timedelta(minutes=10),
                ),
                retrieval_is_repeatable=True,
            )
            operation_ref = await put_protocol_model(
                self._content,
                kind="model-operation",
                model=operation,
            )
            await self._advance_receipt(
                receipt_key,
                started,
                status=ActivityReceiptStatus.ACCEPTED,
                operation_ref=operation_ref,
                result_ref=raw_body_ref,
            )
            timer.finish(DurableLoopOutcome.WAITING)
            return BackgroundStartResultV1(
                disposition=BackgroundStartDisposition.ACCEPTED,
                operation=operation,
                written_bytes=(
                    raw_body_ref.byte_length
                    + provider_response_ref.byte_length
                    + token_ref.byte_length
                    + operation_ref.byte_length
                ),
            )
        except ApimResponsesError as exc:
            timer.finish(DurableLoopOutcome.FAILED)
            return await self._lost_start_acknowledgement(
                receipt_key,
                started,
                request,
                exc,
            )
        except TimeoutError:
            timer.finish(DurableLoopOutcome.FAILED)
            return await self._lost_start_acknowledgement(
                receipt_key,
                started,
                request,
                ApimResponsesError(
                    "model_start_acknowledgement_lost",
                    ambiguous=True,
                ),
            )

    async def poll(
        self,
        operation: ModelOperationV1,
    ) -> BackgroundPollResultV1:
        """Poll the fixed APIM control frontend for the same response ID."""
        timer = DurableLoopTimer(DurableLoopPhase.MODEL_POLL, provenance="apim")
        self._validate_operation(operation)
        receipt_key = _poll_receipt_key(operation)
        replay = await read_activity_receipt(self._receipts, receipt_key)
        if replay is not None:
            return await self._poll_result_from_receipt(operation, replay[0])
        provider_response_id = await self._provider_response_id(operation)
        payload = await self._request_control(
            "GET",
            "/responses",
            provider_response_id,
            deadline=operation.deadline,
        )
        _validate_matching_response_id(payload, provider_response_id)
        body_ref = await self._content.put_bytes(
            kind="provider-response-body",
            payload=canonical_json_bytes(payload),
            media_type="application/json",
            retention_class="run",
        )
        status = _provider_status(payload)
        if status in {"queued", "in_progress"}:
            updated = operation.model_copy(
                update={
                    "status": ModelOperationStatus(status),
                    "last_polled_at": datetime.now(UTC),
                    "poll_count": operation.poll_count + 1,
                }
            )
            await self._record_poll_receipt(
                operation,
                updated,
                body_ref,
            )
            timer.finish(DurableLoopOutcome.WAITING)
            return BackgroundPollResultV1(
                operation=updated,
                written_bytes=body_ref.byte_length,
            )
        if status != "completed":
            error = _terminal_error(operation, status, body_ref)
            await self._record_terminal_receipt(
                operation,
                key=receipt_key,
                error=error,
                result_ref=body_ref,
            )
            timer.finish(DurableLoopOutcome.FAILED)
            return BackgroundPollResultV1(
                error=error,
                written_bytes=body_ref.byte_length,
            )
        decision = _decision_from_provider_payload(operation, payload)
        decision_ref = await put_protocol_model(
            self._content,
            kind="model-decision",
            model=decision,
        )
        await self._record_terminal_receipt(
            operation,
            key=receipt_key,
            result_ref=decision_ref,
            operation_ref=body_ref,
        )
        timer.finish(DurableLoopOutcome.COMPLETED)
        return BackgroundPollResultV1(
            decision=decision,
            written_bytes=body_ref.byte_length + decision_ref.byte_length,
        )

    async def cancel(self, operation: ModelOperationV1) -> ModelOperationV1:
        """Request cancellation through the fixed APIM control frontend."""
        self._validate_operation(operation)
        receipt_key = _cancel_receipt_key(operation)
        replay = await read_activity_receipt(self._receipts, receipt_key)
        if replay is not None and replay[0].operation_ref is not None:
            _validate_operation_receipt(replay[0], operation)
            return await get_protocol_model(
                self._content,
                replay[0].operation_ref,
                ModelOperationV1,
            )
        provider_response_id = await self._provider_response_id(operation)
        payload = await self._request_control(
            "POST",
            "/responses/cancel",
            provider_response_id,
            deadline=operation.deadline,
        )
        _validate_matching_response_id(payload, provider_response_id)
        status = _provider_status(payload)
        resolved = (
            ModelOperationStatus.COMPLETED
            if status == "completed"
            else ModelOperationStatus.CANCELLED
        )
        updated = operation.model_copy(
            update={
                "status": resolved,
                "last_polled_at": datetime.now(UTC),
            }
        )
        body_ref = await self._content.put_bytes(
            kind="provider-cancel-body",
            payload=canonical_json_bytes(payload),
            media_type="application/json",
            retention_class="run",
        )
        operation_ref = await put_protocol_model(
            self._content,
            kind="model-operation",
            model=updated,
        )
        await self._record_terminal_receipt(
            operation,
            key=receipt_key,
            result_ref=body_ref,
            operation_ref=operation_ref,
            status=ActivityReceiptStatus.CANCELLED,
        )
        return updated

    async def _inject_model_fault(
        self,
        request: OneStepModelRequest,
        attempt: int,
    ) -> None:
        if attempt != 1:
            return
        if await self._faults.consume(
            request.fault_profile,
            run_id=request.identity.run_id,
            point="model_timeout",
        ):
            raise TimeoutError("injected model timeout")

    async def _model_client_kwargs(
        self,
        request: OneStepModelRequest,
        attempt: int,
    ) -> dict[str, object]:
        headers = {
            "x-af-operation-id": _model_operation_id(request),
        }
        if attempt == 1 and await self._faults.consume(
            request.fault_profile,
            run_id=request.identity.run_id,
            point="model_apim_429",
        ):
            headers[_DEMO_FAULT_HEADER] = _DEMO_MODEL_429_VALUE
            record_durable_loop_event(
                DurableLoopPhase.RETRY,
                DurableLoopOutcome.WAITING,
                provenance="apim_429",
            )
        return {"extra_headers": headers}

    async def _retry_delay(
        self,
        request: OneStepModelRequest,
        attempt: int,
        retry_after_seconds: float | None,
    ) -> None:
        deadline = request.effective_active_deadline or request.identity.active_deadline
        remaining = (deadline.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        delay = retry_after_seconds or min(2 ** (attempt - 1), 5)
        if remaining <= delay:
            raise TimeoutError("durable model retry deadline elapsed")
        await asyncio.sleep(delay)
        record_durable_loop_event(
            DurableLoopPhase.RETRY,
            DurableLoopOutcome.COMPLETED,
            provenance="model",
        )

    async def _request_control(
        self,
        method: str,
        path: str,
        response_id: str,
        *,
        deadline: datetime,
    ) -> Mapping[str, object]:
        for attempt in range(1, 4):
            remaining = (deadline.astimezone(UTC) - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise ApimResponsesError("model_operation_deadline_exceeded")
            headers = await self._manager.request_headers(
                {"x-af-response-id": _validate_response_id(response_id)}
            )
            status, response_headers, payload = await self._transport.request_json(
                method,
                f"{self._control_base_url}{path}",
                headers=headers,
                timeout_seconds=min(
                    remaining,
                    float(self._settings.activity_timeout_seconds),
                ),
            )
            if 200 <= status < 300:
                return payload
            if status not in _TRANSIENT_STATUS_CODES or attempt == 3:
                raise ApimResponsesError(
                    "model_control_failed",
                    status_code=status,
                )
            await asyncio.sleep(
                min(
                    _retry_after(response_headers) or float(2 ** (attempt - 1)),
                    max(0.0, remaining),
                )
            )
        raise AssertionError("bounded APIM control retry loop did not return")

    async def _provider_response_id(
        self,
        operation: ModelOperationV1,
    ) -> str:
        if operation.provider_response_ref is None:
            raise DurableLoopModelError(
                "background operation has no provider response reference"
            )
        value = (await self._content.get_bytes(operation.provider_response_ref)).decode(
            "ascii"
        )
        return _validate_response_id(value)

    def _validate_operation(self, operation: ModelOperationV1) -> None:
        if operation.backend_binding_hash != self._backend_binding_hash:
            raise DurableLoopModelError(
                "background operation APIM binding changed"
            )

    async def _persist_raw_response(self, response: Any) -> Any:
        raw = response.raw_representation
        nested = getattr(raw, "raw_representation", None)
        if nested is not None:
            raw = nested
        if hasattr(raw, "model_dump"):
            value = raw.model_dump(mode="json")
        elif isinstance(raw, Mapping):
            value = dict(raw)
        else:
            value = {"status": "unavailable"}
        return await self._content.put_bytes(
            kind="provider-response-body",
            payload=canonical_json_bytes(value),
            media_type="application/json",
            retention_class="run",
        )

    async def _start_result_from_receipt(
        self,
        receipt: ActivityReceiptV1,
    ) -> BackgroundStartResultV1:
        if receipt.status is ActivityReceiptStatus.SUCCEEDED and receipt.result_ref:
            return BackgroundStartResultV1(
                disposition=BackgroundStartDisposition.TERMINAL,
                decision=await get_protocol_model(
                    self._content,
                    receipt.result_ref,
                    ModelDecisionEnvelopeV1,
                ),
            )
        if receipt.status in {
            ActivityReceiptStatus.ACCEPTED,
            ActivityReceiptStatus.IN_PROGRESS,
        } and receipt.operation_ref:
            return BackgroundStartResultV1(
                disposition=BackgroundStartDisposition.ACCEPTED,
                operation=await get_protocol_model(
                    self._content,
                    receipt.operation_ref,
                    ModelOperationV1,
                ),
            )
        if receipt.status is ActivityReceiptStatus.FAILED:
            return BackgroundStartResultV1(
                disposition=BackgroundStartDisposition.TERMINAL,
                error=ErrorEnvelopeV1(
                    code=receipt.error_code or "model_start_failed",
                    classification="model",
                    retryable=False,
                    phase="model_step",
                ),
            )
        return BackgroundStartResultV1(
            disposition=BackgroundStartDisposition.LOST_ACKNOWLEDGEMENT,
            error=ErrorEnvelopeV1(
                code=receipt.error_code or "model_start_acknowledgement_lost",
                classification="model",
                retryable=False,
                disposition=ErrorDisposition.AMBIGUOUS,
                possibly_committed=True,
                phase="model_step",
            ),
        )

    async def _lost_start_acknowledgement(
        self,
        key: str,
        receipt: ActivityReceiptV1,
        request: OneStepModelRequest,
        error: ApimResponsesError,
    ) -> BackgroundStartResultV1:
        updated = receipt.model_copy(
            update={
                "error_code": (
                    "model_start_acknowledgement_lost"
                    if error.ambiguous
                    else error.code
                ),
                "status": (
                    ActivityReceiptStatus.AMBIGUOUS
                    if error.ambiguous
                    else ActivityReceiptStatus.FAILED
                ),
                "updated_at": datetime.now(UTC),
            }
        )
        current = await read_activity_receipt(self._receipts, key)
        if current is not None:
            await replace_activity_receipt(
                self._receipts,
                key,
                updated,
                revision=current[1],
            )
        ambiguous = error.ambiguous
        return BackgroundStartResultV1(
            disposition=(
                BackgroundStartDisposition.LOST_ACKNOWLEDGEMENT
                if ambiguous
                else BackgroundStartDisposition.TERMINAL
            ),
            error=ErrorEnvelopeV1(
                code=updated.error_code or "model_start_failed",
                classification="model",
                retryable=False,
                disposition=(
                    ErrorDisposition.AMBIGUOUS
                    if ambiguous
                    else ErrorDisposition.CERTAIN
                ),
                possibly_committed=ambiguous,
                phase="model_step",
                step_index=request.step_index,
            ),
        )

    async def _advance_receipt(
        self,
        key: str,
        receipt: ActivityReceiptV1,
        *,
        status: ActivityReceiptStatus,
        result_ref: Any = None,
        operation_ref: Any = None,
    ) -> None:
        current = await read_activity_receipt(self._receipts, key)
        if current is None:
            raise ApimResponsesError("model_start_receipt_missing")
        observed, revision = current
        _validate_receipt(observed, receipt.request_hash)
        updated = observed.model_copy(
            update={
                "operation_ref": operation_ref,
                "result_ref": result_ref,
                "status": status,
                "updated_at": datetime.now(UTC),
            }
        )
        if not await replace_activity_receipt(
            self._receipts,
            key,
            updated,
            revision=revision,
        ):
            raise ApimResponsesError("model_start_receipt_conflict")

    async def _record_poll_receipt(
        self,
        source: ModelOperationV1,
        operation: ModelOperationV1,
        body_ref: Any,
    ) -> None:
        operation_ref = await put_protocol_model(
            self._content,
            kind="model-operation",
            model=operation,
        )
        await self._upsert_operation_receipt(
            source,
            key=_poll_receipt_key(source),
            status=ActivityReceiptStatus.IN_PROGRESS,
            operation_ref=operation_ref,
            result_ref=body_ref,
        )

    async def _record_terminal_receipt(
        self,
        operation: ModelOperationV1,
        *,
        key: str,
        result_ref: Any,
        operation_ref: Any = None,
        error: ErrorEnvelopeV1 | None = None,
        status: ActivityReceiptStatus = ActivityReceiptStatus.SUCCEEDED,
    ) -> None:
        await self._upsert_operation_receipt(
            operation,
            key=key,
            status=ActivityReceiptStatus.FAILED if error is not None else status,
            result_ref=result_ref,
            operation_ref=operation_ref,
            error_code=None if error is None else error.code,
        )

    async def _upsert_operation_receipt(
        self,
        operation: ModelOperationV1,
        *,
        key: str,
        status: ActivityReceiptStatus,
        result_ref: Any,
        operation_ref: Any,
        error_code: str | None = None,
    ) -> None:
        request_hash = canonical_hash(
            {
                "backend_binding_hash": operation.backend_binding_hash,
                "deployment_hash": operation.deployment_hash,
                "operation_key": operation.operation_key,
            }
        )
        receipt = ActivityReceiptV1(
            operation_key=operation.operation_key,
            request_hash=request_hash,
            kind="model_operation",
            status=status,
            attempt=operation.poll_count + 1,
            updated_at=datetime.now(UTC),
            result_ref=result_ref,
            operation_ref=operation_ref,
            error_code=error_code,
        )
        current = await read_activity_receipt(self._receipts, key)
        if current is None:
            if not await create_activity_receipt(self._receipts, key, receipt):
                raise ApimResponsesError("model_operation_receipt_conflict")
            return
        observed, revision = current
        _validate_receipt(observed, request_hash)
        if not await replace_activity_receipt(
            self._receipts,
            key,
            receipt,
            revision=revision,
        ):
            raise ApimResponsesError("model_operation_receipt_conflict")

    async def _poll_result_from_receipt(
        self,
        operation: ModelOperationV1,
        receipt: ActivityReceiptV1,
    ) -> BackgroundPollResultV1:
        _validate_operation_receipt(receipt, operation)
        if (
            receipt.status is ActivityReceiptStatus.IN_PROGRESS
            and receipt.operation_ref is not None
        ):
            return BackgroundPollResultV1(
                operation=await get_protocol_model(
                    self._content,
                    receipt.operation_ref,
                    ModelOperationV1,
                )
            )
        if (
            receipt.status is ActivityReceiptStatus.SUCCEEDED
            and receipt.result_ref is not None
        ):
            return BackgroundPollResultV1(
                decision=await get_protocol_model(
                    self._content,
                    receipt.result_ref,
                    ModelDecisionEnvelopeV1,
                )
            )
        if receipt.status is ActivityReceiptStatus.FAILED:
            return BackgroundPollResultV1(
                error=ErrorEnvelopeV1(
                    code=receipt.error_code or "model_response_failed",
                    classification="model",
                    retryable=False,
                    phase="model_step",
                    step_index=operation.step_index,
                    detail_ref=receipt.result_ref,
                )
            )
        raise ApimResponsesError("model_operation_receipt_invalid")


def _require_background_response(response: Any | None) -> Any:
    if response is None:
        raise ApimResponsesError("model_start_failed")
    return response


def _decision_from_provider_payload(  # noqa: PLR0912, PLR0915
    operation: ModelOperationV1,
    payload: Mapping[str, object],
) -> ModelDecisionEnvelopeV1:
    output = payload.get("output")
    if not isinstance(output, list):
        raise DurableLoopModelError("provider response output is invalid")
    contents: list[Content] = []
    calls: list[ModelToolCallV1] = []
    text_parts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            raise DurableLoopModelError("provider response item is invalid")
        item_type = item.get("type")
        if item_type == "reasoning":
            summaries = item.get("summary")
            visible = (
                [summary for summary in summaries if isinstance(summary, Mapping)]
                if isinstance(summaries, list)
                else []
            )
            encrypted = item.get("encrypted_content")
            if encrypted is not None and not isinstance(encrypted, str):
                raise DurableLoopModelError("provider encrypted reasoning is invalid")
            if visible:
                for index, summary in enumerate(visible):
                    text = summary.get("text")
                    if not isinstance(text, str):
                        raise DurableLoopModelError("provider reasoning summary is invalid")
                    contents.append(
                        Content.from_text_reasoning(
                            id=_optional_text(item.get("id")),
                            text=text,
                            protected_data=encrypted if index == 0 else None,
                        )
                    )
            else:
                contents.append(
                    Content.from_text_reasoning(
                        id=_optional_text(item.get("id")),
                        text="",
                        protected_data=encrypted,
                    )
                )
            continue
        if item_type == "function_call":
            call_id = _required_text(item.get("call_id"), "provider call ID")
            name = _required_text(item.get("name"), "provider tool name")
            arguments = item.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    raise DurableLoopModelError(
                        "provider tool arguments are invalid JSON"
                    ) from None
            if not isinstance(arguments, Mapping):
                raise DurableLoopModelError("provider tool arguments are invalid")
            normalized = dict(arguments)
            calls.append(
                ModelToolCallV1(
                    call_id=call_id,
                    name=name,
                    arguments=normalized,
                )
            )
            contents.append(
                Content.from_function_call(
                    call_id=call_id,
                    name=name,
                    arguments=normalized,
                    id=_optional_text(item.get("id")),
                    additional_properties={
                        "status": _optional_text(item.get("status")) or "completed"
                    },
                )
            )
            continue
        if item_type == "message":
            message_content = item.get("content")
            if not isinstance(message_content, list):
                raise DurableLoopModelError("provider message content is invalid")
            for part in message_content:
                if not isinstance(part, Mapping) or part.get("type") != "output_text":
                    continue
                text = _required_text(part.get("text"), "provider output text")
                text_parts.append(text)
                contents.append(Content.from_text(text))
    usage = _provider_usage(payload.get("usage"))
    final_text = None if calls else "".join(text_parts)
    if calls and text_parts:
        raise DurableLoopModelError(
            "provider response mixed final text with function calls"
        )
    if not calls and (final_text is None or not final_text.strip()):
        raise DurableLoopModelError("provider response contains no final decision")
    response_id = _required_text(payload.get("id"), "provider response ID")
    return ModelDecisionEnvelopeV1(
        run_id=operation.run_id,
        step_index=operation.step_index,
        model_call_key=operation.operation_key,
        response_id_hash=canonical_hash({"response_id": response_id}),
        deployment_hash=operation.deployment_hash,
        assistant_message=Message("assistant", contents).to_dict(),
        tool_calls=tuple(calls),
        final_text=final_text,
        usage=usage,
        finish_reason="tool_calls" if calls else "stop",
        attempts=max(1, operation.poll_count + 1),
    )


def _provider_usage(value: object) -> UsageV1:
    if not isinstance(value, Mapping):
        return UsageV1()
    output_details = value.get("output_tokens_details")
    reasoning = (
        output_details.get("reasoning_tokens", 0)
        if isinstance(output_details, Mapping)
        else 0
    )
    return UsageV1(
        input_tokens=_nonnegative_int(value.get("input_tokens", 0)),
        output_tokens=_nonnegative_int(value.get("output_tokens", 0)),
        reasoning_tokens=_nonnegative_int(reasoning),
    )


def _terminal_error(
    operation: ModelOperationV1,
    status: str,
    body_ref: Any,
) -> ErrorEnvelopeV1:
    code = {
        "cancelled": "model_response_cancelled",
        "failed": "model_response_failed",
        "incomplete": "model_response_incomplete",
        "expired": "model_response_expired",
    }.get(status, "model_response_terminal")
    return ErrorEnvelopeV1(
        code=code,
        classification="model",
        retryable=False,
        phase="model_step",
        step_index=operation.step_index,
        detail_ref=body_ref,
    )


def _provider_status(payload: Mapping[str, object]) -> str:
    status = payload.get("status")
    if not isinstance(status, str) or status not in {
        "queued",
        "in_progress",
        "completed",
        "incomplete",
        "failed",
        "cancelled",
        "expired",
    }:
        raise DurableLoopModelError("provider response status is invalid")
    return status


def _validate_matching_response_id(
    payload: Mapping[str, object],
    expected: str,
) -> None:
    if _validate_response_id(payload.get("id")) != expected:
        raise DurableLoopModelError("provider response ID changed")


def _continuation_response_id(token: object) -> str:
    if not isinstance(token, Mapping):
        raise DurableLoopModelError("provider continuation token is invalid")
    return _validate_response_id(token.get("response_id"))


def _validate_response_id(value: object) -> str:
    if not isinstance(value, str) or _PROVIDER_RESPONSE_ID.fullmatch(value) is None:
        raise DurableLoopModelError("provider response ID is invalid")
    return value


def _model_operation_id(request: OneStepModelRequest) -> str:
    value = (
        "op-"
        + canonical_hash(
            {
                "run_id": request.identity.run_id,
                "step_index": request.step_index,
            }
        )[:32]
    )
    if _MODEL_OPERATION_ID.fullmatch(value) is None:
        raise DurableLoopModelError("model operation identifier is invalid")
    return value


def _cross_activity_429(
    request: OneStepModelRequest,
    client_kwargs: Mapping[str, object],
    error: Exception,
) -> bool:
    headers = client_kwargs.get("extra_headers")
    return (
        request.identity.orchestration_version
        == DURABLE_LOOP_ORCHESTRATOR_V3_NAME
        and request.fault_profile is DurableFaultProfile.MODEL_APIM_429_ONCE
        and isinstance(error, ApimResponsesError)
        and error.status_code == 429
        and isinstance(headers, Mapping)
        and headers.get(_DEMO_FAULT_HEADER) == _DEMO_MODEL_429_VALUE
    )


def _validate_control_base(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("APIM model control base must be a credential-free HTTPS URL")
    return value.rstrip("/")


def _normalized_model_exception(exc: Exception) -> Exception:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if not isinstance(status, int) and response is not None:
            status = getattr(response, "status_code", None)
        if isinstance(status, int):
            response_headers = getattr(response, "headers", {})
            headers = (
                dict(response_headers)
                if isinstance(response_headers, Mapping)
                else {}
            )
            return ApimResponsesError(
                "model_throttled" if status == 429 else "model_request_failed",
                status_code=status,
                retry_after_seconds=_retry_after(headers),
            )
        inner = getattr(current, "inner_exception", None)
        current = (
            inner
            if isinstance(inner, BaseException)
            else current.__cause__
        )
    return exc


def _retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, ApimResponsesError):
        if exc.ambiguous:
            return False
        return exc.status_code in _TRANSIENT_STATUS_CODES or exc.status_code is None
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in _TRANSIENT_STATUS_CODES


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if 0 <= parsed <= 300 else None


def _validate_receipt(receipt: ActivityReceiptV1, request_hash: str) -> None:
    if receipt.request_hash != request_hash:
        raise ApimResponsesError("model_receipt_request_conflict")


def _receipt_key(kind: str, operation_key: str) -> str:
    return f"activities/{kind}/{operation_key}"


def _poll_receipt_key(operation: ModelOperationV1) -> str:
    return (
        f"activities/model-poll/{operation.operation_key}/"
        f"poll-{operation.poll_count}"
    )


def _cancel_receipt_key(operation: ModelOperationV1) -> str:
    return f"activities/model-cancel/{operation.operation_key}"


def _validate_operation_receipt(
    receipt: ActivityReceiptV1,
    operation: ModelOperationV1,
) -> None:
    expected = canonical_hash(
        {
            "backend_binding_hash": operation.backend_binding_hash,
            "deployment_hash": operation.deployment_hash,
            "operation_key": operation.operation_key,
        }
    )
    _validate_receipt(receipt, expected)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise DurableLoopModelError(f"{name} is invalid")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DurableLoopModelError("provider usage is invalid")
    return value
