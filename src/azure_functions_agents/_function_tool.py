from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import wraps
from typing import Any, cast, overload

from pydantic import BaseModel

import azure_functions_agents as _package

from ._tool_descriptor import (
    NEVER_REQUIRE,
    ApprovalMode,
    ToolDescriptor,
    ToolInput,
    ToolPolicy,
    WorkflowTool,
    WorkflowToolMetadata,
    tool_description,
    tool_name,
)

__all__ = [
    "WorkflowTool",
    "WorkflowToolMetadata",
    "get_workflow_tool_handler",
    "get_workflow_tool_metadata",
    "tool",
    "workflow_tool",
]

_WORKFLOW_TOOL_METADATA_ATTR = "__azure_functions_agents_workflow_tool__"
_WORKFLOW_TOOL_HANDLER_ATTR = "__azure_functions_agents_workflow_handler__"


def get_workflow_tool_metadata(target: object) -> WorkflowToolMetadata | None:
    if isinstance(target, ToolDescriptor):
        return target.workflow_metadata
    metadata = getattr(target, _WORKFLOW_TOOL_METADATA_ATTR, None)
    if isinstance(metadata, WorkflowToolMetadata):
        return metadata
    return None


def get_workflow_tool_handler(target: Callable[..., Any]) -> Callable[..., Any]:
    """Get the dictionary adapter for a schema-wrapped tool."""
    handler = getattr(target, _WORKFLOW_TOOL_HANDLER_ATTR, None)
    if callable(handler):
        return cast("Callable[..., Any]", handler)
    return target


def _wrap_with_schema[SchemaT: BaseModel](
    func: Callable[[SchemaT], Any],
    schema: type[SchemaT],
) -> Callable[..., Awaitable[Any]]:
    @wraps(func)
    async def wrapper(**kwargs: Any) -> Any:
        params = schema(**kwargs)
        result = func(params)
        if inspect.isawaitable(result):
            return await result
        return result

    def workflow_handler(args: dict[str, Any]) -> Any:
        return func(schema(**args))

    async def async_workflow_handler(args: dict[str, Any]) -> Any:
        return await wrapper(**args)

    setattr(
        wrapper,
        _WORKFLOW_TOOL_HANDLER_ATTR,
        async_workflow_handler if inspect.iscoroutinefunction(func) else workflow_handler,
    )
    return wrapper


@overload
def tool(
    func: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    schema: None = None,
    approval_mode: ApprovalMode | None = None,
) -> ToolDescriptor: ...


@overload
def tool[SchemaT: BaseModel](
    func: Callable[[SchemaT], Any],
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[SchemaT],
    approval_mode: ApprovalMode | None = None,
) -> ToolDescriptor: ...


@overload
def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    schema: None = None,
    approval_mode: ApprovalMode | None = None,
) -> Callable[[Callable[..., Any]], ToolDescriptor]: ...


@overload
def tool[SchemaT: BaseModel](
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[SchemaT],
    approval_mode: ApprovalMode | None = None,
) -> Callable[[Callable[[SchemaT], Any]], ToolDescriptor]: ...


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[BaseModel] | None = None,
    approval_mode: ApprovalMode | None = None,
) -> ToolDescriptor | Callable[[Callable[..., Any]], ToolDescriptor]:
    """Record a Python tool without constructing a harness SDK wrapper."""

    def decorator(inner: Callable[..., Any]) -> ToolDescriptor:
        wrapped: Callable[..., Any] = inner
        input_model: type[BaseModel] | None = None
        if schema is not None:
            wrapped = _wrap_with_schema(inner, schema)
            input_model = schema
        descriptor = ToolDescriptor.create(
            name=name or tool_name(inner),
            description=(description or tool_description(inner)).strip(),
            func=wrapped,
            input_model=input_model,
            policy=ToolPolicy(
                approval_mode=approval_mode or NEVER_REQUIRE,
            ),
            workflow_metadata=get_workflow_tool_metadata(inner),
        )
        return descriptor

    if func is not None:
        return decorator(func)
    return decorator


def normalize_tool(candidate: ToolInput) -> ToolDescriptor:
    """Normalize an authored callable into the shared runtime contract."""
    if isinstance(candidate, ToolDescriptor):
        return candidate
    if not callable(candidate):
        raise TypeError(f"Tool '{tool_name(candidate)}' must be callable.")
    return tool(candidate)


@overload
def workflow_tool[DecoratedT](
    func: DecoratedT,
    *,
    name: str | None = None,
    description: str | None = None,
    public: bool = True,
    retry: _package.WorkflowRetryPolicy | None = None,
    timeout: str | None = None,
) -> DecoratedT: ...


@overload
def workflow_tool[DecoratedT](
    *,
    name: str | None = None,
    description: str | None = None,
    public: bool = True,
    retry: _package.WorkflowRetryPolicy | None = None,
    timeout: str | None = None,
) -> Callable[[DecoratedT], DecoratedT]: ...


def workflow_tool[DecoratedT](
    func: DecoratedT | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    public: bool = True,
    retry: _package.WorkflowRetryPolicy | None = None,
    timeout: str | None = None,
) -> DecoratedT | Callable[[DecoratedT], DecoratedT]:
    """Mark a callable for Dynamic Workflow execution without making a normal tool."""
    from .workflows.schema import WorkflowRetryPolicy, workflow_timeout_ms

    if retry is not None and not isinstance(retry, WorkflowRetryPolicy):
        raise TypeError("workflow_tool retry must be a WorkflowRetryPolicy")
    if timeout is not None:
        try:
            workflow_timeout_ms(timeout)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"workflow_tool timeout is invalid: {exc}") from exc
    metadata = WorkflowToolMetadata(
        name=name,
        description=description,
        public=public,
        retry=retry,
        timeout=timeout,
    )

    def decorator(inner: DecoratedT) -> DecoratedT:
        if isinstance(inner, ToolDescriptor):
            return cast("DecoratedT", replace(inner, workflow_metadata=metadata))
        if not callable(inner):
            raise TypeError("workflow_tool requires a callable")
        setattr(inner, _WORKFLOW_TOOL_METADATA_ATTR, metadata)
        return inner

    if func is not None:
        return decorator(func)
    return decorator
