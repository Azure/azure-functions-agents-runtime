"""
Azure Functions + Microsoft Agent Framework — app factory.

Call ``create_function_app()`` to build a fully-configured FunctionApp
with HTTP routes, MCP tool, and dynamic triggers from agent markdown files.

This module is a thin orchestrator. Heavy lifting is delegated to:

* :mod:`app_analyzer` — discovers agent files, wires connector tools
* :mod:`translator` — maps frontmatter triggers to Azure Functions decorators
* :mod:`handlers` — async handler factories for triggered agents
* :mod:`client_manager` — chat client creation / provider auto-detection
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

import azure.functions as func
from azurefunctions.extensions.http.fastapi import Request, Response, StreamingResponse

from .app_analyzer import (
    discover_and_register_agents,
    load_agent_file,
    warn_if_legacy_runtime_field,
)
from .config import get_app_root, set_app_root
from .connector_tool_cache import configure_connector_tools
from .handlers import build_sandbox_tools_for_session
from .runner import run_agent, run_agent_stream

_MCP_AGENT_TOOL_PROPERTIES = json.dumps(
    [
        {
            "propertyName": "prompt",
            "propertyType": "string",
            "description": "Prompt text sent to the agent.",
            "isRequired": True,
            "isArray": False,
        },
    ]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_mcp_tool_name(raw_name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_").lower()
    if not normalized:
        return "agent_chat"
    if normalized[0].isdigit():
        return f"agent_{normalized}"
    return normalized


def _extract_session_id(body: Dict[str, Any]) -> Optional[str]:
    value = body.get("session_id") or body.get("sessionId")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _extract_mcp_session_id(payload: Dict[str, Any]) -> str | None:
    value = payload.get("sessionId") or payload.get("sessionid")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_function_app(app_root: Path | None = None) -> func.FunctionApp:
    """Build and return a fully-configured Azure Functions app.

    Parameters
    ----------
    app_root:
        Root directory of the agent project (contains ``main.agent.md``,
        ``tools/``, ``skills/``, etc.). When *None*, falls back to the
        ``AZURE_FUNCTIONS_AGENTS_APP_ROOT`` env var, then to
        ``AzureWebJobsScriptRoot``, then to the current working directory.
    """
    if app_root is not None:
        set_app_root(app_root)

    resolved_root = get_app_root()

    app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

    # ---- Load main agent (main.agent.md) ----
    main_agent = load_agent_file(resolved_root / "main.agent.md")

    # ---- Register triggered agents from *.agent.md ----
    discover_and_register_agents(app, resolved_root)

    # ---- Configure main agent (if present) ----
    main_sandbox_config: Optional[Dict[str, Any]] = None
    main_instructions: Optional[str] = None
    mcp_tool_name = "agent_chat"
    mcp_tool_description = "Run an agent chat turn with a prompt."

    if main_agent:
        metadata = main_agent["metadata"]
        warn_if_legacy_runtime_field(metadata, "main.agent.md")
        main_instructions = main_agent.get("content") or None

        mcp_tool_name = _safe_mcp_tool_name(
            str(metadata.get("name") or "agent_chat")
        )
        mcp_tool_description = str(
            metadata.get("description") or "Run an agent chat turn with a prompt."
        ).strip() or "Run an agent chat turn with a prompt."

        # ---- Configure connector tools from main agent frontmatter ----
        tools_from_connections = metadata.get("tools_from_connections")
        if isinstance(tools_from_connections, list):
            configure_connector_tools(tools_from_connections)

        # ---- Capture sandbox config (per-request tool construction) ----
        execution_sandbox = metadata.get("execution_sandbox")
        if isinstance(execution_sandbox, dict):
            main_sandbox_config = execution_sandbox
    else:
        logging.info(
            "No main.agent.md found — HTTP chat, MCP, and UI endpoints will return 404."
        )

    # ---- HTTP routes (always registered) ----

    @app.route(
        route="{*ignored}",
        methods=["GET"],
        auth_level=func.AuthLevel.ANONYMOUS,
    )
    def root_chat_page(req: Request) -> Response:
        """Serve the chat UI at the root route."""
        ignored = (req.path_params or {}).get("ignored", "")
        if ignored:
            return Response("Not found", status_code=404)

        if not main_agent:
            return Response("Not found", status_code=404)

        index_path = Path(__file__).parent / "public" / "index.html"
        if not index_path.exists():
            return Response("index.html not found", status_code=404)

        return Response(
            index_path.read_text(encoding="utf-8"),
            status_code=200,
            media_type="text/html",
        )

    @app.route(route="agent/chat", methods=["POST"])
    async def chat(req: Request) -> Response:
        """
        Chat endpoint — send a prompt, get a response.

        POST /agent/chat
        Headers:
            x-ms-session-id (optional): Session ID for multi-turn conversations
        Body: {"prompt": "What is 2+2?"}
        """
        try:
            body = await req.json()
            prompt = body.get("prompt")

            if not prompt:
                return Response(
                    json.dumps({"error": "Missing 'prompt'"}),
                    status_code=400,
                    media_type="application/json",
                )

            session_id = req.headers.get("x-ms-session-id")
            sandbox_tools = build_sandbox_tools_for_session(
                main_sandbox_config, session_id
            )

            result = await run_agent(
                prompt,
                instructions=main_instructions,
                session_id=session_id,
                sandbox_tools=sandbox_tools,
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

        except Exception as e:
            error_msg = str(e) if str(e) else f"{type(e).__name__}: {repr(e)}"
            logging.error(f"Chat error: {error_msg}")
            return Response(
                json.dumps({"error": error_msg}),
                status_code=500,
                media_type="application/json",
            )

    @app.route(route="agent/chatstream", methods=["POST"])
    async def chat_stream(req: Request) -> StreamingResponse:
        """
        Streaming chat endpoint — send a prompt, receive SSE events.

        POST /agent/chatstream
        Headers:
            x-ms-session-id (optional): Session ID for multi-turn conversations
        Body: {"prompt": "What is 2+2?"}

        Response: text/event-stream with events:
            data: {"type": "session", "session_id": "..."}
            data: {"type": "delta", "content": "partial text"}
            data: {"type": "tool_start", ...}
            data: {"type": "tool_end", ...}
            data: {"type": "done"}
        """
        try:
            body = await req.json()
            prompt = body.get("prompt")

            if not main_agent:

                async def no_agent_gen():
                    yield (
                        f"data: {json.dumps({'type': 'error', 'content': 'No main.agent.md found. Create a main.agent.md file in the app root to enable this endpoint.'})}\n\n"
                    )

                return StreamingResponse(
                    no_agent_gen(),
                    media_type="text/event-stream",
                    status_code=404,
                )

            if not prompt:

                async def error_gen():
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Missing prompt'})}\n\n"

                return StreamingResponse(
                    error_gen(), media_type="text/event-stream"
                )

            session_id = req.headers.get("x-ms-session-id")
            sandbox_tools = build_sandbox_tools_for_session(
                main_sandbox_config, session_id
            )

            return StreamingResponse(
                run_agent_stream(
                    prompt,
                    instructions=main_instructions,
                    session_id=session_id,
                    sandbox_tools=sandbox_tools,
                ),
                media_type="text/event-stream",
            )

        except Exception as e:
            error_msg = str(e) if str(e) else f"{type(e).__name__}: {repr(e)}"
            logging.error(f"Chat stream error: {error_msg}")

            async def error_gen():
                yield f"data: {json.dumps({'type': 'error', 'content': error_msg})}\n\n"

            return StreamingResponse(
                error_gen(), media_type="text/event-stream"
            )

    # ---- MCP tool (only when main agent exists) ----

    if main_agent:

        @app.mcp_tool_trigger(
            arg_name="context",
            tool_name=mcp_tool_name,
            description=mcp_tool_description,
            tool_properties=_MCP_AGENT_TOOL_PROPERTIES,
        )
        async def mcp_agent_chat(context: str) -> str:
            """MCP tool endpoint — runs the same agent workflow as /agent/chat."""
            try:
                payload = json.loads(context) if context else {}
                arguments = (
                    payload.get("arguments", {})
                    if isinstance(payload, dict)
                    else {}
                )

                prompt = (
                    arguments.get("prompt")
                    if isinstance(arguments, dict)
                    else None
                )
                if not isinstance(prompt, str) or not prompt.strip():
                    return json.dumps({"error": "Missing 'prompt'"})

                session_id = (
                    _extract_mcp_session_id(payload)
                    if isinstance(payload, dict)
                    else None
                )
                sandbox_tools = build_sandbox_tools_for_session(
                    main_sandbox_config, session_id
                )

                result = await run_agent(
                    prompt.strip(),
                    instructions=main_instructions,
                    session_id=session_id,
                    sandbox_tools=sandbox_tools,
                )

                return json.dumps(
                    {
                        "session_id": result.session_id,
                        "response": result.content,
                        "tool_calls": result.tool_calls,
                    }
                )
            except Exception as exc:
                error_msg = (
                    str(exc) if str(exc) else f"{type(exc).__name__}: {repr(exc)}"
                )
                logging.error(f"MCP tool error: {error_msg}")
                return json.dumps({"error": error_msg})

    return app
