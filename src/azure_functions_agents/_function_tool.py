from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import wraps
from typing import Any, cast, overload

from pydantic import BaseModel

import azure_functions_agents as _package

from ._logger import logger
from ._tool_descriptor import (
    ApprovalMode,
    ToolDescriptor,
    ToolPolicy,
    WorkflowTool,
    WorkflowToolMetadata,
    is_harness_object,
    warn_unsupported_tool,
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
_SUPPORTED_TOOL_KWARGS = ("name", "description", "schema", "approval_mode")


def get_workflow_tool_metadata(target: object) -> WorkflowToolMetadata | None:
    if is_harness_object(target):
        return None
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


def _warn_ignored_tool_kwargs(kwargs: dict[str, Any]) -> None:
    if not kwargs:
        return
    names = ", ".join(sorted(kwargs))
    logger.warning(
        "Ignoring unsupported @tool keyword argument(s): %s. Supported @tool keyword "
        "arguments are: %s. Extra keyword arguments are ignored in every harness.",
        names,
        ", ".join(_SUPPORTED_TOOL_KWARGS),
    )


@overload
def tool(
    func: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    schema: None = None,
    approval_mode: ApprovalMode | None = None,
    **kwargs: Any,
) -> ToolDescriptor: ...


@overload
def tool[SchemaT: BaseModel](
    func: Callable[[SchemaT], Any],
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[SchemaT],
    approval_mode: ApprovalMode | None = None,
    **kwargs: Any,
) -> ToolDescriptor: ...


@overload
def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    schema: None = None,
    approval_mode: ApprovalMode | None = None,
    **kwargs: Any,
) -> Callable[[Callable[..., Any]], ToolDescriptor]: ...


@overload
def tool[SchemaT: BaseModel](
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[SchemaT],
    approval_mode: ApprovalMode | None = None,
    **kwargs: Any,
) -> Callable[[Callable[[SchemaT], Any]], ToolDescriptor]: ...


def tool(
    func: object | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[BaseModel] | None = None,
    approval_mode: ApprovalMode | None = None,
    **kwargs: Any,
) -> object:
    """Record a Python tool without constructing a harness SDK wrapper."""
    _warn_ignored_tool_kwargs(kwargs)

    def decorator(inner: object) -> object:
        if is_harness_object(inner):
            warn_unsupported_tool()
            return inner
        if not callable(inner):
            warn_unsupported_tool()
            return inner
        wrapped: Callable[..., Any] = inner
        input_model: type[BaseModel] | None = None
        if schema is not None:
            wrapped = _wrap_with_schema(inner, schema)
            input_model = schema
        descriptor = ToolDescriptor.create(
            name=name or wrapped.__name__,
            description=(description or inner.__doc__ or "").strip(),
            func=wrapped,
            input_model=input_model,
            policy=ToolPolicy(
                approval_mode=approval_mode or "never_require",
            ),
            workflow_metadata=get_workflow_tool_metadata(inner),
        )
        return descriptor

    if func is not None:
        return decorator(func)
    return decorator


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
        if is_harness_object(inner):
            warn_unsupported_tool()
            return inner
        if isinstance(inner, ToolDescriptor):
            return cast("DecoratedT", replace(inner, workflow_metadata=metadata))
        if not callable(inner):
            raise TypeError("@workflow_tool can only decorate a callable or runtime @tool")
        setattr(inner, _WORKFLOW_TOOL_METADATA_ATTR, metadata)
        return inner

    if func is not None:
        return decorator(func)
    return decorator
