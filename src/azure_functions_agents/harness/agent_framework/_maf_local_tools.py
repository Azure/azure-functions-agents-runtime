"""MAF translation for prepared local process-backed tools."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any
from weakref import ReferenceType, ref

from agent_framework import Content, MCPStdioTool

from ..._local_tool_descriptor import PreparedLocalTool
from ..._logger import logger

_MAF_LOCAL_TOOLS_CACHE: dict[
    int, tuple[ReferenceType[PreparedLocalTool], MCPStdioTool]
] = {}


class _RestartingMCPStdioTool(MCPStdioTool):
    async def call_tool(self, tool_name: str, **kwargs: Any) -> str | list[Content]:
        try:
            return await super().call_tool(tool_name, **kwargs)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.connect(reset=True))
            except Exception:
                logger.warning(
                    "Failed to restart cancelled local tool process: %s",
                    self.name,
                    exc_info=True,
                )
            raise


def clear_maf_local_tools_cache() -> None:
    """Forget wrapper mappings without taking ownership of MAF process lifetime."""
    _MAF_LOCAL_TOOLS_CACHE.clear()


def _build_maf_local_tool(descriptor: PreparedLocalTool) -> MCPStdioTool:
    key = id(descriptor)
    cached = _MAF_LOCAL_TOOLS_CACHE.get(key)
    if cached is not None and cached[0]() is descriptor:
        return cached[1]

    wrapper = _RestartingMCPStdioTool(
        name=f"local_{descriptor.name}",
        command=str(descriptor.launch.command),
        args=list(descriptor.launch.arguments),
        env=dict(descriptor.launch.environment),
        allowed_tools=[descriptor.name],
        load_tools=True,
        load_prompts=False,
        approval_mode="never_require",
    )

    def forget_descriptor(_reference: ReferenceType[PreparedLocalTool]) -> None:
        _MAF_LOCAL_TOOLS_CACHE.pop(key, None)

    _MAF_LOCAL_TOOLS_CACHE[key] = (ref(descriptor, forget_descriptor), wrapper)
    return wrapper


def build_maf_local_tools(
    descriptors: Sequence[PreparedLocalTool],
) -> list[MCPStdioTool]:
    """Reuse each prepared descriptor's MAF stdio wrapper per Python worker."""
    return [_build_maf_local_tool(descriptor) for descriptor in descriptors]
