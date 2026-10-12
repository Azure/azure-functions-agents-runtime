from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Annotated
from uuid import UUID

import pytest
from pydantic import BaseModel, Field, create_model

from azure_functions_agents._function_tool import tool
from azure_functions_agents._tool_descriptor import ToolDescriptor
from azure_functions_agents.harness.agent_framework._maf_tools import (
    build_maf_tools,
)


def test_unsupported_programmatic_tools_are_ignored_without_inspection(caplog):
    from agent_framework import FunctionTool

    from azure_functions_agents._tool_descriptor import describe_tools
    from azure_functions_agents.registration.capabilities import AgentCapabilities

    class Opaque:
        def __getattribute__(self, name):
            raise AssertionError("Unsupported tool attributes must not be inspected")

        def __call__(self, **kwargs):
            raise AssertionError("Unsupported tool must not execute")

    @tool
    def allowed() -> str:
        return "ok"

    def undecorated() -> str:
        return "ignored"

    candidates = [FunctionTool(name="private-sentinel", func=undecorated), Opaque(), undecorated]
    assert describe_tools(candidates) == ()
    assert describe_tools([*candidates, allowed]) == (allowed,)
    assert AgentCapabilities.create(filtered_user_tools=candidates).filtered_user_tools == ()
    assert "Ignoring unsupported custom tool" in caplog.text
    assert "private-sentinel" not in caplog.text


class _Choice(Enum):
    FIRST = "first"
    SECOND = "second"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("annotation", "value", "accepted"),
    [
        (date, "2026-10-06", False),
        (date, date(2026, 10, 6), False),
        (date, "invalid", False),
        (UUID, "12345678-1234-5678-1234-567812345678", False),
        (UUID, UUID("12345678-1234-5678-1234-567812345678"), False),
        (UUID, "invalid", False),
        (date | None, "2026-10-06", True),
        (date | None, None, True),
        (UUID | None, "12345678-1234-5678-1234-567812345678", True),
        (UUID | None, None, True),
        (int, "3", True),
        (_Choice, "first", True),
    ],
)
async def test_python_normalized_operands_match_real_and_authored_maf(
    annotation, value, accepted
):
    from agent_framework import FunctionTool

    calls = []

    def echo(value):
        calls.append(value)
        return "accepted"

    echo.__annotations__ = {"value": annotation}
    native = FunctionTool(name="echo", description="Echo", func=echo)
    authored = tool(echo)
    adapted = build_maf_tools([authored])[0]
    for candidate in (native, adapted, authored):
        if accepted:
            await candidate.invoke(arguments={"value": value})
        else:
            with pytest.raises(TypeError):
                await candidate.invoke(arguments={"value": value})
    if accepted:
        assert len(calls) == 3
        assert calls == [calls[0]] * 3
    else:
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("annotation", [date, UUID])
@pytest.mark.parametrize(
    "value", ["2026-10-06", "12345678-1234-5678-1234-567812345678", "invalid"]
)
async def test_supplied_string_schema_keeps_maf_no_coercion_semantics(annotation, value):
    from agent_framework import FunctionTool

    calls = []

    def echo(value):
        calls.append(value)
        return "accepted"

    echo.__annotations__ = {"value": annotation}
    schema = {
            "properties": {
                "value": {"type": "string", "format": "date" if annotation is date else "uuid"}
            },
            "required": ["value"],
        }
    descriptor = ToolDescriptor.create(
        name="echo", description="Echo", func=echo, parameters=schema,
    )
    native = FunctionTool(name="echo", description="Echo", func=echo, input_model=schema)
    await native.invoke(arguments={"value": value})
    assert await descriptor.invoke(arguments={"value": value}) == "accepted"
    assert calls == [value, value]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("annotation", "value", "accepted"),
    [
        (date, "2026-10-06", False),
        (date, date(2026, 10, 6), False),
        (UUID, "12345678-1234-5678-1234-567812345678", False),
        (UUID, UUID("12345678-1234-5678-1234-567812345678"), False),
        (date | None, "2026-10-06", True),
        (int, "3", True),
        (_Choice, "first", True),
    ],
)
async def test_explicit_author_model_matches_real_built_maf_tool(annotation, value, accepted):
    arguments_model = create_model("Arguments", value=(annotation, ...))
    calls = []

    @tool(schema=arguments_model)
    def echo(arguments):
        calls.append(arguments.value)
        return "accepted"

    native = build_maf_tools([echo])[0]
    for candidate in (native, echo):
        if accepted:
            await candidate.invoke(arguments={"value": value})
        else:
            with pytest.raises(TypeError):
                await candidate.invoke(arguments={"value": value})
    if accepted:
        assert len(calls) == 2
        assert calls[0] == calls[1]
    else:
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule", "value"),
    [
        ({"type": "integer", "minimum": 1}, 0),
        ({"type": "string", "pattern": "^accepted$"}, "other"),
        ({"type": "object", "required": ["nested"]}, {}),
        ({"type": "array", "items": {"type": "integer"}}, ["other"]),
        ({"type": "boolean", "enum": [1]}, True),
        ({"type": ["unknown", "integer"]}, "other"),
    ],
)
async def test_maf_schema_acceptance_is_preserved_at_neutral_invocation(rule, value):
    from agent_framework import FunctionTool

    calls = []

    def echo(value):
        calls.append(value)
        return "accepted"

    schema = {
            "type": "object", "properties": {"value": rule},
            "required": ["value"], "additionalProperties": False,
        }
    descriptor = ToolDescriptor.create(
        name="echo", description="Echo", func=echo, parameters=schema,
    )
    native = FunctionTool(name="echo", description="Echo", func=echo, input_model=schema)
    result = await native.invoke(arguments={"value": value})
    assert [item.text for item in result] == ["accepted"]
    assert await descriptor.invoke(arguments={"value": value}) == "accepted"
    assert calls == [value, value]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{}, {"value": "1"}, {"value": True}, {"value": 1, "extra": 2}, {"value": 3}],
)
async def test_maf_top_level_rejections_match_neutral_invocation(arguments):

    calls = []
    schema = {
            "properties": {"value": {"type": "integer", "enum": [1, 2]}},
            "required": ["value"], "additionalProperties": False,
        }
    descriptor = ToolDescriptor.create(
        name="echo", description="Echo", func=lambda value: calls.append(value), parameters=schema,
    )
    native = build_maf_tools([descriptor])[0]
    with pytest.raises(TypeError):
        await native.invoke(arguments=arguments)
    with pytest.raises(TypeError):
        await descriptor.invoke(arguments=arguments)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_enum_python_values_match_actual_maf_backend(
    asynchronous: bool,
) -> None:
    calls: list[_Choice] = []

    def synchronous(choice: _Choice) -> dict[str, _Choice]:
        calls.append(choice)
        return {"choice": choice}

    async def coroutine(choice: _Choice) -> dict[str, _Choice]:
        calls.append(choice)
        return {"choice": choice}

    descriptor = tool(coroutine if asynchronous else synchronous, name="enum_choice")
    native = build_maf_tools([descriptor])[0]
    for candidate in (descriptor, native):
        with pytest.raises(TypeError, match="Invalid arguments"):
            await candidate.invoke(arguments={"choice": "invalid"})
    assert calls == []
    assert await descriptor.invoke(arguments={"choice": "first"}) == {"choice": _Choice.FIRST}
    await native.invoke(arguments={"choice": "first"})
    assert calls == [_Choice.FIRST, _Choice.FIRST]


@pytest.mark.asyncio
async def test_descriptor_preserves_nested_and_list_enum_values() -> None:
    calls: list[tuple[list[_Choice], dict[str, list[_Choice]]]] = []

    @tool
    def nested(choices: list[_Choice], groups: dict[str, list[_Choice]]) -> object:
        calls.append((choices, groups))
        return choices, groups

    arguments = {"choices": ["first"], "groups": {"nested": ["second"]}}
    native = build_maf_tools([nested])[0]
    for candidate in (nested, native):
        with pytest.raises(TypeError, match="Invalid arguments"):
            await candidate.invoke(arguments={**arguments, "groups": {"nested": ["invalid"]}})
    assert calls == []
    expected = ([_Choice.FIRST], {"nested": [_Choice.SECOND]})
    assert await nested.invoke(arguments=arguments) == expected
    await native.invoke(arguments=arguments)
    assert calls == [expected, expected]


def test_descriptor_schema_is_an_independent_frozen_snapshot() -> None:
    schema = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
    }
    descriptor = ToolDescriptor.create(
        name="sample", description="Sample", func=lambda value: value, parameters=schema
    )
    schema["properties"]["value"]["type"] = "string"
    exported = descriptor.parameters()
    exported["properties"].clear()

    assert descriptor.parameters()["properties"]["value"]["type"] == "integer"
    with pytest.raises(FrozenInstanceError):
        descriptor.name = "changed"
    with pytest.raises(FrozenInstanceError):
        descriptor.policy.approval_mode = "always_require"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sync", "async", "sync_awaitable"])
async def test_descriptor_invokes_callable_and_awaitable_once(kind: str) -> None:
    calls: list[str] = []
    waits: list[str] = []

    async def awaited(value: str) -> dict[str, str]:
        waits.append(value)
        return {"value": value}

    def sync(value: str) -> object:
        calls.append(value)
        return awaited(value) if kind == "sync_awaitable" else {"value": value}

    async def asynchronous(value: str) -> dict[str, str]:
        calls.append(value)
        return await awaited(value)

    descriptor = tool(asynchronous if kind == "async" else sync, name="once")
    result = await descriptor.invoke(arguments={"value": "ok"}, tool_call_id="call-1")

    assert result == {"value": "ok"}
    assert calls == ["ok"]
    assert waits == ([] if kind == "sync" else ["ok"])


@pytest.mark.asyncio
async def test_descriptor_validates_schema_before_side_effects() -> None:
    calls: list[int] = []
    descriptor = ToolDescriptor.create(
        name="validated",
        description="",
        func=lambda value: calls.append(value),
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer", "minimum": 1}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    for arguments in ({}, {"value": "1"}, {"value": 1, "other": True}):
        with pytest.raises(TypeError, match="Invalid arguments"):
            await descriptor.invoke(arguments=arguments)
    assert calls == []
    await descriptor.invoke(arguments={"value": 0})
    await descriptor.invoke(arguments={"value": 1})
    assert calls == [0, 1]


@pytest.mark.asyncio
async def test_descriptor_preserves_pydantic_coercion_defaults_and_explicit_null() -> None:
    calls: list[tuple[int, str | None]] = []

    @tool
    def lookup(count: Annotated[int, "Item count"] = 2, label: str | None = "default") -> str:
        calls.append((count, label))
        return "ok"

    assert lookup.parameters()["properties"]["count"]["description"] == "Item count"
    assert await lookup.invoke(arguments={"count": "3", "label": None}) == "ok"
    assert await lookup.invoke(arguments={}) == "ok"
    with pytest.raises(TypeError):
        await lookup.invoke(arguments={"count": "invalid"})
    assert calls == [(3, None), (2, "default")]


@pytest.mark.asyncio
async def test_explicit_model_validates_before_author_callable() -> None:
    class Arguments(BaseModel):
        value: int = Field(gt=0)

    calls: list[int] = []

    @tool(schema=Arguments)
    def lookup(arguments: Arguments) -> dict[str, int]:
        calls.append(arguments.value)
        return {"value": arguments.value}

    with pytest.raises(TypeError):
        await lookup.invoke(arguments={"value": 0})
    assert calls == []
    assert await lookup.invoke(arguments={"value": "7"}) == {"value": 7}
    assert calls == [7]


def test_sdk_imports_stay_in_named_adapter_and_legacy_boundaries() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "azure_functions_agents"
    maf_package = source / "harness" / "agent_framework"
    copilot_package = source / "harness" / "copilot_sdk"
    violations: list[str] = []
    for path in source.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
                modules = [node.module]
            elif isinstance(node, ast.Call) and node.args:
                function = node.func
                import_call = (
                    isinstance(function, ast.Name)
                    and function.id in {"import_module", "__import__"}
                ) or (
                    isinstance(function, ast.Attribute) and function.attr == "import_module"
                )
                argument = node.args[0]
                if import_call and isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    modules = [argument.value]
            for module in modules:
                if (
                    module.startswith("agent_framework")
                    and path.parent != maf_package
                ):
                    violations.append(f"{path.relative_to(source)}:{node.lineno}: {module}")
                if (
                    (module == "copilot" or module.startswith("copilot."))
                    and path.parent != copilot_package
                ):
                    violations.append(f"{path.relative_to(source)}:{node.lineno}: {module}")
    assert violations == []
