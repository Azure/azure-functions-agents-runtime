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


def _source_marker(source_file: str | None) -> str:
    if not source_file:
        return "<unknown>"
    return Path(str(source_file)).name


def _resolve_builtin_endpoints_session_id(session_id: str | None) -> str:
    return session_id or uuid.uuid4().hex


async def _run_builtin_agent(
    prompt: str,
    *,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    session_id: str | None,
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
        tools=capabilities.filtered_user_tools,
        mcp_tools=capabilities.filtered_mcp_tools,
        skill_paths=capabilities.enabled_skill_paths,
    )


def _run_builtin_agent_stream(
    prompt: str,
    *,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    session_id: str | None,
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
        tools=capabilities.filtered_user_tools,
        mcp_tools=capabilities.filtered_mcp_tools,
        skill_paths=capabilities.enabled_skill_paths,
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
    @app.route(
        route=route,
        methods=["GET"],
        auth_level=func.AuthLevel.ANONYMOUS,
    )
    def agent_chat_page(req: Request) -> Response:
        index_path = _index_path()
        if not index_path.exists():
            return Response("index.html not found", status_code=404)

        return Response(
            index_path.read_text(encoding="utf-8"),
            status_code=200,
            media_type="text/html",
        )

    app.function_name(name=function_name)(agent_chat_page)


def _register_http_chat(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    *,
    route: str,
    function_name: str,
) -> None:
    @app.route(route=route, methods=["POST"])
    async def chat(req: Request) -> Response:
        try:
            body = await req.json()
            prompt = _extract_prompt_from_body(body)
            session_id = req.headers.get("x-ms-session-id")
            result = await _run_builtin_agent(
                prompt,
                resolved=resolved,
                capabilities=capabilities,
                session_id=session_id,
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
                _source_marker(resolved.source_file),
                error_msg,
            )
            return _json_error(error_msg)

    app.function_name(name=function_name)(chat)


def _register_http_chat_stream(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    *,
    route: str,
    function_name: str,
) -> None:
    @app.route(route=route, methods=["POST"])
    async def chat_stream(req: Request) -> StreamingResponse:
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
                ),
                media_type="text/event-stream",
            )
        except ValueError as exc:
            return _sse_error_response(str(exc), status_code=400)
        except Exception as exc:
            error_msg = _format_exception_message(exc)
            logger.error(
                "Built-in chat stream error: source_file=%s error=%s",
                _source_marker(resolved.source_file),
                error_msg,
            )
            return _sse_error_response(error_msg, status_code=500)

    app.function_name(name=function_name)(chat_stream)


def _register_mcp_endpoint(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    *,
    tool_name: str,
    function_name: str,
) -> None:
    @app.mcp_tool_trigger(
        arg_name="context",
        tool_name=tool_name,
        description=resolved.description,
        tool_properties=_MCP_AGENT_TOOL_PROPERTIES,
    )
    async def mcp_agent_chat(context: str) -> str:
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
                _source_marker(resolved.source_file),
                error_msg,
            )
            return json.dumps({"error": error_msg})

    app.function_name(name=function_name)(mcp_agent_chat)


def register_builtin_endpoints(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    slug: str | None = None,
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
        )
        _register_http_chat_stream(
            app,
            resolved,
            capabilities,
            route=stream_route,
            function_name=f"{base_function_name}_chatstream",
        )

    if builtin_endpoints.mcp:
        _register_mcp_endpoint(
            app,
            resolved,
            capabilities,
            tool_name=slug,
            function_name=f"{base_function_name}_mcp",
        )
