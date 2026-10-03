"""Read-only discovery of remote MCP server descriptors."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Self, cast

from .._logger import logger
from ..config.env import has_unresolved_placeholders, resolve_env_vars_in_data

type MCPTransport = Literal["http", "streamable-http"]


def _freeze_headers(
    headers: Mapping[str, object] | Iterable[tuple[str, object]],
) -> tuple[tuple[str, str], ...]:
    entries = headers.items() if isinstance(headers, Mapping) else headers
    return tuple((str(key), str(value)) for key, value in entries)


def _freeze_tool_filter(tools: Iterable[object] | None) -> tuple[str, ...] | None:
    if tools is None:
        return None
    entries = tuple(tools)
    if any(tool == "*" for tool in entries):
        return None
    return tuple(str(tool) for tool in entries)


@dataclass(frozen=True)
class MCPServerDescriptor:
    """Immutable MCP configuration with no SDK objects or acquired credentials."""

    name: str
    url: str
    transport: MCPTransport = "http"
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    tools: tuple[str, ...] | None = None
    auth_scope: str | None = None
    client_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        name: str,
        url: str,
        transport: MCPTransport = "http",
        headers: Mapping[str, object] | Iterable[tuple[str, object]] = (),
        tools: Iterable[object] | None = None,
        auth_scope: str | None = None,
        client_id: str | None = None,
    ) -> Self:
        """Copy mutable inputs into the immutable descriptor shape."""
        scope = auth_scope.strip() if auth_scope is not None else None
        if scope == "":
            logger.warning("MCP server auth requires a non-empty 'scope'")
        return cls(
            name=name,
            url=url,
            transport=transport,
            headers=_freeze_headers(headers),
            tools=_freeze_tool_filter(tools),
            auth_scope=scope,
            client_id=(client_id.strip() or None) if client_id is not None else None,
        )


type MCPTool = MCPServerDescriptor

_DISCOVERED_MCP_SERVERS_CACHE: dict[Path, dict[str, MCPServerDescriptor]] = {}


@dataclass
class MCPDiscoveryResult:
    """Result of MCP server discovery including successes and failures."""

    servers: dict[str, MCPServerDescriptor]
    failed_loads: list[tuple[str, str]]  # [(server_name, error_message), ...]


def clear_mcp_cache() -> None:
    """Clear cached MCP server discovery results."""
    _DISCOVERED_MCP_SERVERS_CACHE.clear()


def _build_mcp_descriptor(
    name: str, server: dict[str, Any]
) -> tuple[MCPServerDescriptor | None, str | None]:
    """Translate one supported mcp.json entry without acquiring runtime resources."""
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
        headers = server.get("headers")
        raw_tools = server.get("tools")
        auth = server.get("auth")
        scope = str(auth.get("scope", "")) if isinstance(auth, dict) else None
        client_id = str(auth.get("client_id", "")) if isinstance(auth, dict) else None
        return MCPServerDescriptor.create(
            name=name,
            url=url,
            transport=cast(MCPTransport, server_type or "http"),
            headers=headers if isinstance(headers, dict) else (),
            tools=raw_tools if isinstance(raw_tools, list) else None,
            auth_scope=scope,
            client_id=client_id,
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
        return MCPDiscoveryResult(servers=dict(cached_servers), failed_loads=[])

    path = resolved_root / "mcp.json"
    if not path.exists():
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = {}
        return MCPDiscoveryResult(servers={}, failed_loads=[])

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read MCP config from %s: %s", path, exc)
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = {}
        return MCPDiscoveryResult(servers={}, failed_loads=[])

    if not isinstance(data, dict):
        logger.warning(
            "Ignoring %s: expected a JSON object at the top level, got %s.",
            path,
            type(data).__name__,
        )
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = {}
        return MCPDiscoveryResult(servers={}, failed_loads=[])

    data = cast(dict[str, Any], resolve_env_vars_in_data(data))
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        logger.warning("Invalid MCP config in %s: 'servers' must be an object", path)
        _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = {}
        return MCPDiscoveryResult(servers={}, failed_loads=[])

    descriptors: dict[str, MCPServerDescriptor] = {}
    failed_loads: list[tuple[str, str]] = []
    for name in sorted(servers.keys()):
        config = servers[name]
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        built, error = _build_mcp_descriptor(name, config)
        if built is not None:
            descriptors[name] = built
        elif error is not None:
            failed_loads.append((name, error))

    if descriptors:
        logger.info("Loaded %d MCP server(s) from %s", len(descriptors), path)
    else:
        logger.info("No valid MCP servers found in %s", path)
    if failed_loads:
        logger.warning("Failed to load %d MCP server(s)", len(failed_loads))
    _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = descriptors
    return MCPDiscoveryResult(servers=dict(descriptors), failed_loads=failed_loads)
