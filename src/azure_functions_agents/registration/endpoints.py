"""Debug HTTP/UI/MCP endpoint registration for resolved agents.

Per-app non-main debug route slugs are tracked on the ``FunctionApp`` instance
via a private ``_afa_debug_slug_names`` mapping. Storing the registry on the app
keeps tests isolated without relying on global module state.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import azure.functions as func
from azurefunctions.extensions.http.fastapi import Request, Response, StreamingResponse

from .._logger import logger
from ..config import ResolvedAgent
from ..system_tools.connectors.cache import configure_connector_tools
from ._handlers import build_sandbox_tools_for_session, validate_request_body
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

_DEBUG_SLUG_ATTR = "_afa_debug_slug_names"


def _dump_connector_specs(resolved: ResolvedAgent) -> list[dict[str, Any]]:
    return [spec.model_dump() for spec in resolved.connector_specs]


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


def _safe_mcp_tool_name(raw_name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_").lower()
    if not normalized:
        return "agent_chat"
    if normalized[0].isdigit():
        return f"agent_{normalized}"
    return normalized


def _safe_function_name(raw_name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_")
    if not name:
        return "agent_function"
    if name[0].isdigit():
        return f"fn_{name}"
    return name


def _extract_mcp_session_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("sessionId") or payload.get("sessionid")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _debug_slug_registry(app: func.FunctionApp) -> dict[str, str]:
    registry = getattr(app, _DEBUG_SLUG_ATTR, None)
    if registry is None:
        registry = {}
        setattr(app, _DEBUG_SLUG_ATTR, registry)
    return registry


def reset_debug_slug_registry(app: func.FunctionApp) -> None:
    """Clear stored non-main debug slugs for ``app``.

    Tests can call this before registering a fresh set of agents on the same
    ``FunctionApp`` instance.
    """

    setattr(app, _DEBUG_SLUG_ATTR, {})


def _ensure_unique_non_main_slug(app: func.FunctionApp, resolved: ResolvedAgent) -> str:
    slug = _safe_mcp_tool_name(resolved.name)
    registry = _debug_slug_registry(app)
    existing_name = registry.get(slug)
    if existing_name is not None:
        raise ValueError(
            "Debug slug collision for non-main agents: "
            f"'{existing_name}' and '{resolved.name}' both map to '{slug}'"
        )
    registry[slug] = resolved.name
    return slug


def _configure_connector_tools_if_needed(
    resolved: ResolvedAgent, capabilities: AgentCapabilities
) -> None:
    if not capabilities.use_connector_tools or not resolved.connector_specs:
        return
    configure_connector_tools(_dump_connector_specs(resolved))


def _index_path() -> Path:
    return Path(__file__).resolve().parent.parent / "public" / "index.html"


async def _run_debug_agent(
    prompt: str,
    *,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    session_id: str | None,
) -> Any:
    sandbox_tools = build_sandbox_tools_for_session(resolved, session_id)
    return await _run_agent(
        prompt,
        instructions=resolved.instructions,
        timeout=resolved.timeout,
        model=resolved.model,
        session_id=session_id,
        sandbox_tools=sandbox_tools,
        tools=capabilities.filtered_user_tools,
        mcp_tools=capabilities.filtered_mcp_tools,
        skills_text=capabilities.skills_text,
        use_connector_tools=capabilities.use_connector_tools,
    )


def _run_debug_agent_stream(
    prompt: str,
    *,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    session_id: str | None,
) -> Any:
    sandbox_tools = build_sandbox_tools_for_session(resolved, session_id)
    return _run_agent_stream(
        prompt,
        instructions=resolved.instructions,
        timeout=resolved.timeout,
        model=resolved.model,
        session_id=session_id,
        sandbox_tools=sandbox_tools,
        tools=capabilities.filtered_user_tools,
        mcp_tools=capabilities.filtered_mcp_tools,
        skills_text=capabilities.skills_text,
        use_connector_tools=capabilities.use_connector_tools,
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
    if resolved.is_main:

        @app.route(
            route=route,
            methods=["GET"],
            auth_level=func.AuthLevel.ANONYMOUS,
        )
        def root_chat_page(req: Request) -> Response:
            """Serve the main chat UI catch-all at ``/`` only."""

            ignored = (req.path_params or {}).get("ignored", "")
            if ignored:
                return Response("Not found", status_code=404)

            index_path = _index_path()
            if not index_path.exists():
                return Response("index.html not found", status_code=404)

            return Response(
                index_path.read_text(encoding="utf-8"),
                status_code=200,
                media_type="text/html",
            )

        app.function_name(name=function_name)(root_chat_page)
        return

    # Azure Functions route matching is expected to prefer this literal route
    # over the main agent's ``{*ignored}`` catch-all when both are present.
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
            validation_error = validate_request_body(body, resolved.input_schema)
            if validation_error is not None:
                return validation_error
            prompt = _extract_prompt_from_body(body)
            session_id = req.headers.get("x-ms-session-id")
            result = await _run_debug_agent(
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
            logger.error("Debug chat error for '%s': %s", resolved.name, error_msg)
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
            validation_error = validate_request_body(body, resolved.input_schema)
            if validation_error is not None:
                payload = json.loads(validation_error.body.decode("utf-8"))
                message = (
                    payload.get("details") or payload.get("error") or "Input validation failed"
                )
                return _sse_error_response(
                    message,
                    status_code=validation_error.status_code,
                )
            prompt = _extract_prompt_from_body(body)
            session_id = req.headers.get("x-ms-session-id")
            return StreamingResponse(
                _run_debug_agent_stream(
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
            logger.error("Debug chat stream error for '%s': %s", resolved.name, error_msg)
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
            result = await _run_debug_agent(
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
            logger.error("Debug MCP error for '%s': %s", resolved.name, error_msg)
            return json.dumps({"error": error_msg})

    app.function_name(name=function_name)(mcp_agent_chat)


def register_debug_endpoints(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
) -> None:
    """Register debug chat UI, REST chat, and MCP endpoints for one agent."""

    _configure_connector_tools_if_needed(resolved, capabilities)

    slug = _safe_mcp_tool_name(resolved.name)
    if not resolved.is_main and (resolved.debug.chat or resolved.debug.http or resolved.debug.mcp):
        slug = _ensure_unique_non_main_slug(app, resolved)

    base_function_name = _safe_function_name(
        ("main" if resolved.is_main else f"agent_{slug}") + "_debug"
    )

    if resolved.debug.chat:
        route = "{*ignored}" if resolved.is_main else f"agents/{slug}/"
        _register_chat_page(
            app,
            resolved,
            function_name=f"{base_function_name}_chat_page",
            route=route,
        )
        logger.info("Registered debug chat page for '%s' at /%s", resolved.name, route)

    if resolved.debug.http:
        chat_route = "agent/chat" if resolved.is_main else f"agents/{slug}/chat"
        stream_route = "agent/chatstream" if resolved.is_main else f"agents/{slug}/chatstream"
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
        logger.info(
            "Registered debug HTTP endpoints for '%s' at /%s and /%s",
            resolved.name,
            chat_route,
            stream_route,
        )

    if resolved.debug.mcp:
        _register_mcp_endpoint(
            app,
            resolved,
            capabilities,
            tool_name=slug,
            function_name=f"{base_function_name}_mcp",
        )
        logger.info("Registered debug MCP tool '%s' for '%s'", slug, resolved.name)
