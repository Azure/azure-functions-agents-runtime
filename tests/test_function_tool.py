from __future__ import annotations

import gc
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import BaseModel, field_validator

from azure_functions_agents._function_tool import (
    get_workflow_tool_handler,
    get_workflow_tool_metadata,
    tool,
    workflow_tool,
)
from azure_functions_agents._tool_descriptor import ToolDescriptor, describe_tool
from azure_functions_agents.config.schema import AgentConfiguration
from azure_functions_agents.discovery.skills import SkillDescriptor
from azure_functions_agents.harness._harness_binding import (
    AppHarness,
    HarnessKind,
    HarnessRequest,
    UnsupportedCapabilityError,
)
from azure_functions_agents.harness.agent_framework import _maf_execution, _maf_tools
from azure_functions_agents.harness.copilot_sdk._copilot_preview import prepare_tools


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


@pytest.mark.parametrize("raw", [False, True])
def test_legacy_tool_registry_releases_thousand_declarations(raw: bool) -> None:
    gc.collect()
    baseline = set(_maf_tools._LEGACY_TOOLS)

    def echo(value: str) -> str:
        return value

    descriptors = [
        (
            describe_tool(_maf_tools.FunctionTool(name=f"raw_{index}", func=echo, max_invocations=1))
            if raw
            else tool(echo, name=f"runtime_{index}", max_invocations=1)
        )
        for index in range(1000)
    ]
    assert len(_maf_tools._LEGACY_TOOLS) == len(baseline) + 1000
    del descriptors
    gc.collect()
    assert set(_maf_tools._LEGACY_TOOLS) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [False, True])
async def test_legacy_tool_policy_owns_state_across_descriptor_copies(raw: bool) -> None:
    calls: list[str] = []

    def echo(value: str) -> str:
        calls.append(value)
        return value

    sdk_tool = _maf_tools.FunctionTool(name="raw", func=echo, max_invocations=1) if raw else None
    descriptor = describe_tool(sdk_tool) if raw else tool(echo, max_invocations=1)
    copied = replace(descriptor)
    workflow_copy = workflow_tool(name="activity")(descriptor)
    key = descriptor.policy.maf_compatibility_key
    assert copied.policy is descriptor.policy
    assert workflow_copy.policy is descriptor.policy
    del descriptor
    gc.collect()

    [first] = _maf_tools.build_maf_tools((copied,))
    [second] = _maf_tools.build_maf_tools((workflow_copy,))
    assert first is second
    if raw:
        assert first is sdk_tool
    assert await first.invoke(arguments={"value": "first"}, skip_parsing=True) == "first"
    with pytest.raises(Exception, match="maximum invocation limit"):
        await second.invoke(arguments={"value": "second"}, skip_parsing=True)
    assert calls == ["first"]
    del copied
    gc.collect()
    assert key in _maf_tools._LEGACY_TOOLS
    assert _maf_tools.build_maf_tools((workflow_copy,))[0] is first
    del workflow_copy
    gc.collect()
    assert key not in _maf_tools._LEGACY_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_legacy_tool_materializes_the_live_bound_descriptor(asynchronous: bool) -> None:
    class Service:
        def __init__(self, name: str) -> None:
            self.name = name

        def echo(self, value: str) -> str:
            return f"{self.name}:{value}"

        async def async_echo(self, value: str) -> str:
            return self.echo(value)

    descriptor = tool(Service.async_echo if asynchronous else Service.echo, max_invocations=1)
    first_bound = descriptor.__get__(Service("A"), Service)
    second_bound = descriptor.__get__(Service("B"), Service)
    key = descriptor.policy.maf_compatibility_key
    assert first_bound.policy is descriptor.policy
    assert second_bound.policy is descriptor.policy
    del descriptor
    gc.collect()
    first, second = _maf_tools.build_maf_tools((first_bound, second_bound))
    assert first is not second
    assert await first.invoke(arguments={"value": "ok"}, skip_parsing=True) == "A:ok"
    assert await second.invoke(arguments={"value": "ok"}, skip_parsing=True) == "B:ok"
    for wrapped in (first, second):
        with pytest.raises(Exception, match="maximum invocation limit"):
            await wrapped.invoke(arguments={"value": "again"}, skip_parsing=True)
    [fresh] = _maf_tools.build_maf_tools((first_bound,))
    assert fresh is not first
    assert await fresh.invoke(arguments={"value": "fresh"}, skip_parsing=True) == "A:fresh"
    assert _maf_tools._LEGACY_TOOLS[key].tool is None
    del first_bound, second_bound
    gc.collect()
    assert key not in _maf_tools._LEGACY_TOOLS


@pytest.mark.asyncio
async def test_raw_bound_maf_tool_retains_sdk_identity_and_counters() -> None:
    class Service:
        def echo(self, value: str) -> str:
            return f"raw:{value}"

    raw = _maf_tools.FunctionTool(name="raw_bound", func=Service().echo, max_invocations=1)
    descriptor = describe_tool(raw)
    [first] = _maf_tools.build_maf_tools((descriptor,))
    [second] = _maf_tools.build_maf_tools((replace(descriptor),))
    assert first is raw
    assert second is raw
    assert await first.invoke(arguments={"value": "ok"}, skip_parsing=True) == "raw:ok"
    with pytest.raises(Exception, match="maximum invocation limit"):
        await second.invoke(arguments={"value": "again"}, skip_parsing=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("source", [None, (), (Path("collection"), Path("invalid-name"))])
async def test_maf_adapter_preserves_caller_skill_source_path_intent(
    streaming: bool, source: tuple[Path, ...] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_framework

    provider = Mock()
    monkeypatch.setattr(agent_framework, "SkillsProvider", provider)
    monkeypatch.setattr(agent_framework, "create_harness_agent", Mock())
    selected = SkillDescriptor(name="selected", path=Path("selected"))
    request = HarnessRequest(
        prompt="test",
        instructions=None,
        agent_slug="main",
        session_id="session",
        new_session=True,
        model="model",
        tools=(),
        max_output_tokens=None,
        deadline=0,
        skills=(selected,),
        skill_source_paths=source,
    )
    expected = source if source is not None else (selected.path,)
    seen: list[tuple[Path, ...] | None] = []

    async def capture(**kwargs: Any) -> Any:
        seen.append(kwargs["skill_paths"])
        _maf_execution._build_role_agent(
            Mock(),
            agent_instructions=None,
            tools=(),
            skill_paths=kwargs["skill_paths"],
            agent_name="main",
            history_provider=None,
            agent_configuration=AgentConfiguration(),
        )
        raise RuntimeError("captured source paths")

    monkeypatch.setattr(_maf_execution, "_build_agent_session", capture)
    harness = AppHarness(HarnessKind.MAF, Path.cwd())
    options = {
        "timeout": 1,
        "instructions": None,
        "system_addendum": None,
        "session_id": None,
        "model": None,
        "agent_name": "main",
        "agent_configuration": None,
        "workflow_enabled": False,
        "workflow_durable_client": None,
        "workflow_agent_slug": None,
        "subagents": None,
        "catalog": None,
        "workflow_policy": None,
    }
    if streaming:
        events = [
            event async for event in _maf_execution.run_stream(harness, request, display_name=None, **options)
        ]
        assert len(events) == 1
        assert "captured source paths" in events[0]
    else:
        with pytest.raises(RuntimeError, match="captured source paths"):
            await _maf_execution.run(harness, request, **options)
    assert seen == [expected]
    if expected:
        provider.from_paths.assert_called_once_with(
            list(expected),
            disable_load_skill_approval=True,
            disable_read_skill_resource_approval=True,
            disable_run_skill_script_approval=True,
        )
    else:
        provider.from_paths.assert_not_called()
