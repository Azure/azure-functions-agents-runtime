"""Agent execution layer for GitHub Copilot SDK.

This module provides agent execution using the GitHub Copilot SDK as an
alternative to the Microsoft Agent Framework (MAF). It implements the same
public interface as :mod:`.runner` to enable transparent switching.

The Copilot SDK is optional and must be installed separately via::

    pip install azurefunctions-agents-runtime[copilot-sdk]

Architecture
------------

* Uses :class:`CopilotClient` to manage sessions with the Copilot CLI runtime.
* Sessions are event-driven — responses arrive via callbacks rather than
  polling.
* Tools are registered via the SDK's ``@define_tool`` decorator or ``Tool``
  objects.
* Streaming is handled via delta events (``AssistantMessageDeltaData``).
* Skills are loaded from SKILL.md files and injected into the system message.
* MCP tools are wrapped as Copilot SDK tools that proxy HTTP calls.

Configuration
-------------

The Copilot SDK supports custom providers via the ``provider`` session option.
This module checks the same environment variables as :mod:`.client_manager`:

* ``AZURE_OPENAI_ENDPOINT`` + ``AZURE_OPENAI_DEPLOYMENT`` → Azure OpenAI
* ``OPENAI_API_KEY`` → vanilla OpenAI
* ``FOUNDRY_PROJECT_ENDPOINT`` → Microsoft Foundry (not yet supported by Copilot SDK)

Session History
---------------

Unlike MAF, the Copilot SDK manages session history internally via infinite
sessions. The ``session_id`` parameter maps to the SDK's session identifier.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import frontmatter

from ._credential import build_credential, build_credential_with_client_id
from ._logger import logger
from .config.env import has_unresolved_placeholders, resolve_env_vars_in_data, runtime_env_value
from .config.paths import get_app_root
from .discovery.tools import discover_user_tools


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def _runtime_timeout_default() -> float:
    env_timeout = runtime_env_value("AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS")
    if env_timeout:
        try:
            return float(env_timeout)
        except ValueError:
            logger.warning(
                "Ignoring invalid AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS value: %s",
                env_timeout,
            )
    return 900.0


DEFAULT_TIMEOUT = _runtime_timeout_default()
DEFAULT_MODEL: str | None = runtime_env_value("AZURE_FUNCTIONS_AGENTS_MODEL") or None

# Validated session-id pattern
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


# ---------------------------------------------------------------------------
# Per-session locks (single-process scope)
# ---------------------------------------------------------------------------

_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_SESSION_LOCKS_GUARD = asyncio.Lock()


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    async with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class AgentResult:
    """Result of a non-streaming agent run."""

    session_id: str
    content: str
    content_intermediate: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Session id validation
# ---------------------------------------------------------------------------


def _validate_session_id(session_id: str | None) -> str | None:
    """Return ``session_id`` if it matches the safe pattern; raise on invalid input."""
    if session_id is None:
        return None
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.match(session_id):
        raise ValueError(f"Invalid session_id (must match {_SESSION_ID_PATTERN.pattern})")
    return session_id


# ---------------------------------------------------------------------------
# Copilot SDK availability check
# ---------------------------------------------------------------------------


def _check_copilot_sdk_available() -> None:
    """Raise ImportError if the Copilot SDK is not installed."""
    try:
        import copilot  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The GitHub Copilot SDK is not installed. "
            "Install it with: pip install azurefunctions-agents-runtime[copilot-sdk]"
        ) from exc


# ---------------------------------------------------------------------------
# Provider detection for Copilot SDK
# ---------------------------------------------------------------------------


def _env(name: str) -> str:
    """Return ``$name`` stripped, or ``""`` if missing/blank."""
    import os

    return (os.environ.get(name) or "").strip()


def _build_copilot_provider_config() -> dict[str, Any] | None:
    """Build provider configuration for Copilot SDK from environment variables.

    Returns None to use default Copilot provider, or a provider config dict
    for custom providers (Azure OpenAI, OpenAI).
    """
    import os

    # Check for explicit provider override
    explicit = _env("AZURE_FUNCTIONS_AGENTS_PROVIDER").lower()

    # Azure OpenAI
    azure_endpoint = _env("AZURE_OPENAI_ENDPOINT")
    if explicit == "azure_openai" or (not explicit and azure_endpoint):
        if not azure_endpoint:
            raise RuntimeError(
                "AZURE_FUNCTIONS_AGENTS_PROVIDER=azure_openai requires "
                "AZURE_OPENAI_ENDPOINT to be set."
            )
        api_key = _env("AZURE_OPENAI_API_KEY")
        api_version = _env("AZURE_OPENAI_API_VERSION") or "2024-10-21"
        if not api_key:
            # Copilot SDK doesn't support managed identity directly
            # Users must provide an API key for Azure OpenAI
            raise RuntimeError(
                "Copilot SDK with Azure OpenAI requires AZURE_OPENAI_API_KEY. "
                "Managed identity is not supported. Use MAF (sdk_mode: maf) "
                "for managed identity support."
            )
        return {
            "type": "azure",
            "base_url": azure_endpoint,
            "api_key": api_key,
            "azure": {"api_version": api_version},
        }

    # OpenAI
    openai_key = _env("OPENAI_API_KEY")
    if explicit == "openai" or (not explicit and openai_key):
        base_url = _env("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        return {
            "type": "openai",
            "base_url": base_url,
            "api_key": openai_key,
        }

    # Foundry is not supported by Copilot SDK
    if explicit == "foundry" or _env("FOUNDRY_PROJECT_ENDPOINT"):
        raise RuntimeError(
            "Copilot SDK does not support Microsoft Foundry as a provider. "
            "Use MAF (sdk_mode: maf) for Foundry support."
        )

    # Default: use Copilot's built-in provider (requires GitHub auth)
    return None


def _resolve_model() -> str:
    """Resolve the model to use from environment variables."""
    if explicit := runtime_env_value("AZURE_FUNCTIONS_AGENTS_MODEL"):
        return explicit
    if deployment := _env("AZURE_OPENAI_DEPLOYMENT"):
        return deployment
    # Default model for Copilot SDK
    return "gpt-4o"


# ---------------------------------------------------------------------------
# Tool conversion
# ---------------------------------------------------------------------------


def _convert_tools_to_copilot(tools: list[Any] | None) -> list[Any]:
    """Convert agent framework tools to Copilot SDK tool format.

    The Copilot SDK uses its own Tool class with a different interface.
    This function converts MAF FunctionTool objects to Copilot Tool objects.
    """
    if not tools:
        return []

    from copilot.tools import Tool, ToolInvocation, ToolResult

    converted: list[Any] = []

    for tool in tools:
        # Check if it's already a Copilot tool
        if isinstance(tool, Tool):
            converted.append(tool)
            continue

        # Extract info from MAF FunctionTool
        name = getattr(tool, "name", None) or getattr(tool, "__name__", "unknown")
        description = getattr(tool, "description", "") or ""
        parameters = getattr(tool, "parameters", None) or {}

        # Get the underlying function
        func = getattr(tool, "func", None) or getattr(tool, "_func", None)
        if func is None and callable(tool):
            func = tool

        # Create async handler wrapper
        async def create_handler(original_func: Any) -> Any:
            async def handler(invocation: ToolInvocation) -> ToolResult:
                try:
                    result = original_func(**invocation.arguments)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return ToolResult(
                        text_result_for_llm=str(result) if result is not None else "",
                        result_type="success",
                    )
                except Exception as e:
                    return ToolResult(
                        text_result_for_llm=f"Error: {e}",
                        result_type="error",
                    )

            return handler

        # Note: We need to capture func in closure properly
        copilot_tool = Tool(
            name=name,
            description=description,
            parameters=parameters if isinstance(parameters, dict) else {},
            handler=None,  # Will be set up during session creation
        )
        # Store original func for later binding
        copilot_tool._original_func = func  # type: ignore[attr-defined]
        converted.append(copilot_tool)

    return converted


# ---------------------------------------------------------------------------
# Skills support
# ---------------------------------------------------------------------------

_SKILL_FILE_NAME = "SKILL.md"


def _resolve_skills_dir(app_root: Path) -> Path | None:
    """Find ``{app_root}/skills`` (or ``Skills``) if it exists."""
    for name in ("skills", "Skills"):
        candidate = app_root / name
        if candidate.is_dir():
            return candidate
    return None


def _load_skill_content(skill_paths: list[Path]) -> str:
    """Load skill content from SKILL.md files and return combined text.

    This injects skill content into the agent's system message for the
    Copilot SDK, similar to how MAF's SkillsProvider provides context.
    """
    if not skill_paths:
        return ""

    skill_sections: list[str] = []

    for skill_dir in skill_paths:
        skill_file = skill_dir / _SKILL_FILE_NAME
        if not skill_file.exists():
            logger.warning("Skill directory %s missing %s", skill_dir, _SKILL_FILE_NAME)
            continue

        try:
            post = frontmatter.load(skill_file)
            name = str(post.metadata.get("name", skill_dir.name))
            description = str(post.metadata.get("description", ""))
            content = post.content.strip()

            # Format skill as a section
            section = f"## Skill: {name}\n"
            if description:
                section += f"{description}\n\n"
            if content:
                section += f"{content}\n"
            skill_sections.append(section)

            logger.debug("Loaded skill: %s from %s", name, skill_file)
        except Exception as exc:
            logger.warning("Failed to load skill from %s: %s", skill_file, exc)
            continue

    if not skill_sections:
        return ""

    return "\n# Available Skills\n\n" + "\n---\n\n".join(skill_sections)


# ---------------------------------------------------------------------------
# MCP tools support
# ---------------------------------------------------------------------------

_DEFAULT_TOKEN_REFRESH_OFFSET_SECONDS = 300


def _build_mcp_proxy_tools(mcp_servers: dict[str, Any]) -> list[Any]:
    """Create Copilot SDK tools that proxy calls to MCP HTTP servers.

    Each MCP server is exposed as a tool that makes HTTP requests to the
    MCP server's endpoint.
    """
    from copilot.tools import Tool, ToolInvocation, ToolResult

    tools: list[Any] = []

    for server_name, server_config in mcp_servers.items():
        url = server_config.get("url", "")
        if not url:
            continue

        # Build auth configuration
        auth_config = server_config.get("auth", {})
        headers_config = server_config.get("headers", {})

        # Create a tool for calling MCP server tools
        async def make_mcp_handler(
            mcp_url: str,
            mcp_name: str,
            mcp_auth: dict[str, Any],
            static_headers: dict[str, str],
        ) -> Any:
            """Create a handler that proxies tool calls to MCP server."""
            # Token cache for managed identity
            cached_token: dict[str, Any] = {"token": "", "expires_on": 0}

            async def get_auth_headers() -> dict[str, str]:
                """Get authentication headers for MCP request using managed identity."""
                result = dict(static_headers)

                if not mcp_auth:
                    return result

                scope = str(mcp_auth.get("scope", "")).strip()

                if not scope:
                    return result

                # Use managed identity for authentication
                now = int(time.time())
                if (
                    not cached_token["token"]
                    or cached_token["expires_on"] - _DEFAULT_TOKEN_REFRESH_OFFSET_SECONDS <= now
                ):
                    client_id = str(mcp_auth.get("client_id", "")).strip()
                    if has_unresolved_placeholders(client_id):
                        client_id = ""
                    credential = (
                        build_credential_with_client_id(client_id)
                        if client_id
                        else build_credential()
                    )
                    token_response = credential.get_token(scope)
                    cached_token["token"] = token_response.token
                    cached_token["expires_on"] = token_response.expires_on

                result["Authorization"] = f"Bearer {cached_token['token']}"
                return result

            async def handler(invocation: ToolInvocation) -> ToolResult:
                """Proxy tool invocation to MCP server."""
                try:
                    tool_name = invocation.arguments.get("tool_name", "")
                    tool_args = invocation.arguments.get("arguments", {})

                    if not tool_name:
                        return ToolResult(
                            text_result_for_llm="Error: tool_name is required",
                            result_type="error",
                        )

                    headers = await get_auth_headers()
                    headers["Content-Type"] = "application/json"

                    # MCP tool call request
                    request_body = {
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "params": {
                            "name": tool_name,
                            "arguments": tool_args,
                        },
                        "id": 1,
                    }

                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            mcp_url,
                            json=request_body,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=60),
                        ) as response:
                            if response.status != 200:
                                error_text = await response.text()
                                return ToolResult(
                                    text_result_for_llm=f"MCP error ({response.status}): {error_text}",
                                    result_type="error",
                                )

                            result_data = await response.json()

                            if "error" in result_data:
                                return ToolResult(
                                    text_result_for_llm=f"MCP error: {result_data['error']}",
                                    result_type="error",
                                )

                            # Extract result content
                            mcp_result = result_data.get("result", {})
                            content = mcp_result.get("content", [])
                            text_parts = [
                                item.get("text", "")
                                for item in content
                                if item.get("type") == "text"
                            ]
                            result_text = "\n".join(text_parts) or json.dumps(mcp_result)

                            return ToolResult(
                                text_result_for_llm=result_text,
                                result_type="success",
                            )
                except Exception as e:
                    logger.error("MCP %s tool call failed: %s", mcp_name, e, exc_info=True)
                    return ToolResult(
                        text_result_for_llm=f"Error calling MCP server: {e}",
                        result_type="error",
                    )

            return handler

        # Create the handler with captured config
        # Note: We create a wrapper to properly capture the loop variables
        def create_tool(
            name: str, url: str, auth: dict[str, Any], headers: dict[str, str]
        ) -> Tool:
            async def tool_handler(invocation: ToolInvocation) -> ToolResult:
                handler_fn = await make_mcp_handler(url, name, auth, headers)
                return await handler_fn(invocation)

            return Tool(
                name=f"mcp_{name}",
                description=f"Call tools on the {name} MCP server. Pass 'tool_name' and 'arguments'.",
                parameters={
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "description": "Name of the MCP tool to call",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Arguments to pass to the MCP tool",
                        },
                    },
                    "required": ["tool_name"],
                },
                handler=tool_handler,
            )

        static_headers = (
            {str(k): str(v) for k, v in headers_config.items()}
            if isinstance(headers_config, dict)
            else {}
        )
        tools.append(create_tool(server_name, url, auth_config, static_headers))

    return tools


def _discover_mcp_servers_raw(app_root: Path) -> dict[str, Any]:
    """Discover MCP servers from mcp.json and return raw config.

    This is similar to discovery.mcp.discover_mcp_servers but returns
    raw config instead of MAF MCPStreamableHTTPTool objects.
    """
    path = app_root / "mcp.json"
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read MCP config from %s: %s", path, exc)
        return {}

    if not isinstance(data, dict):
        logger.warning(
            "Ignoring %s: expected a JSON object at the top level, got %s.",
            path,
            type(data).__name__,
        )
        return {}

    # Resolve environment variables
    data = resolve_env_vars_in_data(data)

    # Filter to HTTP-based servers only
    servers: dict[str, Any] = {}
    for name, config in data.items():
        if not isinstance(config, dict):
            continue
        server_type = str(config.get("type", "")).lower()
        if "url" in config or server_type in {"http", "streamable-http"}:
            url = str(config.get("url", "")).strip()
            if url and not has_unresolved_placeholders(url):
                servers[name] = config
            else:
                logger.warning("MCP server '%s': invalid or unresolved url, skipping", name)
        elif "command" in config or server_type in {"local", "stdio"}:
            logger.warning("MCP stdio transport not supported in Copilot SDK; skipping '%s'", name)

    return servers


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


async def _create_copilot_session(
    client: Any,
    *,
    instructions: str | None,
    session_id: str | None,
    tools: list[Any],
    model: str,
    provider_config: dict[str, Any] | None,
) -> tuple[Any, str]:
    """Create or resume a Copilot session.

    Returns (session, resolved_session_id).
    """
    from copilot.session import PermissionHandler

    session_kwargs: dict[str, Any] = {
        "model": model,
        "on_permission_request": PermissionHandler.approve_all,
        "streaming": True,
    }

    if provider_config:
        session_kwargs["provider"] = provider_config

    if tools:
        session_kwargs["tools"] = tools

    if instructions:
        session_kwargs["system_message"] = {
            "content": instructions.strip(),
        }

    if session_id:
        # Resume existing session
        session = await client.resume_session(session_id, **session_kwargs)
        return session, session_id
    else:
        # Create new session
        session = await client.create_session(**session_kwargs)
        return session, session.session_id


# ---------------------------------------------------------------------------
# Public API: run_agent (non-streaming)
# ---------------------------------------------------------------------------


async def run_agent(
    prompt: str,
    *,
    instructions: str | None = None,
    timeout: float | None = None,
    tools: list[Any] | None = None,
    mcp_tools: list[Any] | None = None,
    skill_paths: list[Path] | None = None,
    model: str | None = None,
    session_id: str | None = None,
    sandbox_tools: list[Any] | None = None,
) -> AgentResult:
    """Execute a single prompt using the GitHub Copilot SDK.

    Parameters match :func:`.runner.run_agent` for API compatibility.

    MCP tools are exposed as proxy tools that make HTTP calls to MCP servers.
    Skills are loaded and injected into the system message.
    """
    _check_copilot_sdk_available()
    from copilot import CopilotClient
    from copilot.session_events import AssistantMessageData, SessionIdleData

    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    validated_id = _validate_session_id(session_id)

    # Resolve configuration
    provider_config = _build_copilot_provider_config()
    resolved_model = model or _resolve_model()

    # Discover and convert tools
    app_root = get_app_root()
    user_tool_list = list(discover_user_tools(app_root)) if tools is None else list(tools)
    if sandbox_tools:
        user_tool_list.extend(sandbox_tools)
    copilot_tools = _convert_tools_to_copilot(user_tool_list)

    # Add MCP proxy tools
    if mcp_tools is None:
        # Auto-discover MCP servers
        mcp_servers = _discover_mcp_servers_raw(app_root)
        if mcp_servers:
            mcp_proxy_tools = _build_mcp_proxy_tools(mcp_servers)
            copilot_tools.extend(mcp_proxy_tools)
            logger.info("Added %d MCP proxy tools for Copilot SDK", len(mcp_proxy_tools))
    elif mcp_tools:
        # Use provided MCP tools (already converted)
        copilot_tools.extend(_convert_tools_to_copilot(mcp_tools))

    # Build instructions with skills content
    final_instructions = instructions or ""
    if skill_paths:
        skill_content = _load_skill_content(skill_paths)
        if skill_content:
            final_instructions = f"{final_instructions}\n\n{skill_content}".strip()
            logger.info("Injected %d skills into system message", len(skill_paths))

    async with CopilotClient() as client:
            session, resolved_id = await _create_copilot_session(
                client,
                instructions=final_instructions or None,
                session_id=validated_id,
                tools=copilot_tools,
                model=resolved_model,
                provider_config=provider_config,
            )

            lock = await _get_session_lock(resolved_id)
            async with lock:
                # Collect response via events
                response_content = ""
                tool_calls: list[dict[str, Any]] = []
                done_event = asyncio.Event()
                error: Exception | None = None

                def on_event(event: Any) -> None:
                    nonlocal response_content, error
                    try:
                        match event.data:
                            case AssistantMessageData() as data:
                                response_content = data.content or ""
                            case SessionIdleData():
                                done_event.set()
                    except Exception as e:
                        error = e
                        done_event.set()

                session.on(on_event)

                try:
                    await session.send(prompt)
                    await asyncio.wait_for(done_event.wait(), timeout=timeout)
                except TimeoutError:
                    raise RuntimeError(f"Agent run timed out after {timeout}s") from None

                if error:
                    raise error

                return AgentResult(
                    session_id=resolved_id,
                    content=response_content,
                    tool_calls=tool_calls,
                )


# ---------------------------------------------------------------------------
# Public API: run_agent_stream (SSE)
# ---------------------------------------------------------------------------


async def run_agent_stream(
    prompt: str,
    *,
    instructions: str | None = None,
    timeout: float | None = None,
    tools: list[Any] | None = None,
    mcp_tools: list[Any] | None = None,
    skill_paths: list[Path] | None = None,
    model: str | None = None,
    session_id: str | None = None,
    sandbox_tools: list[Any] | None = None,
) -> AsyncIterator[str]:
    """SSE-formatted async generator using the GitHub Copilot SDK.

    Event vocabulary matches :func:`.runner.run_agent_stream` for
    compatibility:

    * ``session``      — first event; includes the resolved session id
    * ``delta``        — incremental assistant text token(s)
    * ``message``      — full assistant message
    * ``intermediate`` — reasoning text
    * ``tool_start``   — function call about to execute
    * ``tool_end``     — function call result
    * ``done``         — stream completed normally
    * ``error``        — terminal error message

    MCP tools are exposed as proxy tools that make HTTP calls to MCP servers.
    Skills are loaded and injected into the system message.
    """
    _check_copilot_sdk_available()
    from copilot import CopilotClient
    from copilot.session_events import (
        AssistantMessageData,
        AssistantMessageDeltaData,
        AssistantReasoningData,
        AssistantReasoningDeltaData,
        SessionIdleData,
        ToolUseData,
        ToolResultData,
    )

    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    validated_id = _validate_session_id(session_id)

    # Resolve configuration
    provider_config = _build_copilot_provider_config()
    resolved_model = model or _resolve_model()

    # Discover and convert tools
    app_root = get_app_root()
    user_tool_list = list(discover_user_tools(app_root)) if tools is None else list(tools)
    if sandbox_tools:
        user_tool_list.extend(sandbox_tools)
    copilot_tools = _convert_tools_to_copilot(user_tool_list)

    # Add MCP proxy tools
    if mcp_tools is None:
        # Auto-discover MCP servers
        mcp_servers = _discover_mcp_servers_raw(app_root)
        if mcp_servers:
            mcp_proxy_tools = _build_mcp_proxy_tools(mcp_servers)
            copilot_tools.extend(mcp_proxy_tools)
            logger.info("Added %d MCP proxy tools for Copilot SDK", len(mcp_proxy_tools))
    elif mcp_tools:
        # Use provided MCP tools (already converted)
        copilot_tools.extend(_convert_tools_to_copilot(mcp_tools))

    # Build instructions with skills content
    final_instructions = instructions or ""
    if skill_paths:
        skill_content = _load_skill_content(skill_paths)
        if skill_content:
            final_instructions = f"{final_instructions}\n\n{skill_content}".strip()
            logger.info("Injected %d skills into system message", len(skill_paths))

    try:
        async with CopilotClient() as client:
            session, resolved_id = await _create_copilot_session(
                client,
                instructions=final_instructions or None,
                session_id=validated_id,
                tools=copilot_tools,
                model=resolved_model,
                provider_config=provider_config,
            )

            yield f"data: {json.dumps({'type': 'session', 'session_id': resolved_id})}\n\n"

            lock = await _get_session_lock(resolved_id)
            async with lock:
                # Use a queue to collect events from the callback
                event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
                loop = asyncio.get_event_loop()
                deadline = loop.time() + timeout

                def on_event(event: Any) -> None:
                    try:
                        match event.data:
                            case AssistantMessageDeltaData() as data:
                                delta = data.delta_content or ""
                                if delta:
                                    event_queue.put_nowait({"type": "delta", "content": delta})
                            case AssistantReasoningDeltaData() as data:
                                delta = data.delta_content or ""
                                if delta:
                                    event_queue.put_nowait(
                                        {"type": "intermediate", "content": delta}
                                    )
                            case AssistantMessageData() as data:
                                # Final message - we already streamed deltas
                                pass
                            case AssistantReasoningData():
                                # Final reasoning - we already streamed deltas
                                pass
                            case ToolUseData() as data:
                                event_queue.put_nowait({
                                    "type": "tool_start",
                                    "tool_call_id": getattr(data, "id", None),
                                    "tool_name": getattr(data, "name", None),
                                    "arguments": getattr(data, "arguments", None),
                                })
                            case ToolResultData() as data:
                                event_queue.put_nowait({
                                    "type": "tool_end",
                                    "tool_call_id": getattr(data, "id", None),
                                    "tool_name": getattr(data, "name", None),
                                    "result": getattr(data, "result", None),
                                })
                            case SessionIdleData():
                                event_queue.put_nowait(None)  # Signal completion
                    except Exception as e:
                        event_queue.put_nowait({"type": "error", "content": str(e)})
                        event_queue.put_nowait(None)

                session.on(on_event)

                try:
                    await session.send(prompt)

                    # Process events from the queue
                    while True:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
                            return

                        try:
                            event = await asyncio.wait_for(
                                event_queue.get(), timeout=remaining
                            )
                        except TimeoutError:
                            yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
                            return

                        if event is None:
                            # Stream completed
                            break

                        yield f"data: {json.dumps(event)}\n\n"

                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                except TimeoutError:
                    yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
                except Exception as exc:
                    logger.error("Agent stream failed: %s", exc, exc_info=True)
                    yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
    except Exception as exc:
        logger.error("Failed to create Copilot session: %s", exc, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
