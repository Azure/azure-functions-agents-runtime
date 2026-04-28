"""
Azure Functions + GitHub Copilot SDK — app factory.

Call ``create_function_app()`` to build a fully-configured FunctionApp
with HTTP routes, MCP tool, and dynamic triggers from agent markdown files.

Agent files:
  - ``main.agent.md`` — primary agent (chat endpoints, MCP, UI). Optional.
  - ``<name>.agent.md`` — triggered agents with exactly one trigger each.
"""

import glob
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import azure.functions as func
import frontmatter

from .config import get_app_root, set_app_root, resolve_env_var, substitute_env_vars_in_text, _to_bool
from .connector_tool_cache import configure_connector_tools
from .runner import run_copilot_agent, run_copilot_agent_stream
from .sandbox import create_sandbox_tools
from azurefunctions.extensions.http.fastapi import Request, Response, StreamingResponse

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

def _load_agent_file(path: Path) -> Optional[Dict[str, Any]]:
    """Parse an agent markdown file and return its metadata + content.

    Returns a dict with 'metadata' (frontmatter dict) and 'content' (body str),
    or None if the file doesn't exist or can't be parsed.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = frontmatter.loads(raw)
        metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
        content = (parsed.content or "").strip()

        # Apply inline env-var substitution unless explicitly disabled
        if _to_bool(metadata.get("substitute_variables"), default=True):
            content = substitute_env_vars_in_text(content)

        return {"metadata": metadata, "content": content}
    except Exception as exc:
        logging.warning(f"Failed to parse {path.name}: {exc}")
        return None


def _safe_mcp_tool_name(raw_name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_").lower()
    if not normalized:
        return "agent_chat"
    if normalized[0].isdigit():
        return f"agent_{normalized}"
    return normalized


def _extract_mcp_session_id(payload: Dict[str, Any]) -> str | None:
    """Extract MCP session id from top-level context payload only."""
    value = payload.get("sessionId") or payload.get("sessionid")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _safe_function_name(raw_name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_")
    if not name:
        return "agent_function"
    if name[0].isdigit():
        return f"fn_{name}"
    return name


def _normalize_timer_schedule(schedule: str) -> str:
    """Accept 5-part cron by prepending seconds; keep 6-part schedules unchanged."""
    schedule_parts = schedule.strip().split()
    if len(schedule_parts) == 5:
        return f"0 {schedule.strip()}"
    return schedule.strip()


# _to_bool imported from .config


def _resolve_trigger_params(trigger_params: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve env vars on all string values in trigger params."""
    resolved = {}
    for key, value in trigger_params.items():
        if isinstance(value, str):
            resolved[key] = resolve_env_var(value)
        else:
            resolved[key] = value
    return resolved


# ---------------------------------------------------------------------------
# Triggered agent registration (*.agent.md files)
# ---------------------------------------------------------------------------

def _register_triggered_agents(app: func.FunctionApp, app_root: Path) -> None:
    """Discover and register triggered agents from *.agent.md files."""
    agent_files = sorted(glob.glob(str(app_root / "*.agent.md")))
    if not agent_files:
        logging.info("No agent files found.")
        return

    connectors_instance = None  # Lazy-init if needed
    registered_names: set = set()

    for agent_path_str in agent_files:
        agent_path = Path(agent_path_str)

        # Skip the main agent — it's handled separately
        if agent_path.name == "main.agent.md":
            continue

        agent = _load_agent_file(agent_path)
        if not agent:
            continue

        metadata = agent["metadata"]
        content = agent["content"]
        trigger_spec = metadata.get("trigger")

        if not isinstance(trigger_spec, dict) or "type" not in trigger_spec:
            logging.warning(f"Skipping {agent_path.name}: missing or invalid 'trigger' section (must have 'type')")
            continue

        # Extract trigger type and params
        trigger_type = str(trigger_spec["type"]).strip()
        trigger_params = {k: v for k, v in trigger_spec.items() if k != "type"}

        # Resolve env vars on string params
        trigger_params = _resolve_trigger_params(trigger_params)

        # Agent-level settings
        agent_name = metadata.get("name", agent_path.stem)
        should_log = _to_bool(metadata.get("logger", True), default=True)

        # Function name from filename
        base_name = _safe_function_name(agent_path.stem)
        function_name = base_name
        suffix = 2
        while function_name in registered_names:
            function_name = f"{base_name}_{suffix}"
            suffix += 1
        registered_names.add(function_name)

        # Per-agent connector tools (additive, deduplicated globally)
        agent_connections = metadata.get("tools_from_connections")
        if isinstance(agent_connections, list):
            configure_connector_tools(agent_connections)

        # Per-agent sandbox tools
        agent_sandbox_tools = []
        agent_sandbox = metadata.get("execution_sandbox")
        if isinstance(agent_sandbox, dict):
            agent_sandbox_tools = create_sandbox_tools(agent_sandbox)

        # Determine if this is a built-in trigger or connector trigger
        # Dot notation routes to the connectors library (e.g. "teams.new_channel_message_trigger").
        # "connectors." prefix is stripped if present (e.g. "connectors.generic_trigger" → "generic_trigger").
        is_connector = "." in trigger_type
        if is_connector:
            # Strip leading "connectors." prefix if present
            connector_type = trigger_type.removeprefix("connectors.")
            connectors_instance = _register_connector_agent(
                app, connectors_instance, function_name, agent_name,
                connector_type, trigger_params, content, should_log,
                sandbox_tools=agent_sandbox_tools,
            )
        else:
            # Built-in Azure Functions trigger
            _register_builtin_agent(
                app, function_name, agent_name,
                trigger_type, trigger_params, content, should_log,
                sandbox_tools=agent_sandbox_tools,
                response_example=metadata.get("response_example"),
                response_schema=metadata.get("response_schema"),
            )


def _register_builtin_agent(
    app: func.FunctionApp,
    function_name: str,
    agent_name: str,
    trigger_type: str,
    trigger_params: Dict[str, Any],
    prompt: str,
    should_log: bool,
    sandbox_tools: Optional[list] = None,
    response_example: Optional[str] = None,
    response_schema: Optional[dict] = None,
) -> None:
    """Register a triggered agent using a built-in Azure Functions trigger."""

    # HTTP triggers use a dedicated handler that returns func.HttpResponse
    if trigger_type == "http_trigger":
        _register_http_agent(
            app, function_name, agent_name, trigger_params, prompt,
            should_log, sandbox_tools=sandbox_tools,
            response_example=response_example, response_schema=response_schema,
        )
        return

    # Get the decorator method from the FunctionApp
    decorator_fn = getattr(app, trigger_type, None)
    if decorator_fn is None:
        logging.warning(f"Skipping '{function_name}': unknown trigger type '{trigger_type}'")
        return

    # Timer triggers: normalize schedule
    if trigger_type == "timer_trigger":
        if "schedule" in trigger_params:
            trigger_params["schedule"] = _normalize_timer_schedule(str(trigger_params["schedule"]))

    # Create handler
    handler = _make_agent_handler(function_name, agent_name, trigger_type, should_log, sandbox_tools=sandbox_tools, agent_instructions=prompt)

    # Register with auto-generated arg_name
    trigger_params["arg_name"] = "trigger_data"
    try:
        decorated = decorator_fn(**trigger_params)(handler)
        app.function_name(name=function_name)(decorated)
        logging.info(f"Registered '{function_name}' ({trigger_type}) — {agent_name}")
    except Exception as exc:
        logging.error(f"Failed to register '{function_name}' ({trigger_type}): {exc}")


_AUTH_LEVEL_MAP = {
    "anonymous": func.AuthLevel.ANONYMOUS,
    "function": func.AuthLevel.FUNCTION,
    "admin": func.AuthLevel.ADMIN,
}


def _register_http_agent(
    app: func.FunctionApp,
    function_name: str,
    agent_name: str,
    trigger_params: Dict[str, Any],
    prompt: str,
    should_log: bool,
    sandbox_tools: Optional[list] = None,
    response_example: Optional[str] = None,
    response_schema: Optional[dict] = None,
) -> None:
    """Register an HTTP-triggered agent using app.route()."""
    route = trigger_params.get("route")
    if not route:
        logging.warning(f"Skipping '{function_name}': http_trigger requires 'route'")
        return

    methods = trigger_params.get("methods", ["POST"])
    auth_str = str(trigger_params.get("auth_level", "FUNCTION")).lower()
    auth_level = _AUTH_LEVEL_MAP.get(auth_str, func.AuthLevel.FUNCTION)

    handler = _make_http_agent_handler(
        function_name, agent_name, should_log,
        sandbox_tools=sandbox_tools, agent_instructions=prompt,
        response_example=response_example, response_schema=response_schema,
    )

    try:
        decorated = app.route(route=route, methods=methods, auth_level=auth_level)(handler)
        app.function_name(name=function_name)(decorated)
        logging.info(f"Registered HTTP agent '{function_name}' at /{route} ({methods}) — {agent_name}")
    except Exception as exc:
        logging.error(f"Failed to register HTTP agent '{function_name}': {exc}")


def _register_connector_agent(
    app: func.FunctionApp,
    connectors_instance,
    function_name: str,
    agent_name: str,
    trigger_type: str,
    trigger_params: Dict[str, Any],
    prompt: str,
    should_log: bool,
    sandbox_tools: Optional[list] = None,
):
    """Register a triggered agent using a connector trigger.

    Returns the connectors instance (created lazily on first use).
    """
    if connectors_instance is None:
        try:
            import azure.functions_connectors as fc
            connectors_instance = fc.FunctionsConnectors(app)
        except ImportError:
            logging.error(
                f"Skipping '{function_name}': azure-functions-connectors package not installed. "
                "Install from: https://github.com/anthonychu/azure-functions-connectors-python"
            )
            return None

    # Resolve the decorator via getattr chain (e.g. "teams.new_channel_message_trigger")
    # For top-level methods like "generic_trigger", it's a single getattr
    parts = trigger_type.split(".")
    obj = connectors_instance
    try:
        for part in parts:
            obj = getattr(obj, part)
        decorator_fn = obj
    except AttributeError:
        logging.warning(f"Skipping '{function_name}': could not resolve connector trigger '{trigger_type}'")
        return connectors_instance

    handler = _make_agent_handler(function_name, agent_name, trigger_type, should_log, sandbox_tools=sandbox_tools, agent_instructions=prompt)

    try:
        decorator_fn(**trigger_params)(handler)
        logging.info(f"Registered '{function_name}' ({trigger_type}) — {agent_name}")
    except Exception as exc:
        logging.error(f"Failed to register '{function_name}' ({trigger_type}): {exc}")

    return connectors_instance


def _serialize_trigger_data(trigger_data) -> str:
    """Serialize trigger binding data to a JSON string."""
    if trigger_data is None:
        return "{}"
    if hasattr(trigger_data, "to_dict"):
        payload = trigger_data.to_dict()
    elif hasattr(trigger_data, "model_dump"):
        payload = trigger_data.model_dump()
    elif isinstance(trigger_data, dict):
        payload = trigger_data
    elif isinstance(trigger_data, str):
        return trigger_data
    else:
        payload = str(trigger_data)

    if isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False, default=str)
    return str(payload)


def _make_agent_handler(
    function_name: str,
    agent_name: str,
    trigger_type: str,
    should_log: bool,
    sandbox_tools: Optional[list] = None,
    agent_instructions: Optional[str] = None,
):
    """Create an async handler function for a triggered agent."""
    async def _handler(trigger_data):
        logging.info(f"Agent '{function_name}' triggered")

        try:
            data_json = _serialize_trigger_data(trigger_data)
            parts = []
            if agent_instructions:
                parts.append(agent_instructions)
            parts.append(f"Triggered by: {trigger_type}\n\nTrigger data:\n```json\n{data_json}\n```")
            prompt = "\n\n".join(parts)

            result = await run_copilot_agent(prompt, sandbox_tools=sandbox_tools)

            if should_log:
                logging.info(
                    "Agent '%s' response: %s",
                    function_name,
                    json.dumps(
                        {
                            "session_id": result.session_id,
                            "response": result.content,
                            "response_intermediate": result.content_intermediate,
                            "tool_calls": result.tool_calls,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                )
        except Exception as exc:
            logging.exception(f"Agent '{function_name}' failed: {exc}")

    _handler.__name__ = f"handler_{function_name}"
    return _handler


def _extract_json_from_response(text: str) -> str:
    """Extract JSON from an agent response, stripping markdown code fences if present."""
    stripped = text.strip()
    # Try to extract from ```json ... ``` or ``` ... ```
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", stripped, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


def _make_http_agent_handler(
    function_name: str,
    agent_name: str,
    should_log: bool,
    sandbox_tools: Optional[list] = None,
    agent_instructions: Optional[str] = None,
    response_example: Optional[str] = None,
    response_schema: Optional[dict] = None,
):
    """Create an async handler for an HTTP-triggered agent that returns structured JSON."""
    async def _handler(req: Request) -> Response:
        logging.info(f"HTTP agent '{function_name}' triggered")

        try:
            # Parse request body
            try:
                body = await req.json()
                body_json = json.dumps(body, ensure_ascii=False, default=str)
            except Exception:
                body_bytes = await req.body()
                body_json = body_bytes.decode("utf-8", errors="replace") if body_bytes else "{}"

            # Build prompt
            parts = []
            if agent_instructions:
                parts.append(agent_instructions)

            # Add response format instructions
            if response_example:
                parts.append(
                    "You MUST respond with ONLY a valid JSON object (no markdown, no explanation, no code fences). "
                    f"Your response must match this example format:\n```json\n{response_example}\n```"
                )
            elif response_schema:
                schema_str = json.dumps(response_schema, indent=2)
                parts.append(
                    "You MUST respond with ONLY a valid JSON object (no markdown, no explanation, no code fences). "
                    f"Your response must conform to this JSON Schema:\n```json\n{schema_str}\n```"
                )

            parts.append(f"HTTP request data:\n```json\n{body_json}\n```")
            prompt = "\n\n".join(parts)

            result = await run_copilot_agent(prompt, sandbox_tools=sandbox_tools)

            if should_log:
                logging.info(
                    "HTTP agent '%s' response: %s",
                    function_name,
                    json.dumps(
                        {"session_id": result.session_id, "response": result.content[:500]},
                        ensure_ascii=False, default=str,
                    ),
                )

            # If a response format was specified, parse as JSON
            if response_example or response_schema:
                extracted = _extract_json_from_response(result.content)
                try:
                    parsed = json.loads(extracted)
                    return Response(
                        content=json.dumps(parsed, ensure_ascii=False),
                        status_code=200,
                        media_type="application/json",
                    )
                except json.JSONDecodeError as je:
                    logging.warning(f"HTTP agent '{function_name}' returned invalid JSON: {je}")
                    return Response(
                        content=json.dumps({"error": "Agent returned invalid JSON", "raw_response": result.content}),
                        status_code=500,
                        media_type="application/json",
                    )
            else:
                # No schema — return raw text
                return Response(
                    content=result.content,
                    status_code=200,
                    media_type="text/plain",
                )

        except Exception as exc:
            logging.exception(f"HTTP agent '{function_name}' failed: {exc}")
            return Response(
                content=json.dumps({"error": str(exc)}),
                status_code=500,
                media_type="application/json",
            )

    _handler.__name__ = f"handler_{function_name}"
    return _handler


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_function_app(app_root: Path | None = None) -> func.FunctionApp:
    """Build and return a fully-configured Azure Functions app.

    Parameters
    ----------
    app_root:
        Root directory of the agent project (contains ``main.agent.md``,
        ``tools/``, ``skills/``, etc.).  When *None*, falls back to
        ``COPILOT_APP_ROOT`` env var or the current working directory.
    """
    if app_root is not None:
        set_app_root(app_root)

    resolved_root = get_app_root()

    app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

    # ---- Load main agent (main.agent.md) ----
    main_agent = _load_agent_file(resolved_root / "main.agent.md")

    # ---- Register triggered agents from *.agent.md ----
    _register_triggered_agents(app, resolved_root)

    # ---- Configure main agent (if present) ----
    metadata: Dict[str, Any] = {}
    main_sandbox_tools: list = []
    mcp_tool_name = "agent_chat"
    mcp_tool_description = "Run an agent chat turn with a prompt."

    if main_agent:
        metadata = main_agent["metadata"]

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

        # ---- Configure execution sandbox from main agent frontmatter ----
        execution_sandbox = metadata.get("execution_sandbox")
        if isinstance(execution_sandbox, dict):
            main_sandbox_tools = create_sandbox_tools(execution_sandbox)
    else:
        logging.info("No main.agent.md found — HTTP chat, MCP, and UI endpoints will return 404.")

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
        Chat endpoint - send a prompt, get a response.

        POST /agent/chat
        Headers:
            x-ms-session-id (optional): Session ID for resuming a previous session
        Body:
        {
            "prompt": "What is 2+2?"
        }
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
            result = await run_copilot_agent(prompt, session_id=session_id, sandbox_tools=main_sandbox_tools)

            response = Response(
                json.dumps(
                    {
                        "session_id": result.session_id,
                        "response": result.content,
                        "response_intermediate": result.content_intermediate,
                        "tool_calls": result.tool_calls,
                    }
                ),
                media_type="application/json",
                headers={"x-ms-session-id": result.session_id},
            )
            return response

        except Exception as e:
            error_msg = str(e) if str(e) else f"{type(e).__name__}: {repr(e)}"
            logging.error(f"Chat error: {error_msg}")
            return Response(
                json.dumps({"error": error_msg}), status_code=500, media_type="application/json"
            )

    @app.route(route="agent/chatstream", methods=["POST"])
    async def chat_stream(req: Request) -> StreamingResponse:
        """
        Streaming chat endpoint - send a prompt, receive SSE events.

        POST /agent/chat/stream
        Headers:
            x-ms-session-id (optional): Session ID for resuming a previous session
        Body:
        {
            "prompt": "What is 2+2?"
        }

        Response: text/event-stream with events:
            data: {"type": "session", "session_id": "..."}
            data: {"type": "delta", "content": "partial text"}
            data: {"type": "tool_start", "tool_name": "...", "tool_call_id": "..."}
            data: {"type": "message", "content": "full message"}
            data: {"type": "done"}
        """
        try:
            body = await req.json()
            prompt = body.get("prompt")

            if not main_agent:
                async def no_agent_gen():
                    yield f"data: {json.dumps({'type': 'error', 'content': 'No main.agent.md found. Create a main.agent.md file in the app root to enable this endpoint.'})}\n\n"
                return StreamingResponse(no_agent_gen(), media_type="text/event-stream", status_code=404)

            if not prompt:
                async def error_gen():
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Missing prompt'})}\n\n"
                return StreamingResponse(error_gen(), media_type="text/event-stream")

            session_id = req.headers.get("x-ms-session-id")
            return StreamingResponse(
                run_copilot_agent_stream(prompt, session_id=session_id, sandbox_tools=main_sandbox_tools),
                media_type="text/event-stream",
            )

        except Exception as e:
            error_msg = str(e) if str(e) else f"{type(e).__name__}: {repr(e)}"
            logging.error(f"Chat stream error: {error_msg}")
            async def error_gen():
                yield f"data: {json.dumps({'type': 'error', 'content': error_msg})}\n\n"
            return StreamingResponse(error_gen(), media_type="text/event-stream")

    # ---- MCP tool (only when main agent exists) ----

    if main_agent:
        @app.mcp_tool_trigger(
            arg_name="context",
            tool_name=mcp_tool_name,
            description=mcp_tool_description,
            tool_properties=_MCP_AGENT_TOOL_PROPERTIES,
        )
        async def mcp_agent_chat(context: str) -> str:
            """MCP tool endpoint that runs the same agent workflow as /agent/chat."""
            try:
                payload = json.loads(context) if context else {}
                arguments = payload.get("arguments", {}) if isinstance(payload, dict) else {}

                prompt = arguments.get("prompt") if isinstance(arguments, dict) else None
                if not isinstance(prompt, str) or not prompt.strip():
                    return json.dumps({"error": "Missing 'prompt'"})

                session_id = _extract_mcp_session_id(payload) if isinstance(payload, dict) else None

                result = await run_copilot_agent(prompt.strip(), session_id=session_id, sandbox_tools=main_sandbox_tools)

                return json.dumps(
                    {
                        "session_id": result.session_id,
                        "response": result.content,
                        "response_intermediate": result.content_intermediate,
                        "tool_calls": result.tool_calls,
                    }
                )
            except Exception as exc:
                error_msg = str(exc) if str(exc) else f"{type(exc).__name__}: {repr(exc)}"
                logging.error(f"MCP tool error: {error_msg}")
                return json.dumps({"error": error_msg})

    return app
