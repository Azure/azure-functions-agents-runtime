"""MCP server discovery and translation to Microsoft Agent Framework tools."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from agent_framework import MCPStreamableHTTPTool

from .._credential import build_credential, build_credential_with_client_id
from .._logger import logger
from ..config.env import has_unresolved_placeholders, resolve_env_vars_in_data

type MCPTool = MCPStreamableHTTPTool

_DISCOVERED_MCP_SERVERS_CACHE: dict[Path, MCPDiscoveryResult] = {}
_DEFAULT_TOKEN_REFRESH_OFFSET_SECONDS = 300


@dataclass
class MCPDiscoveryResult:
    """Result of MCP server discovery including successes and failures."""

    servers: dict[str, MCPTool]  # {server_name: MCPTool}
    failed_loads: list[tuple[str, str]]  # [(server_name, error_message), ...]
    definitions: dict[str, MCPServerDefinition] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPServerDefinition:
    """Resolved MCP metadata shared by tool discovery and egress compilation."""

    name: str
    url: str
    headers: Mapping[str, object]
    auth: Mapping[str, object]

    @classmethod
    def create(cls, name: str, server: Mapping[str, object]) -> MCPServerDefinition:
        url = str(server.get("url", "")).strip()
        headers = server.get("headers")
        auth = server.get("auth")
        return cls(
            name=name,
            url=url,
            headers=MappingProxyType(dict(headers)) if isinstance(headers, Mapping) else MappingProxyType({}),
            auth=MappingProxyType(dict(auth)) if isinstance(auth, Mapping) else MappingProxyType({}),
        )


def clear_mcp_cache() -> None:
    """Clear cached MCP server discovery results."""
    _DISCOVERED_MCP_SERVERS_CACHE.clear()


def _copy_discovery_result(result: MCPDiscoveryResult) -> MCPDiscoveryResult:
    return MCPDiscoveryResult(
        servers=dict(result.servers),
        failed_loads=list(result.failed_loads),
        definitions=dict(result.definitions),
    )


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


def _build_mcp_tool(name: str, server: dict[str, Any]) -> tuple[MCPTool | None, str | None]:
    """Translate a single mcp.json entry to a MAF MCP tool object.
    
    Returns (tool, error_message). If tool is None, error_message explains why.
    """
    server_type = str(server.get("type", "")).lower()
    raw_tools = server.get("tools", ["*"])
    if isinstance(raw_tools, list) and any(tool == "*" for tool in raw_tools):
        allowed_tools: list[str] | None = None
    elif isinstance(raw_tools, list):
        allowed_tools = [str(tool) for tool in raw_tools]
    else:
        allowed_tools = None
    if "command" in server or server_type in {"local", "stdio"}:
        error = "MCP stdio transport is not supported"
        logger.warning("%s; skipping server '%s'", error, name)
        return None, error

    if "url" in server or server_type in {"http", "streamable-http"}:
        if server_type and server_type not in {"http", "streamable-http"}:
            error = f"unknown server type '{server_type}'; supported types are 'http' and 'streamable-http'"
            logger.warning(
                "MCP server '%s': %s",
                name,
                error,
            )
            return None, error
        url = str(server.get("url", "")).strip()
        if not url:
            error = "missing 'url'"
            logger.warning("MCP server '%s': %s, skipping", name, error)
            return None, error
        if has_unresolved_placeholders(url):
            error = f"could not resolve url '{url}'"
            logger.warning("MCP server '%s': %s, skipping", name, error)
            return None, error
        header_provider = _build_header_provider(server)

        return MCPStreamableHTTPTool(
            name=name,
            url=url,
            allowed_tools=allowed_tools,
            load_tools=True,
            load_prompts=False,
            header_provider=header_provider,
            http_client=_build_http_client(header_provider),
        ), None

    if server_type:
        error = f"unknown server type '{server_type}'; supported types are 'http' and 'streamable-http'"
        logger.warning(
            "MCP server '%s': %s",
            name,
            error,
        )
    else:
        error = "unrecognized config (expected 'url' plus type 'http' or 'streamable-http')"
        logger.warning(
            "MCP server '%s': %s, skipping",
            name,
            error,
        )
    return None, error


def discover_mcp_servers(app_root: Path) -> MCPDiscoveryResult:
    resolved_root = Path(app_root).resolve()
    cached_servers = _DISCOVERED_MCP_SERVERS_CACHE.get(resolved_root)
    if cached_servers is not None:
        return _copy_discovery_result(cached_servers)

    path = resolved_root / "mcp.json"
    if not path.exists():
        result = MCPDiscoveryResult(servers={}, failed_loads=[], definitions={})
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = result
        return _copy_discovery_result(result)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read MCP config from %s: %s", path, exc)
        result = MCPDiscoveryResult(servers={}, failed_loads=[], definitions={})
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = result
        return _copy_discovery_result(result)

    if not isinstance(data, dict):
        logger.warning(
            "Ignoring %s: expected a JSON object at the top level, got %s.",
            path,
            type(data).__name__,
        )
        result = MCPDiscoveryResult(servers={}, failed_loads=[], definitions={})
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = result
        return _copy_discovery_result(result)

    data = cast(dict[str, Any], resolve_env_vars_in_data(data))
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        logger.warning("Invalid MCP config in %s: 'servers' must be an object", path)
        result = MCPDiscoveryResult(servers={}, failed_loads=[], definitions={})
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = result
        return _copy_discovery_result(result)

    tools: dict[str, MCPTool] = {}
    failed_loads: list[tuple[str, str]] = []
    definitions: dict[str, MCPServerDefinition] = {}
    for name in sorted(servers.keys()):
        config = servers[name]
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        built, error = _build_mcp_tool(name, config)
        if built is not None:
            tools[name] = built
            definitions[name] = MCPServerDefinition.create(name, config)
        elif error is not None:
            failed_loads.append((name, error))

    if tools:
        logger.info("Loaded %d MCP server(s) from %s", len(tools), path)
    else:
        logger.info("No valid MCP servers found in %s", path)
    if failed_loads:
        logger.warning("Failed to load %d MCP server(s)", len(failed_loads))
    result = MCPDiscoveryResult(
        servers=tools,
        failed_loads=failed_loads,
        definitions=definitions,
    )
    _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = result
    return _copy_discovery_result(result)
