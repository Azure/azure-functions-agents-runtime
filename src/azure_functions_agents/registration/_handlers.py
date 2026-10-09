"""Private handler factories for trigger registration."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING, Any

import azure.functions as func
import jsonschema
from azurefunctions.extensions.http.fastapi import Request, Response

from .._agent_execution import (
    _set_run_result_attributes,
    _total_tool_error_count,
    build_sandbox_tools_for_session,
)
from .._logger import logger
from .._observability import (
    ATTR_FAULT_DOMAIN,
    FaultDomain,
    LifecycleStage,
    capture_sensitive_data,
    start_span,
)
from .._source_marker import source_marker
from ..config import EndpointAuthConfig, ResolvedAgent, _to_bool
from ..harness._harness_binding import HarnessKind, bind_harness
from ..harness._session_storage import SessionStorageError
from ..response_contract import (
    InvalidResponseJsonError,
    ResponseSchemaValidationError,
    response_format_instructions,
    validate_response_contract,
)
from ._auth import authorize_entra_request
from ._trigger_serialization import serialize_trigger_data
from .capabilities import AgentCapabilities
from .catalog import AgentCatalog

if TYPE_CHECKING:
    from ..workflows.schema import WorkflowPlanPolicy

AUTH_LEVEL_MAP = {
    "anonymous": func.AuthLevel.ANONYMOUS,
    "function": func.AuthLevel.FUNCTION,
    "admin": func.AuthLevel.ADMIN,
}
_SESSION_ID_HEADER = "x-ms-session-id"


def normalize_timer_schedule(schedule: str) -> str:
    """Accept 5-part cron by prepending seconds; keep 6-part schedules unchanged."""
    schedule_parts = schedule.strip().split()
    if len(schedule_parts) == 5:
        return f"0 {schedule.strip()}"
    return schedule.strip()


def validate_request_body(body: Any, input_schema: dict[str, Any] | None) -> Response | None:
    """Validate body against JSON Schema, returning an HTTP error response on failure."""
    if input_schema is None:
        return None

    try:
        jsonschema.validate(instance=body, schema=input_schema)
    except jsonschema.ValidationError as exc:
        return Response(
            content=json.dumps(
                {
                    "error": "Input validation failed",
                    "details": exc.message,
                }
            ),
            status_code=400,
            media_type="application/json",
        )
    except jsonschema.SchemaError as exc:
        return Response(
            content=json.dumps(
                {
                    "error": "Invalid input schema",
                    "details": exc.message,
                }
            ),
            status_code=500,
            media_type="application/json",
        )

    return None


def _should_log(resolved: ResolvedAgent) -> bool:
    return _to_bool(resolved.metadata.get("logger", True), default=True)


def _run_log_payload(resolved: ResolvedAgent, result: Any) -> dict[str, Any]:
    """Build the response log body, gating raw content behind capture_sensitive_data."""
    tool_calls = list(getattr(result, "tool_calls", None) or [])
    content = str(getattr(result, "content", "") or "")
    payload: dict[str, Any] = {
        "session_id": getattr(result, "session_id", None),
        "response_bytes": len(content),
        "tool_call_count": len(tool_calls),
        "tool_error_count": _total_tool_error_count(result),
    }
    if capture_sensitive_data():
        payload["response"] = content
        payload["tool_calls"] = tool_calls
    return payload


async def _run_agent(*args: Any, **kwargs: Any) -> Any:
    runner_module = import_module("azure_functions_agents.runner")
    return await runner_module.run_agent(*args, **kwargs)


def _request_header_value(req: Request, header_name: str) -> str | None:
    headers = getattr(req, "headers", None)
    if headers is None:
        return None

    value = headers.get(header_name) if hasattr(headers, "get") else None
    if isinstance(value, str) and value.strip():
        return value.strip()

    if hasattr(headers, "items"):
        for key, item in headers.items():
            if key.lower() == header_name.lower() and isinstance(item, str) and item.strip():
                return item.strip()

    return None


def _new_session_id() -> str:
    return uuid.uuid4().hex


def make_agent_handler(
    resolved: ResolvedAgent,
    trigger_type: str,
    capabilities: AgentCapabilities,
    catalog: AgentCatalog | None = None,
    *,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> Callable[..., Any]:
    """Create an async handler function for a non-HTTP triggered agent."""
    harness = bind_harness(resolved, capabilities)

    # NOTE: deliberately omit a type annotation on `trigger_data`. The Azure
    # Functions Python worker validates annotations against the binding's
    # expected type (e.g. ``func.TimerRequest``) and rejects ``Any``. Leaving
    # the parameter unannotated tells the worker to skip that type check, so
    # this single handler can be reused across all non-HTTP trigger types.
    async def _handle(trigger_data, durable_client: Any | None) -> None:  # type: ignore[no-untyped-def]
        logger.info(
            "Agent triggered: trigger_type=%s source_file=%s",
            trigger_type,
            source_marker(resolved.source_file),
        )

        session_id = _new_session_id()
        with start_span(
            f"agent.run {resolved.slug}",
            lifecycle_stage=LifecycleStage.AGENT_RUN,
            attributes={
                "af.agent.name": resolved.slug,
                "af.agent.display_name": resolved.name,
                "af.agent.trigger_type": trigger_type,
                "af.agent.session_id": session_id,
                "af.agent.model": resolved.model,
            },
        ) as span:
            try:
                data_json = serialize_trigger_data(trigger_data)
                span.set_attribute("af.agent.input_bytes", len(data_json))
                span.set_content("af.agent.input", data_json)
                parts: list[str] = [
                    f"Triggered by: {trigger_type}\n\nTrigger data:\n```json\n{data_json}\n```"
                ]
                prompt = "\n\n".join(parts)

                result = await _run_agent(
                    prompt,
                    instructions=resolved.instructions,
                    timeout=resolved.timeout,
                    model=resolved.model,
                    session_id=session_id,
                    sandbox_tools=build_sandbox_tools_for_session(resolved, session_id),
                    web_request_tools=capabilities.web_request_tools,
                    tools=capabilities.filtered_user_tools,
                    mcp_tools=capabilities.filtered_mcp_tools,
                    skills=capabilities.skills,
                    skill_catalog=capabilities.skill_catalog,
                    agent_configuration=resolved.agent_configuration,
                    subagents=resolved.subagents,
                    catalog=catalog,
                    system_addendum=workflow_system_addendum,
                    workflow_enabled=workflows_enabled,
                    workflow_durable_client=durable_client,
                    workflow_agent_slug=resolved.slug,
                    workflow_policy=workflow_policy,
                    agent_name=resolved.slug,
                    _harness=harness,
                    _session_is_new=True,
                )

                _set_run_result_attributes(span, result)
                span.add_event("af.agent.invoke.completed")
                span.set_attribute("af.agent.outcome", "success")

                if _should_log(resolved):
                    logger.info(
                        "Agent response: source_file=%s payload=%s",
                        source_marker(resolved.source_file),
                        json.dumps(
                            {
                                "session_id": result.session_id,
                                "response": result.content,
                                "tool_calls": result.tool_calls,
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                    )
            except Exception as exc:
                span.set_attribute("af.agent.outcome", "error")
                span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                logger.exception(
                    "Agent execution failed: source_file=%s error=%s",
                    source_marker(resolved.source_file),
                    exc,
                )
                raise

    async def _handler_with_client(trigger_data, client: str) -> None:  # type: ignore[no-untyped-def]
        await _handle(trigger_data, client)

    async def _handler_without_client(trigger_data) -> None:  # type: ignore[no-untyped-def]
        await _handle(trigger_data, None)

    handler = _handler_with_client if workflows_enabled else _handler_without_client
    handler.__name__ = f"handler_{re.sub(r'[^a-zA-Z0-9_]', '_', resolved.name)}"
    return handler


def make_http_agent_handler(
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    catalog: AgentCatalog | None = None,
    auth: EndpointAuthConfig | None = None,
    *,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> Callable[..., Any]:
    """Create an async handler for an HTTP-triggered agent.

    ``auth`` is the resolved inbound authentication policy. In ``entra`` mode the
    request is authorized (App Service Authentication principal + allow-lists)
    before the runner is ever invoked; the other modes are enforced by the
    Functions host key check via the route's ``AuthLevel``.
    """
    auth_policy = auth or EndpointAuthConfig()
    harness = bind_harness(resolved, capabilities)

    async def _handle(req: Request, durable_client: Any | None) -> Response:
        auth_error = authorize_entra_request(req.headers.get, auth_policy)
        if auth_error is not None:
            return Response(
                content=json.dumps({"error": auth_error.message}),
                status_code=auth_error.status_code,
                media_type="application/json",
            )

        logger.info(
            "HTTP agent triggered: source_file=%s",
            source_marker(resolved.source_file),
        )

        with start_span(
            f"agent.run {resolved.slug}",
            lifecycle_stage=LifecycleStage.AGENT_RUN,
            attributes={
                "af.agent.name": resolved.slug,
                "af.agent.display_name": resolved.name,
                "af.agent.trigger_type": "http",
                "af.agent.model": resolved.model,
            },
        ) as span:
            try:
                supplied_session_id = _request_header_value(req, _SESSION_ID_HEADER)
                session_id = supplied_session_id or _new_session_id()
                echo_failed_session_id = (
                    harness.name is HarnessKind.MAF or supplied_session_id is not None
                )
                turn_completed = False
                span.set_attribute("af.agent.session_id", session_id)
                try:
                    body = await req.json()
                    body_json = json.dumps(body, ensure_ascii=False, default=str)
                except Exception:
                    body_bytes = await req.body()
                    body = body_bytes.decode("utf-8", errors="replace") if body_bytes else {}
                    body_json = body if isinstance(body, str) else json.dumps(body)

                span.set_attribute("af.agent.input_bytes", len(body_json))
                span.set_content("af.agent.input", body_json)

                validation_error = validate_request_body(body, resolved.input_schema)
                if validation_error is not None:
                    if validation_error.status_code == 500:
                        logger.error(
                            "HTTP agent '%s' has invalid input schema: %s",
                            resolved.name,
                            validation_error.body.decode("utf-8"),
                        )
                    span.set_attribute("af.agent.outcome", "error")
                    span.set_error("input validation failed", fault_domain=FaultDomain.APP)
                    span.add_event(
                        "af.input.validation_failed",
                        {
                            ATTR_FAULT_DOMAIN: FaultDomain.APP,
                            "af.http.status_code": validation_error.status_code,
                        },
                    )
                    if echo_failed_session_id:
                        validation_error.headers[_SESSION_ID_HEADER] = session_id
                    return validation_error

                parts: list[str] = []
                parts.extend(response_format_instructions(resolved))
                parts.append(f"HTTP request data:\n```json\n{body_json}\n```")
                prompt = "\n\n".join(parts)

                result = await _run_agent(
                    prompt,
                    instructions=resolved.instructions,
                    timeout=resolved.timeout,
                    model=resolved.model,
                    session_id=session_id,
                    sandbox_tools=build_sandbox_tools_for_session(resolved, session_id),
                    web_request_tools=capabilities.web_request_tools,
                    tools=capabilities.filtered_user_tools,
                    mcp_tools=capabilities.filtered_mcp_tools,
                    skills=capabilities.skills,
                    skill_catalog=capabilities.skill_catalog,
                    agent_configuration=resolved.agent_configuration,
                    subagents=resolved.subagents,
                    catalog=catalog,
                    system_addendum=workflow_system_addendum,
                    workflow_enabled=workflows_enabled,
                    workflow_durable_client=durable_client,
                    workflow_agent_slug=resolved.slug,
                    workflow_policy=workflow_policy,
                    agent_name=resolved.slug,
                    _harness=harness,
                    _session_is_new=not supplied_session_id,
                )
                turn_completed = True

                _set_run_result_attributes(span, result)
                span.add_event("af.agent.invoke.completed")
                span.set_attribute("af.agent.outcome", "success")

                if _should_log(resolved):
                    logger.info(
                        "HTTP agent '%s' response: %s",
                        resolved.name,
                        json.dumps(
                            _run_log_payload(resolved, result),
                            ensure_ascii=False,
                            default=str,
                        ),
                    )

                if resolved.response_example or resolved.response_schema is not None:
                    try:
                        parsed = validate_response_contract(
                            result.content,
                            resolved.response_schema,
                        )
                    except InvalidResponseJsonError as exc:
                        logger.warning(
                            "HTTP agent '%s' returned invalid JSON: %s",
                            resolved.name,
                            exc,
                        )
                        span.set_attribute("af.agent.outcome", "error")
                        span.set_error("agent returned invalid JSON", fault_domain=FaultDomain.APP)
                        span.add_event(
                            "af.response.invalid_json",
                            {ATTR_FAULT_DOMAIN: FaultDomain.APP},
                        )
                        return Response(
                            content=json.dumps(
                                {
                                    "error": "Agent returned invalid JSON",
                                    "raw_response": result.content,
                                }
                            ),
                            status_code=500,
                            media_type="application/json",
                            headers={_SESSION_ID_HEADER: session_id},
                        )
                    except ResponseSchemaValidationError as exc:
                        logger.warning(
                            "HTTP agent '%s' returned JSON that failed schema validation: %s",
                            resolved.name,
                            exc.details,
                        )
                        span.set_attribute("af.agent.outcome", "error")
                        span.set_error(
                            "response schema validation failed", fault_domain=FaultDomain.APP
                        )
                        span.add_event(
                            "af.response.schema_validation_failed",
                            {ATTR_FAULT_DOMAIN: FaultDomain.APP},
                        )
                        return Response(
                            content=json.dumps(
                                {
                                    "error": "Agent response validation failed",
                                    "details": exc.details,
                                }
                            ),
                            status_code=500,
                            media_type="application/json",
                            headers={_SESSION_ID_HEADER: session_id},
                        )
                    return Response(
                        content=json.dumps(parsed, ensure_ascii=False),
                        status_code=200,
                        media_type="application/json",
                        headers={_SESSION_ID_HEADER: session_id},
                    )

                return Response(
                    content=result.content,
                    status_code=200,
                    media_type="text/plain",
                    headers={_SESSION_ID_HEADER: session_id},
                )
            except SessionStorageError as exc:
                span.set_attribute("af.agent.outcome", "error")
                span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                logger.error("HTTP agent '%s' storage failed: %s", resolved.name, exc)
                return Response(
                    content=json.dumps({"error": str(exc)}),
                    status_code=exc.status_code,
                    media_type="application/json",
                    headers=(
                        {_SESSION_ID_HEADER: session_id}
                        if echo_failed_session_id or turn_completed
                        else None
                    ),
                )
            except Exception as exc:
                span.set_attribute("af.agent.outcome", "error")
                span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                logger.exception("HTTP agent '%s' failed: %s", resolved.name, exc)
                return Response(
                    content=json.dumps({"error": str(exc)}),
                    status_code=500,
                    media_type="application/json",
                    headers=(
                        {_SESSION_ID_HEADER: session_id}
                        if echo_failed_session_id or turn_completed
                        else None
                    ),
                )

    async def _handler_with_client(req: Request, client: str) -> Response:
        return await _handle(req, client)

    async def _handler_without_client(req: Request) -> Response:
        return await _handle(req, None)

    handler = _handler_with_client if workflows_enabled else _handler_without_client
    handler.__name__ = f"handler_{re.sub(r'[^a-zA-Z0-9_]', '_', resolved.name)}"
    return handler
