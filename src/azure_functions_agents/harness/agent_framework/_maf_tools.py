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


def build_maf_tools(candidates: Sequence[ToolDescriptor]) -> list[FunctionTool]:
    """Adapt normalized runtime declarations into MAF tools."""
    return [
        FunctionTool(
            name=candidate.name,
            description=candidate.description,
            func=_maf_callable(candidate),
            input_model=candidate.input_model or candidate.parameters(),
            approval_mode=candidate.policy.approval_mode.value,
        )
        for candidate in candidates
    ]
