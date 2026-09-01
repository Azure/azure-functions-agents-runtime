"""MCP server discovery and translation to Microsoft Agent Framework tools."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from agent_framework import MCPStreamableHTTPTool

from .._credential import build_credential, build_credential_with_client_id
from .._logger import logger
from ..config.env import has_unresolved_placeholders, resolve_env_vars_in_data

type MCPTool = MCPStreamableHTTPTool

_DISCOVERED_MCP_DEFINITIONS_CACHE: dict[
    Path,
    tuple[dict[str, MCPServerDefinition], list[tuple[str, str]]],
] = {}
_DEFAULT_TOKEN_REFRESH_OFFSET_SECONDS = 300


@dataclass(frozen=True)
class MCPAuthConfig:
    """Authentication settings for one HTTP MCP server."""

    scope: str
    client_id: str


@dataclass(frozen=True)
class MCPHTTPServerConfig:
    """Validated immutable configuration for one HTTP MCP server."""

    url: str
    allowed_tools: tuple[str, ...] | None
    headers: tuple[tuple[str, str], ...]
    auth: MCPAuthConfig | None


@dataclass(frozen=True)
class MCPServerDefinition:
    """Immutable resolved MCP configuration that can build an owned tool."""

    name: str
    config: MCPHTTPServerConfig

    def build_tool(self) -> MCPTool:
        return _build_mcp_tool(self.name, self.config)


@dataclass
class MCPDefinitionDiscoveryResult:
    """Resolved MCP definitions and discovery failures."""

    definitions: dict[str, MCPServerDefinition]
    failed_loads: list[tuple[str, str]]


@dataclass
class MCPDiscoveryResult:
    """Result of MCP server discovery including successes and failures."""

    servers: dict[str, MCPTool]  # {server_name: MCPTool}
    failed_loads: list[tuple[str, str]]  # [(server_name, error_message), ...]


def clear_mcp_cache() -> None:
    """Clear cached MCP server discovery results."""
    _DISCOVERED_MCP_DEFINITIONS_CACHE.clear()


def _build_header_provider(server: MCPHTTPServerConfig) -> Any:
    static_headers = dict(server.headers)
    auth = server.auth
    if auth is None:
        if not static_headers:
            return None

        def static_header_provider(_ctx: Any) -> dict[str, str]:
            return dict(static_headers)

        return static_header_provider

    scope = auth.scope
    if not scope:
        logger.warning("MCP server auth requires a non-empty 'scope'")
        if not static_headers:
            return None

        def missing_scope_header_provider(_ctx: Any) -> dict[str, str]:
            return dict(static_headers)

        return missing_scope_header_provider

    client_id = auth.client_id
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


def _parse_server_config(
    name: str,
    server: dict[str, Any],
) -> tuple[MCPHTTPServerConfig | None, str | None]:
    """Validate one raw mcp.json entry and normalize it to an HTTP config."""
    server_type = str(server.get("type", "")).lower()
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

        raw_tools = server.get("tools", ["*"])
        if isinstance(raw_tools, list) and any(tool == "*" for tool in raw_tools):
            allowed_tools: tuple[str, ...] | None = None
        elif isinstance(raw_tools, list):
            allowed_tools = tuple(str(tool) for tool in raw_tools)
        else:
            allowed_tools = None

        raw_headers = server.get("headers")
        headers = (
            tuple(sorted((str(key), str(value)) for key, value in raw_headers.items()))
            if isinstance(raw_headers, dict)
            else ()
        )
        raw_auth = server.get("auth")
        if isinstance(raw_auth, dict):
            client_id = str(raw_auth.get("client_id", "")).strip()
            if has_unresolved_placeholders(client_id):
                client_id = ""
            auth = MCPAuthConfig(
                scope=str(raw_auth.get("scope", "")).strip(),
                client_id=client_id,
            )
        else:
            auth = None

        return MCPHTTPServerConfig(
            url=url,
            allowed_tools=allowed_tools,
            headers=headers,
            auth=auth,
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


def _build_mcp_tool(name: str, server: MCPHTTPServerConfig) -> MCPTool:
    """Build one invocation-owned MAF MCP tool from validated configuration."""
    header_provider = _build_header_provider(server)
    return MCPStreamableHTTPTool(
        name=name,
        url=server.url,
        allowed_tools=list(server.allowed_tools) if server.allowed_tools is not None else None,
        load_tools=True,
        load_prompts=False,
        header_provider=header_provider,
        http_client=_build_http_client(header_provider),
    )


def _definition_from_config(
    name: str,
    server: MCPHTTPServerConfig,
) -> MCPServerDefinition:
    return MCPServerDefinition(name=name, config=server)


def discover_mcp_server_definitions(app_root: Path) -> MCPDefinitionDiscoveryResult:
    """Load and cache immutable resolved MCP server definitions."""
    resolved_root = Path(app_root).resolve()
    cached = _DISCOVERED_MCP_DEFINITIONS_CACHE.get(resolved_root)
    if cached is not None:
        return MCPDefinitionDiscoveryResult(
            definitions=dict(cached[0]),
            failed_loads=list(cached[1]),
        )

    path = resolved_root / "mcp.json"
    if not path.exists():
        _DISCOVERED_MCP_DEFINITIONS_CACHE[resolved_root] = ({}, [])
        return MCPDefinitionDiscoveryResult(definitions={}, failed_loads=[])

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read MCP config from %s: %s", path, exc)
        _DISCOVERED_MCP_DEFINITIONS_CACHE[resolved_root] = ({}, [])
        return MCPDefinitionDiscoveryResult(definitions={}, failed_loads=[])

    if not isinstance(data, dict):
        logger.warning(
            "Ignoring %s: expected a JSON object at the top level, got %s.",
            path,
            type(data).__name__,
        )
        _DISCOVERED_MCP_DEFINITIONS_CACHE[resolved_root] = ({}, [])
        return MCPDefinitionDiscoveryResult(definitions={}, failed_loads=[])

    data = cast(dict[str, Any], resolve_env_vars_in_data(data))
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        logger.warning("Invalid MCP config in %s: 'servers' must be an object", path)
        _DISCOVERED_MCP_DEFINITIONS_CACHE[resolved_root] = ({}, [])
        return MCPDefinitionDiscoveryResult(definitions={}, failed_loads=[])

    definitions: dict[str, MCPServerDefinition] = {}
    failed_loads: list[tuple[str, str]] = []
    for name in sorted(servers.keys()):
        config = servers[name]
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        server_config, error = _parse_server_config(name, config)
        if server_config is not None:
            definitions[name] = _definition_from_config(name, server_config)
        elif error is not None:
            failed_loads.append((name, error))

    if definitions:
        logger.info("Loaded %d MCP server(s) from %s", len(definitions), path)
    else:
        logger.info("No valid MCP servers found in %s", path)
    if failed_loads:
        logger.warning("Failed to load %d MCP server(s)", len(failed_loads))
    _DISCOVERED_MCP_DEFINITIONS_CACHE[resolved_root] = (
        dict(definitions),
        list(failed_loads),
    )
    return MCPDefinitionDiscoveryResult(
        definitions=dict(definitions),
        failed_loads=list(failed_loads),
    )


def discover_mcp_servers(app_root: Path) -> MCPDiscoveryResult:
    """Build fresh MAF MCP tools from cached immutable definitions."""
    discovered = discover_mcp_server_definitions(app_root)
    return MCPDiscoveryResult(
        servers={
            name: definition.build_tool()
            for name, definition in discovered.definitions.items()
        },
        failed_loads=discovered.failed_loads,
    )
