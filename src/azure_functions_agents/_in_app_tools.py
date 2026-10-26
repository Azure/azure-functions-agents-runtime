"""In-app Function tools declared with ``app.tool``.

Spike: a late-bound registry (decorators run after the catalog is frozen) plus adapters that
build a trigger payload from validated tool arguments and call the author's handler in-process.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, get_type_hints

import azure.functions as func
from azure.functions.decorators.function_app import FunctionBuilder
from azurefunctions.extensions.http.fastapi import Request, Response
from pydantic import BaseModel

from ._function_tool import tool as make_tool
from ._tool_descriptor import (
    ApprovalMode,
    ToolDescriptor,
    ToolPolicy,
)
from .config.schema import ResolvedAgent

type _Handler = Callable[..., Any]
type _InputBuilder = Callable[[bytes], Any]

_JSON_CONTENT_TYPE = "application/json"


def _http_request(body: bytes) -> Request:
    # The runtime's HTTP triggers use the Functions FastAPI extension, so HTTP handlers
    # receive a Starlette request rather than ``func.HttpRequest``.
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/tool",
        "raw_path": b"/api/tool",
        "query_string": b"",
        "headers": [(b"content-type", _JSON_CONTENT_TYPE.encode())],
        "server": ("localhost", 80),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _service_bus_message(body: bytes) -> func.ServiceBusMessage:
    return func.ServiceBusMessage(body=body, content_type=_JSON_CONTENT_TYPE)


def _queue_message(body: bytes) -> func.QueueMessage:
    return func.QueueMessage(body=body)


_INPUT_BUILDERS: dict[type, _InputBuilder] = {
    Request: _http_request,
    func.ServiceBusMessage: _service_bus_message,
    func.QueueMessage: _queue_message,
}


def unwrap_function_builder(candidate: Any) -> tuple[_Handler, bool]:
    """Return the author's handler and whether ``candidate`` was a trigger builder."""
    if isinstance(candidate, FunctionBuilder):
        return candidate._function.get_user_function(), True
    return candidate, False


def _trigger_input_builder(handler: _Handler) -> tuple[str, _InputBuilder] | None:
    try:
        hints = get_type_hints(handler)
    except (NameError, TypeError):
        hints = {}
    for parameter in inspect.signature(handler).parameters.values():
        annotation = hints.get(parameter.name, parameter.annotation)
        builder = _INPUT_BUILDERS.get(annotation) if isinstance(annotation, type) else None
        if builder is not None:
            return parameter.name, builder
    return None


def _jsonable_result(result: Any) -> Any:
    # Message-trigger handlers have no ``$return`` binding, so they return ``None``.
    if result is None:
        return "completed"
    if isinstance(result, Response):
        return {
            "status_code": result.status_code,
            "body": bytes(result.body).decode("utf-8", errors="replace"),
        }
    return result


def _trigger_backed_tool(
    handler: _Handler,
    *,
    name: str,
    description: str,
    schema: type[BaseModel],
    parameter_name: str,
    build_input: _InputBuilder,
    approval_mode: ApprovalMode | str | None,
) -> ToolDescriptor:
    extra = [
        p.name
        for p in inspect.signature(handler).parameters.values()
        if p.name != parameter_name and p.default is inspect.Parameter.empty
    ]
    if extra:
        raise TypeError(
            f"app.tool on trigger-backed function {handler.__name__!r} supports a single "
            f"trigger input parameter; unsupported required parameter(s): {extra}"
        )

    def payload(values: dict[str, Any]) -> bytes:
        return json.dumps(values, ensure_ascii=False, default=str).encode("utf-8")

    if inspect.iscoroutinefunction(handler):

        async def invoke_async(**values: Any) -> Any:
            return _jsonable_result(
                await handler(**{parameter_name: build_input(payload(values))})
            )

        invoke: _Handler = invoke_async
    else:

        def invoke_sync(**values: Any) -> Any:
            return _jsonable_result(handler(**{parameter_name: build_input(payload(values))}))

        invoke = invoke_sync

    return ToolDescriptor.create(
        name=name,
        description=description,
        func=invoke,
        input_model=schema,
        parameters=schema.model_json_schema(),
        policy=ToolPolicy(approval_mode=ApprovalMode(approval_mode or "never_require")),
    )


def build_in_app_tool(
    handler: _Handler,
    *,
    name: str | None,
    description: str | None,
    schema: type[BaseModel] | None,
    approval_mode: ApprovalMode | str | None,
) -> ToolDescriptor:
    """Build a descriptor for a standalone or trigger-backed function."""
    resolved_name = name or handler.__name__
    resolved_description = (description or inspect.getdoc(handler) or "").strip()
    if not resolved_description:
        raise ValueError(
            f"app.tool {resolved_name!r} needs a description: add a docstring or pass "
            "description=. The agent uses it to decide when to call the tool."
        )

    trigger_input = _trigger_input_builder(handler)
    if trigger_input is None:
        return make_tool(
            handler,
            name=resolved_name,
            description=resolved_description,
            schema=schema,
            approval_mode=approval_mode,
        )
    if schema is None:
        raise TypeError(
            f"app.tool {resolved_name!r} wraps a trigger-backed function, so the tool's "
            "input contract can't be inferred; pass schema=<pydantic model> describing "
            "the payload your handler reads."
        )
    return _trigger_backed_tool(
        handler,
        name=resolved_name,
        description=resolved_description,
        schema=schema,
        parameter_name=trigger_input[0],
        build_input=trigger_input[1],
        approval_mode=approval_mode,
    )


@dataclass(frozen=True)
class _Entry:
    descriptor: ToolDescriptor
    agents: frozenset[str] | None


class InAppToolRegistry:
    """Shared, append-only registry resolved lazily per agent at execution time."""

    def __init__(self) -> None:
        self._entries: list[_Entry] = []

    def add(self, descriptor: ToolDescriptor, agents: Sequence[str] | None) -> None:
        if any(entry.descriptor.name == descriptor.name for entry in self._entries):
            raise ValueError(f"Duplicate app.tool name {descriptor.name!r}.")
        self._entries.append(
            _Entry(descriptor, None if agents is None else frozenset(agents))
        )

    def tools_for(self, resolved: ResolvedAgent) -> tuple[ToolDescriptor, ...]:
        if resolved.tools_disabled:
            return ()
        excluded = set(resolved.tool_filter.exclude)
        return tuple(
            entry.descriptor
            for entry in self._entries
            if entry.descriptor.name not in excluded
            and (entry.agents is None or resolved.slug in entry.agents)
        )

    def provider_for(self, resolved: ResolvedAgent) -> Callable[[], tuple[ToolDescriptor, ...]]:
        return lambda: self.tools_for(resolved)
