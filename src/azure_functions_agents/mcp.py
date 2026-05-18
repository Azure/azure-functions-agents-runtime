"""MCP server discovery and translation to Microsoft Agent Framework tools.

Reads ``.vscode/mcp.json`` (or top-level ``mcp.json``) and converts each
declared server to a MAF MCP tool object:

* stdio (``command`` / ``args`` / ``env``)  → :class:`MCPStdioTool`
* HTTP / streamable-HTTP (``url`` / ``headers``) → :class:`MCPStreamableHTTPTool`

The ``tools`` array on each server controls which MCP tools are exposed:

* ``["*"]`` (the default) → ``allowed_tools=None`` (all tools allowed)
* explicit list           → passed through as ``allowed_tools=[...]``

SSE-only MCP servers are NOT supported by MAF v1.2.x. Declare them with
``type: http`` (streamable-HTTP) or use a stdio bridge.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Union

from agent_framework import MCPStdioTool, MCPStreamableHTTPTool

from ._logger import logger
from .config import get_app_root

MCPTool = Union[MCPStdioTool, MCPStreamableHTTPTool]

_MCP_TOOLS_CACHE: Optional[List[MCPTool]] = None


def _build_mcp_tool(name: str, server: Dict[str, Any]) -> Optional[MCPTool]:
    """Translate a single mcp.json entry to a MAF MCP tool object."""
    server_type = str(server.get("type", "")).lower()
    raw_tools = server.get("tools", ["*"])
    if isinstance(raw_tools, list) and any(t == "*" for t in raw_tools):
        allowed_tools = None  # Wildcard → allow everything
    elif isinstance(raw_tools, list):
        allowed_tools = list(raw_tools)
    else:
        allowed_tools = None

    if "command" in server or server_type == "local" or server_type == "stdio":
        command = str(server.get("command", "")).strip()
        if not command:
            logger.warning("MCP server '%s': missing 'command', skipping", name)
            return None
        return MCPStdioTool(
            name=name,
            command=command,
            args=server.get("args") or None,
            env=server.get("env") or None,
            allowed_tools=allowed_tools,
        )

    if "url" in server or server_type in {"http", "sse", "streamable-http"}:
        if server_type == "sse":
            logger.warning(
                "MCP server '%s': SSE transport is not supported by the MAF runtime; "
                "use 'http' (streamable-HTTP) or a stdio bridge.",
                name,
            )
            return None
        url = str(server.get("url", "")).strip()
        if not url:
            logger.warning("MCP server '%s': missing 'url', skipping", name)
            return None
        headers = server.get("headers")
        header_provider = None
        if isinstance(headers, dict):
            # MAF takes a callable that returns headers per request. Keep it
            # simple: return a copy of the static dict from mcp.json.
            static_headers = {str(k): str(v) for k, v in headers.items()}

            def header_provider(_ctx):  # noqa: ANN001 - opaque MAF context
                return dict(static_headers)
        return MCPStreamableHTTPTool(
            name=name,
            url=url,
            allowed_tools=allowed_tools,
            header_provider=header_provider,
        )

    logger.warning("MCP server '%s': unrecognized config (no 'command' or 'url'), skipping", name)
    return None


def _load_mcp_tools_from_file() -> List[MCPTool]:
    app_root = str(get_app_root())
    candidates = [
        os.path.join(app_root, ".vscode", "mcp.json"),
        os.path.join(app_root, "mcp.json"),
    ]

    for path in candidates:
        if not os.path.exists(path):
            continue

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("Failed to read MCP config from %s: %s", path, e)
            continue

        servers = data.get("servers", {})
        if not isinstance(servers, dict):
            logger.warning("Invalid MCP config in %s: 'servers' must be an object", path)
            return []

        tools: List[MCPTool] = []
        for name in sorted(servers.keys()):
            config = servers[name]
            if not isinstance(name, str) or not isinstance(config, dict):
                continue
            built = _build_mcp_tool(name, config)
            if built is not None:
                tools.append(built)

        if tools:
            logger.info("Loaded %d MCP server(s) from %s", len(tools), path)
        else:
            logger.info("No valid MCP servers found in %s", path)
        return tools

    return []


def get_cached_mcp_tools() -> List[MCPTool]:
    """Return all MCP tools declared in ``.vscode/mcp.json`` (or ``mcp.json``).

    Cached after the first call.
    """
    global _MCP_TOOLS_CACHE
    if _MCP_TOOLS_CACHE is None:
        _MCP_TOOLS_CACHE = _load_mcp_tools_from_file()
    return _MCP_TOOLS_CACHE

