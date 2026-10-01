"""Policy-scoped refs-only HTTP routes for experimental durable agent runs."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import azure.durable_functions as df
import azure.functions as func
from azurefunctions.extensions.http.fastapi import Request, Response, StreamingResponse

from .._logger import logger
from ..client_manager import get_client_manager
from ..config import EndpointAuthConfig, ResolvedAgent
from ..registration._auth import (
    AuthError,
    resolve_endpoint_auth_level,
    resolve_owner_principal,
)
from ..session_state import EntraPrincipal, FunctionAppPrincipal, OwnerPrincipal
from ..strict_json import assert_json_value, canonical_json_bytes, decode_json_object
from .durable_chat_config import DurableChatSettings, durable_chat_route
from .durable_chat_journal import (
    DurableChatInitializationError,
    get_durable_chat_journal,
)
from .durable_chat_protocol import DurableChatRunInitializationV1
from .durable_loop import create_run_identity
from .durable_loop_activities import (
    DurableContentStore,
    DurableLoopContentError,
    get_protocol_model,
    put_protocol_model,
)
from .durable_loop_catalog import (
    DurableSkillCatalogAdmissionError,
    admit_durable_skill_catalog,
)
from .durable_loop_config import DurableLoopSettings
from .durable_loop_execution import build_durable_skill_provider
from .durable_loop_protocol import (
    CheckpointStateV1,
    CheckpointStateV2,
    ContentRefV1,
    DurableChatModelMode,
    DurableChatRunOptionsV1,
    DurableChatStartOptionsV1,
    DurableFaultProfile,
    DurableLoopBudgetV2,
    DurableLoopPlanDocumentV1,
    DurableLoopPlanDocumentV2,
    DurableLoopRunStatus,
    DurableOrchestrationInputV1,
    DurableOrchestrationInputV2,
    DurablePublicEventType,
    DurablePublicStatusV1,
    DurableRetentionPolicyV1,
    DurableRunDocumentV1,
    DurableRunDocumentV2,
    DurableRunIdentityV1,
    DurableRunIdentityV2,
    DurableTriggerAdmissionRecordV1,
    HumanInputRequestV1,
    HumanInputResponseV1,
    HumanResponseDisposition,
    MAFMessageBundleV1,
    SandboxExecutionProfile,
    WorkingContextV1,
    canonical_hash,
    validate_human_response_value,
)
from .durable_loop_receipts import DurableReceiptStoreError
from .durable_loop_registration import (
    DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
    DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME,
    DURABLE_LOOP_CANCEL_EVENT_NAME,
    DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
    DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME,
    DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME,
    DURABLE_LOOP_ORCHESTRATOR_V2_NAME,
    DURABLE_LOOP_ORCHESTRATOR_V3_NAME,
    DURABLE_LOOP_ORCHESTRATOR_V4_NAME,
    _normalize_durable_client_binding_annotation,
    configure_durable_loop_execution_binding,
    get_durable_loop_activity_runtime,
)
from .durable_loop_tools import (
    DurableRetainedSandboxInspectionPort,
    DurableToolCatalogPort,
    DurableToolInspectionError,
)
from .durable_retention import (
    DurableAdmissionReceiptState,
    DurableRetentionBusyError,
    DurableRetentionConflictError,
    DurableRetentionError,
    DurableRetentionManager,
    DurableRunAccessRecord,
    DurableRunVisibility,
    DurableSessionBusyError,
    DurableSessionExpiredError,
    is_retention_indexed_identity,
    retained_identifier_hash,
)
from .durable_run_observations import (
    DurableRunObservationError,
    DurableRunObservationReplayV2,
    DurableRunReplayDisposition,
    get_durable_run_observation_journal,
    initialize_durable_run_observations,
    reconcile_durable_run_terminal,
    render_public_sse_frame,
    replay_durable_run_observations,
)
from .durable_skill_providers import DurableSkillProvider, DurableSkillProviderError

if TYPE_CHECKING:
    from .durable_trigger_admission import (
        DurableTriggerAdmissionAttemptV1,
        DurableTriggerAdmissionCallback,
    )

_ROUTE_BASE = "experimental/durable-agent-runs"
_SHORT_WAIT_SECONDS = 30.0
_SHORT_POLL_SECONDS = 0.05
_MAX_PROMPT_BYTES = 256 * 1024
_MAX_HUMAN_ANSWER_BYTES = 64 * 1024
_EVENT_LEASE_SECONDS = 210.0
_EVENT_HEARTBEAT_SECONDS = 15.0
_EVENT_POLL_SECONDS = 1.0
_EVENT_CURSOR_PATTERN = re.compile(r"^(?:0|[1-9][0-9]{0,18})$")
_DURABLE_DATA_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_DURABLE_DATA_HEADER_NAMES = frozenset(
    header.casefold() for header in _DURABLE_DATA_HEADERS
)


class _DurableStartOutcome(StrEnum):
    STARTED = "started"
    RECONCILED = "reconciled"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class _AnonymousDurableOwner:
    """The separate shared scope of an explicitly anonymous durable app."""


type _DurableOwner = OwnerPrincipal | _AnonymousDurableOwner


def register_durable_loop_http_routes(  # noqa: PLR0915
    app: func.FunctionApp,
    *,
    resolved: ResolvedAgent,
    settings: DurableLoopSettings,
    chat_settings: DurableChatSettings | None = None,
) -> None:
    """Register private start/status/result/cancel/human-input routes."""
    configure_durable_loop_execution_binding(
        enabled_mcp_names=resolved.enabled_mcp_names,
        local_tools_enabled=not resolved.tools_disabled,
    )
    auth = resolved.builtin_endpoints.http_auth
    auth_level = resolve_endpoint_auth_level(auth)

    async def start_run(  # noqa: PLR0912, PLR0915
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        owner = _authorized_owner(req, auth)
        if isinstance(owner, Response):
            return owner
        csrf_failure = _same_origin_mutation_failure(req)
        if csrf_failure is not None:
            return csrf_failure
        try:
            body = await req.json()
            payload = _start_payload(
                body,
                chat_enabled=chat_settings is not None and chat_settings.enabled,
            )
            chat_options = _frozen_chat_options(payload, settings)
            session_id = _session_id(req, payload)
            request_id = _request_id(req, payload)
            owner_hash = _owner_hash(owner)
            run_id = _run_id()
            chat_ui = chat_options is not None
            if chat_ui:
                admission_now = datetime.now(UTC)
                admission_expires_at = admission_now + timedelta(
                    seconds=settings.max_run_wait_seconds
                )
                metadata = None
            else:
                metadata = await _run_metadata(
                    resolved=resolved,
                    settings=settings,
                    body=payload,
                    owner_hash=owner_hash,
                    session_id=session_id,
                    request_id=request_id,
                    run_id=run_id,
                )
                admission_now = metadata.identity.created_at
                admission_expires_at = metadata.identity.absolute_deadline
        except ValueError as exc:
            return _json_response({"error": str(exc)}, status_code=400)

        if chat_ui:
            admission_payload: dict[str, object] = {
                "chat_ui": True,
                "expires_at": admission_expires_at.isoformat(),
                "now": admission_now.isoformat(),
                "request_hash": canonical_hash(payload),
                "request_id_hash": canonical_hash({"request_id": request_id}),
                "run_id": run_id,
                "session_entity_key": _session_entity_key(owner_hash, session_id),
            }
        else:
            if metadata is None:
                return _json_response({"error": "admission_unavailable"}, status_code=503)
            admission_payload = {
                "expires_at": metadata.identity.absolute_deadline.isoformat(),
                "now": metadata.identity.created_at.isoformat(),
                "request_hash": metadata.identity.request_hash,
                "request_id_hash": metadata.identity.request_id_hash,
                "run_id": run_id,
                "session_entity_key": metadata.session_entity_key,
            }
        admission = await _admit_durable_run(client, admission_payload)
        disposition = admission.get("disposition")
        if disposition == "replayed":
            existing_run_id = admission.get("run_id")
            if not isinstance(existing_run_id, str):
                return _json_response(
                    {"error": "invalid_admission_receipt"},
                    status_code=503,
                )
            existing = await client.get_status(existing_run_id)
            if existing is not None:
                return _accepted_response(
                    existing_run_id,
                    session_id,
                    chat_ui=chat_ui,
                )
            lifecycle = admission.get("lifecycle")
            if lifecycle == "completed":
                response_ref = _optional_content_ref(
                    admission.get("terminal_response_ref")
                )
                if response_ref is None:
                    return _json_response(
                        {
                            "error": "result_expired",
                            "run_id": existing_run_id,
                        },
                        status_code=410,
                    )
                response = (
                    await get_durable_loop_activity_runtime().content.get_bytes(
                        response_ref
                    )
                ).decode("utf-8")
                return _json_response(
                    {
                        "response": response,
                        "run_id": existing_run_id,
                        "session_id": session_id,
                        "status": DurableLoopRunStatus.COMPLETED.value,
                    }
                )
            if lifecycle == "aborted":
                return _json_response(
                    {
                        "disposition": admission.get("terminal_disposition"),
                        "error": admission.get("terminal_error") or "run_terminal",
                        "possibly_committed": (
                            admission.get("terminal_possibly_committed") is True
                        ),
                        "run_id": existing_run_id,
                        "status": admission.get("terminal_status")
                        or DurableLoopRunStatus.FAILED.value,
                    },
                    status_code=410,
                )
            if lifecycle == "running" and not chat_ui:
                return _json_response(
                    {
                        "possibly_committed": True,
                        "run_id": existing_run_id,
                        "session_id": session_id,
                        "status": DurableLoopRunStatus.RUNNING.value,
                        "status_url": (
                            f"{_durable_route_base()}/{existing_run_id}"
                        ),
                    },
                    status_code=202,
                )
            run_id = existing_run_id
            if metadata is not None:
                metadata = replace(
                    metadata,
                    identity=metadata.identity.model_copy(update={"run_id": run_id}),
                )
        elif disposition == "conflict":
            return _json_response({"error": "idempotency_conflict"}, status_code=409)
        elif disposition == "busy":
            return _json_response(
                {
                    "active_run_id": admission.get("active_run_id"),
                    "error": "session_busy",
                },
                status_code=409,
            )
        elif disposition != "admitted":
            return _json_response({"error": "admission_unavailable"}, status_code=503)

        if chat_ui:
            durable_input = await _recover_or_initialize_chat_run(
                admission=admission,
                client=client,
                resolved=resolved,
                settings=settings,
                chat_settings=chat_settings,
                payload=payload,
                owner_hash=owner_hash,
                session_id=session_id,
                request_id=request_id,
                run_id=run_id,
            )
            if isinstance(durable_input, Response):
                return durable_input
        else:
            if metadata is None:
                return _json_response({"error": "admission_unavailable"}, status_code=503)
            committed_generation = _nonnegative_int(
                admission.get("committed_generation"),
                "committed_generation",
            )
            try:
                durable_input = await _persist_run_input(
                    metadata,
                    prompt=str(payload["prompt"]).strip(),
                    committed_context_ref=_optional_content_ref(
                        admission.get("committed_context_ref")
                    ),
                    committed_generation=committed_generation,
                )
            except Exception:
                logger.exception("durable-loop content persistence failed")
                await _run_short_orchestration(
                    client,
                    DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
                    {
                        "operation": "release_admission",
                        "run_id": run_id,
                        "session_entity_key": metadata.session_entity_key,
                    },
                )
                return _json_response(
                    {"error": "content_persistence_failed"},
                    status_code=503,
                )
        start_outcome = await _start_durable_run(
            client,
            durable_input,
            run_id=run_id,
        )
        if start_outcome is _DurableStartOutcome.UNCONFIRMED:
            return _json_response(
                {
                    "error": "run_start_acknowledgement_lost",
                    "possibly_committed": True,
                    "run_id": run_id,
                },
                status_code=202,
            )
        return _accepted_response(run_id, session_id, chat_ui=chat_ui)

    async def get_status(
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return authorized
        if authorized.retained is not None and (
            authorized.retained.visibility is DurableRunVisibility.EXPIRED
        ):
            return _json_response({"error": "run_expired"}, status_code=410)
        if authorized.terminal_projection is not None:
            return _json_response(
                authorized.terminal_projection.model_dump(mode="json")
            )
        status = authorized.status
        durable_input = authorized.durable_input
        if status is None or durable_input is None:
            return _json_response(
                {"error": "retention_state_unavailable"},
                status_code=503,
            )
        body = _status_projection(status)
        custom = status.custom_status
        if isinstance(custom, Mapping) and custom.get("phase") == "human_wait":
            request_ref = _optional_content_ref(custom.get("request_ref"))
            if request_ref is not None:
                runtime = get_durable_loop_activity_runtime()
                request = await get_protocol_model(
                    runtime.content,
                    request_ref,
                    HumanInputRequestV1,
                )
                body["human_input"] = {
                    "allow_free_text": request.allow_free_text,
                    "choice_count": len(request.choices),
                    "detail_url": (
                        f"{_durable_route_base()}/{status.instance_id}/"
                        f"input/{request.request_id}"
                    ),
                    "expires_at": request.expires_at.isoformat(),
                    "request_id": request.request_id,
                    "respond_url": (
                        f"{_durable_route_base()}/{status.instance_id}/"
                        f"input/{request.request_id}"
                    ),
                    "schema_present": request.response_schema is not None,
                }
        body["session_id"] = durable_input.identity.session_id
        return _json_response(body)

    async def get_events(
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return authorized
        if authorized.retained is not None and (
            authorized.retained.visibility is DurableRunVisibility.EXPIRED
        ):
            return _json_response({"error": "events_expired"}, status_code=410)
        status = authorized.status
        durable_input = authorized.durable_input
        run_id = _path_parameter(req, "run_id")
        journal = get_durable_run_observation_journal()
        try:
            if durable_input is not None:
                await initialize_durable_run_observations(
                    durable_input,
                    journal=journal,
                )
            initialization = await journal.load_observation_initialization(
                run_id=run_id
            )
            if (
                initialization is not None
                and datetime.now(UTC)
                >= initialization.events_expires_at.astimezone(UTC)
            ):
                return _json_response({"error": "events_expired"}, status_code=410)
            after_sequence = _event_cursor(req)
            if status is not None:
                await _reconcile_public_terminal_status(
                    status=status,
                    run_id=run_id,
                    journal=journal,
                )
            initial_page = await replay_durable_run_observations(
                run_id=run_id,
                after_sequence=after_sequence,
                journal=journal,
            )
        except ValueError:
            return _json_response({"error": "invalid_event_cursor"}, status_code=400)
        except DurableRunObservationError:
            return _json_response({"error": "events_unavailable"}, status_code=503)
        if initial_page.disposition is DurableRunReplayDisposition.CURSOR_AHEAD:
            return _json_response(
                {
                    "error": "event_cursor_ahead",
                    "through_sequence": initial_page.through_sequence,
                },
                status_code=400,
            )
        return StreamingResponse(
            _stream_public_events(
                client=client,
                initial_page=initial_page,
                journal=journal,
                run_id=run_id,
            ),
            media_type="text/event-stream",
            headers={
                **_DURABLE_DATA_HEADERS,
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def get_result(
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return authorized
        if authorized.retained is not None and (
            authorized.retained.visibility is DurableRunVisibility.EXPIRED
        ):
            return _json_response({"error": "result_expired"}, status_code=410)
        status = authorized.status
        durable_input = authorized.durable_input
        if status is None or durable_input is None:
            return _json_response({"error": "result_expired"}, status_code=410)
        output = status.output if isinstance(status.output, Mapping) else {}
        if output.get("status") != DurableLoopRunStatus.COMPLETED.value:
            if _runtime_status_name(status) in {"Completed", "Failed", "Terminated"}:
                return _json_response(
                    {
                        "error": output.get("error") or "durable_run_failed",
                        "run_id": status.instance_id,
                        "status": output.get("status")
                        or DurableLoopRunStatus.FAILED.value,
                    },
                    status_code=409,
                )
            return _json_response({"error": "result_not_ready"}, status_code=202)
        response_ref = _optional_content_ref(output.get("response_ref"))
        if response_ref is None:
            return _json_response({"error": "result_unavailable"}, status_code=410)
        response = (
            await get_durable_loop_activity_runtime().content.get_bytes(response_ref)
        ).decode("utf-8")
        return _json_response(
            {
                "response": response,
                "run_id": status.instance_id,
                "session_id": durable_input.identity.session_id,
                "status": DurableLoopRunStatus.COMPLETED.value,
            }
        )

    async def get_sandbox(
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return authorized
        status = authorized.status
        durable_input = authorized.durable_input
        if status is None or durable_input is None:
            return _json_response({"error": "run_expired"}, status_code=410)
        if (
            durable_input.sandbox_profile
            is not SandboxExecutionProfile.RETAINED_SESSION
        ):
            return _json_response(
                {"error": "retained_sandbox_not_configured"},
                status_code=404,
            )
        runtime = get_durable_loop_activity_runtime()
        if not isinstance(
            runtime.tools,
            DurableRetainedSandboxInspectionPort,
        ):
            return _json_response(
                {"error": "retained_sandbox_inspection_unavailable"},
                status_code=503,
            )
        try:
            inspection = await runtime.tools.inspect_retained_sandbox(
                run_id=status.instance_id,
                session_id=durable_input.identity.session_id,
            )
        except DurableToolInspectionError:
            logger.exception("durable-loop retained sandbox inspection failed")
            return _json_response(
                {"error": "retained_sandbox_inspection_unavailable"},
                status_code=503,
            )
        if inspection is None:
            return _json_response(
                {"error": "retained_sandbox_not_found"},
                status_code=404,
            )
        return _json_response(asdict(inspection))

    async def cancel_run(
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return authorized
        csrf_failure = _same_origin_mutation_failure(req)
        if csrf_failure is not None:
            return csrf_failure
        status = authorized.status
        durable_input = authorized.durable_input
        if status is None or durable_input is None:
            return _json_response({"error": "run_expired"}, status_code=410)
        if _runtime_status_name(status) in {"Completed", "Failed", "Terminated"}:
            return _json_response({"error": "run_terminal"}, status_code=410)
        receipt = await _run_short_orchestration(
            client,
            DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
            {
                "operation": "cancel",
                "run_id": status.instance_id,
                "session_entity_key": durable_input.session_entity_key,
            },
        )
        if receipt.get("disposition") not in {"accepted", "replayed"}:
            return _json_response({"error": "cancel_conflict"}, status_code=409)
        cancel_delivery_input = {
            "event_data": {"run_id": status.instance_id},
            "event_name": DURABLE_LOOP_CANCEL_EVENT_NAME,
            "expires_at": durable_input.identity.absolute_deadline.isoformat(),
            "run_id": status.instance_id,
        }
        cancel_delivery_id = "cancel-delivery-" + canonical_hash(
            {"run_id": status.instance_id}
        )[:48]
        if await client.get_status(cancel_delivery_id) is None:
            try:
                await client.start_new(
                    DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME,
                    instance_id=cancel_delivery_id,
                    client_input=cancel_delivery_input,
                )
            except Exception:
                logger.exception("durable cancellation delivery outbox failed to start")
        delivery = "delivered"
        try:
            await client.raise_event(
                status.instance_id,
                DURABLE_LOOP_CANCEL_EVENT_NAME,
                {"run_id": status.instance_id},
            )
        except Exception:
            delivery = "retry_pending"
        return _json_response(
            {
                "delivery": delivery,
                "run_id": status.instance_id,
                "status": DurableLoopRunStatus.CANCELLED.value,
            },
            status_code=202,
        )

    async def submit_human_input(  # noqa: PLR0912, PLR0915
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return authorized
        csrf_failure = _same_origin_mutation_failure(req)
        if csrf_failure is not None:
            return csrf_failure
        status = authorized.status
        durable_input = authorized.durable_input
        if status is None or durable_input is None:
            return _json_response({"error": "human_input_gone"}, status_code=410)
        custom = status.custom_status
        if not isinstance(custom, Mapping) or custom.get("phase") != "human_wait":
            return _json_response({"error": "human_input_not_pending"}, status_code=410)
        request_id = _path_parameter(req, "request_id")
        request_ref = _optional_content_ref(custom.get("request_ref"))
        if request_ref is None:
            return _json_response({"error": "human_input_not_found"}, status_code=404)
        runtime = get_durable_loop_activity_runtime()
        try:
            human = await get_protocol_model(
                runtime.content,
                request_ref,
                HumanInputRequestV1,
            )
        except DurableLoopContentError:
            if is_retention_indexed_identity(durable_input.identity):
                return _json_response({"error": "human_input_gone"}, status_code=410)
            raise
        owner_hash = durable_input.identity.owner_hash
        if (
            human.request_id != request_id
            or human.run_id != status.instance_id
        ):
            return _json_response({"error": "human_input_not_found"}, status_code=404)
        if human.actor_policy_hash != owner_hash:
            return _json_response({"error": "human_input_not_found"}, status_code=404)
        try:
            body = await req.json()
        except ValueError:
            return _json_response({"error": "invalid_json"}, status_code=400)
        if not isinstance(body, Mapping) or "answer" not in body:
            return _json_response({"error": "missing_answer"}, status_code=400)
        submission_id = req.headers.get("Idempotency-Key")
        if not submission_id:
            return _json_response({"error": "missing_submission_id"}, status_code=400)
        try:
            _validate_answer(human, body["answer"])
        except ValueError as exc:
            return _json_response({"error": str(exc)}, status_code=400)
        body_hash = canonical_hash({"answer": body["answer"]})
        submission_id_hash = canonical_hash({"submission_id": submission_id})
        accepted_at = datetime.now(UTC)
        if (
            is_retention_indexed_identity(durable_input.identity)
            and accepted_at >= human.expires_at
        ):
            return _json_response({"error": "human_input_gone"}, status_code=410)
        reservation = await _run_short_orchestration(
            client,
            DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
            {
                "accepted_at": accepted_at.isoformat(),
                "body_hash": body_hash,
                "call_key": human.call_key,
                "expires_at": human.expires_at.isoformat(),
                "generation": human.generation,
                "now": accepted_at.isoformat(),
                "operation": "reserve_human",
                "owner_hash": owner_hash,
                "request_id": human.request_id,
                "run_id": human.run_id,
                "session_entity_key": durable_input.session_entity_key,
                "submission_id_hash": submission_id_hash,
            },
        )
        reservation_disposition = reservation.get("disposition")
        if reservation_disposition == "conflict":
            return _json_response({"error": "human_input_conflict"}, status_code=409)
        if reservation_disposition in {"gone", "stale"}:
            return _json_response({"error": "human_input_gone"}, status_code=410)
        if reservation_disposition not in {
            "accepted",
            "consumed",
            "orphaned",
            "reserved",
        }:
            return _json_response(
                {"error": "human_input_reservation_unavailable"},
                status_code=503,
            )
        response_ref = _optional_content_ref(reservation.get("response_ref"))
        if response_ref is None:
            stored_accepted_at = reservation.get("accepted_at")
            if isinstance(stored_accepted_at, str):
                accepted_at = datetime.fromisoformat(
                    stored_accepted_at.replace("Z", "+00:00")
                )
            answer_payload = canonical_json_bytes(body["answer"])
            if (
                runtime.retention is not None
                and is_retention_indexed_identity(durable_input.identity)
            ):
                answer_ref = await runtime.retention.tracked_put(
                    run_id=human.run_id,
                    kind="human-answer",
                    payload=answer_payload,
                    media_type="application/json",
                    retention_class="human",
                    expires_at=human.expires_at,
                    now=accepted_at,
                )
            else:
                answer_ref = await runtime.content.put_bytes(
                    kind="human-answer",
                    payload=answer_payload,
                    media_type="application/json",
                    retention_class="human",
                )
            response = HumanInputResponseV1(
                request_id=human.request_id,
                run_id=human.run_id,
                session_id=human.session_id,
                generation=human.generation,
                call_id=human.call_id,
                submission_id_hash=submission_id_hash,
                body_hash=body_hash,
                actor_hash=owner_hash,
                answer_ref=answer_ref,
                accepted_at=accepted_at,
                schema_valid=True,
                disposition=HumanResponseDisposition.ACCEPTED,
            )
            if (
                runtime.retention is not None
                and is_retention_indexed_identity(durable_input.identity)
            ):
                response_ref = await runtime.retention.tracked_put(
                    run_id=human.run_id,
                    kind="human-response",
                    payload=canonical_json_bytes(response),
                    media_type="application/json",
                    retention_class="human",
                    expires_at=human.expires_at,
                    now=accepted_at,
                )
            else:
                response_ref = await put_protocol_model(
                    runtime.content,
                    kind="human-response",
                    model=response,
                )
        receipt = await _run_short_orchestration(
            client,
            DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME,
            {
                "body_hash": body_hash,
                "call_key": human.call_key,
                "generation": human.generation,
                "expires_at": human.expires_at.isoformat(),
                "now": accepted_at.isoformat(),
                "owner_hash": owner_hash,
                "request_id": human.request_id,
                "response_ref": response_ref.model_dump(mode="json"),
                "run_id": human.run_id,
                "session_entity_key": durable_input.session_entity_key,
                "submission_id_hash": submission_id_hash,
            },
        )
        disposition = receipt.get("disposition")
        if disposition == "conflict":
            return _json_response({"error": "human_input_conflict"}, status_code=409)
        if disposition in {"gone", "stale"}:
            return _json_response({"error": "human_input_gone"}, status_code=410)
        if disposition not in {"accepted", "replayed"}:
            return _json_response(
                {"error": "human_input_acceptance_unavailable"},
                status_code=503,
            )
        if receipt.get("delivery") == "orphaned":
            return _json_response(
                {
                    "delivery": "orphaned",
                    "error": "run_terminal",
                    "request_id": request_id,
                    "run_id": status.instance_id,
                    "status": "accepted",
                },
                status_code=410,
            )

        if receipt.get("delivery") == "delivered":
            return _json_response(
                {
                    "delivery": "delivered",
                    "request_id": request_id,
                    "run_id": status.instance_id,
                    "status": "accepted",
                },
                status_code=202,
            )
        delivery_input = {
            "event_data": {
                "body_hash": body_hash,
                "request_id": human.request_id,
                "submission_id_hash": submission_id_hash,
            },
            "event_name": human.event_name,
            "expires_at": human.expires_at.isoformat(),
            "request_id": human.request_id,
            "run_id": human.run_id,
            "session_entity_key": durable_input.session_entity_key,
        }
        delivery_id = "human-delivery-" + canonical_hash(
            {
                "body_hash": body_hash,
                "request_id": human.request_id,
                "run_id": human.run_id,
            }
        )[:48]
        if await client.get_status(delivery_id) is None:
            try:
                await client.start_new(
                    DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME,
                    instance_id=delivery_id,
                    client_input=delivery_input,
                )
            except Exception:
                logger.exception("durable human-input delivery outbox failed to start")
        try:
            await client.raise_event(
                status.instance_id,
                human.event_name,
                {
                    "body_hash": body_hash,
                    "request_id": human.request_id,
                    "submission_id_hash": submission_id_hash,
                },
            )
            await _run_short_orchestration(
                client,
                DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
                {
                    "delivery": "delivered",
                    "operation": "mark_human_delivery",
                    "request_id": human.request_id,
                    "session_entity_key": durable_input.session_entity_key,
                },
            )
            delivery = "delivered"
        except Exception as exc:
            if _http_status_code(exc) in {404, 410}:
                await _run_short_orchestration(
                    client,
                    DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
                    {
                        "operation": "orphan_human",
                        "request_id": human.request_id,
                        "session_entity_key": durable_input.session_entity_key,
                    },
                )
                return _json_response(
                    {
                        "delivery": "orphaned",
                        "error": "run_terminal",
                        "request_id": request_id,
                        "run_id": status.instance_id,
                        "status": "accepted",
                    },
                    status_code=410,
                )
            delivery = "retry_pending"
        return _json_response(
            {
                "delivery": delivery,
                "request_id": request_id,
                "run_id": status.instance_id,
                "status": "accepted",
            },
            status_code=202,
        )

    async def get_human_input(
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return authorized
        status = authorized.status
        durable_input = authorized.durable_input
        if status is None or durable_input is None:
            return _json_response({"error": "human_input_gone"}, status_code=410)
        custom = status.custom_status
        if not isinstance(custom, Mapping) or custom.get("phase") != "human_wait":
            return _json_response({"error": "human_input_gone"}, status_code=410)
        request_ref = _optional_content_ref(custom.get("request_ref"))
        if request_ref is None:
            return _json_response({"error": "human_input_not_found"}, status_code=404)
        runtime = get_durable_loop_activity_runtime()
        try:
            human = await get_protocol_model(
                runtime.content,
                request_ref,
                HumanInputRequestV1,
            )
        except DurableLoopContentError:
            if is_retention_indexed_identity(durable_input.identity):
                return _json_response({"error": "human_input_gone"}, status_code=410)
            raise
        if (
            is_retention_indexed_identity(durable_input.identity)
            and datetime.now(UTC) >= human.expires_at
        ):
            return _json_response({"error": "human_input_gone"}, status_code=410)
        request_id = _path_parameter(req, "request_id")
        if (
            human.request_id != request_id
            or human.run_id != status.instance_id
            or human.actor_policy_hash != durable_input.identity.owner_hash
        ):
            return _json_response({"error": "human_input_not_found"}, status_code=404)
        question = (
            await runtime.content.get_bytes(human.question_ref)
        ).decode("utf-8")
        return _json_response(
            {
                "allow_free_text": human.allow_free_text,
                "choices": list(human.choices),
                "expires_at": human.expires_at.isoformat(),
                "question": question,
                "request_id": human.request_id,
                "response_schema": human.response_schema,
                "run_id": human.run_id,
            }
        )

    _register_route(app, "durable_agent_run_start_v1", _ROUTE_BASE, ["POST"], auth_level, start_run)
    _register_route(
        app,
        "durable_agent_run_status_v1",
        f"{_ROUTE_BASE}/{{run_id}}",
        ["GET"],
        auth_level,
        get_status,
    )
    _register_route(
        app,
        "durable_agent_run_result_v1",
        f"{_ROUTE_BASE}/{{run_id}}/result",
        ["GET"],
        auth_level,
        get_result,
    )
    _register_route(
        app,
        "durable_agent_run_events_v2",
        f"{_ROUTE_BASE}/{{run_id}}/events",
        ["GET"],
        auth_level,
        get_events,
    )
    _register_route(
        app,
        "durable_agent_run_sandbox_v1",
        f"{_ROUTE_BASE}/{{run_id}}/sandbox",
        ["GET"],
        auth_level,
        get_sandbox,
    )
    _register_route(
        app,
        "durable_agent_run_cancel_v1",
        f"{_ROUTE_BASE}/{{run_id}}/cancel",
        ["POST"],
        auth_level,
        cancel_run,
    )
    _register_route(
        app,
        "durable_agent_run_human_input_detail_v1",
        f"{_ROUTE_BASE}/{{run_id}}/input/{{request_id}}",
        ["GET"],
        auth_level,
        get_human_input,
    )
    _register_route(
        app,
        "durable_agent_run_human_input_v1",
        f"{_ROUTE_BASE}/{{run_id}}/input/{{request_id}}",
        ["POST"],
        auth_level,
        submit_human_input,
    )


def _register_route(
    app: func.FunctionApp,
    name: str,
    route: str,
    methods: list[str],
    auth_level: func.AuthLevel,
    handler: Any,
) -> None:
    handler.__name__ = name
    decorated = app.durable_client_input(client_name="client")(handler)
    _normalize_durable_client_binding_annotation(
        decorated,
        client_name="client",
    )
    app.route(route=route, methods=methods, auth_level=auth_level)(decorated)


async def _admit_durable_run(
    client: df.DurableOrchestrationClient,
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    """Use the single session admission entity flow for every durable run source."""
    return await _run_short_orchestration(
        client,
        DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
        dict(payload),
    )


async def _start_durable_run(
    client: df.DurableOrchestrationClient,
    durable_input: DurableOrchestrationInputV1 | DurableOrchestrationInputV2,
    *,
    run_id: str,
) -> _DurableStartOutcome:
    """Initialize observations before start and reconcile a lost start acknowledgement."""
    try:
        await initialize_durable_run_observations(durable_input)
    except Exception:
        logger.warning(
            "durable run observation initialization was unavailable: run_id=%s",
            run_id,
        )
    try:
        await client.start_new(
            durable_input.identity.orchestration_version,
            instance_id=run_id,
            client_input=durable_input.model_dump(mode="json"),
        )
    except Exception:
        logger.exception("durable-loop orchestration start acknowledgement was lost")
        if await client.get_status(run_id) is not None:
            return _DurableStartOutcome.RECONCILED
        return _DurableStartOutcome.UNCONFIRMED
    return _DurableStartOutcome.STARTED


@dataclass(frozen=True, slots=True)
class _RunMetadata:
    identity: DurableRunIdentityV1
    plan: DurableLoopPlanDocumentV1
    session_entity_key: str


@dataclass(frozen=True, slots=True)
class _TriggerRunMetadata:
    identity: DurableRunIdentityV2
    plan: DurableLoopPlanDocumentV2
    session_entity_key: str


@dataclass(frozen=True, slots=True)
class _AuthorizedRun:
    status: Any | None
    durable_input: DurableOrchestrationInputV1 | None
    retained: DurableRunAccessRecord | None = None
    terminal_projection: DurablePublicStatusV1 | None = None

    def __iter__(self) -> Iterator[Any]:
        """Preserve the legacy two-value authorization helper contract."""
        yield self.status
        yield self.durable_input


def create_durable_trigger_admission_callback(  # noqa: PLR0915
    *,
    settings: DurableLoopSettings,
    resolved_agents: Mapping[str, ResolvedAgent],
    content: DurableContentStore | None = None,
    retention: DurableRetentionManager | None = None,
) -> DurableTriggerAdmissionCallback:
    """Create the trigger adapter for the shared durable admission/start flow."""
    provider = build_durable_skill_provider(settings)

    async def admit(  # noqa: PLR0912, PLR0915
        record: DurableTriggerAdmissionRecordV1,
        client: df.DurableOrchestrationClient,
    ) -> DurableTriggerAdmissionAttemptV1:
        from .durable_trigger_admission import (
            DurableTriggerAdmissionAttemptV1,
            DurableTriggerAttemptOutcome,
        )

        resolved = resolved_agents.get(record.agent_slug)
        if resolved is None:
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.TERMINAL,
                error_code="trigger_agent_unavailable",
            )
        manager = retention
        if manager is None:
            try:
                manager = get_durable_loop_activity_runtime().retention
            except RuntimeError:
                manager = None
        session_resource = None
        try:
            metadata = await _trigger_run_metadata(
                record=record,
                resolved=resolved,
                settings=settings,
                provider=provider,
                content=content,
            )
            if manager is None and isinstance(metadata, _TriggerRunMetadata):
                return DurableTriggerAdmissionAttemptV1(
                    outcome=DurableTriggerAttemptOutcome.RETRY,
                    error_code="retention_state_unavailable",
                )
            if manager is not None:
                _, retained_admission = await manager.admit_run_resource(
                    metadata.identity
                )
                if (
                    retained_admission.replayed
                    and retained_admission.receipt.state
                    is DurableAdmissionReceiptState.TERMINAL
                ):
                    return DurableTriggerAdmissionAttemptV1(
                        outcome=DurableTriggerAttemptOutcome.ADMITTED
                    )
                session_resource = await manager.acquire_session_admission(
                    session_id_hash=retained_identifier_hash(
                        "session",
                        record.session_id,
                    ),
                    owner_hash=record.owner_hash,
                    access_namespace_hash=record.access_namespace_hash,
                    run_id_hash=retained_identifier_hash("run", record.run_id),
                    now=record.staged_at,
                )
        except DurableRetentionConflictError:
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.TERMINAL,
                error_code="idempotency_conflict",
            )
        except DurableSessionExpiredError:
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.TERMINAL,
                error_code="session_expired",
            )
        except DurableSkillCatalogAdmissionError:
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.TERMINAL,
                error_code="trigger_skill_catalog_invalid",
            )
        except ValueError:
            logger.exception(
                "durable trigger metadata was rejected: record_key=%s",
                record.record_key,
            )
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.TERMINAL,
                error_code="trigger_run_invalid",
            )
        except (DurableRetentionBusyError, DurableSessionBusyError):
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.RETRY,
                error_code="session_busy",
            )
        except (DurableRetentionError, DurableLoopContentError, DurableSkillProviderError):
            logger.exception(
                "durable trigger retention admission was unavailable: record_key=%s",
                record.record_key,
            )
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.RETRY,
                error_code="retention_state_unavailable",
            )

        async def release_retained_fence() -> None:
            if session_resource is None or manager is None:
                return
            await manager.release_session_admission(
                session_id_hash=retained_identifier_hash(
                    "session",
                    record.session_id,
                ),
                run_id_hash=retained_identifier_hash("run", record.run_id),
            )

        session_entity_key = getattr(
            metadata,
            "session_entity_key",
            _session_entity_key(record.owner_hash, record.session_id),
        )
        admission_payload: dict[str, object] = {
            "expires_at": (
                record.staged_at + timedelta(seconds=settings.max_run_wait_seconds)
            ).isoformat(),
            "now": record.staged_at.isoformat(),
            "request_hash": record.request_hash,
            "request_id_hash": record.stable_event_id_hash,
            "run_id": record.run_id,
            "session_entity_key": session_entity_key,
        }
        if session_resource is not None:
            admission_payload.update(
                {
                    "retained_context_ref": (
                        session_resource.context_ref.model_dump(mode="json")
                    ),
                    "retained_generation": session_resource.committed_generation,
                }
            )
        admission = await _admit_durable_run(client, admission_payload)
        disposition = admission.get("disposition")
        if disposition == "conflict":
            await release_retained_fence()
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.TERMINAL,
                error_code="idempotency_conflict",
            )
        if disposition in {"busy", "idempotency_capacity_exceeded"}:
            await release_retained_fence()
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.RETRY,
                error_code=str(disposition),
            )
        if disposition not in {"admitted", "replayed"}:
            await release_retained_fence()
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.RETRY,
                error_code="admission_unavailable",
            )
        admitted_run_id = admission.get("run_id")
        if not isinstance(admitted_run_id, str):
            await release_retained_fence()
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.RETRY,
                error_code="invalid_admission_receipt",
            )
        if admitted_run_id != record.run_id:
            await release_retained_fence()
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.TERMINAL,
                error_code="admission_run_mismatch",
            )
        if disposition == "replayed":
            if await client.get_status(record.run_id) is not None:
                return DurableTriggerAdmissionAttemptV1(
                    outcome=DurableTriggerAttemptOutcome.ADMITTED
                )
            lifecycle = admission.get("lifecycle")
            if lifecycle in {"running", "completed", "aborted"}:
                return DurableTriggerAdmissionAttemptV1(
                    outcome=DurableTriggerAttemptOutcome.ADMITTED
                )
            if lifecycle != "admitted":
                await release_retained_fence()
                return DurableTriggerAdmissionAttemptV1(
                    outcome=DurableTriggerAttemptOutcome.RETRY,
                    error_code="invalid_admission_receipt",
                )

        try:
            committed_context_ref = _optional_content_ref(
                admission.get("committed_context_ref")
            )
            committed_generation = _nonnegative_int(
                admission.get("committed_generation"),
                "committed_generation",
            )
        except ValueError:
            await _release_trigger_entity_admission(
                client,
                run_id=record.run_id,
                session_entity_key=session_entity_key,
            )
            await release_retained_fence()
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.RETRY,
                error_code="invalid_admission_receipt",
            )
        try:
            if manager is not None:
                await _track_trigger_run_artifacts(
                    manager,
                    metadata=metadata,
                    record=record,
                    content=content,
                )
            durable_input = await _persist_trigger_run_input(
                metadata,
                record=record,
                committed_context_ref=committed_context_ref,
                committed_generation=committed_generation,
                content=content,
                retention=manager,
            )
        except ValueError:
            logger.exception(
                "durable trigger run construction was rejected: record_key=%s",
                record.record_key,
            )
            await _release_trigger_entity_admission(
                client,
                run_id=record.run_id,
                session_entity_key=session_entity_key,
            )
            await release_retained_fence()
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.TERMINAL,
                error_code="trigger_run_invalid",
            )
        except (DurableLoopContentError, DurableSkillProviderError):
            logger.exception(
                "durable trigger run content was unavailable: record_key=%s",
                record.record_key,
            )
            await _release_trigger_entity_admission(
                client,
                run_id=record.run_id,
                session_entity_key=session_entity_key,
            )
            await release_retained_fence()
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.RETRY,
                error_code="trigger_content_unavailable",
            )
        except DurableSkillCatalogAdmissionError:
            logger.exception(
                "durable trigger skill catalog was rejected: record_key=%s",
                record.record_key,
            )
            await _release_trigger_entity_admission(
                client,
                run_id=record.run_id,
                session_entity_key=session_entity_key,
            )
            await release_retained_fence()
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.TERMINAL,
                error_code="trigger_skill_catalog_invalid",
            )
        start = await _start_durable_run(
            client,
            durable_input,
            run_id=record.run_id,
        )
        if start is _DurableStartOutcome.UNCONFIRMED:
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.RETRY,
                error_code="run_start_unconfirmed",
            )
        return DurableTriggerAdmissionAttemptV1(
            outcome=DurableTriggerAttemptOutcome.ADMITTED
        )

    return admit


async def _release_trigger_entity_admission(
    client: df.DurableOrchestrationClient,
    *,
    run_id: str,
    session_entity_key: str,
) -> None:
    await _run_short_orchestration(
        client,
        DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
        {
            "operation": "release_admission",
            "run_id": run_id,
            "session_entity_key": session_entity_key,
        },
    )


async def _run_metadata(
    *,
    resolved: ResolvedAgent,
    settings: DurableLoopSettings,
    body: Mapping[str, object],
    owner_hash: str,
    session_id: str,
    request_id: str,
    run_id: str,
    now: datetime | None = None,
    include_human_input: bool = True,
) -> _RunMetadata:
    client_manager = get_client_manager()
    target = client_manager.resolve_inference_target(resolved.model)
    resolved_model = target.model
    if not resolved_model or not target.provider:
        raise ValueError("durable-loop inference target is incomplete")
    api_version = target.api_version or "responses-v1"
    sandbox_profile = _sandbox_profile(body, settings)
    fault_profile = _fault_profile(body, settings)
    orchestration_version = (
        DURABLE_LOOP_ORCHESTRATOR_V3_NAME
        if fault_profile is DurableFaultProfile.MODEL_APIM_429_ONCE
        else DURABLE_LOOP_ORCHESTRATOR_V2_NAME
    )
    base_policy_hash = canonical_hash(
        {
            "agent": resolved.slug,
            "auth": resolved.builtin_endpoints.http_auth.model_dump(mode="json"),
        }
    )
    runtime = get_durable_loop_activity_runtime()
    if not isinstance(runtime.tools, DurableToolCatalogPort):
        raise ValueError("durable tool catalog provider is unavailable")
    snapshot = await runtime.tools.freeze_catalog(
        policy_hash=base_policy_hash,
        sandbox_profile=sandbox_profile,
        include_human_input=include_human_input,
    )
    catalog = snapshot.catalog
    identity = create_run_identity(
        run_id=run_id,
        session_id=session_id,
        request_id=request_id,
        request_body=body,
        owner_hash=owner_hash,
        agent_slug=resolved.slug,
        agent_hash=canonical_hash(
            {
                "instructions": resolved.instructions,
                "model": resolved_model,
                "slug": resolved.slug,
            }
        ),
        catalog_hash=catalog.catalog_hash,
        deployment_hash=canonical_hash(
            {
                "api_version": api_version,
                "endpoint": (target.endpoint or "").rstrip("/"),
                "model": resolved_model,
                "provider": target.provider,
            }
        ),
        tool_package_hash=snapshot.package_hash,
        policy_hash=catalog.policy_hash,
        settings=settings,
        orchestration_version=orchestration_version,
        now=now,
        execution_binding_hash=canonical_hash(
            {
                "catalog_hash": catalog.catalog_hash,
                "fault_profile": fault_profile.value,
                "package_hash": snapshot.package_hash,
                "policy_hash": catalog.policy_hash,
                "sandbox_profile": sandbox_profile.value,
            }
        ),
    )
    plan = DurableLoopPlanDocumentV1(
        instructions=resolved.instructions or "",
        catalog=catalog,
        model_settings={"background": settings.background_model_enabled},
        maf_core_version="1.17.0",
        provider=target.provider,
        model=resolved_model,
        api_version=api_version,
        settings=asdict(settings),
        sandbox_profile=sandbox_profile,
        fault_profile=fault_profile,
        ui=_frozen_chat_options(body, settings),
    )
    return _RunMetadata(
        identity=identity,
        plan=plan,
        session_entity_key=canonical_hash(
            {"owner_hash": owner_hash, "session_id": session_id}
        ),
    )


async def _trigger_run_metadata(
    *,
    record: DurableTriggerAdmissionRecordV1,
    resolved: ResolvedAgent,
    settings: DurableLoopSettings,
    provider: DurableSkillProvider,
    content: DurableContentStore | None = None,
) -> _TriggerRunMetadata:
    runtime = get_durable_loop_activity_runtime()
    content_store = content or runtime.content
    prompt = (await content_store.get_bytes(record.prompt_ref)).decode("utf-8").strip()
    if not prompt:
        raise ValueError("durable trigger prompt is empty")
    trigger_args = resolved.trigger.args if resolved.trigger is not None else {}
    allow_human_input = trigger_args.get("allow_human_input", False)
    if not isinstance(allow_human_input, bool):
        raise ValueError("durable trigger allow_human_input must be a boolean")
    base = await _run_metadata(
        resolved=resolved,
        settings=settings,
        body={"prompt": prompt},
        owner_hash=record.owner_hash,
        session_id=record.session_id,
        request_id=record.stable_event_id_hash,
        run_id=record.run_id,
        now=record.staged_at,
        include_human_input=allow_human_input,
    )
    retention = DurableRetentionPolicyV1(
        event_result_seconds=settings.event_result_retention_seconds,
        receipt_seconds=settings.receipt_retention_seconds,
        skill_grace_seconds=settings.skill_grace_seconds,
        human_content_seconds=settings.human_content_retention_seconds,
        session_seconds=settings.session_retention_seconds,
        tombstone_seconds=settings.tombstone_retention_seconds,
        trigger_admission_deadline_seconds=(
            settings.trigger_admission_deadline_seconds
        ),
    )
    skill_admission = await admit_durable_skill_catalog(
        provider=provider,
        content=content_store,
        agent_slug=record.agent_slug,
        enabled_skill_ids=resolved.enabled_skills_names,
        retain_until=base.identity.absolute_deadline
        + timedelta(seconds=retention.skill_grace_seconds),
        tool_catalog=base.plan.catalog,
    )
    budget = DurableLoopBudgetV2(
        **base.identity.budget.model_dump(
            mode="python",
            exclude={"schema_version"},
        ),
        schema_version="2",
    )
    identity = DurableRunIdentityV2(
        **base.identity.model_dump(
            mode="python",
            exclude={
                "budget",
                "catalog_hash",
                "execution_binding_hash",
                "orchestration_version",
                "request_hash",
                "request_id_hash",
                "schema_version",
                "tool_package_hash",
            },
        ),
        schema_version="2",
        request_id_hash=record.stable_event_id_hash,
        request_hash=record.request_hash,
        catalog_hash=skill_admission.tool_catalog.catalog_hash,
        execution_binding_hash=canonical_hash(
            {
                "catalog_hash": skill_admission.tool_catalog.catalog_hash,
                "fault_profile": base.plan.fault_profile.value,
                "package_hash": skill_admission.tool_catalog.package_hash,
                "policy_hash": skill_admission.tool_catalog.policy_hash,
                "sandbox_profile": base.plan.sandbox_profile.value,
                "skill_catalog_hash": skill_admission.catalog_hash,
            }
        ),
        tool_package_hash=skill_admission.tool_catalog.package_hash,
        orchestration_version=DURABLE_LOOP_ORCHESTRATOR_V4_NAME,
        budget=budget,
        skill_catalog_hash=skill_admission.catalog_hash,
        access_namespace_hash=record.access_namespace_hash,
        retention_policy=retention,
    )
    response_schema_ref: ContentRefV1 | None = None
    response_schema_hash: str | None = None
    if resolved.response_schema is not None:
        response_schema_payload = canonical_json_bytes(resolved.response_schema)
        response_schema_hash = canonical_hash(resolved.response_schema)
        response_schema_ref = await content_store.put_bytes(
            kind="response-schema",
            payload=response_schema_payload,
            media_type="application/json",
            retention_class="run",
        )
    plan = DurableLoopPlanDocumentV2(
        **base.plan.model_dump(
            mode="python",
            exclude={"catalog", "schema_version"},
        ),
        schema_version="2",
        catalog=skill_admission.tool_catalog,
        skill_catalog_ref=skill_admission.catalog_ref,
        skill_catalog_hash=skill_admission.catalog_hash,
        initial_skill_metadata=skill_admission.initial_metadata,
        retention_policy=retention,
        allow_human_input=allow_human_input,
        response_schema_ref=response_schema_ref,
        response_schema_hash=response_schema_hash,
    )
    return _TriggerRunMetadata(
        identity=identity,
        plan=plan,
        session_entity_key=_session_entity_key(record.owner_hash, record.session_id),
    )


async def _persist_run_input(
    metadata: _RunMetadata,
    *,
    prompt: str,
    committed_context_ref: ContentRefV1 | None,
    committed_generation: int,
) -> DurableOrchestrationInputV1:
    runtime = get_durable_loop_activity_runtime()
    prior_messages: tuple[dict[str, object], ...] = ()
    prior_workspace_ref: ContentRefV1 | None = None
    if committed_context_ref is not None:
        previous = await get_protocol_model(
            runtime.content,
            committed_context_ref,
            DurableRunDocumentV1,
        )
        if (
            previous.plan.maf_core_version != metadata.plan.maf_core_version
            or previous.plan.provider != metadata.plan.provider
            or previous.plan.model != metadata.plan.model
            or previous.plan.api_version != metadata.plan.api_version
            or previous.checkpoint.identity.agent_hash
            != metadata.identity.agent_hash
            or previous.checkpoint.identity.policy_hash
            != metadata.identity.policy_hash
        ):
            raise ValueError(
                "committed session context is incompatible with the requested binding"
            )
        prior_messages = previous.checkpoint.working_context.bundle.messages
        prior_workspace_ref = previous.checkpoint.workspace_ref
    user_message: dict[str, object] = {
        "contents": [{"text": prompt, "type": "text"}],
        "role": "user",
    }
    messages = (*prior_messages, user_message)
    bundle = MAFMessageBundleV1.create(
        messages=messages,
        maf_core_version=metadata.plan.maf_core_version,
        provider=metadata.plan.provider,
        model=metadata.plan.model,
        api_version=metadata.plan.api_version,
    )
    working = WorkingContextV1(
        bundle=bundle,
        compaction_generation=0,
        source_audit_hash=bundle.bundle_hash,
        source_start=0,
        source_end=len(messages),
        estimated_tokens=max(1, len(canonical_json_bytes(messages)) // 4),
    )
    checkpoint = CheckpointStateV1(
        identity=metadata.identity,
        status=DurableLoopRunStatus.PENDING,
        audit_bundle=bundle,
        working_context=working,
        audit_head_hash=working.source_audit_hash,
        completed_model_steps=0,
        completed_tool_calls=0,
        human_wait_count=0,
        next_model_step=0,
        committed_session_generation=committed_generation,
        continue_as_new_generation=0,
        checkpoints_in_generation=0,
        workspace_ref=prior_workspace_ref,
    )
    document_ref = await put_protocol_model(
        runtime.content,
        kind="run-document",
        model=DurableRunDocumentV1(
            plan=metadata.plan,
            checkpoint=checkpoint,
        ),
    )
    return DurableOrchestrationInputV1(
        identity=metadata.identity,
        run_document_ref=document_ref,
        session_entity_key=metadata.session_entity_key,
        committed_session_generation=committed_generation,
        external_content_bytes=document_ref.byte_length,
        working_context_bytes=len(canonical_json_bytes(messages)),
        sandbox_profile=metadata.plan.sandbox_profile,
        fault_profile=metadata.plan.fault_profile,
    )


async def _track_trigger_run_artifacts(
    retention: DurableRetentionManager,
    *,
    metadata: _TriggerRunMetadata,
    record: DurableTriggerAdmissionRecordV1,
    content: DurableContentStore | None,
) -> None:
    content_store = content or get_durable_loop_activity_runtime().content
    identity = metadata.identity
    run_expiry = identity.absolute_deadline + timedelta(
        seconds=identity.retention_policy.receipt_seconds
    )
    skill_expiry = identity.absolute_deadline + timedelta(
        seconds=(
            identity.retention_policy.event_result_seconds
            + identity.retention_policy.skill_grace_seconds
        )
    )
    artifacts = [
        (
            "trigger-payload",
            record.payload_ref,
            "application/json",
            "run",
            run_expiry,
        ),
        (
            "trigger-prompt",
            record.prompt_ref,
            "text/plain; charset=utf-8",
            "run",
            run_expiry,
        ),
        (
            "skill-catalog",
            metadata.plan.skill_catalog_ref,
            "application/json",
            "skill",
            skill_expiry,
        ),
    ]
    if metadata.plan.response_schema_ref is not None:
        artifacts.append(
            (
                "response-schema",
                metadata.plan.response_schema_ref,
                "application/json",
                "run",
                run_expiry,
            )
        )
    for kind, reference, media_type, retention_class, expires_at in artifacts:
        await retention.tracked_put(
            run_id=identity.run_id,
            kind=kind,
            payload=await content_store.get_bytes(reference),
            media_type=media_type,
            retention_class=retention_class,
            expires_at=expires_at,
            now=identity.created_at,
        )


async def _persist_trigger_run_input(
    metadata: _TriggerRunMetadata,
    *,
    record: DurableTriggerAdmissionRecordV1,
    committed_context_ref: ContentRefV1 | None,
    committed_generation: int,
    content: DurableContentStore | None = None,
    retention: DurableRetentionManager | None = None,
) -> DurableOrchestrationInputV2:
    runtime = get_durable_loop_activity_runtime()
    content_store = content or runtime.content
    prior_messages: tuple[dict[str, object], ...] = ()
    prior_workspace_ref: ContentRefV1 | None = None
    if committed_context_ref is not None:
        raw = decode_json_object(
            await content_store.get_bytes(committed_context_ref)
        )
        if raw.get("schema_version") == "2":
            previous: DurableRunDocumentV1 | DurableRunDocumentV2 = (
                DurableRunDocumentV2.model_validate(raw)
            )
        else:
            previous = DurableRunDocumentV1.model_validate(raw)
        if (
            previous.plan.maf_core_version != metadata.plan.maf_core_version
            or previous.plan.provider != metadata.plan.provider
            or previous.plan.model != metadata.plan.model
            or previous.plan.api_version != metadata.plan.api_version
            or previous.checkpoint.identity.agent_hash
            != metadata.identity.agent_hash
            or previous.checkpoint.identity.policy_hash
            != metadata.identity.policy_hash
        ):
            raise ValueError(
                "committed session context is incompatible with the requested binding"
            )
        prior_messages = previous.checkpoint.working_context.bundle.messages
        prior_workspace_ref = previous.checkpoint.workspace_ref
    prompt = (await content_store.get_bytes(record.prompt_ref)).decode("utf-8").strip()
    if not prompt:
        raise ValueError("durable trigger prompt is empty")
    user_message: dict[str, object] = {
        "contents": [{"text": prompt, "type": "text"}],
        "role": "user",
    }
    messages = (*prior_messages, user_message)
    bundle = MAFMessageBundleV1.create(
        messages=messages,
        maf_core_version=metadata.plan.maf_core_version,
        provider=metadata.plan.provider,
        model=metadata.plan.model,
        api_version=metadata.plan.api_version,
    )
    working = WorkingContextV1(
        bundle=bundle,
        compaction_generation=0,
        source_audit_hash=bundle.bundle_hash,
        source_start=0,
        source_end=len(messages),
        estimated_tokens=max(1, len(canonical_json_bytes(messages)) // 4),
    )
    checkpoint = CheckpointStateV2(
        identity=metadata.identity,
        status=DurableLoopRunStatus.PENDING,
        audit_bundle=bundle,
        working_context=working,
        audit_head_hash=working.source_audit_hash,
        completed_model_steps=0,
        completed_tool_calls=0,
        human_wait_count=0,
        next_model_step=0,
        committed_session_generation=committed_generation,
        continue_as_new_generation=0,
        checkpoints_in_generation=0,
        workspace_ref=prior_workspace_ref,
        skill_catalog_ref=metadata.plan.skill_catalog_ref,
        skill_catalog_hash=metadata.plan.skill_catalog_hash,
    )
    document = DurableRunDocumentV2(
        plan=metadata.plan,
        checkpoint=checkpoint,
    )
    if retention is None:
        document_ref = await put_protocol_model(
            content_store,
            kind="run-document",
            model=document,
        )
    else:
        document_ref = await retention.tracked_put(
            run_id=metadata.identity.run_id,
            kind="run-document",
            payload=canonical_json_bytes(document),
            media_type="application/json",
            retention_class="run",
            expires_at=metadata.identity.absolute_deadline
            + timedelta(seconds=metadata.identity.retention_policy.receipt_seconds),
            now=metadata.identity.created_at,
        )
    external_content_bytes = (
        document_ref.byte_length
        + metadata.plan.skill_catalog_ref.byte_length
        + record.payload_ref.byte_length
        + record.prompt_ref.byte_length
        + (
            metadata.plan.response_schema_ref.byte_length
            if metadata.plan.response_schema_ref is not None
            else 0
        )
    )
    return DurableOrchestrationInputV2(
        identity=metadata.identity,
        run_document_ref=document_ref,
        session_entity_key=metadata.session_entity_key,
        committed_session_generation=committed_generation,
        external_content_bytes=external_content_bytes,
        working_context_bytes=len(canonical_json_bytes(messages)),
        sandbox_profile=metadata.plan.sandbox_profile,
        fault_profile=metadata.plan.fault_profile,
        skill_catalog_ref=metadata.plan.skill_catalog_ref,
        skill_catalog_hash=metadata.plan.skill_catalog_hash,
    )


async def _recover_or_initialize_chat_run(
    *,
    admission: Mapping[str, object],
    client: df.DurableOrchestrationClient,
    resolved: ResolvedAgent,
    settings: DurableLoopSettings,
    chat_settings: DurableChatSettings | None,
    payload: Mapping[str, object],
    owner_hash: str,
    session_id: str,
    request_id: str,
    run_id: str,
) -> DurableOrchestrationInputV1 | Response:
    """Return the winner or reclaim only its expired, unstarted UI admission."""
    if chat_settings is None or not chat_settings.enabled:
        return _json_response({"error": "durable-chat UI is not enabled"}, status_code=400)
    journal = get_durable_chat_journal()
    try:
        initialization = await journal.load_run_initialization(run_id=run_id)
        if initialization is None:
            admitted_at = _receipt_datetime(admission.get("admitted_at"), "admitted_at")
            expires_at = _receipt_datetime(admission.get("expires_at"), "expires_at")
            if datetime.now(UTC) >= expires_at:
                await _reclaim_expired_chat_admission(
                    client=client,
                    owner_hash=owner_hash,
                    payload=payload,
                    request_id=request_id,
                    run_id=run_id,
                    session_id=session_id,
                )
                return _expired_chat_initialization_response(run_id)
            committed_generation = _nonnegative_int(
                admission.get("committed_generation"),
                "committed_generation",
            )
            metadata = await _run_metadata(
                resolved=resolved,
                settings=settings,
                body=payload,
                owner_hash=owner_hash,
                session_id=session_id,
                request_id=request_id,
                run_id=run_id,
                now=admitted_at,
            )
            metadata = replace(
                metadata,
                identity=metadata.identity.model_copy(
                    update={
                        "active_deadline": min(
                            metadata.identity.active_deadline,
                            expires_at,
                        ),
                        "absolute_deadline": expires_at,
                    }
                ),
            )
            persisted = await _persist_chat_run_input(
                metadata,
                prompt=str(payload["prompt"]).strip(),
                committed_context_ref=_optional_content_ref(
                    admission.get("committed_context_ref")
                ),
                committed_generation=committed_generation,
            )
            ui = _require_chat_options(metadata.plan.ui)
            initialization = await journal.create_run_initialization_once(
                initialization=DurableChatRunInitializationV1(
                    run_id=run_id,
                    session_id=session_id,
                    owner_hash=owner_hash,
                    request_id_hash=metadata.identity.request_id_hash,
                    request_hash=metadata.identity.request_hash,
                    plan_ref=persisted.plan_ref,
                    input_ref=persisted.input_ref,
                    expires_at=expires_at,
                    committed_generation=committed_generation,
                    ui=ui,
                    created_at=admitted_at,
                    diagnostics=chat_settings.freeze_diagnostics(
                        request_started_at=admitted_at,
                        request_ends_at=expires_at,
                    ),
                )
            )
        _validate_chat_initialization_binding(
            initialization,
            owner_hash=owner_hash,
            session_id=session_id,
            request_id=request_id,
            payload=payload,
        )
        if datetime.now(UTC) >= initialization.expires_at.astimezone(UTC):
            await _reclaim_expired_chat_admission(
                client=client,
                owner_hash=owner_hash,
                payload=payload,
                request_id=request_id,
                run_id=run_id,
                session_id=session_id,
            )
            return _expired_chat_initialization_response(run_id)
        return await _load_initialized_chat_input(initialization)
    except (DurableChatInitializationError, ValueError):
        logger.warning(
            "durable-chat initialization failed: run_id=%s",
            run_id,
        )
    except Exception as exc:
        logger.warning(
            "durable-chat initialization storage failed: run_id=%s error_type=%s",
            run_id,
            type(exc).__name__,
        )
    return _json_response(
        {
            "error": "chat_initialization_failed",
            "possibly_committed": True,
            "run_id": run_id,
        },
        status_code=503,
    )


async def _reclaim_expired_chat_admission(
    *,
    client: df.DurableOrchestrationClient,
    owner_hash: str,
    payload: Mapping[str, object],
    request_id: str,
    run_id: str,
    session_id: str,
) -> None:
    receipt = await _run_short_orchestration(
        client,
        DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
        {
            "now": datetime.now(UTC).isoformat(),
            "operation": "reclaim_expired_chat_admission",
            "request_hash": canonical_hash(payload),
            "request_id_hash": canonical_hash({"request_id": request_id}),
            "run_id": run_id,
            "session_entity_key": _session_entity_key(owner_hash, session_id),
        },
    )
    if receipt.get("disposition") not in {"reclaimed", "stale"}:
        raise DurableChatInitializationError(
            "durable-chat expired admission could not be reclaimed"
        )


def _expired_chat_initialization_response(run_id: str) -> Response:
    return _json_response(
        {
            "error": "chat_initialization_expired",
            "possibly_committed": True,
            "run_id": run_id,
        },
        status_code=410,
    )


def _validate_chat_initialization_binding(
    initialization: DurableChatRunInitializationV1,
    *,
    owner_hash: str,
    session_id: str,
    request_id: str,
    payload: Mapping[str, object],
) -> None:
    if (
        initialization.owner_hash != owner_hash
        or initialization.session_id != session_id
        or initialization.request_id_hash != canonical_hash({"request_id": request_id})
        or initialization.request_hash != canonical_hash(payload)
    ):
        raise DurableChatInitializationError(
            "durable-chat initialization does not match the admitted request"
        )


@dataclass(frozen=True, slots=True)
class _PersistedChatRun:
    durable_input: DurableOrchestrationInputV1
    plan_ref: ContentRefV1
    input_ref: ContentRefV1


async def _persist_chat_run_input(
    metadata: _RunMetadata,
    *,
    prompt: str,
    committed_context_ref: ContentRefV1 | None,
    committed_generation: int,
) -> _PersistedChatRun:
    """Persist independent plan and input references before chat initialization."""
    durable_input = await _persist_run_input(
        metadata,
        prompt=prompt,
        committed_context_ref=committed_context_ref,
        committed_generation=committed_generation,
    )
    runtime = get_durable_loop_activity_runtime()
    plan_ref = await put_protocol_model(
        runtime.content,
        kind="durable-chat-plan",
        model=metadata.plan,
    )
    input_ref = await put_protocol_model(
        runtime.content,
        kind="durable-chat-input",
        model=durable_input,
    )
    return _PersistedChatRun(
        durable_input=durable_input,
        plan_ref=plan_ref,
        input_ref=input_ref,
    )


async def _load_initialized_chat_input(
    initialization: DurableChatRunInitializationV1,
) -> DurableOrchestrationInputV1:
    """Rehydrate only the winner's exact input and plan references."""
    runtime = get_durable_loop_activity_runtime()
    durable_input = await get_protocol_model(
        runtime.content,
        initialization.input_ref,
        DurableOrchestrationInputV1,
    )
    plan = await get_protocol_model(
        runtime.content,
        initialization.plan_ref,
        DurableLoopPlanDocumentV1,
    )
    document = await get_protocol_model(
        runtime.content,
        durable_input.run_document_ref,
        DurableRunDocumentV1,
    )
    if (
        durable_input.identity.run_id != initialization.run_id
        or durable_input.identity.session_id != initialization.session_id
        or durable_input.identity.owner_hash != initialization.owner_hash
        or durable_input.identity.request_hash != initialization.request_hash
        or durable_input.identity.request_id_hash != initialization.request_id_hash
        or durable_input.committed_session_generation
        != initialization.committed_generation
        or plan != document.plan
        or plan.ui != initialization.ui
    ):
        raise DurableChatInitializationError(
            "durable-chat initialization references do not match"
        )
    return durable_input


async def _authorized_status(  # noqa: PLR0912
    req: Request,
    client: Any,
    auth: EndpointAuthConfig,
) -> _AuthorizedRun | Response:
    owner = _authorized_owner(req, auth)
    if isinstance(owner, Response):
        return owner
    run_id = _path_parameter(req, "run_id")
    owner_hash = _owner_hash(owner)
    runtime = get_durable_loop_activity_runtime()
    retained: DurableRunAccessRecord | None = None
    if runtime.retention is not None:
        try:
            retained = await runtime.retention.get_run_access_record(
                run_id,
                now=datetime.now(UTC),
            )
        except (
            DurableReceiptStoreError,
            DurableRetentionError,
            TypeError,
            ValueError,
        ):
            logger.exception("durable retention access lookup failed")
            return _json_response(
                {"error": "retention_state_unavailable"},
                status_code=503,
            )
        if retained is not None:
            from .durable_trigger_admission import durable_access_namespace_hash

            if (
                retained.owner_hash != owner_hash
                or retained.access_namespace_hash
                != durable_access_namespace_hash(auth.mode)
            ):
                logger.warning("durable retained owner/access binding did not match")
                return _json_response({"error": "run_not_found"}, status_code=404)
            if retained.visibility is DurableRunVisibility.EXPIRED:
                return _AuthorizedRun(
                    status=None,
                    durable_input=None,
                    retained=retained,
                )
    status = await client.get_status(run_id, show_input=True)
    if status is None:
        if (
            retained is not None
            and retained.visibility is DurableRunVisibility.TERMINAL
            and runtime.retention is not None
        ):
            try:
                projection = await runtime.retention.read_terminal_projection(
                    run_id,
                    now=datetime.now(UTC),
                )
            except (
                DurableReceiptStoreError,
                DurableRetentionError,
                TypeError,
                ValueError,
            ):
                logger.exception("durable retained terminal projection read failed")
                return _json_response(
                    {"error": "retention_state_unavailable"},
                    status_code=503,
                )
            return _AuthorizedRun(
                status=None,
                durable_input=None,
                retained=retained,
                terminal_projection=projection,
            )
        logger.warning("durable-loop status lookup returned no instance")
        return _json_response({"error": "run_not_found"}, status_code=404)
    raw_input = status.input_
    try:
        durable_input = _durable_status_input(raw_input)
    except Exception as exc:
        logger.warning(
            "durable-loop status input was rejected: input_type=%s error_type=%s",
            type(raw_input).__name__,
            type(exc).__name__,
        )
        return _json_response({"error": "run_not_found"}, status_code=404)
    if durable_input.identity.owner_hash != owner_hash:
        logger.warning("durable-loop status owner binding did not match")
        return _json_response({"error": "run_not_found"}, status_code=404)
    if is_retention_indexed_identity(durable_input.identity):
        if retained is None:
            logger.error("V4 durable run is missing its retained access root")
            return _json_response(
                {"error": "retention_state_unavailable"},
                status_code=503,
            )
        if retained.header is None or (
            retained.header.run_id != durable_input.identity.run_id
            or retained.header.session_id != durable_input.identity.session_id
        ):
            return _json_response(
                {"error": "retention_state_unavailable"},
                status_code=503,
            )
    return _AuthorizedRun(
        status=status,
        durable_input=durable_input,
        retained=retained,
    )


def _durable_status_input(value: object) -> DurableOrchestrationInputV1:
    """Parse the mapping or JSON-string input returned by the Durable client."""
    raw = value.encode("utf-8") if isinstance(value, str) else canonical_json_bytes(value)
    decoded = decode_json_object(raw)
    model = (
        DurableOrchestrationInputV2
        if decoded.get("schema_version") == "2"
        else DurableOrchestrationInputV1
    )
    return model.model_validate_json(canonical_json_bytes(decoded))


async def _run_short_orchestration(
    client: Any,
    name: str,
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    instance_id = f"{name}-{uuid.uuid4().hex}"
    await client.start_new(name, instance_id=instance_id, client_input=dict(payload))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SHORT_WAIT_SECONDS
    while loop.time() < deadline:
        status = await client.get_status(instance_id)
        if status is not None and _runtime_status_name(status) == "Completed":
            return status.output if isinstance(status.output, Mapping) else {"disposition": "invalid"}
        if status is not None and _runtime_status_name(status) in {"Failed", "Terminated"}:
            return {"disposition": "failed"}
        await asyncio.sleep(_SHORT_POLL_SECONDS)
    return {"disposition": "timeout"}


def _validate_answer(request: HumanInputRequestV1, answer: object) -> None:
    assert_json_value(answer)
    if len(canonical_json_bytes(answer)) > _MAX_HUMAN_ANSWER_BYTES:
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


def _start_payload(
    body: object,
    *,
    chat_enabled: bool = False,
) -> Mapping[str, object]:
    if not isinstance(body, Mapping):
        raise ValueError("request body must be a JSON object")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ValueError("prompt exceeds the byte limit")
    if "ui" not in body:
        return body
    if not chat_enabled:
        raise ValueError("durable-chat UI is not enabled")
    try:
        ui = DurableChatStartOptionsV1.model_validate(body["ui"])
    except ValueError as exc:
        raise ValueError("ui is invalid") from exc
    normalized = dict(body)
    normalized["ui"] = ui.model_dump(mode="json")
    return normalized


def _frozen_chat_options(
    body: Mapping[str, object],
    settings: DurableLoopSettings,
) -> DurableChatRunOptionsV1 | None:
    if "ui" not in body:
        return None
    ui = DurableChatStartOptionsV1.model_validate(body["ui"])
    model_mode = (
        DurableChatModelMode.BACKGROUND
        if settings.background_model_enabled
        else DurableChatModelMode.FOREGROUND
    )
    return ui.freeze(model_mode=model_mode)


def _session_entity_key(owner_hash: str, session_id: str) -> str:
    return canonical_hash({"owner_hash": owner_hash, "session_id": session_id})


def _receipt_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field_name} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} is invalid")
    return parsed.astimezone(UTC)


def _require_chat_options(
    options: DurableChatRunOptionsV1 | None,
) -> DurableChatRunOptionsV1:
    if options is None:
        raise ValueError("durable-chat initialization has no UI options")
    return options


def _sandbox_profile(
    body: Mapping[str, object],
    settings: DurableLoopSettings,
) -> SandboxExecutionProfile:
    raw = body.get("sandbox_profile", SandboxExecutionProfile.PER_CALL.value)
    if not isinstance(raw, str):
        raise ValueError("sandbox_profile is invalid")
    try:
        profile = SandboxExecutionProfile(raw)
    except ValueError:
        raise ValueError(
            "sandbox_profile must be per_call or retained_session"
        ) from None
    if (
        profile is SandboxExecutionProfile.RETAINED_SESSION
        and not settings.retained_sandbox_enabled
    ):
        raise ValueError(
            "retained_session requires the private retained-sandbox gate"
        )
    return profile


def _fault_profile(
    body: Mapping[str, object],
    settings: DurableLoopSettings,
) -> DurableFaultProfile:
    raw = body.get("fault_profile", DurableFaultProfile.NONE.value)
    if not isinstance(raw, str):
        raise ValueError("fault_profile is invalid")
    try:
        profile = DurableFaultProfile(raw)
    except ValueError:
        raise ValueError("fault_profile is not a supported fixed profile") from None
    if profile is not DurableFaultProfile.NONE and not settings.fault_injection_enabled:
        raise ValueError(
            "fault_profile requires the private fault-injection gate"
        )
    return profile


def _authorized_owner(req: Request, auth: EndpointAuthConfig) -> _DurableOwner | Response:
    if auth.mode == "anonymous":
        return _AnonymousDurableOwner()
    owner = resolve_owner_principal(req.headers.get, auth)
    if isinstance(owner, AuthError):
        return _json_response({"error": owner.message}, status_code=owner.status_code)
    return owner


def _same_origin_mutation_failure(req: Request) -> Response | None:
    """Reject browser-origin mutations that do not target this exact origin."""
    origin = req.headers.get("Origin")
    if origin is None:
        return None
    # The Functions streaming proxy overwrites forwarded headers and replaces Host.
    host = req.headers.get("X-Forwarded-Host", req.headers.get("Host"))
    if not host or "," in host or any(character.isspace() for character in host):
        return _json_response({"error": "cross_origin_request_rejected"}, status_code=403)
    forwarded = req.headers.get("X-Forwarded-Proto")
    expected_scheme = (
        forwarded.strip().casefold()
        if forwarded
        else _request_scheme(req)
    )
    try:
        parsed = urlsplit(origin)
        expected_host = urlsplit(f"//{host}")
        origin_port = parsed.port
        expected_port = expected_host.port
    except ValueError:
        return _json_response({"error": "cross_origin_request_rejected"}, status_code=403)
    if (
        expected_scheme not in {"http", "https"}
        or parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or expected_host.hostname is None
        or expected_host.username is not None
        or expected_host.password is not None
        or expected_host.path
        or expected_host.query
        or expected_host.fragment
        or parsed.hostname is None
        or parsed.hostname.casefold() != expected_host.hostname.casefold()
        or _effective_port(parsed.scheme, origin_port)
        != _effective_port(expected_scheme, expected_port)
    ):
        return _json_response({"error": "cross_origin_request_rejected"}, status_code=403)
    if parsed.scheme.casefold() != expected_scheme:
        return _json_response({"error": "cross_origin_request_rejected"}, status_code=403)
    return None


def _request_scheme(req: Request) -> str:
    url = getattr(req, "url", None)
    scheme = getattr(url, "scheme", None)
    if isinstance(scheme, str) and scheme.casefold() in {"http", "https"}:
        return scheme.casefold()
    return "https"


def _effective_port(scheme: str, port: int | None) -> int:
    if port is not None:
        return port
    return 443 if scheme.casefold() == "https" else 80


def _owner_hash(owner: _DurableOwner) -> str:
    if isinstance(owner, _AnonymousDurableOwner):
        return canonical_hash({"kind": "anonymous_app"})
    if isinstance(owner, FunctionAppPrincipal):
        return canonical_hash({"kind": "function_app"})
    if isinstance(owner, EntraPrincipal):
        return canonical_hash(
            {
                "kind": "entra_user",
                "object_id": owner.object_id,
                "tenant_id": owner.tenant_id,
            }
        )
    raise PermissionError("unsupported durable-loop owner")


def _session_id(req: Request, body: Mapping[str, object]) -> str:
    value = req.headers.get("x-ms-session-id") or body.get("session_id")
    if value is None:
        return uuid.uuid4().hex
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("session_id is invalid")
    return value


def _request_id(req: Request, body: Mapping[str, object]) -> str:
    value = body.get("request_id") or req.headers.get("Idempotency-Key")
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("request_id or Idempotency-Key is required")
    return value


def _run_id() -> str:
    return f"run-{uuid.uuid4().hex}"


def _path_parameter(req: Request, name: str) -> str:
    value = (getattr(req, "path_params", {}) or {}).get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value


def _runtime_status_name(status: Any) -> str:
    return getattr(status.runtime_status, "name", str(status.runtime_status))


def _http_status_code(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _status_projection(status: Any) -> dict[str, object]:
    custom = status.custom_status if isinstance(status.custom_status, Mapping) else {}
    output = status.output if isinstance(status.output, Mapping) else {}
    runtime_status = _runtime_status_name(status)
    projection: dict[str, object] = {
        "phase": _projected_phase(output, custom, runtime_status),
        "run_id": status.instance_id,
        "status": _projected_run_status(output, custom, runtime_status),
    }
    error = output.get("error")
    if isinstance(error, str):
        projection["error"] = error
    disposition = output.get("disposition")
    if isinstance(disposition, str):
        projection["disposition"] = disposition
    if isinstance(output.get("possibly_committed"), bool):
        projection["possibly_committed"] = output["possibly_committed"]
    for name in (
        "cost_microunits",
        "external_content_bytes",
        "human_waits",
        "input_tokens",
        "model_steps",
        "output_tokens",
        "parked_seconds",
        "reasoning_tokens",
        "step_index",
        "tool_calls",
    ):
        value = output.get(name, custom.get(name))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            projection[name] = value
    return projection


def _event_cursor(req: Request) -> int:
    header = req.headers.get("Last-Event-ID")
    query_values = getattr(req, "query_params", None)
    query = query_values.get("after_sequence") if query_values is not None else None
    if query is not None and not isinstance(query, str):
        raise ValueError("event cursor is invalid")
    if header is not None and query is not None and header != query:
        raise ValueError("event cursor sources conflict")
    raw = header if header is not None else query
    if raw is None:
        return 0
    if _EVENT_CURSOR_PATTERN.fullmatch(raw) is None:
        raise ValueError("event cursor is invalid")
    return int(raw)


async def _reconcile_public_terminal_status(
    *,
    status: Any,
    run_id: str,
    journal: Any,
) -> None:
    projection = _status_projection(status)
    raw_status = projection.get("status")
    if not isinstance(raw_status, str):
        return
    try:
        run_status = DurableLoopRunStatus(raw_status)
    except ValueError:
        return
    if run_status not in {
        DurableLoopRunStatus.COMPLETED,
        DurableLoopRunStatus.FAILED,
        DurableLoopRunStatus.CANCELLED,
    }:
        return
    output = status.output if isinstance(status.output, Mapping) else {}
    result_available = (
        run_status is DurableLoopRunStatus.COMPLETED
        and output.get("status") == DurableLoopRunStatus.COMPLETED.value
    )
    if run_status is DurableLoopRunStatus.COMPLETED and not result_available:
        return
    raw_error = projection.get("error")
    error_code = (
        raw_error
        if isinstance(raw_error, str)
        and re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", raw_error) is not None
        else None
    )
    await reconcile_durable_run_terminal(
        run_id=run_id,
        status=run_status,
        result_available=result_available,
        error_code=error_code,
        possibly_committed=projection.get("possibly_committed") is True,
        journal=journal,
    )


async def _stream_public_events(
    *,
    client: Any,
    initial_page: DurableRunObservationReplayV2,
    journal: Any,
    run_id: str,
) -> AsyncIterator[str]:
    page = initial_page
    cursor = page.requested_after_sequence
    deadline = time.monotonic() + _EVENT_LEASE_SECONDS
    next_heartbeat = time.monotonic() + _EVENT_HEARTBEAT_SECONDS
    while True:
        emitted = False
        if page.snapshot is not None:
            emitted = True
            cursor = page.snapshot.sequence
            yield render_public_sse_frame(page.snapshot)
        for frame in page.events:
            emitted = True
            cursor = frame.sequence
            yield render_public_sse_frame(frame)
        if _public_page_is_terminal(page):
            return
        now = time.monotonic()
        if now >= deadline:
            yield ": lease-ended\n\n"
            return
        if not emitted and now >= next_heartbeat:
            next_heartbeat = now + _EVENT_HEARTBEAT_SECONDS
            yield ": heartbeat\n\n"
        await asyncio.sleep(min(_EVENT_POLL_SECONDS, max(0.001, deadline - now)))
        try:
            status = await client.get_status(run_id, show_input=False)
            if status is not None:
                await _reconcile_public_terminal_status(
                    status=status,
                    run_id=run_id,
                    journal=journal,
                )
            page = await replay_durable_run_observations(
                run_id=run_id,
                after_sequence=cursor,
                journal=journal,
            )
        except (DurableRunObservationError, ValueError):
            yield ": observation-unavailable\n\n"
            return


def _public_page_is_terminal(page: DurableRunObservationReplayV2) -> bool:
    if page.snapshot is not None and page.snapshot.projection.terminal is not None:
        return True
    return any(
        event.type
        in {
            DurablePublicEventType.RUN_COMPLETED,
            DurablePublicEventType.RUN_FAILED,
            DurablePublicEventType.RUN_CANCELLED,
        }
        for event in page.events
    )


def _projected_run_status(
    output: Mapping[str, object],
    custom: Mapping[str, object],
    runtime_status: str,
) -> str:
    projected = output.get("status")
    if isinstance(projected, str):
        return projected
    if custom.get("phase") == "human_wait":
        return DurableLoopRunStatus.WAITING.value
    if runtime_status == "Pending":
        return DurableLoopRunStatus.PENDING.value
    if runtime_status == "Completed":
        return DurableLoopRunStatus.COMPLETED.value
    if runtime_status == "Failed":
        return DurableLoopRunStatus.FAILED.value
    if runtime_status in {"Canceled", "Terminated"}:
        return DurableLoopRunStatus.CANCELLED.value
    return DurableLoopRunStatus.RUNNING.value


def _projected_phase(
    output: Mapping[str, object],
    custom: Mapping[str, object],
    runtime_status: str,
) -> str:
    phase = output.get("phase") or custom.get("phase")
    if isinstance(phase, str):
        return phase
    if runtime_status == "Completed":
        return "completed"
    if runtime_status in {"Canceled", "Terminated"}:
        return "cancellation"
    if runtime_status == "Failed":
        return "run"
    return "durable"


def _optional_content_ref(value: object) -> ContentRefV1 | None:
    if value is None:
        return None
    return ContentRefV1.model_validate(value)


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _durable_route_base() -> str:
    return durable_chat_route(f"/{_ROUTE_BASE}")


def _accepted_response(
    run_id: str,
    session_id: str,
    *,
    chat_ui: bool = False,
    possibly_committed: bool = False,
    status: DurableLoopRunStatus = DurableLoopRunStatus.PENDING,
) -> Response:
    base = _durable_route_base()
    body: dict[str, object] = {
        "cancel_url": f"{base}/{run_id}/cancel",
        "events_url": f"{base}/{run_id}/events",
        "result_url": f"{base}/{run_id}/result",
        "run_id": run_id,
        "session_id": session_id,
        "status": status.value,
        "status_url": f"{base}/{run_id}",
    }
    if possibly_committed:
        body["possibly_committed"] = True
    headers = {"x-ms-session-id": session_id}
    if chat_ui:
        headers["Cache-Control"] = "no-store"
        headers["Referrer-Policy"] = "no-referrer"
        headers["X-Content-Type-Options"] = "nosniff"
    return _json_response(
        body,
        status_code=202,
        headers=headers,
    )


def _json_response(
    body: Mapping[str, object],
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> Response:
    response_body = dict(body)
    error = response_body.get("error")
    if isinstance(error, str):
        response_body.setdefault("schema_version", "1")
        response_body.setdefault("status", status_code)
        response_body.setdefault(
            "code",
            error
            if re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", error) is not None
            else "request_failed",
        )
    response_headers = {
        name: value
        for name, value in (headers or {}).items()
        if name.casefold() not in _DURABLE_DATA_HEADER_NAMES
    }
    response_headers.update(_DURABLE_DATA_HEADERS)
    return Response(
        content=json.dumps(response_body, ensure_ascii=True, separators=(",", ":")),
        status_code=status_code,
        media_type="application/json",
        headers=response_headers,
    )
