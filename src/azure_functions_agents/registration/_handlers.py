"""Private handler factories for trigger registration."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from importlib import import_module
from typing import Any, TypedDict, cast

import azure.functions as func
import jsonschema
from azurefunctions.extensions.http.fastapi import Request, Response

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
from ..controller.budget import RequestBudget
from ..controller.http import ControllerResponse, prefers_respond_async, submit_run
from ..controller.readiness import SessionRuntimeBinding
from ..execution.backend import RunError, RunResult
from ..execution.compat import run_to_agent_result, split_runner_call
from ..execution.factory import create_execution_backend
from ..execution.setup_budget import synchronous_wait_seconds
from ..session_state import OwnerPrincipal
from ._auth import AuthError, authorize_entra_request, resolve_owner_principal
from ._trigger_serialization import serialize_trigger_data
from .capabilities import AgentCapabilities
from .catalog import AgentCatalog

AUTH_LEVEL_MAP = {
    "anonymous": func.AuthLevel.ANONYMOUS,
    "function": func.AuthLevel.FUNCTION,
    "admin": func.AuthLevel.ADMIN,
}
_SESSION_ID_HEADER = "x-ms-session-id"


class _SessionRuntimeKwargs(TypedDict):
    session_runtime: SessionRuntimeBinding
    owner: OwnerPrincipal | None


def extract_json_from_response(text: str) -> str:
    """Extract JSON from an agent response, stripping markdown code fences if present."""
    stripped = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", stripped, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


def normalize_timer_schedule(schedule: str) -> str:
    """Accept 5-part cron by prepending seconds; keep 6-part schedules unchanged."""
    schedule_parts = schedule.strip().split()
    if len(schedule_parts) == 5:
        return f"0 {schedule.strip()}"
    return schedule.strip()


def build_sandbox_tools_for_session(
    resolved: ResolvedAgent, session_id: str | None
) -> list[Any] | None:
    """Build per-request sandbox tools using the resolved session id."""
    if resolved.tools_disabled:
        return None
    if resolved.sandbox_config is None:
        return None
    fallback = session_id or uuid.uuid4().hex
    sandbox_module = import_module("azure_functions_agents.system_tools.sandbox")
    create_sandbox_tools = sandbox_module.create_sandbox_tools
    return cast(
        list[Any],
        create_sandbox_tools(
            resolved.sandbox_config.model_dump(),
            fallback_session_id=fallback,
        ),
    )


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


def _looks_like_tool_error(result: Any) -> bool:
    """Best-effort: does a recorded tool result represent a failure?

    Catches both the sandbox error envelope (``{"error": ...}``) and a "successful" call whose
    ``stderr`` is non-empty — the case that used to hide broken code execution.
    """
    if not isinstance(result, str):
        return False
    try:
        parsed = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    if parsed.get("error"):
        return True
    stderr = parsed.get("stderr")
    return bool(isinstance(stderr, str) and stderr.strip())


def _tool_error_count(tool_calls: list[dict[str, Any]] | None) -> int:
    if not tool_calls:
        return 0
    return sum(1 for call in tool_calls if _looks_like_tool_error(call.get("result")))


def _total_tool_error_count(result: Any) -> int:
    """Combine tool-call error heuristics with explicit delegate-error accounting.

    ``_tool_error_count`` only recognizes the sandbox error envelope /
    non-empty ``stderr`` heuristic (:func:`_looks_like_tool_error`), which
    cannot classify a specialist's sanitized free-text delegation failure
    (FRD 0007 §4.12: "do NOT rely on `_looks_like_tool_error`'s JSON
    `{error}`/stderr heuristic for a specialist's sanitized free-text
    failure"). ``AgentResult.delegate_error_count`` is incremented
    explicitly by the ``delegate_<slug>`` adapter instead, and is added
    here so both the run span and the response log see one combined count.
    """
    tool_calls = list(getattr(result, "tool_calls", None) or [])
    delegate_error_count = int(getattr(result, "delegate_error_count", 0) or 0)
    return _tool_error_count(tool_calls) + delegate_error_count


def _set_run_result_attributes(span: Any, result: Any) -> None:
    """Attach non-sensitive run-summary attributes; content only when opted in."""
    tool_calls = list(getattr(result, "tool_calls", None) or [])
    content = str(getattr(result, "content", "") or "")
    span.set_attribute("af.agent.tool_call_count", len(tool_calls))
    span.set_attribute("af.agent.tool_error_count", _total_tool_error_count(result))
    span.set_attribute("af.agent.response_bytes", len(content))
    span.set_content("af.agent.response", content)


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


def _response_format_instructions(resolved: ResolvedAgent) -> list[str]:
    if resolved.response_example:
        return [
            "You MUST respond with ONLY a valid JSON object "
            "(no markdown, no explanation, no code fences). "
            "Your response must match this example format:\n"
            f"```json\n{resolved.response_example}\n```"
        ]
    if resolved.response_schema:
        schema_str = json.dumps(resolved.response_schema, indent=2)
        return [
            "You MUST respond with ONLY a valid JSON object "
            "(no markdown, no explanation, no code fences). "
            "Your response must conform to this JSON Schema:\n"
            f"```json\n{schema_str}\n```"
        ]
    return []


async def _run_agent(
    *args: Any,
    session_runtime: SessionRuntimeBinding | None = None,
    owner: OwnerPrincipal | None = None,
    **kwargs: Any,
) -> Any:
    request, binding = split_runner_call(args, kwargs, stream=False)
    backend = (
        create_execution_backend(binding=binding)
        if session_runtime is None
        else create_execution_backend(
            binding=binding,
            session_runtime=session_runtime,
            owner=owner,
        )
    )
    wait_timeout_seconds = (
        None if session_runtime is None else synchronous_wait_seconds(request.timeout)
    )
    return await run_to_agent_result(
        backend,
        request,
        wait_timeout_seconds=wait_timeout_seconds,
    )


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


def _session_runtime_kwargs(
    session_runtime: SessionRuntimeBinding | None,
    owner: OwnerPrincipal | None,
) -> _SessionRuntimeKwargs | None:
    if session_runtime is None:
        return None
    return {"session_runtime": session_runtime, "owner": owner}


def _controller_response_to_fastapi(response: ControllerResponse) -> Response:
    """Adapt a framework-neutral controller response at the registration boundary."""
    content = (
        response.body
        if isinstance(response.body, str)
        else json.dumps(response.body, ensure_ascii=False)
    )
    return Response(
        content=content,
        status_code=response.status_code,
        media_type="text/plain" if isinstance(response.body, str) else "application/json",
        headers=dict(response.headers),
    )


def _controller_session_id(response: ControllerResponse) -> str | None:
    for name, value in response.headers.items():
        if name.casefold() == _SESSION_ID_HEADER and value.strip():
            return value
    return None


def _render_validated_http_response(
    resolved: ResolvedAgent,
    content: str,
    session_id: str,
    *,
    on_invalid_json: Callable[[json.JSONDecodeError], None] | None = None,
    on_schema_error: Callable[[jsonschema.ValidationError], None] | None = None,
) -> Response:
    """Apply the existing custom HTTP response contract to a controller result."""
    extracted = extract_json_from_response(content)
    try:
        parsed = json.loads(extracted)
    except json.JSONDecodeError as exc:
        if on_invalid_json is not None:
            on_invalid_json(exc)
        return Response(
            content=json.dumps(
                {
                    "error": "Agent returned invalid JSON",
                    "raw_response": content,
                }
            ),
            status_code=500,
            media_type="application/json",
            headers={_SESSION_ID_HEADER: session_id},
        )
    if resolved.response_schema:
        try:
            jsonschema.validate(instance=parsed, schema=resolved.response_schema)
        except jsonschema.ValidationError as exc:
            if on_schema_error is not None:
                on_schema_error(exc)
            return Response(
                content=json.dumps(
                    {
                        "error": "Agent response validation failed",
                        "details": exc.message,
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


def _output_validator(resolved: ResolvedAgent) -> Callable[[RunResult], RunError | None] | None:
    """Build the controller-side validator used before an ACA success is adopted."""
    if not resolved.response_example and not resolved.response_schema:
        return None

    def validate(result: RunResult) -> RunError | None:
        try:
            parsed = json.loads(extract_json_from_response(result.content))
        except json.JSONDecodeError:
            return RunError(
                code="response_validation_failed",
                message="Agent returned invalid JSON.",
                fault_domain="app",
            )
        if resolved.response_schema is not None:
            try:
                jsonschema.validate(instance=parsed, schema=resolved.response_schema)
            except jsonschema.ValidationError:
                return RunError(
                    code="response_validation_failed",
                    message="Agent response failed schema validation.",
                    fault_domain="app",
                )
        return None

    return validate


def make_agent_handler(
    resolved: ResolvedAgent,
    trigger_type: str,
    capabilities: AgentCapabilities,
    catalog: AgentCatalog | None = None,
    *,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
) -> Callable[..., Any]:
    """Create an async handler function for a non-HTTP triggered agent."""

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
                    skill_paths=capabilities.enabled_skill_paths,
                    subagents=resolved.subagents,
                    catalog=catalog,
                    system_addendum=workflow_system_addendum,
                    workflow_enabled=workflows_enabled,
                    workflow_durable_client=durable_client,
                    agent_name=resolved.slug,
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
    session_runtime: SessionRuntimeBinding | None = None,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
) -> Callable[..., Any]:
    """Create an async handler for an HTTP-triggered agent.

    ``auth`` is the resolved inbound authentication policy. In ``entra`` mode the
    request is authorized (App Service Authentication principal + allow-lists)
    before the runner is ever invoked; the other modes are enforced by the
    Functions host key check via the route's ``AuthLevel``.
    """
    auth_policy = auth or EndpointAuthConfig()

    async def _handle(req: Request, durable_client: Any | None) -> Response:
        owner: OwnerPrincipal | None = None
        if session_runtime is None:
            auth_error = authorize_entra_request(req.headers.get, auth_policy)
            if auth_error is not None:
                return Response(
                    content=json.dumps({"error": auth_error.message}),
                    status_code=auth_error.status_code,
                    media_type="application/json",
                )
        else:
            resolved_owner = resolve_owner_principal(req.headers.get, auth_policy)
            if isinstance(resolved_owner, AuthError):
                return Response(
                    content=json.dumps({"error": resolved_owner.message}),
                    status_code=resolved_owner.status_code,
                    media_type="application/json",
                )
            owner = resolved_owner

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
                session_id = _request_header_value(req, _SESSION_ID_HEADER) or (
                    None if session_runtime is not None else _new_session_id()
                )
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
                    validation_error.headers[_SESSION_ID_HEADER] = session_id or _new_session_id()
                    return validation_error

                parts: list[str] = []
                parts.extend(_response_format_instructions(resolved))
                parts.append(f"HTTP request data:\n```json\n{body_json}\n```")
                prompt = "\n\n".join(parts)

                runner_kwargs: dict[str, Any] = {
                    "instructions": resolved.instructions,
                    "timeout": resolved.timeout,
                    "model": resolved.model,
                    "session_id": session_id,
                    "sandbox_tools": build_sandbox_tools_for_session(resolved, session_id),
                    "web_request_tools": capabilities.web_request_tools,
                    "tools": capabilities.filtered_user_tools,
                    "mcp_tools": capabilities.filtered_mcp_tools,
                    "skill_paths": capabilities.enabled_skill_paths,
                    "subagents": resolved.subagents,
                    "catalog": catalog,
                    "system_addendum": workflow_system_addendum,
                    "workflow_enabled": workflows_enabled,
                    "workflow_durable_client": durable_client,
                    "agent_name": resolved.slug,
                    "output_validator": _output_validator(resolved),
                }
                runtime_kwargs = _session_runtime_kwargs(session_runtime, owner)
                if runtime_kwargs is None:
                    result = await _run_agent(prompt, **runner_kwargs)
                else:
                    budget = RequestBudget.start(authored_timeout=resolved.timeout)
                    request, binding = split_runner_call(
                        (prompt,),
                        {
                            **runner_kwargs,
                            "idempotency_key": _request_header_value(req, "Idempotency-Key"),
                        },
                        stream=False,
                    )
                    backend = create_execution_backend(
                        binding=binding,
                        session_runtime=runtime_kwargs["session_runtime"],
                        owner=runtime_kwargs["owner"],
                        setup_budget=budget.setup,
                    )
                    controller_response = await submit_run(
                        backend,
                        request,
                        agent_slug=resolved.slug,
                        respond_async=prefers_respond_async(req.headers),
                        budget=budget,
                    )
                    if controller_response.status_code != 200:
                        return _controller_response_to_fastapi(controller_response)
                    body_value = controller_response.body
                    if not isinstance(body_value, dict):
                        return _controller_response_to_fastapi(controller_response)
                    result_content = body_value.get("response")
                    if not isinstance(result_content, str):
                        return _controller_response_to_fastapi(controller_response)
                    controller_session_id = _controller_session_id(controller_response)
                    if controller_session_id is None:
                        return _controller_response_to_fastapi(controller_response)
                    if resolved.response_example or resolved.response_schema:
                        return _render_validated_http_response(
                            resolved,
                            result_content,
                            controller_session_id,
                        )
                    return Response(
                        content=result_content,
                        status_code=200,
                        media_type="text/plain",
                        headers={_SESSION_ID_HEADER: controller_session_id},
                    )

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

                if resolved.response_example or resolved.response_schema:
                    def record_invalid_json(exc: json.JSONDecodeError) -> None:
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

                    def record_schema_error(exc: jsonschema.ValidationError) -> None:
                        logger.warning(
                            "HTTP agent '%s' returned JSON that failed schema validation: %s",
                            resolved.name,
                            exc,
                        )
                        span.set_attribute("af.agent.outcome", "error")
                        span.set_error(
                            "response schema validation failed",
                            fault_domain=FaultDomain.APP,
                        )
                        span.add_event(
                            "af.response.schema_validation_failed",
                            {ATTR_FAULT_DOMAIN: FaultDomain.APP},
                        )

                    return _render_validated_http_response(
                        resolved,
                        result.content,
                        result.session_id,
                        on_invalid_json=record_invalid_json,
                        on_schema_error=record_schema_error,
                    )

                return Response(
                    content=result.content,
                    status_code=200,
                    media_type="text/plain",
                    headers={_SESSION_ID_HEADER: result.session_id},
                )
            except Exception as exc:
                span.set_attribute("af.agent.outcome", "error")
                span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                logger.exception("HTTP agent '%s' failed: %s", resolved.name, exc)
                return Response(
                    content=json.dumps({"error": str(exc)}),
                    status_code=500,
                    media_type="application/json",
                    headers={_SESSION_ID_HEADER: session_id or _new_session_id()},
                )

    async def _handler_with_client(req: Request, client: str) -> Response:
        return await _handle(req, client)

    async def _handler_without_client(req: Request) -> Response:
        return await _handle(req, None)

    handler = _handler_with_client if workflows_enabled else _handler_without_client
    handler.__name__ = f"handler_{re.sub(r'[^a-zA-Z0-9_]', '_', resolved.name)}"
    return handler
