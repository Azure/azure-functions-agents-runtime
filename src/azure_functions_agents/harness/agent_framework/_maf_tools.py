"""Lazy MAF adaptation of runtime-owned tool declarations."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any

from ..._tool_descriptor import ToolCallable, ToolDescriptor
from ._maf_warnings import suppress_experimental_warnings

with suppress_experimental_warnings():
    from agent_framework import FunctionTool


def _maf_callable(descriptor: ToolDescriptor) -> ToolCallable:
    if descriptor.input_model is None:
        async def validated(**arguments: Any) -> Any:
            return await descriptor.invoke(arguments=arguments)

        return validated
    if inspect.iscoroutinefunction(descriptor.func):
        async def asynchronous(**arguments: Any) -> Any:
            result = descriptor.func(**arguments)
            return await result if inspect.isawaitable(result) else result

        return asynchronous

    def synchronous(**arguments: Any) -> Any:
        return descriptor.func(**arguments)

    return synchronous


def build_maf_tools(descriptors: Sequence[ToolDescriptor]) -> list[FunctionTool]:
    """Adapt neutral runtime tool descriptors into MAF FunctionTool instances."""
    tools: list[FunctionTool] = []
    for descriptor in descriptors:
        tools.append(FunctionTool(
            name=descriptor.name,
            description=descriptor.description,
            func=_maf_callable(descriptor),
            input_model=descriptor.input_model or descriptor.parameters(),
            approval_mode=descriptor.policy.approval_mode,
        ))
    return tools
