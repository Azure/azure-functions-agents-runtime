from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar, overload

from agent_framework import FunctionTool
from pydantic import BaseModel

__all__ = ["FunctionTool", "tool"]

SchemaT = TypeVar("SchemaT", bound=BaseModel)


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
