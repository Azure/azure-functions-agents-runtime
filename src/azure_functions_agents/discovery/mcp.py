"""MCP server discovery and translation to Microsoft Agent Framework tools."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, cast

from agent_framework import MCPStreamableHTTPTool as _MCPStreamableHTTPTool

from .._credential import build_credential, build_credential_with_client_id
from .._logger import logger
from ..config.env import has_unresolved_placeholders, resolve_env_vars_in_data


class MCPStreamableHTTPTool(_MCPStreamableHTTPTool):
    """MCP Streamable HTTP tool with non-fatal prompt discovery.

    Some connector-backed MCP servers support tools but return 400 for
    ``prompts/list``. Agent Framework calls ``load_prompts()`` during connect
    and on prompt-list change notifications, so make prompt discovery
    best-effort while preserving normal tool discovery behavior.
    """

    async def load_prompts(self) -> None:
        try:
            await super().load_prompts()
        except Exception as exc:
            logger.warning(
                "MCP server '%s': failed to load prompts; continuing without prompts. "
                "Set load_prompts=false in mcp.json to skip prompt discovery. Error: %s",
                self.name,
                exc,
            )


type MCPTool = MCPStreamableHTTPTool

_DISCOVERED_MCP_SERVERS_CACHE: dict[Path, dict[str, MCPTool]] = {}
_DEFAULT_TOKEN_REFRESH_OFFSET_SECONDS = 300


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

    scope = str(auth.get("scope", "")).strip()
    if not scope:
        logger.warning("MCP server auth requires a non-empty 'scope'")
        if not static_headers:
            return None

        def missing_scope_header_provider(_ctx: Any) -> dict[str, str]:
            return dict(static_headers)

        return missing_scope_header_provider

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
    load_tools = bool(server.get("load_tools", True))
    load_prompts = bool(server.get("load_prompts", True))

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
            load_tools=load_tools,
            load_prompts=load_prompts,
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
