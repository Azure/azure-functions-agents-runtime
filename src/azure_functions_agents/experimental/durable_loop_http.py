"""Authenticated refs-only HTTP routes for private durable agent runs."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

import azure.durable_functions as df
import azure.functions as func
from azurefunctions.extensions.http.fastapi import Request, Response

from .._logger import logger
from ..client_manager import get_client_manager
from ..config import EndpointAuthConfig, ResolvedAgent
from ..registration._auth import (
    AuthError,
    resolve_endpoint_auth_level,
    resolve_owner_principal,
)
from ..session_state import EntraPrincipal, FunctionAppPrincipal, OwnerPrincipal
from ..strict_json import assert_json_value, canonical_json_bytes
from .durable_loop import create_run_identity
from .durable_loop_activities import get_protocol_model, put_protocol_model
from .durable_loop_config import DurableLoopSettings
from .durable_loop_protocol import (
    CheckpointStateV1,
    ContentRefV1,
    DurableFaultProfile,
    DurableLoopPlanDocumentV1,
    DurableLoopRunStatus,
    DurableOrchestrationInputV1,
    DurableRunDocumentV1,
    DurableRunIdentityV1,
    HumanInputRequestV1,
    HumanInputResponseV1,
    HumanResponseDisposition,
    MAFMessageBundleV1,
    SandboxExecutionProfile,
    WorkingContextV1,
    canonical_hash,
    validate_human_response_value,
)
from .durable_loop_registration import (
    DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
    DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME,
    DURABLE_LOOP_CANCEL_EVENT_NAME,
    DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
    DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME,
    DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME,
    DURABLE_LOOP_ORCHESTRATOR_NAME,
    configure_durable_loop_execution_binding,
    get_durable_loop_activity_runtime,
)
from .durable_loop_tools import DurableToolCatalogPort

_ROUTE_BASE = "experimental/durable-agent-runs"
_SHORT_WAIT_SECONDS = 5.0
_SHORT_POLL_SECONDS = 0.05
_MAX_PROMPT_BYTES = 256 * 1024
_MAX_HUMAN_ANSWER_BYTES = 64 * 1024


def register_durable_loop_http_routes(  # noqa: PLR0915
    app: func.FunctionApp,
    *,
    resolved: ResolvedAgent,
    settings: DurableLoopSettings,
) -> None:
    """Register private start/status/result/cancel/human-input routes."""
    configure_durable_loop_execution_binding(
        enabled_mcp_names=resolved.enabled_mcp_names,
        local_tools_enabled=not resolved.tools_disabled,
    )
    auth = resolved.builtin_endpoints.http_auth
    auth_level = resolve_endpoint_auth_level(auth)

    async def start_run(  # noqa: PLR0912
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        owner = _authorized_owner(req, auth)
        if isinstance(owner, Response):
            return owner
        try:
            body = await req.json()
            payload = _start_payload(body)
            session_id = _session_id(req, payload)
            request_id = _request_id(req, payload)
            owner_hash = _owner_hash(owner)
            run_id = _run_id()
            metadata = await _run_metadata(
                resolved=resolved,
                settings=settings,
                body=payload,
                owner_hash=owner_hash,
                session_id=session_id,
                request_id=request_id,
                run_id=run_id,
            )
        except ValueError as exc:
            return _json_response({"error": str(exc)}, status_code=400)

        admission = await _run_short_orchestration(
            client,
            DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
            {
                "expires_at": metadata.identity.absolute_deadline.isoformat(),
                "now": metadata.identity.created_at.isoformat(),
                "request_hash": metadata.identity.request_hash,
                "request_id_hash": metadata.identity.request_id_hash,
                "run_id": run_id,
                "session_entity_key": metadata.session_entity_key,
            },
        )
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
                return _accepted_response(existing_run_id, session_id)
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
            if lifecycle == "running":
                return _json_response(
                    {
                        "possibly_committed": True,
                        "run_id": existing_run_id,
                        "session_id": session_id,
                        "status": DurableLoopRunStatus.RUNNING.value,
                        "status_url": f"/api/{_ROUTE_BASE}/{existing_run_id}",
                    },
                    status_code=202,
                )
            run_id = existing_run_id
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
            return _json_response({"error": "content_persistence_failed"}, status_code=503)
        try:
            await client.start_new(
                DURABLE_LOOP_ORCHESTRATOR_NAME,
                instance_id=run_id,
                client_input=durable_input.model_dump(mode="json"),
            )
        except Exception:
            logger.exception("durable-loop orchestration start acknowledgement was lost")
            if await client.get_status(run_id) is not None:
                return _accepted_response(run_id, session_id)
            return _json_response(
                {
                    "error": "run_start_acknowledgement_lost",
                    "possibly_committed": True,
                    "run_id": run_id,
                },
                status_code=202,
            )
        return _accepted_response(run_id, session_id)

    async def get_status(
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return authorized
        status, durable_input = authorized
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
                        f"/api/{_ROUTE_BASE}/{status.instance_id}/"
                        f"input/{request.request_id}"
                    ),
                    "expires_at": request.expires_at.isoformat(),
                    "request_id": request.request_id,
                    "respond_url": (
                        f"/api/{_ROUTE_BASE}/{status.instance_id}/"
                        f"input/{request.request_id}"
                    ),
                    "schema_present": request.response_schema is not None,
                }
        body["session_id"] = durable_input.identity.session_id
        return _json_response(body)

    async def get_result(
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return authorized
        status, durable_input = authorized
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

    async def cancel_run(
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return authorized
        status, durable_input = authorized
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
        status, durable_input = authorized
        custom = status.custom_status
        if not isinstance(custom, Mapping) or custom.get("phase") != "human_wait":
            return _json_response({"error": "human_input_not_pending"}, status_code=410)
        request_id = _path_parameter(req, "request_id")
        request_ref = _optional_content_ref(custom.get("request_ref"))
        if request_ref is None:
            return _json_response({"error": "human_input_not_found"}, status_code=404)
        runtime = get_durable_loop_activity_runtime()
        human = await get_protocol_model(
            runtime.content,
            request_ref,
            HumanInputRequestV1,
        )
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
            answer_ref = await runtime.content.put_bytes(
                kind="human-answer",
                payload=canonical_json_bytes(body["answer"]),
                media_type="application/json",
                retention_class="run",
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
        status, durable_input = authorized
        custom = status.custom_status
        if not isinstance(custom, Mapping) or custom.get("phase") != "human_wait":
            return _json_response({"error": "human_input_gone"}, status_code=410)
        request_ref = _optional_content_ref(custom.get("request_ref"))
        if request_ref is None:
            return _json_response({"error": "human_input_not_found"}, status_code=404)
        runtime = get_durable_loop_activity_runtime()
        human = await get_protocol_model(
            runtime.content,
            request_ref,
            HumanInputRequestV1,
        )
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
    app.route(route=route, methods=methods, auth_level=auth_level)(decorated)


@dataclass(frozen=True, slots=True)
class _RunMetadata:
    identity: DurableRunIdentityV1
    plan: DurableLoopPlanDocumentV1
    session_entity_key: str


async def _run_metadata(
    *,
    resolved: ResolvedAgent,
    settings: DurableLoopSettings,
    body: Mapping[str, object],
    owner_hash: str,
    session_id: str,
    request_id: str,
    run_id: str,
) -> _RunMetadata:
    client_manager = get_client_manager()
    target = client_manager.resolve_inference_target(resolved.model)
    resolved_model = target.model
    if not resolved_model or not target.provider:
        raise ValueError("durable-loop inference target is incomplete")
    api_version = target.api_version or "responses-v1"
    sandbox_profile = _sandbox_profile(body, settings)
    fault_profile = _fault_profile(body, settings)
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
    )
    return _RunMetadata(
        identity=identity,
        plan=plan,
        session_entity_key=canonical_hash(
            {"owner_hash": owner_hash, "session_id": session_id}
        ),
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


async def _authorized_status(
    req: Request,
    client: Any,
    auth: EndpointAuthConfig,
) -> tuple[Any, DurableOrchestrationInputV1] | Response:
    owner = _authorized_owner(req, auth)
    if isinstance(owner, Response):
        return owner
    run_id = _path_parameter(req, "run_id")
    status = await client.get_status(run_id, show_input=True)
    if status is None:
        return _json_response({"error": "run_not_found"}, status_code=404)
    try:
        durable_input = DurableOrchestrationInputV1.model_validate_json(
            canonical_json_bytes(status.input)
        )
    except Exception:
        return _json_response({"error": "run_not_found"}, status_code=404)
    if durable_input.identity.owner_hash != _owner_hash(owner):
        return _json_response({"error": "run_not_found"}, status_code=404)
    return status, durable_input


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


def _start_payload(body: object) -> Mapping[str, object]:
    if not isinstance(body, Mapping):
        raise ValueError("request body must be a JSON object")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ValueError("prompt exceeds the byte limit")
    return body


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


def _authorized_owner(req: Request, auth: EndpointAuthConfig) -> OwnerPrincipal | Response:
    owner = resolve_owner_principal(req.headers.get, auth)
    if isinstance(owner, AuthError):
        return _json_response({"error": owner.message}, status_code=owner.status_code)
    return owner


def _owner_hash(owner: OwnerPrincipal) -> str:
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
    projected = output.get("status")
    if not isinstance(projected, str):
        projected = (
            DurableLoopRunStatus.WAITING.value
            if custom.get("phase") == "human_wait"
            else (
                DurableLoopRunStatus.PENDING.value
                if _runtime_status_name(status) == "Pending"
                else DurableLoopRunStatus.RUNNING.value
            )
        )
    projection: dict[str, object] = {
        "phase": output.get("phase") or custom.get("phase") or "durable",
        "run_id": status.instance_id,
        "status": projected,
    }
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


def _optional_content_ref(value: object) -> ContentRefV1 | None:
    if value is None:
        return None
    return ContentRefV1.model_validate(value)


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _accepted_response(run_id: str, session_id: str) -> Response:
    return _json_response(
        {
            "cancel_url": f"/api/{_ROUTE_BASE}/{run_id}/cancel",
            "result_url": f"/api/{_ROUTE_BASE}/{run_id}/result",
            "run_id": run_id,
            "session_id": session_id,
            "status": DurableLoopRunStatus.PENDING.value,
            "status_url": f"/api/{_ROUTE_BASE}/{run_id}",
        },
        status_code=202,
        headers={"x-ms-session-id": session_id},
    )


def _json_response(
    body: Mapping[str, object],
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> Response:
    return Response(
        content=json.dumps(body, ensure_ascii=True, separators=(",", ":")),
        status_code=status_code,
        media_type="application/json",
        headers=dict(headers or {}),
    )
