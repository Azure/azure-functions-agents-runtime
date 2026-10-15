"""Lazy MAF adaptation of runtime-owned tool declarations."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any

from agent_framework import FunctionTool, ToolTypes

from ..._tool_descriptor import ToolCallable, ToolDescriptor, ToolInput


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


def build_maf_tools(candidates: Sequence[ToolInput]) -> list[ToolTypes | ToolCallable]:
    """Adapt runtime descriptors and leave authored inputs to MAF."""
    tools: list[ToolTypes | ToolCallable] = []
    for candidate in candidates:
        if isinstance(candidate, ToolDescriptor):
            tools.append(FunctionTool(
                name=candidate.name,
                description=candidate.description,
                func=_maf_callable(candidate),
                input_model=candidate.input_model or candidate.parameters(),
                approval_mode=candidate.policy.approval_mode,
            ))
        else:
            tools.append(candidate)
    return tools
