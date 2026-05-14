"""MCP server discovery and translation to Microsoft Agent Framework tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_framework import MCPStdioTool, MCPStreamableHTTPTool

from .._logger import logger

MCPTool = MCPStdioTool | MCPStreamableHTTPTool


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
                "MCP server '%s': SSE transport is not supported by the MAF runtime; use 'http' (streamable-HTTP) or a stdio bridge.",
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
            static_headers = {str(key): str(value) for key, value in headers.items()}

            def header_provider(_ctx: Any) -> dict[str, str]:
                return dict(static_headers)

        return MCPStreamableHTTPTool(
            name=name,
            url=url,
            allowed_tools=allowed_tools,
            header_provider=header_provider,
        )

    logger.warning(
        "MCP server '%s': unrecognized config (no 'command' or 'url'), skipping",
        name,
    )
    return None


def discover_mcp_servers(app_root: Path) -> dict[str, MCPTool]:
    candidates = [
        app_root / ".vscode" / "mcp.json",
        app_root / "mcp.json",
    ]

    for path in candidates:
        if not path.exists():
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read MCP config from %s: %s", path, exc)
            continue

        servers = data.get("servers", {})
        if not isinstance(servers, dict):
            logger.warning("Invalid MCP config in %s: 'servers' must be an object", path)
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
        return tools

    return {}
