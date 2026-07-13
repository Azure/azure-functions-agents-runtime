from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar, overload

from agent_framework import FunctionTool
from pydantic import BaseModel

__all__ = [
    "FunctionTool",
    "WorkflowTool",
    "WorkflowToolMetadata",
    "get_workflow_tool_metadata",
    "tool",
    "workflow_tool",
]

SchemaT = TypeVar("SchemaT", bound=BaseModel)
_WORKFLOW_TOOL_METADATA_ATTR = "__azure_functions_agents_workflow_tool__"


@dataclass(frozen=True)
class WorkflowToolMetadata:
    """Author-supplied workflow tool metadata attached by ``@workflow_tool``."""

    name: str | None = None
    description: str | None = None
    public: bool = True


@dataclass(frozen=True)
class WorkflowTool:
    """Discovered workflow tool declaration ready for registry registration."""

    name: str
    description: str
    handler: Callable[..., Any] | None
    public: bool = True


def get_workflow_tool_metadata(target: object) -> WorkflowToolMetadata | None:
    metadata = getattr(target, _WORKFLOW_TOOL_METADATA_ATTR, None)
    if isinstance(metadata, WorkflowToolMetadata):
        return metadata
    return None


def _wrap_with_schema(  # noqa: UP047
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

    return wrapper


@overload
def tool(
    func: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    schema: None = None,
    **kwargs: Any,
) -> FunctionTool: ...


@overload
def tool(  # noqa: UP047
    func: Callable[[SchemaT], Any],
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[SchemaT],
    **kwargs: Any,
) -> FunctionTool: ...


@overload
def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    schema: None = None,
    **kwargs: Any,
) -> Callable[[Callable[..., Any]], FunctionTool]: ...


@overload
def tool(  # noqa: UP047
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[SchemaT],
    **kwargs: Any,
) -> Callable[[Callable[[SchemaT], Any]], FunctionTool]: ...


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[BaseModel] | None = None,
    **kwargs: Any,
) -> FunctionTool | Callable[[Callable[..., Any]], FunctionTool]:
    def decorator(inner: Callable[..., Any]) -> FunctionTool:
        wrapped: Callable[..., Any] = inner
        input_model: type[BaseModel] | None = None
        if schema is not None:
            wrapped = _wrap_with_schema(inner, schema)
            input_model = schema
        return FunctionTool(
            name=name or inner.__name__,
            description=(description or inner.__doc__ or "").strip(),
            func=wrapped,
            input_model=input_model,
            **kwargs,
        )

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
    **kwargs: Any,
) -> DecoratedT: ...


@overload
def workflow_tool[DecoratedT](
    *,
    name: str | None = None,
    description: str | None = None,
    public: bool = True,
    **kwargs: Any,
) -> Callable[[DecoratedT], DecoratedT]: ...


def workflow_tool[DecoratedT](
    func: DecoratedT | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    public: bool = True,
    **kwargs: Any,
) -> DecoratedT | Callable[[DecoratedT], DecoratedT]:
    """Mark a ``tools/`` callable as a Dynamic Workflow tool.

    The decorator records metadata and returns the original object so it does not
    make the callable a normal MAF ``FunctionTool`` unless ``@tool`` is also used.
    """
    if kwargs:
        unknown = ", ".join(sorted(kwargs))
        raise TypeError(f"unknown workflow_tool argument(s): {unknown}")

    metadata = WorkflowToolMetadata(
        name=name,
        description=description,
        public=public,
    )

    def decorator(inner: DecoratedT) -> DecoratedT:
        if not callable(inner) and not isinstance(inner, FunctionTool):
            raise TypeError("@workflow_tool can only decorate a callable or FunctionTool")
        setattr(inner, _WORKFLOW_TOOL_METADATA_ATTR, metadata)
        return inner

    if func is not None:
        return decorator(func)
    return decorator
