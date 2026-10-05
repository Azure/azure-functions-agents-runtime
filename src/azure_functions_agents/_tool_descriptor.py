"""Immutable, SDK-independent Python tool declarations."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MethodType
from typing import Annotated, Any, Literal, get_args, get_origin, get_type_hints

import jsonschema
from pydantic import BaseModel, Field, ValidationError, create_model

import azure_functions_agents as _package

type ApprovalMode = Literal["always_require", "never_require"]
type ToolCallable = Callable[..., Any]
type ToolInput = ToolDescriptor | ToolCallable


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
    """Authoring policy, including an opaque reference to MAF-only extensions."""

    approval_mode: ApprovalMode = "never_require"
    maf_only_options: tuple[str, ...] = ()
    maf_compatibility_key: str | None = field(default=None, repr=False)


def _parameter_annotation(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated:
        value, *metadata = get_args(annotation)
        if metadata and isinstance(metadata[0], str):
            description = Field(description=metadata[0])
            if len(metadata) > 1:
                return Annotated[value, description, tuple(metadata[1:])]
            return Annotated[value, description]
    return annotation


def _is_invocation_context(annotation: Any) -> bool:
    candidates = get_args(annotation) or (annotation,)
    return any(
        candidate == "FunctionInvocationContext"
        or (
            isinstance(candidate, type)
            and candidate.__name__ == "FunctionInvocationContext"
            and candidate.__module__.startswith("agent_framework")
        )
        for candidate in candidates
    )


def _function_hints(func: ToolCallable) -> dict[str, Any]:
    try:
        return get_type_hints(func, include_extras=True)
    except (NameError, TypeError):
        return {
            parameter.name: parameter.annotation
            for parameter in inspect.signature(func).parameters.values()
        }


def requires_maf_context(func: ToolCallable) -> bool:
    return any(
        _is_invocation_context(annotation)
        for name, annotation in _function_hints(func).items()
        if name != "return"
    )


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
        if _is_invocation_context(annotation):
            continue
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
    input_model_is_explicit: bool = False
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
        explicit = input_model is not None or parameters is not None
        model = input_model
        if parameters is None:
            model = model or _input_model(name, func)
            parameters = model.model_json_schema()
        return cls(
            name=name,
            description=description,
            func=func,
            input_model=model,
            input_model_is_explicit=explicit,
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
        if self.policy.maf_only_options:
            raise TypeError("This tool requires the MAF compatibility adapter.")
        values = dict(arguments)
        schema_values = values
        if self.input_model is not None:
            try:
                validated = self.input_model.model_validate(values)
                values = validated.model_dump(exclude_unset=True)
                schema_values = validated.model_dump(mode="json", exclude_unset=True)
            except ValidationError as exc:
                raise TypeError(f"Invalid arguments for '{self.name}': {exc}") from exc
        try:
            jsonschema.validate(schema_values, self.parameters())
        except jsonschema.ValidationError as exc:
            raise TypeError(f"Invalid arguments for '{self.name}': {exc.message}") from exc
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


def _is_maf_tool(candidate: object) -> bool:
    """Recognize the legacy extension seam without importing its SDK."""
    return any(
        base.__name__ == "FunctionTool" and base.__module__.startswith("agent_framework")
        for base in type(candidate).__mro__
    )


def describe_tool(candidate: ToolInput) -> ToolDescriptor:
    if isinstance(candidate, ToolDescriptor):
        return candidate
    if _is_maf_tool(candidate):
        from .harness.agent_framework._maf_tools import describe_maf_tool

        return describe_maf_tool(candidate)
    from ._function_tool import tool

    return tool(candidate)


def describe_tools(candidates: Iterable[ToolInput]) -> tuple[ToolDescriptor, ...]:
    return tuple(describe_tool(candidate) for candidate in candidates)
