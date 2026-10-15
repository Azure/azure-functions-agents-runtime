"""Immutable, SDK-independent Python tool declarations."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MethodType
from typing import Annotated, Any, Final, Literal, get_args, get_origin, get_type_hints

from pydantic import BaseModel, Field, ValidationError, create_model

import azure_functions_agents as _package

type ApprovalMode = Literal["always_require", "never_require"]
ALWAYS_REQUIRE: Final[ApprovalMode] = "always_require"
NEVER_REQUIRE: Final[ApprovalMode] = "never_require"
type ToolCallable = Callable[..., Any]
type ToolInput = object

_JSON_PARAMETER_TYPES: dict[str, type[object] | tuple[type[object], ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def _parameter_type_matches(value: Any, kind: str) -> bool:
    expected = _JSON_PARAMETER_TYPES.get(kind)
    if expected is None:
        return True
    if kind in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def _validate_json_arguments(
    name: str, values: dict[str, Any], schema: dict[str, Any]
) -> None:
    """Preserve top-level schema checks without enforcing extra JSON Schema keywords."""
    missing = {
        key for key in schema.get("required", ()) if isinstance(key, str)
    } - values.keys()
    if missing:
        raise TypeError(f"Invalid arguments for '{name}': missing {sorted(missing)}")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        extra = values.keys() - properties.keys()
        if extra:
            raise TypeError(f"Invalid arguments for '{name}': unexpected {sorted(extra)}")
    for key, value in values.items():
        rule = properties.get(key)
        if not isinstance(rule, dict):
            continue
        choices = rule.get("enum")
        if isinstance(choices, list) and choices and value not in choices:
            raise TypeError(f"Invalid arguments for '{name}': '{key}' is not in {choices!r}")
        kinds = rule.get("type")
        if isinstance(kinds, str):
            kinds = [kinds]
        if isinstance(kinds, list):
            types = [kind for kind in kinds if isinstance(kind, str)]
            if types and not any(_parameter_type_matches(value, kind) for kind in types):
                raise TypeError(f"Invalid arguments for '{name}': '{key}' must match {types!r}")


@dataclass(frozen=True)
class WorkflowToolMetadata:
    """Author-supplied workflow tool metadata attached by ``@workflow_tool``."""

    name: str | None = None
    description: str | None = None
    public: bool = True
    retry: _package.WorkflowRetryPolicy | None = None
    timeout: str | None = None


@dataclass(frozen=True)
class WorkflowTool:
    """Discovered workflow tool declaration ready for registry registration."""

    name: str
    description: str
    handler: ToolCallable | None
    public: bool = True
    retry: _package.WorkflowRetryPolicy | None = None
    timeout: str | None = None


@dataclass(frozen=True)
class ToolPolicy:
    """Harness-neutral authoring policy."""

    approval_mode: ApprovalMode = NEVER_REQUIRE


def _parameter_annotation(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated:
        value, *metadata = get_args(annotation)
        if metadata and isinstance(metadata[0], str):
            description = Field(description=metadata[0])
            if len(metadata) > 1:
                return Annotated[value, description, tuple(metadata[1:])]
            return Annotated[value, description]
    return annotation


def _function_hints(func: ToolCallable) -> dict[str, Any]:
    target = func if inspect.isroutine(func) or inspect.isclass(func) else type(func).__call__
    try:
        return get_type_hints(target, include_extras=True)
    except (NameError, TypeError):
        return {
            parameter.name: parameter.annotation
            for parameter in inspect.signature(func).parameters.values()
        }


def _input_model(name: str, func: ToolCallable) -> type[BaseModel]:
    signature = inspect.signature(func)
    hints = _function_hints(func)
    fields: dict[str, Any] = {}
    for parameter in signature.parameters.values():
        if parameter.name in {"self", "cls"} or parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        annotation = hints.get(parameter.name, parameter.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = str
        default = parameter.default if parameter.default is not inspect.Parameter.empty else ...
        fields[parameter.name] = (_parameter_annotation(annotation), default)
    return create_model(f"{name}_input", **fields)


@dataclass(frozen=True)
class ToolDescriptor:
    """A tool's frozen schema, callable, and execution policy, without SDK state."""

    name: str
    description: str
    func: ToolCallable = field(repr=False, compare=False)
    input_model: type[BaseModel] | None = field(default=None, repr=False, compare=False)
    policy: ToolPolicy = field(default_factory=ToolPolicy)
    workflow_metadata: WorkflowToolMetadata | None = field(default=None, repr=False)
    _parameters_json: str = field(default="{}", repr=False)

    @classmethod
    def create(
        cls,
        *,
        name: str,
        description: str,
        func: ToolCallable,
        input_model: type[BaseModel] | None = None,
        parameters: Mapping[str, Any] | None = None,
        policy: ToolPolicy | None = None,
        workflow_metadata: WorkflowToolMetadata | None = None,
    ) -> ToolDescriptor:
        model = input_model
        if parameters is None:
            model = model or _input_model(name, func)
            parameters = model.model_json_schema()
        return cls(
            name=name,
            description=description,
            func=func,
            input_model=model,
            policy=policy or ToolPolicy(),
            workflow_metadata=workflow_metadata,
            _parameters_json=json.dumps(dict(parameters), ensure_ascii=True, allow_nan=False),
        )

    def parameters(self) -> dict[str, Any]:
        """Return a fresh serialization of the registered JSON input schema."""
        parameters: dict[str, Any] = json.loads(self._parameters_json)
        return parameters

    async def invoke(
        self, *, arguments: dict[str, Any], tool_call_id: str | None = None
    ) -> Any:
        """Validate arguments before calling once and awaiting at most once."""
        values = dict(arguments)
        if self.input_model is not None:
            try:
                validated = self.input_model.model_validate(values)
                values = validated.model_dump(exclude_unset=True)
            except ValidationError as exc:
                raise TypeError(f"Invalid arguments for '{self.name}': {exc}") from exc
        _validate_json_arguments(self.name, values, self.parameters())
        if inspect.iscoroutinefunction(self.func):
            result = self.func(**values)
        else:
            result = await asyncio.to_thread(self.func, **values)
        if inspect.isawaitable(result):
            return await result
        return result

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)

    def __get__(self, instance: object | None, owner: type | None = None) -> ToolDescriptor:
        if instance is None:
            return self
        parameters = tuple(inspect.signature(self.func).parameters)
        if parameters and parameters[0] in {"self", "cls"}:
            return replace(self, func=MethodType(self.func, instance))
        return self


def tool_name(candidate: ToolInput) -> str:
    """Read public tool metadata without interpreting the implementation."""
    name = getattr(candidate, "name", None) or getattr(candidate, "__name__", None)
    if name is None and callable(candidate):
        name = type(candidate).__name__
    return str(name or "")


def tool_description(candidate: ToolInput) -> str:
    description = getattr(candidate, "description", None)
    return (
        description if isinstance(description, str) and description else candidate.__doc__ or ""
    ).strip()


def describe_tools(candidates: Iterable[ToolInput]) -> tuple[ToolDescriptor, ...]:
    return tuple(candidate for candidate in candidates if isinstance(candidate, ToolDescriptor))
