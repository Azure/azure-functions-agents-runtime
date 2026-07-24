"""Built-in HTTP/UI/MCP endpoint registration for resolved agents."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import azure.functions as func
from azurefunctions.extensions.http.fastapi import Request, Response, StreamingResponse

from .._logger import logger
from .._observability import FaultDomain, LifecycleStage, start_span
from .._session_id import SESSION_ID_PATTERN
from .._source_marker import source_marker
from ..config import EndpointAuthConfig, ResolvedAgent
from ._auth import authorize_entra_request, resolve_endpoint_auth_level
from ._handlers import _set_run_result_attributes, build_sandbox_tools_for_session
from ._naming import _function_name_from_source, _safe_function_name
from .capabilities import AgentCapabilities
from .catalog import AgentCatalog

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
type ChatStreamHandler = Callable[[Request, Any | None], Awaitable[StreamingResponse]]
type McpAgentChatHandler = Callable[[str, Any | None], Awaitable[str]]


def _format_exception_message(exc: Exception) -> str:
    message = str(exc)
    return message if message else f"{type(exc).__name__}: {exc!r}"


async def _run_agent(*args: Any, **kwargs: Any) -> Any:
    from importlib import import_module

    runner_module = import_module("azure_functions_agents.runner")
    return await runner_module.run_agent(*args, **kwargs)


def _run_agent_stream(*args: Any, **kwargs: Any) -> Any:
    from importlib import import_module

    runner_module = import_module("azure_functions_agents.runner")
    return runner_module.run_agent_stream(*args, **kwargs)


# The runner uses the session id as a filename component, so it rejects anything
# outside this safe set. Shared with the runner via ``_session_id`` (a tiny,
# dependency-free module) so this layer stays valid without eagerly importing
# the heavy ``runner`` module.
_SAFE_SESSION_ID_PATTERN = SESSION_ID_PATTERN


def _extract_mcp_session_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("sessionId") or payload.get("sessionid")
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if _SAFE_SESSION_ID_PATTERN.match(value):
        return value
    # The MCP extension mints its own transport session id (e.g. the
    # streamable-HTTP ``Mcp-Session-Id``), whose format we do not control and
    # which may contain characters the runner rejects or exceed its length
    # cap. Map any such value deterministically into the safe space so the same
    # MCP session still resolves to the same agent session (conversation
    # continuity) without tripping the runner's validation.
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"mcp-{digest}"


def _index_path() -> Path:
    return Path(__file__).resolve().parent.parent / "public" / "index.html"


def _resolve_builtin_endpoints_session_id(session_id: str | None) -> str:
    return session_id or uuid.uuid4().hex


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
) -> Callable[[Request, str], Awaitable[StreamingResponse]]:
    async def chat_stream(req: Request, client: str) -> StreamingResponse:
        return await handle_chat_stream(req, client)

    return chat_stream


def _chat_stream_handler_without_client(
    handle_chat_stream: ChatStreamHandler,
) -> Callable[[Request], Awaitable[StreamingResponse]]:
    async def chat_stream(req: Request) -> StreamingResponse:
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
) -> Any:
    resolved_session_id = _resolve_builtin_endpoints_session_id(session_id)
    sandbox_tools = build_sandbox_tools_for_session(resolved, resolved_session_id)
    return await _run_agent(
        prompt,
        instructions=resolved.instructions,
        timeout=resolved.timeout,
        model=resolved.model,
        session_id=resolved_session_id,
        sandbox_tools=sandbox_tools,
        web_request_tools=capabilities.web_request_tools,
        tools=capabilities.filtered_user_tools,
        mcp_tools=capabilities.filtered_mcp_tools,
        skill_paths=capabilities.enabled_skill_paths,
        system_addendum=workflow_system_addendum,
        workflow_enabled=workflows_enabled,
        workflow_durable_client=durable_client,
        agent_name=resolved.slug,
        subagents=resolved.subagents,
        catalog=catalog,
    )


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
) -> Any:
    resolved_session_id = _resolve_builtin_endpoints_session_id(session_id)
    sandbox_tools = build_sandbox_tools_for_session(resolved, resolved_session_id)
    return _run_agent_stream(
        prompt,
        instructions=resolved.instructions,
        timeout=resolved.timeout,
        model=resolved.model,
        session_id=resolved_session_id,
        sandbox_tools=sandbox_tools,
        web_request_tools=capabilities.web_request_tools,
        tools=capabilities.filtered_user_tools,
        mcp_tools=capabilities.filtered_mcp_tools,
        skill_paths=capabilities.enabled_skill_paths,
        system_addendum=workflow_system_addendum,
        workflow_enabled=workflows_enabled,
        workflow_durable_client=durable_client,
        agent_name=resolved.slug,
        # S1b: `_register_http_chat_stream`'s `handle_chat_stream` (unlike
        # `handle_chat`/`handle_mcp_agent_chat` above) opens no span of its
        # own around this call, so `run_agent_stream`'s own internal
        # `agent.run {name}` span is the only place `af.agent.display_name`
        # can be recorded for the streaming surface — thread it through.
        display_name=resolved.name,
        subagents=resolved.subagents,
        catalog=catalog,
    )


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
) -> None:
    async def handle_chat(req: Request, durable_client: Any | None) -> Response:
        resolved_session_id = _resolve_builtin_endpoints_session_id(
            req.headers.get("x-ms-session-id")
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
            try:
                auth_error = authorize_entra_request(req.headers.get, auth)
                if auth_error is not None:
                    span.set_attribute("af.agent.outcome", "error")
                    span.set_error(auth_error.message, fault_domain=FaultDomain.APP)
                    return _json_error(auth_error.message, status_code=auth_error.status_code)
                body = await req.json()
                prompt = _extract_prompt_from_body(body)
                result = await _run_builtin_agent(
                    prompt,
                    resolved=resolved,
                    capabilities=capabilities,
                    session_id=resolved_session_id,
                    workflows_enabled=workflows_enabled,
                    workflow_system_addendum=workflow_system_addendum,
                    durable_client=durable_client,
                    catalog=catalog,
                )
                _set_run_result_attributes(span, result)
                span.set_attribute("af.agent.outcome", "success")
                return Response(
                    json.dumps(
                        {
                            "session_id": result.session_id,
                            "response": result.content,
                            "tool_calls": result.tool_calls,
                        }
                    ),
                    media_type="application/json",
                    headers={"x-ms-session-id": result.session_id},
                )
            except ValueError as exc:
                span.set_attribute("af.agent.outcome", "error")
                span.set_error(str(exc), fault_domain=FaultDomain.APP)
                return _json_error(str(exc), status_code=400)
            except Exception as exc:
                span.set_attribute("af.agent.outcome", "error")
                span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                error_msg = _format_exception_message(exc)
                logger.error(
                    "Built-in chat API error: source_file=%s error=%s",
                    source_marker(resolved.source_file),
                    error_msg,
                )
                return _json_error(error_msg)

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
) -> None:
    async def handle_chat_stream(
        req: Request,
        durable_client: Any | None,
    ) -> StreamingResponse:
        try:
            auth_error = authorize_entra_request(req.headers.get, auth)
            if auth_error is not None:
                return _sse_error_response(auth_error.message, status_code=auth_error.status_code)
            body = await req.json()
            prompt = _extract_prompt_from_body(body)
            session_id = req.headers.get("x-ms-session-id")
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
    from ..workflows.tools import (
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


def register_builtin_endpoints(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    slug: str | None = None,
    *,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    catalog: AgentCatalog | None = None,
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
        )
