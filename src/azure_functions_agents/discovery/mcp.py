"""MCP server discovery and translation to Microsoft Agent Framework tools."""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent_framework import MCPStreamableHTTPTool

from .._credential import build_credential, build_credential_with_client_id
from .._logger import logger
from .._obo import BIGMAC_ACCESS_TOKEN_HEADER, BIGMAC_HOOKS_SESSION_TOKEN_HEADER
from ..config.env import has_unresolved_placeholders, resolve_env_vars_in_data

if TYPE_CHECKING:
    from .._obo import UserContext

type MCPTool = MCPStreamableHTTPTool

_DISCOVERED_MCP_SERVERS_CACHE: dict[Path, dict[str, MCPTool]] = {}
_DEFAULT_TOKEN_REFRESH_OFFSET_SECONDS = 300

# Context variable to store the current request's UserContext for OBO
_current_user_context: contextvars.ContextVar[UserContext | None] = contextvars.ContextVar(
    "current_user_context", default=None
)


def set_current_user_context(user_context: UserContext | None) -> contextvars.Token[UserContext | None]:
    """Set the current user context for OBO-enabled MCP servers.

    Returns a token that can be used to reset the context.
    """
    return _current_user_context.set(user_context)


def get_current_user_context() -> UserContext | None:
    """Get the current user context for OBO."""
    return _current_user_context.get()


def reset_current_user_context(token: contextvars.Token[UserContext | None]) -> None:
    """Reset the user context using the token from set_current_user_context."""
    _current_user_context.reset(token)


def clear_mcp_cache() -> None:
    """Clear cached MCP server discovery results."""
    _DISCOVERED_MCP_SERVERS_CACHE.clear()


def _build_header_provider(server: dict[str, Any]) -> Any:
    headers = server.get("headers")
    static_headers = (
        {str(key): str(value) for key, value in headers.items()}
        if isinstance(headers, dict)
        else {}
    )

    auth = server.get("auth")
    if not isinstance(auth, dict):
        if not static_headers:
            return None

        def static_header_provider(_ctx: Any) -> dict[str, str]:
            return dict(static_headers)

        return static_header_provider

    auth_type = str(auth.get("type", "managed_identity")).strip().lower()
    scope = str(auth.get("scope", "")).strip()

    if not scope:
        logger.warning("MCP server auth requires a non-empty 'scope'")
        if not static_headers:
            return None

        def missing_scope_header_provider(_ctx: Any) -> dict[str, str]:
            return dict(static_headers)

        return missing_scope_header_provider

    # Handle OBO authentication type
    if auth_type == "obo":
        return _build_obo_header_provider(scope, static_headers)

    # Default: managed identity authentication
    client_id = str(auth.get("client_id", "")).strip()
    if has_unresolved_placeholders(client_id):
        client_id = ""

    credential = build_credential_with_client_id(client_id) if client_id else build_credential()
    cached_token: dict[str, str | int] = {"token": "", "expires_on": 0}

    def default_credential_header_provider(_ctx: Any) -> dict[str, str]:
        now = int(time.time())
        expires_on = int(cached_token["expires_on"])
        if not cached_token["token"] or expires_on - _DEFAULT_TOKEN_REFRESH_OFFSET_SECONDS <= now:
            token = credential.get_token(scope)
            cached_token["token"] = token.token
            cached_token["expires_on"] = token.expires_on

        result = dict(static_headers)
        result["Authorization"] = f"Bearer {cached_token['token']}"
        return result

    return default_credential_header_provider


def _build_obo_header_provider(scope: str, static_headers: dict[str, str]) -> Any:
    """Build a header provider that uses OBO to get tokens on behalf of the user.

    If no user context is available, falls back to managed identity.
    If managed identity also fails, the request will fail.
    """
    # Fallback credential for when no user context is available
    fallback_credential = build_credential()
    fallback_cached_token: dict[str, str | int] = {"token": "", "expires_on": 0}

    def _get_or_refresh_managed_identity_token() -> str:
        now = int(time.time())
        expires_on = int(fallback_cached_token["expires_on"])
        if not fallback_cached_token["token"] or expires_on - _DEFAULT_TOKEN_REFRESH_OFFSET_SECONDS <= now:
            token = fallback_credential.get_token(scope)
            fallback_cached_token["token"] = token.token
            fallback_cached_token["expires_on"] = token.expires_on
        return str(fallback_cached_token["token"])

    def obo_header_provider(_ctx: Any) -> dict[str, str]:
        user_context = get_current_user_context()

        # BigMac callback flow: forward hooks/access headers and always use MI auth.
        if (
            user_context is not None
            and user_context.hooks_session_token
            and user_context.access_token
        ):
            mi_token = _get_or_refresh_managed_identity_token()
            result = dict(static_headers)
            result[BIGMAC_ACCESS_TOKEN_HEADER] = user_context.access_token
            result[BIGMAC_HOOKS_SESSION_TOKEN_HEADER] = user_context.hooks_session_token
            result["Authorization"] = f"Bearer {mi_token}"
            logger.debug("MCP: Using BigMac hook-session callback headers for scope %s", scope)
            return result

        # Try OBO if user context is available
        if user_context is not None and user_context.has_obo_support:
            try:
                # Run async token acquisition in sync context
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop is not None:
                    # We're in an async context, need to use run_coroutine_threadsafe
                    # This is tricky - the header provider is called from a thread
                    # Let's try getting the token synchronously
                    pass

                # For now, try to get token synchronously via a new event loop
                # This is not ideal but works for the header provider context
                token = _get_obo_token_sync(user_context, scope)
                if token:
                    result = dict(static_headers)
                    result["Authorization"] = f"Bearer {token}"
                    logger.debug("MCP: Using OBO token for scope %s", scope)
                    return result
            except Exception as exc:
                logger.warning("MCP: OBO token acquisition failed, falling back to managed identity: %s", exc)

        # Fallback to managed identity
        mi_token = _get_or_refresh_managed_identity_token()

        result = dict(static_headers)
        result["Authorization"] = f"Bearer {mi_token}"
        logger.debug("MCP: Using managed identity token for scope %s", scope)
        return result

    return obo_header_provider


def _get_obo_token_sync(user_context: Any, scope: str) -> str | None:
    """Synchronously get an OBO token for the given scope.

    This runs the async token acquisition in a new event loop when called
    from a sync context (like the header provider).
    """
    import asyncio

    async def _get_token() -> str | None:
        return await user_context.get_token_for_scope(scope)

    try:
        # Try to run in existing loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is None:
            # No running loop, create one
            return asyncio.run(_get_token())
        else:
            # Running loop exists - we're in a thread, need different approach
            # Create a new loop in this thread
            new_loop = asyncio.new_event_loop()
            try:
                return new_loop.run_until_complete(_get_token())
            finally:
                new_loop.close()
    except Exception as exc:
        logger.warning("Failed to get OBO token synchronously: %s", exc)
        return None


def _build_http_client(header_provider: Any) -> Any:
    if header_provider is None:
        return None

    from httpx import AsyncClient

    async def inject_headers(request: Any) -> None:
        headers = await asyncio.to_thread(header_provider, {})
        for key, value in headers.items():
            request.headers[key] = value

    return AsyncClient(follow_redirects=True, event_hooks={"request": [inject_headers]})


def _build_mcp_tool(name: str, server: dict[str, Any]) -> MCPTool | None:
    """Translate a single mcp.json entry to a MAF MCP tool object."""
    server_type = str(server.get("type", "")).lower()
    raw_tools = server.get("tools", ["*"])
    if isinstance(raw_tools, list) and any(tool == "*" for tool in raw_tools):
        allowed_tools: list[str] | None = None
    elif isinstance(raw_tools, list):
        allowed_tools = [str(tool) for tool in raw_tools]
    else:
        allowed_tools = None
    if "command" in server or server_type in {"local", "stdio"}:
        logger.warning("MCP stdio transport is not supported; skipping server '%s'", name)
        return None

    if "url" in server or server_type in {"http", "streamable-http"}:
        if server_type and server_type not in {"http", "streamable-http"}:
            logger.warning(
                "MCP server '%s': unknown server type '%s'; supported types are 'http' and 'streamable-http'",
                name,
                server_type,
            )
            return None
        url = str(server.get("url", "")).strip()
        if not url:
            logger.warning("MCP server '%s': missing 'url', skipping", name)
            return None
        if has_unresolved_placeholders(url):
            logger.warning("MCP server '%s': could not resolve url '%s', skipping", name, url)
            return None
        header_provider = _build_header_provider(server)

        return MCPStreamableHTTPTool(
            name=name,
            url=url,
            allowed_tools=allowed_tools,
            load_tools=True,
            load_prompts=False,
            header_provider=header_provider,
            http_client=_build_http_client(header_provider),
        )

    if server_type:
        logger.warning(
            "MCP server '%s': unknown server type '%s'; supported types are 'http' and 'streamable-http'",
            name,
            server_type,
        )
    else:
        logger.warning(
            "MCP server '%s': unrecognized config (expected 'url' plus type 'http' or 'streamable-http'), skipping",
            name,
        )
    return None


def discover_mcp_servers(app_root: Path) -> dict[str, MCPTool]:
    resolved_root = Path(app_root).resolve()
    cached_servers = _DISCOVERED_MCP_SERVERS_CACHE.get(resolved_root)
    if cached_servers is not None:
        return dict(cached_servers)

    path = resolved_root / "mcp.json"
    if not path.exists():
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = {}
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read MCP config from %s: %s", path, exc)
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = {}
        return {}

    if not isinstance(data, dict):
        logger.warning(
            "Ignoring %s: expected a JSON object at the top level, got %s.",
            path,
            type(data).__name__,
        )
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = {}
        return {}

    data = cast(dict[str, Any], resolve_env_vars_in_data(data))
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        logger.warning("Invalid MCP config in %s: 'servers' must be an object", path)
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = {}
        return {}

    tools: dict[str, MCPTool] = {}
    for name in sorted(servers.keys()):
        config = servers[name]
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        built = _build_mcp_tool(name, config)
        if built is not None:
            tools[name] = built

    if tools:
        logger.info("Loaded %d MCP server(s) from %s", len(tools), path)
    else:
        logger.info("No valid MCP servers found in %s", path)
    _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = tools
    return dict(tools)
