from __future__ import annotations

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
from azure_functions_agents._tool_descriptor import ToolDescriptor, describe_tools
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


def test_raw_maf_input_models_never_enter_neutral_inventory() -> None:
    raw = _maf_tools.FunctionTool(name="raw", func=lambda value: value)
    assert describe_tools((raw,)) == ()


def test_runtime_decorators_never_wrap_or_inspect_raw_sdk_tools(caplog) -> None:
    class OpaqueSDKTool(_maf_tools.FunctionTool):
        def __getattribute__(self, name):
            raise AssertionError("No SDK attribute introspection")

    raw = object.__new__(OpaqueSDKTool)
    assert tool(raw) is raw
    assert workflow_tool(raw) is raw
    assert get_workflow_tool_metadata(raw) is None
    assert describe_tools((raw,)) == ()
    assert "Ignoring unsupported custom tool" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"max_invocations": 1}, "max_invocations"),
        ({"result_parser": lambda value: value, "secret_option": "top-secret"}, "result_parser"),
    ],
)
async def test_tool_warns_and_ignores_unexpected_keyword_arguments_at_authoring(
    args: dict[str, object], expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    decorator = tool(**args)
    direct = tool(lambda value: value, **args)
    decorated = decorator(lambda value: value)

    assert isinstance(direct, ToolDescriptor)
    assert isinstance(decorated, ToolDescriptor)
    assert await direct.invoke(arguments={"value": "ok"}) == "ok"
    assert await decorated.invoke(arguments={"value": "ok"}) == "ok"
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "Ignoring unsupported @tool keyword argument(s):" in record.getMessage()
    ]
    assert len(warnings) == 2
    assert all(expected in message for message in warnings)
    assert all(
        "Supported @tool keyword arguments are: name, description, schema, approval_mode."
        in message
        for message in warnings
    )
    assert "top-secret" not in caplog.text


def test_raw_maf_subclass_is_ignored_by_both_adapters() -> None:
    class CustomTool(_maf_tools.FunctionTool):
        pass

    raw = CustomTool(name="custom", func=lambda value: value)
    assert describe_tools((raw,)) == ()
    assert _maf_tools.build_maf_tools(describe_tools((raw,))) == []
    assert prepare_tools((raw,)) == ()


def test_authored_approval_is_preserved_for_maf_and_rejected_by_preview() -> None:
    descriptor = tool(lambda value: value, name="approval", approval_mode="always_require")
    [wrapped] = _maf_tools.build_maf_tools((descriptor,))

    assert wrapped.approval_mode == "always_require"
    with pytest.raises(UnsupportedCapabilityError):
        prepare_tools((descriptor,))


def test_workflow_tool_rejects_unexpected_keyword_arguments_at_authoring() -> None:
    with pytest.raises(TypeError, match=r"unexpected keyword argument 'max_invocations'"):
        workflow_tool(max_invocations=1)


@pytest.mark.asyncio
async def test_portable_tool_copies_preserve_schema_policy_and_workflow_metadata() -> None:
    class Arguments(BaseModel):
        value: str

    calls: list[str] = []

    def echo(arguments: Arguments) -> str:
        calls.append(arguments.value)
        return arguments.value

    descriptor = tool(
        echo,
        name="echo",
        schema=Arguments,
        approval_mode="always_require",
        max_invocations=1,
    )
    copied = replace(descriptor)
    workflow_copy = workflow_tool(name="activity")(descriptor)

    assert copied.parameters() == descriptor.parameters() == workflow_copy.parameters()
    assert copied.policy == descriptor.policy == workflow_copy.policy
    assert copied.workflow_metadata is None
    assert workflow_copy.workflow_metadata is not None
    assert await copied.invoke(arguments={"value": "first"}) == "first"
    assert await workflow_copy.invoke(arguments={"value": "second"}) == "second"
    assert calls == ["first", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_authored_options_materialize_the_live_bound_descriptor(asynchronous: bool) -> None:
    class Service:
        def __init__(self, name: str) -> None:
            self.name = name

        def echo(self, value: str) -> str:
            return f"{self.name}:{value}"

        async def async_echo(self, value: str) -> str:
            return self.echo(value)

    descriptor = tool(Service.async_echo if asynchronous else Service.echo)
    first_bound = descriptor.__get__(Service("A"), Service)
    second_bound = descriptor.__get__(Service("B"), Service)
    assert first_bound.policy is descriptor.policy
    assert second_bound.policy is descriptor.policy
    first, second = _maf_tools.build_maf_tools((first_bound, second_bound))
    assert first is not second
    assert await first.invoke(arguments={"value": "ok"}, skip_parsing=True) == "A:ok"
    assert await second.invoke(arguments={"value": "ok"}, skip_parsing=True) == "B:ok"
    [fresh] = _maf_tools.build_maf_tools((first_bound,))
    assert fresh is not first
    assert await fresh.invoke(arguments={"value": "fresh"}, skip_parsing=True) == "A:fresh"


@pytest.mark.asyncio
async def test_removed_maf_passthrough_keywords_are_ignored_by_both_harnesses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    descriptor = tool(
        lambda value: value,
        name="portable",
        max_invocation_exceptions=2,
        kind="custom",
        additional_properties={"authored": True},
    )

    assert isinstance(descriptor, ToolDescriptor)
    [maf_tool] = _maf_tools.build_maf_tools((descriptor,))
    [copilot_tool] = prepare_tools((descriptor,))
    assert await descriptor.invoke(arguments={"value": "ok"}) == "ok"
    assert await maf_tool.invoke(arguments={"value": "ok"}, skip_parsing=True) == "ok"
    assert copilot_tool is descriptor
    assert "max_invocation_exceptions" in caplog.text
    assert "additional_properties" in caplog.text
    assert "{'authored': True}" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("skills", [(), ("selected", "nested")])
async def test_maf_adapter_uses_skill_descriptor_paths(
    streaming: bool, skills: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_framework

    provider = Mock()
    monkeypatch.setattr(agent_framework, "SkillsProvider", provider)
    monkeypatch.setattr(agent_framework, "create_harness_agent", Mock())
    selected = tuple(
        SkillDescriptor(name=name, path=Path(name))
        for name in skills
    )
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
        skills=selected,
    )
    expected = tuple(skill.path for skill in selected) or None
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
            event
            async for event in _maf_execution.run_events(
                harness,
                request,
                display_name=None,
                execution_surface=None,
                **options,
            )
        ]
        assert len(events) == 1
        assert "captured source paths" in (events[0].content or "")
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
