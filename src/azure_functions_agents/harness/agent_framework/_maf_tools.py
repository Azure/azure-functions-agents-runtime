"""Lazy MAF adaptation and compatibility for legacy SDK tool extensions."""

from __future__ import annotations

import inspect
import uuid
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from agent_framework import FunctionTool as FunctionTool

from ..._tool_descriptor import (
    ToolCallable,
    ToolDescriptor,
    ToolPolicy,
    _input_model,
    _is_maf_tool,
)


@dataclass
class _LegacyTool:
    options: dict[str, Any] | None = None
    tool: FunctionTool | None = None


# SDK extension state stays here; neutral inventories carry only opaque keys.
_LEGACY_TOOLS: dict[str, _LegacyTool] = {}
_MAF_TOOL_ATTRIBUTES = frozenset({
    "_cached_parameters",
    "_context_parameter_name",
    "_declaration_only",
    "_input_model_explicitly_provided",
    "_input_schema_cached",
    "_instance",
    "_invocation_duration_histogram",
    "_invoke_sync_on_event_loop",
    "_schema_supplied",
    "__azure_functions_agents_workflow_tool__",
    "additional_properties",
    "approval_mode",
    "description",
    "func",
    "input_model",
    "invocation_count",
    "invocation_exception_count",
    "kind",
    "max_invocations",
    "max_invocation_exceptions",
    "name",
    "result_parser",
    "type",
})


def _unavailable(**arguments: Any) -> Any:
    raise TypeError("This tool requires the MAF compatibility adapter.")


def describe_maf_tool(candidate: object) -> ToolDescriptor:
    """Snapshot a legacy tool's metadata while retaining its SDK state only here."""
    if not isinstance(candidate, FunctionTool):
        raise TypeError("Expected a MAF FunctionTool.")
    unsupported: set[str] = set()
    if type(candidate) is not FunctionTool:
        unsupported.add("custom tool class")
    if candidate.max_invocations is not None:
        unsupported.add("max_invocations")
    if candidate.max_invocation_exceptions is not None:
        unsupported.add("max_invocation_exceptions")
    if candidate.declaration_only:
        unsupported.add("declaration_only")
    if candidate.result_parser is not None:
        unsupported.add("result_parser")
    if candidate.kind is not None:
        unsupported.add("kind")
    if candidate.additional_properties is not None:
        unsupported.add("additional_properties")
    if candidate._context_parameter_name is not None:
        unsupported.add("invocation context")
    if candidate._instance is not None:
        unsupported.add("bound SDK tool")
    if candidate._invoke_sync_on_event_loop:
        unsupported.add("sync invocation scheduling")
    if candidate.type != "function_tool":
        unsupported.add("tool type")
    if candidate.func is not None and _is_maf_tool(candidate.func):
        unsupported.add("nested SDK tool")
    unsupported.update(set(vars(candidate)) - _MAF_TOOL_ATTRIBUTES)
    func = candidate.func if candidate.func is not None else _unavailable
    if _is_maf_tool(func) or (
        inspect.ismethod(func)
        and type(func.__self__).__module__.startswith("agent_framework")
    ):
        unsupported.add("SDK callable")
        func = _unavailable
    model = None
    if not unsupported and candidate.input_model is not None:
        if not candidate._input_model_explicitly_provided:
            model = _input_model(candidate.name, func)
        elif candidate.input_model.__module__.startswith("agent_framework"):
            unsupported.add("SDK input model")
        else:
            model = candidate.input_model
    key = f"raw:{uuid.uuid4().hex}"
    policy = ToolPolicy(
        approval_mode=candidate.approval_mode,
        maf_only_options=tuple(sorted(unsupported)),
        maf_compatibility_key=key,
    )
    descriptor = ToolDescriptor.create(
        name=candidate.name,
        description=candidate.description,
        func=func,
        input_model=model,
        parameters=candidate.parameters(),
        policy=policy,
    )
    _LEGACY_TOOLS[key] = _LegacyTool(tool=candidate)
    weakref.finalize(policy, _LEGACY_TOOLS.pop, key, None)
    return descriptor


def with_maf_options(
    descriptor: ToolDescriptor, options: dict[str, Any]
) -> ToolDescriptor:
    """Defer MAF-specific keyword handling until the MAF execution boundary."""
    if "input_model" in options:
        raise TypeError("tool() supplies input_model through its schema argument.")
    for name in ("max_invocations", "max_invocation_exceptions"):
        value = options.get(name)
        if value is not None and value < 1:
            raise ValueError(f"{name} must be at least 1 or None.")
    key = f"runtime:{uuid.uuid4().hex}"
    policy = replace(
        descriptor.policy,
        maf_only_options=tuple(sorted(options)),
        maf_compatibility_key=key,
    )
    declaration = replace(descriptor, policy=policy)
    _LEGACY_TOOLS[key] = _LegacyTool(options=dict(options))
    weakref.finalize(policy, _LEGACY_TOOLS.pop, key, None)
    return declaration


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


def _function_tool(descriptor: ToolDescriptor, **options: Any) -> FunctionTool:
    legacy = bool(descriptor.policy.maf_only_options)
    return FunctionTool(
        name=descriptor.name,
        description=descriptor.description,
        func=descriptor.func if legacy else _maf_callable(descriptor),
        input_model=(
            descriptor.input_model or descriptor.parameters()
            if not legacy or descriptor.input_model_is_explicit
            else None
        ),
        approval_mode=descriptor.policy.approval_mode,
        **options,
    )


def build_maf_tools(descriptors: Sequence[ToolDescriptor]) -> list[FunctionTool]:
    """Materialize only the selected tools, preserving legacy SDK object identity."""
    tools: list[FunctionTool] = []
    for descriptor in descriptors:
        key = descriptor.policy.maf_compatibility_key
        if key is None:
            tools.append(_function_tool(descriptor))
            continue
        legacy = _LEGACY_TOOLS[key]
        if legacy.options is not None and inspect.ismethod(descriptor.func):
            tools.append(_function_tool(descriptor, **legacy.options))
            continue
        if legacy.tool is None:
            legacy.tool = _function_tool(descriptor, **(legacy.options or {}))
        tools.append(legacy.tool)
    return tools
