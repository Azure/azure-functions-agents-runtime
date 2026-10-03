from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from pydantic import BaseModel, field_validator

from azure_functions_agents import _maf_tools
from azure_functions_agents._function_tool import (
    get_workflow_tool_handler,
    get_workflow_tool_metadata,
    tool,
    workflow_tool,
)
from azure_functions_agents._harness import UnsupportedCapabilityError, prepare_tools
from azure_functions_agents._tool_descriptor import ToolDescriptor, describe_tool


def test_ordinary_authoring_never_constructs_maf_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(**kwargs: Any) -> Any:
        raise AssertionError("Authoring must not construct a MAF tool")

    monkeypatch.setattr(_maf_tools, "FunctionTool", forbidden)

    @tool(name="lookup", description="  Authored description  ")
    def local(value: int) -> int:
        return value

    assert type(local) is ToolDescriptor
    assert local.name == "lookup"
    assert local.description == "Authored description"
    assert local.policy.maf_compatibility_key is None
    assert local.parameters()["properties"]["value"]["type"] == "integer"


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_outer", [False, True])
async def test_schema_and_workflow_decorator_orders_preserve_metadata(workflow_outer: bool) -> None:
    class Arguments(BaseModel):
        value: str

    def authored(arguments: Arguments) -> dict[str, str]:
        return {"value": arguments.value}

    if workflow_outer:
        descriptor = workflow_tool(name="activity_name", description="Activity")(
            tool(name="chat_name", schema=Arguments)(authored)
        )
    else:
        descriptor = tool(name="chat_name", schema=Arguments)(
            workflow_tool(name="activity_name", description="Activity")(authored)
        )
    metadata = get_workflow_tool_metadata(descriptor)

    assert descriptor.name == "chat_name"
    assert metadata.name == "activity_name"
    assert metadata.description == "Activity"
    assert descriptor.parameters() == Arguments.model_json_schema()
    assert await descriptor.invoke(arguments={"value": "direct"}) == {"value": "direct"}
    handler = get_workflow_tool_handler(descriptor.func)
    assert handler({"value": "workflow"}) == {"value": "workflow"}
    with pytest.raises(FrozenInstanceError):
        descriptor.workflow_metadata = None


@pytest.mark.asyncio
async def test_maf_adapter_preserves_schema_and_python_result() -> None:
    @tool
    def local(value: int) -> dict[str, int]:
        return {"value": value}

    [wrapped] = _maf_tools.build_maf_tools((local,))

    assert wrapped.name == local.name
    assert wrapped.description == local.description
    assert wrapped.parameters() == local.parameters()
    assert await wrapped.invoke(arguments={"value": "4"}, skip_parsing=True) == {"value": 4}


@pytest.mark.asyncio
async def test_maf_adapter_does_not_add_an_extra_pydantic_validation_pass() -> None:
    validations: list[str] = []
    effects: list[str] = []

    class Arguments(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def record(cls, value: str) -> str:
            validations.append(value)
            return value

    @tool(schema=Arguments)
    def local(arguments: Arguments) -> str:
        effects.append(arguments.value)
        return arguments.value

    [wrapped] = _maf_tools.build_maf_tools((local,))
    assert await wrapped.invoke(arguments={"value": "ok"}, skip_parsing=True) == "ok"
    assert validations == ["ok", "ok"]
    assert effects == ["ok"]


def test_raw_simple_maf_input_models_are_copied_to_neutral_validation() -> None:
    raw = _maf_tools.FunctionTool(name="raw", func=lambda value: value)
    descriptor = describe_tool(raw)

    assert descriptor.input_model.__module__ == "azure_functions_agents._tool_descriptor"
    assert descriptor.parameters() == raw.parameters()
    assert _maf_tools.build_maf_tools((descriptor,))[0] is raw


@pytest.mark.asyncio
async def test_maf_only_options_retain_sdk_limits_without_entering_neutral_request() -> None:
    calls: list[str] = []

    @tool(max_invocations=1)
    def bounded(value: str) -> str:
        calls.append(value)
        return value

    with pytest.raises(UnsupportedCapabilityError):
        prepare_tools((bounded,))
    [first] = _maf_tools.build_maf_tools((bounded,))
    [second] = _maf_tools.build_maf_tools((bounded,))
    assert first is second
    assert await first.invoke(arguments={"value": "first"}, skip_parsing=True) == "first"
    with pytest.raises(Exception, match="maximum invocation limit"):
        await second.invoke(arguments={"value": "second"}, skip_parsing=True)
    assert calls == ["first"]


def test_raw_maf_subclass_stays_in_compatibility_layer() -> None:
    class CustomTool(_maf_tools.FunctionTool):
        pass

    raw = CustomTool(name="custom", func=lambda value: value)
    descriptor = describe_tool(raw)

    assert type(descriptor) is ToolDescriptor
    assert descriptor.policy.maf_only_options == ("custom tool class",)
    assert _maf_tools.build_maf_tools((descriptor,)) == [raw]
    with pytest.raises(UnsupportedCapabilityError):
        prepare_tools((descriptor,))


def test_authored_approval_is_preserved_for_maf_and_rejected_by_preview() -> None:
    descriptor = tool(lambda value: value, name="approval", approval_mode="always_require")
    [wrapped] = _maf_tools.build_maf_tools((descriptor,))

    assert wrapped.approval_mode == "always_require"
    with pytest.raises(UnsupportedCapabilityError):
        prepare_tools((descriptor,))
