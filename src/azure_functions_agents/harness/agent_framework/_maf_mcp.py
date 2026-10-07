"""MAF-only translation of remote MCP server descriptors."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from weakref import ReferenceType, ref

from agent_framework import MCPStreamableHTTPTool

from ..._logger import logger
from ..._mcp_auth import build_mcp_header_provider
from ...discovery.mcp import MCPServerDescriptor

_MAF_MCP_TOOLS_CACHE: dict[
    int, tuple[ReferenceType[MCPServerDescriptor], MCPStreamableHTTPTool]
] = {}


class _OwnedHTTPClientMCPStreamableHTTPTool(MCPStreamableHTTPTool):
    """Close SDK-created HTTP clients when the MCP transport context ends."""

    @asynccontextmanager
    async def get_mcp_client(self) -> AsyncIterator[Any]:
        prior_http_client = self._httpx_client
        context = super().get_mcp_client()
        owned_http_client = (
            self._httpx_client
            if self._header_provider is not None
            and prior_http_client is None
            and self._httpx_client is not None
            else None
        )
        try:
            async with context as transport:
                yield transport
        except BaseException:
            try:
                await _close_owned_http_client(self, owned_http_client)
            except BaseException:
                logger.error("MAF MCP HTTP client cleanup failed.", exc_info=True)
            raise
        finally:
            if owned_http_client is not None and self._httpx_client is owned_http_client:
                try:
                    await _close_owned_http_client(self, owned_http_client)
                except Exception:
                    logger.error("MAF MCP HTTP client cleanup failed.", exc_info=True)
                    raise


async def _close_owned_http_client(
    tool: _OwnedHTTPClientMCPStreamableHTTPTool, owned_http_client: Any | None
) -> None:
    if owned_http_client is None:
        return
    try:
        await owned_http_client.aclose()
    finally:
        if tool._httpx_client is owned_http_client:
            tool._httpx_client = None
        if hasattr(tool, "_inject_headers_hook"):
            delattr(tool, "_inject_headers_hook")


def clear_maf_mcp_cache() -> None:
    """Forget wrapper mappings without taking ownership of MAF connection lifetime."""
    _MAF_MCP_TOOLS_CACHE.clear()


def _build_maf_mcp_tool(server: MCPServerDescriptor) -> MCPStreamableHTTPTool:
    key = id(server)
    cached = _MAF_MCP_TOOLS_CACHE.get(key)
    if cached is not None and cached[0]() is server:
        return cached[1]

    header_provider = build_mcp_header_provider(server)
    tool = _OwnedHTTPClientMCPStreamableHTTPTool(
        name=server.name,
        url=server.url,
        allowed_tools=list(server.tools) if server.tools is not None else None,
        load_tools=True,
        load_prompts=False,
        approval_mode="never_require",
        header_provider=header_provider,
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
