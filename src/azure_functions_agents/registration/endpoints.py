"""Built-in HTTP/UI/MCP endpoint registration for resolved agents."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import azure.functions as func
from azurefunctions.extensions.http.fastapi import Request, Response, StreamingResponse

from .._logger import logger
from .._observability import (
    FaultDomain,
    LifecycleStage,
    get_current_operation_id,
    start_span,
)
from .._session_id import SESSION_ID_PATTERN
from .._source_marker import source_marker
from ..config import EndpointAuthConfig, ResolvedAgent
from ..controller.budget import RequestBudget
from ..controller.http import (
    cancel_run as cancel_controller_run,
)
from ..controller.http import (
    parse_last_event_id,
    prefers_respond_async,
    read_result,
    read_status,
    submit_run,
)
from ..controller.readiness import touch_session_activity
from ..controller.streaming import render_events
from ..execution.backend import SESSION_TOMBSTONED_ERROR_CODE, RunContext, StartRunRequest
from ..execution.binding import AgentBinding
from ..execution.compat import (
    render_sse_event,
    run_to_agent_result,
    split_runner_call,
)
from ..execution.factory import create_execution_backend
from ..execution.session_runtime import (
    SessionExecutionRuntime,
    is_aca_sandbox_runtime,
)
from ..execution.setup_budget import synchronous_wait_seconds
from ..session_state import (
    FunctionAppPrincipal,
    OwnerPrincipal,
    SessionStateContractError,
    encode_label_safe_digest,
    resolve_owner_context,
    validate_run_id,
    validate_session_id,
)
from ._auth import (
    AuthError,
    authorize_entra_request,
    resolve_endpoint_auth_level,
    resolve_owner_principal,
)
from ._handlers import (
    _controller_response_to_fastapi,
    _controller_session_id,
    _session_runtime_kwargs,
    _set_run_result_attributes,
    build_output_validator,
    build_sandbox_tools_for_session,
)
from ._naming import _function_name_from_source, _safe_function_name
from .capabilities import AgentCapabilities
from .catalog import AgentCatalog

if TYPE_CHECKING:
    from ..workflows.workflow_schema import WorkflowPlanPolicy

_MCP_AGENT_TOOL_PROPERTIES = json.dumps(
    [
        {
            "propertyName": "prompt",
            "propertyType": "string",
            "description": "Prompt text sent to the agent.",
            "isRequired": True,
            "isArray": False,
        }
    ]
)

type ChatHandler = Callable[[Request, Any | None], Awaitable[Response]]
type ChatStreamHandler = Callable[[Request, Any | None], Awaitable[Response]]
type McpAgentChatHandler = Callable[[str, Any | None], Awaitable[str]]


def _format_exception_message(exc: Exception) -> str:
    message = str(exc)
    return message if message else f"{type(exc).__name__}: {exc!r}"


async def _run_agent(
    *args: Any,
    session_runtime: SessionExecutionRuntime | None = None,
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


def _run_agent_stream(
    *args: Any,
    session_runtime: SessionExecutionRuntime | None = None,
    owner: OwnerPrincipal | None = None,
    **kwargs: Any,
) -> AsyncIterator[str]:
    async def stream() -> AsyncIterator[str]:
        request, binding = split_runner_call(args, kwargs, stream=True)
        backend = (
            create_execution_backend(binding=binding, stream_events=True)
            if session_runtime is None
            else create_execution_backend(
                binding=binding,
                stream_events=True,
                session_runtime=session_runtime,
                owner=owner,
            )
        )
        handle = await backend.start_run(request)
        context = RunContext(run_id=handle.run_id, session_id=handle.session_id)
        async for event in backend.read_events(context, after_sequence=0):
            yield render_sse_event(event)
        await backend.get_run(context)

    return stream()


# The runner uses the session id as a filename component, so it rejects anything
# outside this safe set. Shared with the runner via ``_session_id`` (a tiny,
# dependency-free module) so this layer stays valid without eagerly importing
# the heavy ``runner`` module.
_SAFE_SESSION_ID_PATTERN = SESSION_ID_PATTERN
_PROVIDER_LABEL_VALUE_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$"
)
_MAX_HISTORY_REPLAY_MESSAGES = 200
_TRACE_ID_HEADER = "x-ms-trace-id"
_FHA_SESSION_ID_HEADER = "x-ms-fha-session-id"


def _extract_mcp_session_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("sessionId") or payload.get("sessionid")
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if (
        len(value) <= 63
        and _SAFE_SESSION_ID_PATTERN.match(value)
        and _PROVIDER_LABEL_VALUE_PATTERN.fullmatch(value)
    ):
        return value
    # The MCP extension mints its own transport session id (e.g. the
    # streamable-HTTP ``Mcp-Session-Id``), whose format we do not control and
    # which may contain characters the runner rejects or exceed its length
    # cap. Map any such value deterministically into the safe space so the same
    # MCP session still resolves to the same agent session (conversation
    # continuity) without tripping the runner's validation.
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return f"mcp-{encode_label_safe_digest(digest)}"


def _index_path() -> Path:
    return Path(__file__).resolve().parent.parent / "public" / "index.html"


def _resolve_builtin_endpoints_session_id(session_id: str | None) -> str:
    return session_id or uuid.uuid4().hex


def _resolve_session_owner(
    get_header: Callable[[str], str | None],
    auth: EndpointAuthConfig,
    session_runtime: SessionExecutionRuntime | None,
) -> tuple[OwnerPrincipal | None, AuthError | None]:
    if session_runtime is None:
        return None, authorize_entra_request(get_header, auth)
    resolved_owner = resolve_owner_principal(get_header, auth)
    if isinstance(resolved_owner, AuthError):
        return None, resolved_owner
    return resolved_owner, None


def _chat_handler_with_client(handle_chat: ChatHandler) -> Callable[[Request, str], Awaitable[Response]]:
    async def chat(req: Request, client: str) -> Response:
        return await handle_chat(req, client)

    return chat


def _chat_handler_without_client(handle_chat: ChatHandler) -> Callable[[Request], Awaitable[Response]]:
    async def chat(req: Request) -> Response:
        return await handle_chat(req, None)

    return chat


def _chat_stream_handler_with_client(
    handle_chat_stream: ChatStreamHandler,
) -> Callable[[Request, str], Awaitable[Response]]:
    async def chat_stream(req: Request, client: str) -> Response:
        return await handle_chat_stream(req, client)

    return chat_stream


def _chat_stream_handler_without_client(
    handle_chat_stream: ChatStreamHandler,
) -> Callable[[Request], Awaitable[Response]]:
    async def chat_stream(req: Request) -> Response:
        return await handle_chat_stream(req, None)

    return chat_stream


def _mcp_agent_chat_handler_with_client(
    handle_mcp_agent_chat: McpAgentChatHandler,
) -> Callable[[str, str], Awaitable[str]]:
    async def mcp_agent_chat(context: str, client: str) -> str:
        return await handle_mcp_agent_chat(context, client)

    return mcp_agent_chat


def _mcp_agent_chat_handler_without_client(
    handle_mcp_agent_chat: McpAgentChatHandler,
) -> Callable[[str], Awaitable[str]]:
    async def mcp_agent_chat(context: str) -> str:
        return await handle_mcp_agent_chat(context, None)

    return mcp_agent_chat


async def _run_builtin_agent(
    prompt: str,
    *,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    session_id: str | None,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    durable_client: Any | None = None,
    catalog: AgentCatalog | None = None,
    session_runtime: SessionExecutionRuntime | None = None,
    owner: OwnerPrincipal | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> Any:
    resolved_session_id = (
        session_id
        if session_id is not None
        else (
            None
            if session_runtime is not None
            else _resolve_builtin_endpoints_session_id(session_id)
        )
    )
    sandbox_tools = build_sandbox_tools_for_session(resolved, resolved_session_id)
    runner_kwargs: dict[str, Any] = {
        "instructions": resolved.instructions,
        "timeout": resolved.timeout,
        "model": resolved.model,
        "session_id": resolved_session_id,
        "sandbox_tools": sandbox_tools,
        "web_request_tools": capabilities.web_request_tools,
        "tools": capabilities.filtered_user_tools,
        "mcp_tools": capabilities.filtered_mcp_tools,
        "skill_paths": capabilities.enabled_skill_paths,
        "system_addendum": workflow_system_addendum,
        "workflow_enabled": workflows_enabled,
        "workflow_durable_client": durable_client,
        "agent_name": resolved.slug,
        "subagents": resolved.subagents,
        "catalog": catalog,
        "output_validator": build_output_validator(resolved),
    }
    runtime_kwargs = _session_runtime_kwargs(session_runtime, owner)
    if runtime_kwargs is None:
        return await _run_agent(
            prompt,
            **runner_kwargs,
            workflow_policy=workflow_policy,
        )
    return await _run_agent(prompt, **runner_kwargs, **runtime_kwargs)


def _run_builtin_agent_stream(
    prompt: str,
    *,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    session_id: str | None,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    durable_client: Any | None = None,
    catalog: AgentCatalog | None = None,
    session_runtime: SessionExecutionRuntime | None = None,
    owner: OwnerPrincipal | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> Any:
    resolved_session_id = (
        session_id
        if session_id is not None
        else (
            None
            if session_runtime is not None
            else _resolve_builtin_endpoints_session_id(session_id)
        )
    )
    sandbox_tools = build_sandbox_tools_for_session(resolved, resolved_session_id)
    runner_kwargs: dict[str, Any] = {
        "instructions": resolved.instructions,
        "timeout": resolved.timeout,
        "model": resolved.model,
        "session_id": resolved_session_id,
        "sandbox_tools": sandbox_tools,
        "web_request_tools": capabilities.web_request_tools,
        "tools": capabilities.filtered_user_tools,
        "mcp_tools": capabilities.filtered_mcp_tools,
        "skill_paths": capabilities.enabled_skill_paths,
        "system_addendum": workflow_system_addendum,
        "workflow_enabled": workflows_enabled,
        "workflow_durable_client": durable_client,
        "agent_name": resolved.slug,
        "display_name": resolved.name,
        "subagents": resolved.subagents,
        "catalog": catalog,
        "output_validator": build_output_validator(resolved),
    }
    runtime_kwargs = _session_runtime_kwargs(session_runtime, owner)
    if runtime_kwargs is None:
        return _run_agent_stream(
            prompt,
            **runner_kwargs,
            workflow_policy=workflow_policy,
        )
    return _run_agent_stream(prompt, **runner_kwargs, **runtime_kwargs)


def _extract_prompt_from_body(body: Any) -> str:
    prompt = body.get("prompt") if isinstance(body, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Missing 'prompt'")
    return prompt.strip()


def _json_error(message: str, status_code: int = 500) -> Response:
    return Response(
        content=json.dumps({"error": message}),
        status_code=status_code,
        media_type="application/json",
    )


def _with_trace_id(response: Response, trace_id: str | None) -> Response:
    """Attach the active worker trace id when runtime telemetry created one."""
    if trace_id is not None:
        response.headers[_TRACE_ID_HEADER] = trace_id
    return response


def _sse_error_response(message: str, status_code: int = 400) -> StreamingResponse:
    async def error_gen() -> AsyncIterator[str]:
        yield f"data: {json.dumps({'type': 'error', 'content': message})}\n\n"

    return StreamingResponse(
        error_gen(),
        media_type="text/event-stream",
        status_code=status_code,
    )


def _register_chat_page(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    function_name: str,
    route: str,
) -> None:
    def agent_chat_page(req: Request) -> Response:
        index_path = _index_path()
        if not index_path.exists():
            return Response("index.html not found", status_code=404)

        return Response(
            index_path.read_text(encoding="utf-8"),
            status_code=200,
            media_type="text/html",
        )

    decorated = app.route(
        route=route,
        methods=["GET"],
        auth_level=func.AuthLevel.ANONYMOUS,
    )(agent_chat_page)
    app.function_name(name=function_name)(decorated)


def _register_http_chat(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    *,
    route: str,
    function_name: str,
    auth: EndpointAuthConfig,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    catalog: AgentCatalog | None = None,
    session_runtime: SessionExecutionRuntime | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> None:
    async def handle_chat(req: Request, durable_client: Any | None) -> Response:
        owner: OwnerPrincipal | None = None
        if session_runtime is not None:
            owner, auth_error = _resolve_session_owner(
                req.headers.get,
                auth,
                session_runtime,
            )
            if auth_error is not None:
                return _json_error(auth_error.message, status_code=auth_error.status_code)
        requested_session_id = req.headers.get("x-ms-session-id")
        resolved_session_id = (
            requested_session_id
            if requested_session_id is not None
            else (
                None
                if session_runtime is not None
                else _resolve_builtin_endpoints_session_id(None)
            )
        )
        # This endpoint calls `run_agent` directly rather than going through
        # `_handlers.py`'s trigger-registered handlers, so — unlike a
        # user-defined `trigger:` agent — nothing upstream opens an
        # `agent.run {name}` span for it. Opened here so this built-in
        # surface gets the same run-level span/attributes (including B3's
        # `af.agent.tool_error_count`, which folds in delegate errors) that
        # `make_agent_handler`/`make_http_agent_handler` already provide.
        with start_span(
            f"agent.run {resolved.slug}",
            lifecycle_stage=LifecycleStage.AGENT_RUN,
            attributes={
                "af.agent.name": resolved.slug,
                "af.agent.display_name": resolved.name,
                "af.agent.trigger_type": "builtin_chat",
                "af.agent.session_id": resolved_session_id,
                "af.agent.model": resolved.model,
            },
        ) as span:
            trace_id = get_current_operation_id()
            try:
                if session_runtime is None:
                    auth_error = authorize_entra_request(req.headers.get, auth)
                    if auth_error is not None:
                        span.set_attribute("af.agent.outcome", "error")
                        span.set_error(auth_error.message, fault_domain=FaultDomain.APP)
                        return _with_trace_id(
                            _json_error(auth_error.message, status_code=auth_error.status_code),
                            trace_id,
                        )
                body = await req.json()
                prompt = _extract_prompt_from_body(body)
                if session_runtime is not None:
                    budget = RequestBudget.start(authored_timeout=resolved.timeout)
                    backend = create_execution_backend(
                        binding=AgentBinding(
                            agent_name=resolved.slug,
                            output_validator=build_output_validator(resolved),
                        ),
                        session_runtime=session_runtime,
                        owner=owner,
                        setup_budget=budget.setup,
                    )
                    controller_response = await submit_run(
                        backend,
                        StartRunRequest(
                            prompt=prompt,
                            session_id=resolved_session_id,
                            idempotency_key=req.headers.get("Idempotency-Key"),
                            timeout=resolved.timeout,
                        ),
                        agent_slug=resolved.slug,
                        respond_async=prefers_respond_async(req.headers),
                        budget=budget,
                    )
                    if controller_response.status_code != 200:
                        return _with_trace_id(
                            _controller_response_to_fastapi(controller_response),
                            trace_id,
                        )
                    controller_body = controller_response.body
                    if not isinstance(controller_body, dict) or not isinstance(
                        controller_body.get("response"), str
                    ):
                        return _with_trace_id(
                            _controller_response_to_fastapi(controller_response),
                            trace_id,
                        )
                    controller_session_id = _controller_session_id(controller_response)
                    if controller_session_id is None:
                        return _with_trace_id(
                            _controller_response_to_fastapi(controller_response),
                            trace_id,
                        )
                    span.set_attribute("af.agent.outcome", "success")
                    return _with_trace_id(
                        Response(
                            json.dumps(
                                {
                                    "session_id": controller_session_id,
                                    "response": controller_body["response"],
                                    "tool_calls": controller_body.get("tool_calls", []),
                                }
                            ),
                            media_type="application/json",
                            headers={
                                **controller_response.headers,
                                "x-ms-session-id": controller_session_id,
                            },
                        ),
                        trace_id,
                    )
                result = await _run_builtin_agent(
                    prompt,
                    resolved=resolved,
                    capabilities=capabilities,
                    session_id=resolved_session_id,
                    workflows_enabled=workflows_enabled,
                    workflow_system_addendum=workflow_system_addendum,
                    durable_client=durable_client,
                    catalog=catalog,
                    session_runtime=session_runtime,
                    owner=owner,
                    workflow_policy=workflow_policy,
                )
                _set_run_result_attributes(span, result)
                span.set_attribute("af.agent.outcome", "success")
                return _with_trace_id(
                    Response(
                        json.dumps(
                            {
                                "session_id": result.session_id,
                                "response": result.content,
                                "tool_calls": result.tool_calls,
                            }
                        ),
                        media_type="application/json",
                        headers={"x-ms-session-id": result.session_id},
                    ),
                    trace_id,
                )
            except ValueError as exc:
                span.set_attribute("af.agent.outcome", "error")
                span.set_error(str(exc), fault_domain=FaultDomain.APP)
                return _with_trace_id(_json_error(str(exc), status_code=400), trace_id)
            except Exception as exc:
                span.set_attribute("af.agent.outcome", "error")
                span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                error_msg = _format_exception_message(exc)
                logger.error(
                    "Built-in chat API error: source_file=%s error=%s",
                    source_marker(resolved.source_file),
                    error_msg,
                )
                return _with_trace_id(_json_error(error_msg), trace_id)

    decorated: Any
    if workflows_enabled:
        decorated = _chat_handler_with_client(handle_chat)
        decorated = app.durable_client_input(client_name="client")(decorated)
    else:
        decorated = _chat_handler_without_client(handle_chat)

    decorated = app.route(
        route=route,
        methods=["POST"],
        auth_level=resolve_endpoint_auth_level(auth),
    )(decorated)
    app.function_name(name=function_name)(decorated)


async def _start_session_stream(
    *,
    prompt: str,
    resolved: ResolvedAgent,
    session_id: str | None,
    idempotency_key: str | None,
    session_runtime: SessionExecutionRuntime,
    owner: OwnerPrincipal | None,
    respond_async: bool,
    after_sequence: int,
) -> Response:
    """Accept a built-in stream through the shared LRO path, then render journal SSE."""
    budget = RequestBudget.start(authored_timeout=resolved.timeout)
    backend = create_execution_backend(
        binding=AgentBinding(
            agent_name=resolved.slug,
            output_validator=build_output_validator(resolved),
        ),
        stream_events=not respond_async,
        session_runtime=session_runtime,
        owner=owner,
        setup_budget=budget.setup,
    )
    accepted = await submit_run(
        backend,
        StartRunRequest(
            prompt=prompt,
            session_id=session_id,
            idempotency_key=idempotency_key,
            timeout=resolved.timeout,
        ),
        agent_slug=resolved.slug,
        respond_async=True,
        budget=budget,
    )
    if accepted.status_code != 202 or respond_async:
        return _controller_response_to_fastapi(accepted)
    body = accepted.body
    if not isinstance(body, dict):
        return _controller_response_to_fastapi(accepted)
    accepted_session_id = body.get("session_id")
    run_id = body.get("run_id")
    if not isinstance(accepted_session_id, str) or not isinstance(run_id, str):
        return _json_error("sandbox run acceptance response was invalid")
    response_headers = {"x-ms-session-id": accepted_session_id}
    provider_session_id = accepted.headers.get(_FHA_SESSION_ID_HEADER)
    if provider_session_id is not None:
        response_headers[_FHA_SESSION_ID_HEADER] = provider_session_id
    return StreamingResponse(
        render_events(
            backend,
            RunContext(session_id=accepted_session_id, run_id=run_id),
            after_sequence=after_sequence,
        ),
        media_type="text/event-stream",
        headers=response_headers,
    )


def _register_http_chat_stream(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    *,
    route: str,
    function_name: str,
    auth: EndpointAuthConfig,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    catalog: AgentCatalog | None = None,
    session_runtime: SessionExecutionRuntime | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> None:
    async def handle_chat_stream(
        req: Request,
        durable_client: Any | None,
    ) -> Response:
        owner, auth_error = _resolve_session_owner(
            req.headers.get,
            auth,
            session_runtime,
        )
        if auth_error is not None:
            return _sse_error_response(auth_error.message, status_code=auth_error.status_code)

        requested_session_id = req.headers.get("x-ms-session-id")
        session_id = (
            requested_session_id
            if requested_session_id is not None
            else (
                None
                if session_runtime is not None
                else _resolve_builtin_endpoints_session_id(None)
            )
        )
        if session_runtime is not None:
            with start_span(
                f"agent.run {resolved.slug}",
                lifecycle_stage=LifecycleStage.AGENT_RUN,
                attributes={
                    "af.agent.name": resolved.slug,
                    "af.agent.display_name": resolved.name,
                    "af.agent.trigger_type": "builtin_chatstream",
                    "af.agent.session_id": session_id,
                    "af.agent.model": resolved.model,
                },
            ) as span:
                trace_id = get_current_operation_id()
                try:
                    body = await req.json()
                    prompt = _extract_prompt_from_body(body)
                    try:
                        after_sequence = parse_last_event_id(req.headers)
                    except ValueError as exc:
                        span.set_attribute("af.agent.outcome", "error")
                        span.set_error(str(exc), fault_domain=FaultDomain.APP)
                        return _with_trace_id(_json_error(str(exc), status_code=400), trace_id)
                    response = await _start_session_stream(
                        prompt=prompt,
                        resolved=resolved,
                        session_id=session_id,
                        idempotency_key=req.headers.get("Idempotency-Key"),
                        session_runtime=session_runtime,
                        owner=owner,
                        respond_async=prefers_respond_async(req.headers),
                        after_sequence=after_sequence,
                    )
                    span.set_attribute(
                        "af.agent.outcome",
                        "success" if response.status_code < 400 else "error",
                    )
                    return _with_trace_id(response, trace_id)
                except ValueError as exc:
                    span.set_attribute("af.agent.outcome", "error")
                    span.set_error(str(exc), fault_domain=FaultDomain.APP)
                    return _with_trace_id(_sse_error_response(str(exc), status_code=400), trace_id)
                except Exception as exc:
                    span.set_attribute("af.agent.outcome", "error")
                    span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                    error_msg = _format_exception_message(exc)
                    logger.error(
                        "Built-in chat stream error: source_file=%s error=%s",
                        source_marker(resolved.source_file),
                        error_msg,
                    )
                    return _with_trace_id(_sse_error_response(error_msg, status_code=500), trace_id)

        try:
            body = await req.json()
            prompt = _extract_prompt_from_body(body)
            return StreamingResponse(
                _run_builtin_agent_stream(
                    prompt,
                    resolved=resolved,
                    capabilities=capabilities,
                    session_id=session_id,
                    workflows_enabled=workflows_enabled,
                    workflow_system_addendum=workflow_system_addendum,
                    durable_client=durable_client,
                    catalog=catalog,
                    session_runtime=None,
                    owner=owner,
                    workflow_policy=workflow_policy,
                ),
                media_type="text/event-stream",
            )
        except ValueError as exc:
            return _sse_error_response(str(exc), status_code=400)
        except Exception as exc:
            error_msg = _format_exception_message(exc)
            logger.error(
                "Built-in chat stream error: source_file=%s error=%s",
                source_marker(resolved.source_file),
                error_msg,
            )
            return _sse_error_response(error_msg, status_code=500)

    decorated: Any
    if workflows_enabled:
        decorated = _chat_stream_handler_with_client(handle_chat_stream)
        decorated = app.durable_client_input(client_name="client")(decorated)
    else:
        decorated = _chat_stream_handler_without_client(handle_chat_stream)

    decorated = app.route(
        route=route,
        methods=["POST"],
        auth_level=resolve_endpoint_auth_level(auth),
    )(decorated)
    app.function_name(name=function_name)(decorated)


def _register_mcp_endpoint(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    *,
    tool_name: str,
    function_name: str,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    catalog: AgentCatalog | None = None,
    session_runtime: SessionExecutionRuntime | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> None:
    async def handle_mcp_agent_chat(context: str, durable_client: Any | None) -> str:
        # Same rationale as `handle_chat` above: this built-in MCP surface
        # calls `run_agent` directly, so nothing upstream opens an
        # `agent.run {name}` span for it — open one here to get the same
        # run-level attributes (including B3's `af.agent.tool_error_count`).
        with start_span(
            f"agent.run {resolved.slug}",
            lifecycle_stage=LifecycleStage.AGENT_RUN,
            attributes={
                "af.agent.name": resolved.slug,
                "af.agent.display_name": resolved.name,
                "af.agent.trigger_type": "builtin_mcp",
                "af.agent.model": resolved.model,
            },
        ) as span:
            try:
                payload = json.loads(context) if context else {}
                arguments = payload.get("arguments", {}) if isinstance(payload, dict) else {}
                prompt = arguments.get("prompt") if isinstance(arguments, dict) else None
                if not isinstance(prompt, str) or not prompt.strip():
                    span.set_attribute("af.agent.outcome", "error")
                    span.set_error("Missing 'prompt'", fault_domain=FaultDomain.APP)
                    return json.dumps({"error": "Missing 'prompt'"})

                session_id = (
                    _extract_mcp_session_id(payload) if isinstance(payload, dict) else None
                )
                span.set_attribute("af.agent.session_id", session_id)
                result = await _run_builtin_agent(
                    prompt.strip(),
                    resolved=resolved,
                    capabilities=capabilities,
                    session_id=session_id,
                    workflows_enabled=workflows_enabled,
                    workflow_system_addendum=workflow_system_addendum,
                    durable_client=durable_client,
                    catalog=catalog,
                    session_runtime=session_runtime,
                    owner=FunctionAppPrincipal() if session_runtime is not None else None,
                    workflow_policy=workflow_policy,
                )
                # When the caller supplies no explicit session id (`session_id`
                # is `None` above), the runner still resolves/generates one for
                # this turn (`result.session_id`) — refresh the span attribute
                # with it (N1) instead of leaving the pre-call `None` in place,
                # which otherwise left this attribute permanently unset for
                # every caller-omitted-session-id turn.
                span.set_attribute("af.agent.session_id", result.session_id)
                _set_run_result_attributes(span, result)
                span.set_attribute("af.agent.outcome", "success")
                return json.dumps(
                    {
                        "session_id": result.session_id,
                        "response": result.content,
                        "tool_calls": result.tool_calls,
                    }
                )
            except Exception as exc:
                span.set_attribute("af.agent.outcome", "error")
                span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                error_msg = _format_exception_message(exc)
                logger.error(
                    "Built-in MCP error: source_file=%s error=%s",
                    source_marker(resolved.source_file),
                    error_msg,
                )
                return json.dumps({"error": error_msg})

    decorated: Any
    if workflows_enabled:
        decorated = _mcp_agent_chat_handler_with_client(handle_mcp_agent_chat)
        decorated = app.durable_client_input(client_name="client")(decorated)
    else:
        decorated = _mcp_agent_chat_handler_without_client(handle_mcp_agent_chat)

    decorated = app.mcp_tool_trigger(
        arg_name="context",
        tool_name=tool_name,
        description=resolved.description,
        tool_properties=_MCP_AGENT_TOOL_PROPERTIES,
    )(decorated)
    app.function_name(name=function_name)(decorated)


def _register_workflow_status_endpoints(
    app: func.FunctionApp,
    *,
    slug: str,
    base_function_name: str,
    auth: EndpointAuthConfig,
) -> None:
    from ..workflows.workflow_tools import (
        fetch_session_workflow_status,
        fetch_session_workflows,
    )

    auth_level = resolve_endpoint_auth_level(auth)

    async def list_session_workflows(req: Request, client: str) -> Response:
        auth_error = authorize_entra_request(req.headers.get, auth)
        if auth_error is not None:
            return _json_error(auth_error.message, status_code=auth_error.status_code)
        session_id = req.headers.get("x-ms-session-id") or ""
        if not session_id:
            return Response(
                json.dumps({"workflows": []}),
                media_type="application/json",
            )
        try:
            envelopes = await fetch_session_workflows(client, session_id)
        except Exception:
            logger.exception("workflows list endpoint failed")
            return Response(
                json.dumps({"error": "failed to list workflows"}),
                status_code=500,
                media_type="application/json",
            )
        return Response(
            json.dumps({"workflows": envelopes}),
            media_type="application/json",
        )

    decorated_list = app.function_name(name=f"{base_function_name}_workflows")(
        list_session_workflows
    )
    decorated_list = app.durable_client_input(client_name="client")(decorated_list)
    app.route(route=f"agents/{slug}/workflows", methods=["GET"], auth_level=auth_level)(
        decorated_list
    )

    async def get_session_workflow_status(req: Request, client: str) -> Response:
        auth_error = authorize_entra_request(req.headers.get, auth)
        if auth_error is not None:
            return _json_error(auth_error.message, status_code=auth_error.status_code)
        session_id = req.headers.get("x-ms-session-id") or ""
        workflow_id = (req.query_params or {}).get("workflow_id", "") or ""
        if not session_id or not workflow_id:
            return Response(
                json.dumps({"error": "missing session or workflow_id"}),
                status_code=400,
                media_type="application/json",
            )
        try:
            envelope = await fetch_session_workflow_status(client, session_id, workflow_id)
        except Exception:
            logger.exception("workflow status endpoint failed")
            return Response(
                json.dumps({"error": "failed to fetch workflow status"}),
                status_code=500,
                media_type="application/json",
            )
        if envelope is None:
            return Response(
                json.dumps({"error": "workflow not found"}),
                status_code=404,
                media_type="application/json",
            )
        return Response(json.dumps(envelope), media_type="application/json")

    decorated_status = app.function_name(name=f"{base_function_name}_workflow_status")(
        get_session_workflow_status
    )
    decorated_status = app.durable_client_input(client_name="client")(decorated_status)
    app.route(route=f"agents/{slug}/workflow-status", methods=["GET"], auth_level=auth_level)(
        decorated_status
    )


def _register_history_endpoint(
    app: func.FunctionApp,
    *,
    slug: str,
    base_function_name: str,
    auth: EndpointAuthConfig,
) -> None:
    """Register a read-only endpoint that returns a session's persisted transcript.

    The built-in chat UI calls this to repaint prior user/assistant messages when a
    user resumes an older session. It reads the same blob history the runtime writes
    each turn; when no storage is configured it degrades to an empty transcript so
    older deployments (and local runs without storage) keep working.
    """
    auth_level = resolve_endpoint_auth_level(auth)

    async def get_session_history(req: Request) -> Response:
        auth_error = authorize_entra_request(req.headers.get, auth)
        if auth_error is not None:
            return _json_error(auth_error.message, status_code=auth_error.status_code)
        session_id = req.headers.get("x-ms-session-id") or ""
        if not session_id:
            return Response(
                json.dumps({"messages": [], "truncated": False}),
                media_type="application/json",
            )
        # session_id becomes part of the blob path; reject anything unsafe.
        if not _SAFE_SESSION_ID_PATTERN.match(session_id):
            return Response(
                json.dumps({"error": "invalid session id"}),
                status_code=400,
                media_type="application/json",
            )

        from .._blob_history import build_blob_provider_from_environment

        provider = build_blob_provider_from_environment()
        if provider is None:
            return Response(
                json.dumps({"messages": [], "truncated": False}),
                media_type="application/json",
            )
        # Present a clean transcript: drop internal/excluded turns.
        provider.skip_excluded = True
        try:
            messages = await provider.get_messages(session_id)
        except Exception:
            logger.exception("history endpoint failed")
            return Response(
                json.dumps({"error": "failed to load history"}),
                status_code=500,
                media_type="application/json",
            )

        rendered: list[dict[str, str]] = []
        for message in messages:
            role = str(getattr(message, "role", "") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            text = getattr(message, "text", "")
            if not isinstance(text, str) or not text:
                continue
            rendered.append({"role": role, "text": text})

        truncated = len(rendered) > _MAX_HISTORY_REPLAY_MESSAGES
        if truncated:
            rendered = rendered[-_MAX_HISTORY_REPLAY_MESSAGES:]
        return Response(
            json.dumps({"messages": rendered, "truncated": truncated}),
            media_type="application/json",
        )

    # Order mirrors the chat/chatstream endpoints (route first, then
    # function_name) so the applied name is idiomatic `@app.function_name`
    # above `@app.route`. This is also what lets the route be looked up by
    # its function name in tests.
    decorated = app.route(
        route=f"agents/{slug}/history", methods=["GET"], auth_level=auth_level
    )(get_session_history)
    app.function_name(name=f"{base_function_name}_history")(decorated)


def register_builtin_endpoints(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    slug: str | None = None,
    *,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    catalog: AgentCatalog | None = None,
    session_runtime: SessionExecutionRuntime | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> None:
    """Register built-in debug chat UI, REST chat, and MCP endpoints for one agent."""

    slug = slug or _function_name_from_source(resolved.source_file, resolved.name)
    builtin_endpoints = resolved.builtin_endpoints

    base_function_name = _safe_function_name(f"agent_{slug}_builtin")
    auth = builtin_endpoints.http_auth

    if builtin_endpoints.debug_chat_ui:
        route = f"agents/{slug}/"
        _register_chat_page(
            app,
            resolved,
            function_name=f"{base_function_name}_chat_page",
            route=route,
        )

    if builtin_endpoints.chat_api:
        chat_route = f"agents/{slug}/chat"
        stream_route = f"agents/{slug}/chatstream"
        _register_http_chat(
            app,
            resolved,
            capabilities,
            route=chat_route,
            function_name=f"{base_function_name}_chat",
            auth=auth,
            workflows_enabled=workflows_enabled,
            workflow_system_addendum=workflow_system_addendum,
            catalog=catalog,
            session_runtime=session_runtime,
            workflow_policy=workflow_policy,
        )
        _register_http_chat_stream(
            app,
            resolved,
            capabilities,
            route=stream_route,
            function_name=f"{base_function_name}_chatstream",
            auth=auth,
            workflows_enabled=workflows_enabled,
            workflow_system_addendum=workflow_system_addendum,
            catalog=catalog,
            session_runtime=session_runtime,
            workflow_policy=workflow_policy,
        )
        _register_history_endpoint(
            app,
            slug=slug,
            base_function_name=base_function_name,
            auth=auth,
        )
        if workflows_enabled:
            _register_workflow_status_endpoints(
                app,
                slug=slug,
                base_function_name=base_function_name,
                auth=auth,
            )

    if builtin_endpoints.mcp:
        _register_mcp_endpoint(
            app,
            resolved,
            capabilities,
            tool_name=slug,
            function_name=f"{base_function_name}_mcp",
            workflows_enabled=workflows_enabled,
            workflow_system_addendum=workflow_system_addendum,
            catalog=catalog,
            session_runtime=session_runtime,
            workflow_policy=workflow_policy,
        )


def register_session_management_endpoints(
    app: func.FunctionApp,
    *,
    slug: str,
    auth: EndpointAuthConfig,
    session_runtime: SessionExecutionRuntime,
    binding: AgentBinding,
) -> None:
    """Register one authenticated status/result/events/cancel set for a session runtime."""
    base_function_name = _safe_function_name(f"agent_{slug}_session_management")
    route_base = f"agents/{slug}/sessions/{{session_id}}/runs/{{run_id}}"

    async def authorized_context(
        req: Request,
    ) -> tuple[OwnerPrincipal | None, RunContext] | Response:
        owner, auth_error = _resolve_session_owner(req.headers.get, auth, session_runtime)
        if auth_error is not None:
            return _json_error(auth_error.message, status_code=auth_error.status_code)
        path_params = getattr(req, "path_params", {}) or {}
        session_id = path_params.get("session_id")
        run_id = path_params.get("run_id")
        if not isinstance(session_id, str) or not isinstance(run_id, str):
            return _json_error("missing session or run identifier", status_code=400)
        try:
            return owner, RunContext(
                session_id=validate_session_id(session_id),
                run_id=validate_run_id(run_id),
            )
        except (SessionStateContractError, ValueError):
            return _json_error("invalid session or run identifier", status_code=400)

    def backend_for(owner: OwnerPrincipal | None) -> Any:
        return create_execution_backend(
            binding=binding,
            session_runtime=session_runtime,
            owner=owner,
        )

    def touch_for(
        owner: OwnerPrincipal | None,
        context: RunContext,
    ) -> Callable[[], Awaitable[None]] | None:
        if not is_aca_sandbox_runtime(session_runtime):
            return None
        owner_context = resolve_owner_context(session_runtime.app_identity, slug, owner)
        return lambda: touch_session_activity(session_runtime, owner_context, context.session_id)

    async def status_handler(req: Request) -> Response:
        resolved = await authorized_context(req)
        if isinstance(resolved, Response):
            return resolved
        owner, context = resolved
        response = await read_status(
            backend_for(owner),
            context,
            touch=touch_for(owner, context),
        )
        return _controller_response_to_fastapi(response)

    async def result_handler(req: Request) -> Response:
        resolved = await authorized_context(req)
        if isinstance(resolved, Response):
            return resolved
        owner, context = resolved
        response = await read_result(
            backend_for(owner),
            context,
            touch=touch_for(owner, context),
        )
        return _controller_response_to_fastapi(response)

    async def events_handler(req: Request) -> Response:
        resolved = await authorized_context(req)
        if isinstance(resolved, Response):
            return resolved
        owner, context = resolved
        try:
            after_sequence = parse_last_event_id(req.headers)
        except ValueError as exc:
            return _json_error(str(exc), status_code=400)
        backend = backend_for(owner)
        preflight = await read_status(backend, context)
        if preflight.status_code != 200:
            return _controller_response_to_fastapi(preflight)
        body = preflight.body
        error = body.get("error") if isinstance(body, dict) else None
        if (
            isinstance(error, dict)
            and error.get("code") == SESSION_TOMBSTONED_ERROR_CODE
        ):
            return _controller_response_to_fastapi(preflight)
        touch = touch_for(owner, context)
        if touch is not None:
            await touch()
        return StreamingResponse(
            render_events(
                backend,
                context,
                after_sequence=after_sequence,
            ),
            media_type="text/event-stream",
        )

    async def cancel_handler(req: Request) -> Response:
        resolved = await authorized_context(req)
        if isinstance(resolved, Response):
            return resolved
        owner, context = resolved
        response = await cancel_controller_run(
            backend_for(owner),
            context,
            touch=touch_for(owner, context),
        )
        return _controller_response_to_fastapi(response)

    registrations: tuple[tuple[str, list[str], str, Any], ...] = (
        (route_base, ["GET"], f"{base_function_name}_status", status_handler),
        (f"{route_base}/result", ["GET"], f"{base_function_name}_result", result_handler),
        (f"{route_base}/events", ["GET"], f"{base_function_name}_events", events_handler),
        (f"{route_base}/cancel", ["POST"], f"{base_function_name}_cancel", cancel_handler),
    )
    for route, methods, function_name, handler in registrations:
        decorated = app.route(
            route=route,
            methods=methods,
            auth_level=resolve_endpoint_auth_level(auth),
        )(handler)
        app.function_name(name=function_name)(decorated)


_start_aca_stream = _start_session_stream
register_sandbox_management_endpoints = register_session_management_endpoints
