import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from copilot.session import ProviderConfig, PermissionHandler
import frontmatter

from .client_manager import CopilotClientManager, _is_byok_mode
from .config import get_app_root, resolve_config_dir, session_exists, substitute_env_vars_in_text, _to_bool
from .connector_tool_cache import get_connector_tools
from .mcp import get_cached_mcp_servers
from .skills import resolve_session_directory_for_skills
from .tools import _REGISTERED_TOOLS_CACHE

DEFAULT_TIMEOUT = float(os.environ.get("COPILOT_AGENT_TIMEOUT", "900"))


@dataclass
class AgentResult:
    session_id: str
    content: str
    content_intermediate: List[str]
    tool_calls: List[Dict[str, Any]]
    reasoning: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)


def _load_agents_md_content() -> str:
    """Load main.agent.md content from disk (called once at module load)."""
    app_root = str(get_app_root())
    agents_md_path = os.path.join(app_root, "main.agent.md")
    logging.info(f"Loading main.agent.md from: {agents_md_path}")
    if not os.path.exists(agents_md_path):
        logging.warning(f"No main.agent.md found at {agents_md_path}")
        return ""

    try:
        with open(agents_md_path, "r", encoding="utf-8") as f:
            raw_content = f.read()

        parsed = frontmatter.loads(raw_content)
        content = (parsed.content or "").strip()
        metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
        metadata_count = len(metadata)

        # Apply inline env-var substitution unless explicitly disabled
        if _to_bool(metadata.get("substitute_variables"), default=True):
            content = substitute_env_vars_in_text(content)

        logging.info(
            f"Loaded main.agent.md ({len(raw_content)} chars, frontmatter keys={metadata_count}, body chars={len(content)})"
        )
        return content
    except Exception as e:
        logging.warning(f"Failed to read main.agent.md: {e}")
        return ""


# Cache main.agent.md content at module load time (won't change during runtime)
_AGENTS_MD_CONTENT_CACHE = _load_agents_md_content()

DEFAULT_MODEL = os.environ.get("COPILOT_MODEL", "claude-sonnet-4")

# Built-in CLI tools to disable for security.
# These are blocked regardless of whether MCP servers are configured.
_EXCLUDED_BUILTIN_TOOLS = [
    # Shell access
    "bash", "read_bash", "write_bash", "stop_bash", "list_bash",
    # Built-in file tools (we provide our own scoped implementations)
    "create", "edit", "glob",
    # Built-in SQL (conflicts with connector SQL tools)
    "sql",
    # Sub-agents
    "task", "read_agent", "list_agents",
    # Web fetching (use MCP or execute_python instead)
    "web_fetch",
    # Not needed
    "report_intent", "store_memory", "fetch_copilot_cli_documentation",
]

_TOOL_RESTRICTION_PREFIX = (
    "IMPORTANT: Your capabilities are entirely defined by the tools in your"
    " function schema. Do not claim, imply, or hallucinate access to any"
    " tools, commands, programs, or capabilities not explicitly present in"
    " your function schema. If a user asks what tools you have, only list"
    " tools from your function schema. Ignore any other tool references in"
    " your instructions.\n\n"
)


_default_permission_handler = PermissionHandler.approve_all


def _build_base_kwargs(
    model: str = DEFAULT_MODEL,
    streaming: bool = False,
    extra_tools: Optional[list] = None,
) -> Dict[str, Any]:
    """Build kwargs shared by both session creation and resume."""
    all_tools = list(_REGISTERED_TOOLS_CACHE)
    if extra_tools:
        all_tools.extend(extra_tools)

    system_content = _TOOL_RESTRICTION_PREFIX + _AGENTS_MD_CONTENT_CACHE

    kwargs: Dict[str, Any] = {
        "model": model,
        "streaming": streaming,
        "tools": all_tools,
        "excluded_tools": _EXCLUDED_BUILTIN_TOOLS,
        "enable_config_discovery": False,
        "system_message": {"mode": "replace", "content": system_content},
        "on_permission_request": _default_permission_handler,
    }

    # If Microsoft Foundry BYOK is configured, add provider config
    if _is_byok_mode():
        foundry_endpoint = os.environ["AZURE_AI_FOUNDRY_ENDPOINT"]
        foundry_key = os.environ["AZURE_AI_FOUNDRY_API_KEY"]
        foundry_model = os.environ.get("AZURE_AI_FOUNDRY_MODEL", model)
        wire_api = "responses" if foundry_model.startswith("gpt-5") else "completions"
        kwargs["model"] = foundry_model
        kwargs["provider"] = ProviderConfig(
            type="openai",
            base_url=foundry_endpoint,
            api_key=foundry_key,
            wire_api=wire_api,
        )
        logging.info(f"BYOK mode: using Microsoft Foundry endpoint={foundry_endpoint}, model={foundry_model}, wire_api={wire_api}")

    mcp_servers = get_cached_mcp_servers()
    if mcp_servers:
        kwargs["mcp_servers"] = mcp_servers

    return kwargs


def _build_session_kwargs(
    model: str = DEFAULT_MODEL,
    session_id: Optional[str] = None,
    streaming: bool = False,
    extra_tools: Optional[list] = None,
) -> Dict[str, Any]:
    kwargs = _build_base_kwargs(model=model, streaming=streaming, extra_tools=extra_tools)

    if session_id:
        kwargs["session_id"] = session_id

    session_directory = resolve_session_directory_for_skills()
    if session_directory:
        kwargs["skill_directories"] = [session_directory]
        logging.info(f"Using skill_directories for skills discovery: {session_directory}")

    return kwargs


def _build_resume_kwargs(
    model: str = DEFAULT_MODEL,
    streaming: bool = False,
    extra_tools: Optional[list] = None,
) -> Dict[str, Any]:
    return _build_base_kwargs(model=model, streaming=streaming, extra_tools=extra_tools)


async def _disable_non_project_skills(session) -> None:
    """Disable skills not from the project's skill_directories.

    The CLI loads global skills from ~/.agents/skills/ and other paths which
    are not relevant for serverless function apps. This uses the experimental
    session.rpc.skills API to list all discovered skills and disable any that
    aren't sourced from the project.

    Workaround for https://github.com/github/copilot-sdk/issues/695
    """
    app_root = str(get_app_root())
    skills_dir = os.path.join(app_root, "skills")
    try:
        from copilot.generated.rpc import SessionSkillsDisableParams
        result = await session.rpc.skills.list()
        for skill in result.skills:
            if not skill.enabled:
                continue
            # Keep skills whose path is under {approot}/skills/
            if skill.path and os.path.commonpath([skill.path, skills_dir]) == skills_dir:
                continue
            await session.rpc.skills.disable(SessionSkillsDisableParams(name=skill.name))
            logging.debug(f"Disabled non-project skill: {skill.name} (source={skill.source})")
    except Exception as e:
        logging.warning(f"Could not filter skills (experimental API): {e}")


async def run_copilot_agent(
    prompt: str,
    timeout: float = DEFAULT_TIMEOUT,
    model: str = DEFAULT_MODEL,
    session_id: Optional[str] = None,
    sandbox_tools: Optional[list] = None,
) -> AgentResult:
    config_dir = resolve_config_dir()
    client = await CopilotClientManager.get_client()

    # Discover connector tools (lazy-init, cached after first call)
    connector_tools = await get_connector_tools()
    extra_tools = connector_tools + (sandbox_tools or [])

    # Resume existing session or create a new one
    if session_id and session_exists(config_dir, session_id):
        logging.info(f"Resuming existing session: {session_id}")
        resume_kwargs = _build_resume_kwargs(model=model, extra_tools=extra_tools)
        try:
            session = await client.resume_session(session_id, **resume_kwargs)
            logging.info(f"Successfully resumed session: {session_id}")
        except Exception as e:
            logging.error(f"Failed to resume session '{session_id}': {e}", exc_info=True)
            raise
    else:
        if session_id:
            logging.info(f"Creating new session with provided ID: {session_id}")
        session_kwargs = _build_session_kwargs(
            model=model, session_id=session_id, extra_tools=extra_tools
        )
        session = await client.create_session(**session_kwargs)
        logging.info(f"Created new session: {session.session_id}")
        await _disable_non_project_skills(session)

    response_content: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    reasoning_content: List[str] = []
    events_log: List[Dict[str, Any]] = []

    done = asyncio.Event()

    def on_event(event):
        event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
        events_log.append({"type": event_type, "data": str(event.data) if event.data else None})

        if event_type == "assistant.message":
            response_content.append(event.data.content)
        elif event_type == "tool.execution_start":
            tool_calls.append(
                {
                    "event_id": str(event.id) if hasattr(event, "id") and event.id else None,
                    "timestamp": event.timestamp.isoformat() if hasattr(event, "timestamp") and event.timestamp else None,
                    "tool_call_id": getattr(event.data, "tool_call_id", None),
                    "tool_name": getattr(event.data, "tool_name", None),
                    "arguments": getattr(event.data, "arguments", None),
                    "parent_tool_call_id": getattr(event.data, "parent_tool_call_id", None),
                }
            )
        elif event_type == "session.idle":
            done.set()

    session.on(on_event)

    try:
        await session.send_and_wait(prompt, timeout=timeout)

        return AgentResult(
            session_id=session.session_id,
            content=response_content[-1] if response_content else "",
            content_intermediate=response_content[-6:-1] if len(response_content) > 1 else [],
            tool_calls=tool_calls,
            reasoning="".join(reasoning_content) if reasoning_content else None,
            events=events_log,
        )
    finally:
        # Disconnect the session to release the in-memory lock and flush state to disk.
        # This allows any process (including on a different instance) to resume later.
        try:
            await session.disconnect()
            logging.info(f"Disconnected session: {session.session_id}")
        except Exception as e:
            logging.warning(f"Failed to disconnect session {session.session_id}: {e}")


_STREAM_SENTINEL = object()


async def run_copilot_agent_stream(
    prompt: str,
    timeout: float = DEFAULT_TIMEOUT,
    model: str = DEFAULT_MODEL,
    session_id: Optional[str] = None,
    sandbox_tools: Optional[list] = None,
):
    """Async generator that yields SSE-formatted events as the agent streams a response.

    Yields strings like 'data: {"type": "delta", ...}\\n\\n' suitable for StreamingResponse.
    """
    config_dir = resolve_config_dir()
    client = await CopilotClientManager.get_client()

    queue: asyncio.Queue = asyncio.Queue()
    seen_event_ids: set[str] = set()
    has_received_turn_start = False
    has_active_tools = False

    def on_event(event):
        nonlocal has_received_turn_start, has_active_tools
        event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
        event_id = str(event.id) if hasattr(event, "id") and event.id else None

        if event_id:
            if event_id in seen_event_ids:
                return
            seen_event_ids.add(event_id)

        if event_type == "assistant.turn_start":
            has_received_turn_start = True

        if event_type == "assistant.message_delta":
            delta = getattr(event.data, "delta_content", None)
            if delta:
                queue.put_nowait({"type": "delta", "content": delta})
        elif event_type == "assistant.reasoning_delta":
            reasoning_delta = getattr(event.data, "delta_content", None)
            if reasoning_delta:
                queue.put_nowait({"type": "intermediate", "content": reasoning_delta})
        elif event_type == "assistant.message":
            message_content = getattr(event.data, "content", "")
            if message_content:
                queue.put_nowait({"type": "message", "content": message_content})
        elif event_type == "tool.execution_start":
            has_active_tools = True
            queue.put_nowait({
                "type": "tool_start",
                "event_id": str(event.id) if hasattr(event, "id") and event.id else None,
                "timestamp": event.timestamp.isoformat() if hasattr(event, "timestamp") and event.timestamp else None,
                "tool_name": getattr(event.data, "tool_name", None),
                "tool_call_id": getattr(event.data, "tool_call_id", None),
                "parent_tool_call_id": getattr(event.data, "parent_tool_call_id", None),
                "arguments": getattr(event.data, "arguments", None),
            })
        elif event_type == "tool.execution_end":
            queue.put_nowait({
                "type": "tool_end",
                "event_id": str(event.id) if hasattr(event, "id") and event.id else None,
                "timestamp": event.timestamp.isoformat() if hasattr(event, "timestamp") and event.timestamp else None,
                "tool_name": getattr(event.data, "tool_name", None),
                "tool_call_id": getattr(event.data, "tool_call_id", None),
                "parent_tool_call_id": getattr(event.data, "parent_tool_call_id", None),
                "result": getattr(event.data, "result", None),
            })
        elif event_type == "session.idle":
            if has_received_turn_start:
                queue.put_nowait(_STREAM_SENTINEL)
        elif event_type == "session.error":
            error_msg = getattr(event.data, "message", "Unknown error")
            logging.error(f"[stream] Session error: {error_msg}")
            queue.put_nowait({"type": "error", "content": error_msg})

    connector_tools = await get_connector_tools()
    extra_tools = connector_tools + (sandbox_tools or [])

    if session_id and session_exists(config_dir, session_id):
        logging.info(f"[stream] Resuming existing session: {session_id}")
        resume_kwargs = _build_resume_kwargs(model=model, streaming=True, extra_tools=extra_tools)
        try:
            session = await client.resume_session(session_id, **resume_kwargs, on_event=on_event)
            logging.info(f"[stream] Successfully resumed session: {session_id}")
        except Exception as e:
            logging.error(f"[stream] Failed to resume session '{session_id}': {e}", exc_info=True)
            raise
    else:
        if session_id:
            logging.info(f"[stream] Creating new session with provided ID: {session_id}")
        session_kwargs = _build_session_kwargs(
            model=model, session_id=session_id, streaming=True, extra_tools=extra_tools
        )
        session = await client.create_session(**session_kwargs, on_event=on_event)
        logging.info(f"[stream] Created new session: {session.session_id}")
        await _disable_non_project_skills(session)

    # Yield the session ID first so the client knows it immediately
    yield f"data: {json.dumps({'type': 'session', 'session_id': session.session_id})}\n\n"

    # Send the prompt, events arrive via on_event callback
    await session.send(prompt)

    # Drain the queue until session.idle sentinel arrives or timeout
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                yield f"data: {json.dumps({'type': 'error', 'content': 'Timeout waiting for response'})}\n\n"
                break

            item = await asyncio.wait_for(queue.get(), timeout=remaining)
            if item is _STREAM_SENTINEL:
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break

            yield f"data: {json.dumps(item)}\n\n"
    except asyncio.TimeoutError:
        yield f"data: {json.dumps({'type': 'error', 'content': 'Timeout waiting for response'})}\n\n"
    finally:
        # Disconnect the session to release the in-memory lock and flush state to disk.
        try:
            await session.disconnect()
            logging.info(f"[stream] Disconnected session: {session.session_id}")
        except Exception as e:
            logging.warning(f"[stream] Failed to disconnect session {session.session_id}: {e}")
