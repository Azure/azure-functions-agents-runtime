"""MAF-only translation of remote MCP server descriptors."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from weakref import ReferenceType, ref

from agent_framework import MCPStreamableHTTPTool
from httpx import AsyncClient, Request

from ..._mcp_auth import MCPHeaderProvider, build_mcp_header_provider
from ...discovery.mcp import MCPServerDescriptor

_MAF_MCP_TOOLS_CACHE: dict[
    int, tuple[ReferenceType[MCPServerDescriptor], MCPStreamableHTTPTool]
] = {}


def clear_maf_mcp_cache() -> None:
    """Forget wrapper mappings without taking ownership of MAF connection lifetime."""
    _MAF_MCP_TOOLS_CACHE.clear()


def _build_http_client(header_provider: MCPHeaderProvider | None) -> AsyncClient | None:
    if header_provider is None:
        return None

    async def inject_headers(request: Request) -> None:
        headers = await asyncio.to_thread(header_provider, {})
        for key, value in headers.items():
            request.headers[key] = value

    return AsyncClient(follow_redirects=True, event_hooks={"request": [inject_headers]})


def _build_maf_mcp_tool(server: MCPServerDescriptor) -> MCPStreamableHTTPTool:
    key = id(server)
    cached = _MAF_MCP_TOOLS_CACHE.get(key)
    if cached is not None and cached[0]() is server:
        return cached[1]

    header_provider = build_mcp_header_provider(server)
    tool = MCPStreamableHTTPTool(
        name=server.name,
        url=server.url,
        allowed_tools=list(server.tools) if server.tools is not None else None,
        load_tools=True,
        load_prompts=False,
        approval_mode="never_require",
        header_provider=header_provider,
        http_client=_build_http_client(header_provider),
    )

    def forget_descriptor(_reference: ReferenceType[MCPServerDescriptor]) -> None:
        _MAF_MCP_TOOLS_CACHE.pop(key, None)

    _MAF_MCP_TOOLS_CACHE[key] = (ref(server, forget_descriptor), tool)
    return tool


def build_maf_mcp_tools(
    servers: Sequence[MCPServerDescriptor],
) -> list[MCPStreamableHTTPTool]:
    """Reuse each discovered descriptor's MAF wrapper at the execution boundary."""
    return [_build_maf_mcp_tool(server) for server in servers]
