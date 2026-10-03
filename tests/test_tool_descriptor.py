from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import BaseModel, Field

from azure_functions_agents._function_tool import tool
from azure_functions_agents._tool_descriptor import ToolDescriptor


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
    for arguments in ({}, {"value": "1"}, {"value": 0}, {"value": 1, "other": True}):
        with pytest.raises(TypeError, match="Invalid arguments"):
            await descriptor.invoke(arguments=arguments)
    assert calls == []
    await descriptor.invoke(arguments={"value": 1})
    assert calls == [1]


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
    maf_boundaries = {
        "__init__.py",
        "_maf_tools.py",
        "_maf_mcp.py",
        "client_manager.py",
        "_observability.py",
        "_blob_history.py",
        "_file_history.py",
    }
    copilot_boundaries = {
        "_copilot.py",
        "_copilot_capabilities.py",
        "_copilot_providers.py",
        "_copilot_session_fs.py",
        "_copilot_tool_calls.py",
    }
    violations: list[str] = []
    for path in source.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
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
                if module.startswith("agent_framework") and path.name not in maf_boundaries:
                    violations.append(f"{path.relative_to(source)}:{node.lineno}: {module}")
                if (module == "copilot" or module.startswith("copilot.")) and path.name not in copilot_boundaries:
                    violations.append(f"{path.relative_to(source)}:{node.lineno}: {module}")
    assert violations == []
