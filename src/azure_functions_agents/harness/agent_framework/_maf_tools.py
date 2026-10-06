"""Lazy MAF adaptation of runtime-owned tool declarations."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any

from agent_framework import FunctionTool

from ..._tool_descriptor import ToolCallable, ToolDescriptor


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
    """Let MAF validate authored options and own each constructed tool's state."""
    tools: list[FunctionTool] = []
    for descriptor in descriptors:
        sdk_owned = bool(descriptor.maf_options)
        tools.append(FunctionTool(
            name=descriptor.name,
            description=descriptor.description,
            func=descriptor.func if sdk_owned else _maf_callable(descriptor),
            input_model=(
                descriptor.input_model or descriptor.parameters()
                if not sdk_owned or descriptor.input_model_is_explicit
                else None
            ),
            approval_mode=descriptor.policy.approval_mode,
            **dict(descriptor.maf_options),
        ))
    return tools
