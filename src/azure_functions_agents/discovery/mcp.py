"""MCP server discovery and translation to Microsoft Agent Framework tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_framework import MCPStreamableHTTPTool

from .._logger import logger

MCPTool = MCPStreamableHTTPTool

_DISCOVERED_MCP_SERVERS_CACHE: dict[Path, dict[str, MCPTool]] = {}


def clear_mcp_cache() -> None:
    """Clear cached MCP server discovery results."""
    _DISCOVERED_MCP_SERVERS_CACHE.clear()


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
        headers = server.get("headers")
        header_provider = None
        if isinstance(headers, dict):
            static_headers = {str(key): str(value) for key, value in headers.items()}

            def header_provider(_ctx: Any) -> dict[str, str]:
                return dict(static_headers)

        return MCPStreamableHTTPTool(
            name=name,
            url=url,
            allowed_tools=allowed_tools,
            header_provider=header_provider,
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

    candidates = [
        resolved_root / ".vscode" / "mcp.json",
        resolved_root / "mcp.json",
    ]

    for path in candidates:
        if not path.exists():
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read MCP config from %s: %s", path, exc)
            continue

        if not isinstance(data, dict):
            logger.warning(
                "Ignoring %s: expected a JSON object at the top level, got %s.",
                path,
                type(data).__name__,
            )
            _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = {}
            return {}

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

    _DISCOVERED_MCP_SERVERS_CACHE[resolved_root] = {}
    return {}
