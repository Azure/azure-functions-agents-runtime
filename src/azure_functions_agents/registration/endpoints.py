"""Built-in HTTP/UI/MCP endpoint registration for resolved agents.

Per-app built-in endpoint slugs are tracked on the ``FunctionApp`` instance
via a private ``_afa_builtin_slug_names`` set. Storing the registry on the app
keeps tests isolated without relying on global module state.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import azure.functions as func
from azurefunctions.extensions.http.fastapi import Request, Response, StreamingResponse

from .._logger import logger
from .._source_marker import source_marker
from ..config import ResolvedAgent
from ._handlers import build_sandbox_tools_for_session
from ._naming import _function_name_from_source, _safe_function_name, allocate_unique_builtin_slug
from .capabilities import AgentCapabilities

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

_BUILTIN_SLUG_ATTR = "_afa_builtin_slug_names"


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


def _extract_mcp_session_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("sessionId") or payload.get("sessionid")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _builtin_slug_registry(app: func.FunctionApp) -> set[str]:
    registry = getattr(app, _BUILTIN_SLUG_ATTR, None)
    if registry is None:
        registry = set()
        setattr(app, _BUILTIN_SLUG_ATTR, registry)
    return registry


def reset_builtin_slug_registry(app: func.FunctionApp) -> None:
    """Clear stored built-in endpoint slugs for ``app``.

    Tests can call this before registering a fresh set of agents on the same
    ``FunctionApp`` instance.
    """

    setattr(app, _BUILTIN_SLUG_ATTR, set())


def _ensure_unique_slug(app: func.FunctionApp, resolved: ResolvedAgent) -> str:
    return allocate_unique_builtin_slug(
        resolved.source_file,
        resolved.name,
        _builtin_slug_registry(app),
    )


def _index_path() -> Path:
    return Path(__file__).resolve().parent.parent / "public" / "index.html"


def _resolve_builtin_endpoints_session_id(session_id: str | None) -> str:
    return session_id or uuid.uuid4().hex


async def _run_builtin_agent(
    prompt: str,
    *,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    session_id: str | None,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    durable_client: Any | None = None,
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
        agent_name=resolved.name,
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
        agent_name=resolved.name,
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
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
) -> None:
    async def chat(req: Request, client: Any | None = None) -> Response:
        try:
            body = await req.json()
            prompt = _extract_prompt_from_body(body)
            session_id = req.headers.get("x-ms-session-id")
            result = await _run_builtin_agent(
                prompt,
                resolved=resolved,
                capabilities=capabilities,
                session_id=session_id,
                workflows_enabled=workflows_enabled,
                workflow_system_addendum=workflow_system_addendum,
                durable_client=client if workflows_enabled else None,
            )
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
            return _json_error(str(exc), status_code=400)
        except Exception as exc:
            error_msg = _format_exception_message(exc)
            logger.error(
                "Built-in chat API error: source_file=%s error=%s",
                source_marker(resolved.source_file),
                error_msg,
            )
            return _json_error(error_msg)

    decorated = chat
    if workflows_enabled:
        decorated = app.durable_client_input(client_name="client")(decorated)
    decorated = app.route(route=route, methods=["POST"])(decorated)
    app.function_name(name=function_name)(decorated)


def _register_http_chat_stream(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    *,
    route: str,
    function_name: str,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
) -> None:
    async def chat_stream(req: Request, client: Any | None = None) -> StreamingResponse:
        try:
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
                    durable_client=client if workflows_enabled else None,
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

    decorated = chat_stream
    if workflows_enabled:
        decorated = app.durable_client_input(client_name="client")(decorated)
    decorated = app.route(route=route, methods=["POST"])(decorated)
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
) -> None:
    async def mcp_agent_chat(context: str, client: Any | None = None) -> str:
        try:
            payload = json.loads(context) if context else {}
            arguments = payload.get("arguments", {}) if isinstance(payload, dict) else {}
            prompt = arguments.get("prompt") if isinstance(arguments, dict) else None
            if not isinstance(prompt, str) or not prompt.strip():
                return json.dumps({"error": "Missing 'prompt'"})

            session_id = _extract_mcp_session_id(payload) if isinstance(payload, dict) else None
            result = await _run_builtin_agent(
                prompt.strip(),
                resolved=resolved,
                capabilities=capabilities,
                session_id=session_id,
                workflows_enabled=workflows_enabled,
                workflow_system_addendum=workflow_system_addendum,
                durable_client=client if workflows_enabled else None,
            )
            return json.dumps(
                {
                    "session_id": result.session_id,
                    "response": result.content,
                    "tool_calls": result.tool_calls,
                }
            )
        except Exception as exc:
            error_msg = _format_exception_message(exc)
            logger.error(
                "Built-in MCP error: source_file=%s error=%s",
                source_marker(resolved.source_file),
                error_msg,
            )
            return json.dumps({"error": error_msg})

    decorated = mcp_agent_chat
    if workflows_enabled:
        decorated = app.durable_client_input(client_name="client")(decorated)
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
) -> None:
    from ..workflows.tools import (
        fetch_session_workflow_status,
        fetch_session_workflows,
    )

    async def list_session_workflows(req: Request, client: Any) -> Response:
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
    app.route(route=f"agents/{slug}/workflows", methods=["GET"])(decorated_list)

    async def get_session_workflow_status(req: Request, client: Any) -> Response:
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
    app.route(route=f"agents/{slug}/workflow-status", methods=["GET"])(decorated_status)


def register_builtin_endpoints(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    slug: str | None = None,
    *,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
) -> None:
    """Register built-in debug chat UI, REST chat, and MCP endpoints for one agent."""

    slug = slug or _function_name_from_source(resolved.source_file, resolved.name)
    builtin_endpoints = resolved.builtin_endpoints
    if builtin_endpoints.debug_chat_ui or builtin_endpoints.chat_api or builtin_endpoints.mcp:
        if slug in _builtin_slug_registry(app):
            slug = _ensure_unique_slug(app, resolved)
        else:
            _builtin_slug_registry(app).add(slug)

    base_function_name = _safe_function_name(f"agent_{slug}_builtin")

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
            workflows_enabled=workflows_enabled,
            workflow_system_addendum=workflow_system_addendum,
        )
        _register_http_chat_stream(
            app,
            resolved,
            capabilities,
            route=stream_route,
            function_name=f"{base_function_name}_chatstream",
            workflows_enabled=workflows_enabled,
            workflow_system_addendum=workflow_system_addendum,
        )
        if workflows_enabled:
            _register_workflow_status_endpoints(
                app,
                slug=slug,
                base_function_name=base_function_name,
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
        )
