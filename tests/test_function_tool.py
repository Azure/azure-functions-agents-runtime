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
    normalize_optional_tools,
    normalize_tool,
    tool,
    workflow_tool,
)
from azure_functions_agents._tool_descriptor import ToolDescriptor
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


def test_optional_tool_normalization_preserves_discovery_and_empty_sentinels():
    assert normalize_optional_tools(None) is None
    assert normalize_optional_tools([]) == ()
    assert normalize_optional_tools(()) == ()
    def lookup(value: int) -> int:
        raise AssertionError("Normalization must not execute authored code")

    [descriptor] = normalize_optional_tools([lookup])
    assert descriptor.func is lookup
    assert normalize_optional_tools([descriptor]) == (descriptor,)


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


def test_normalizing_a_runtime_descriptor_preserves_identity() -> None:
    descriptor = tool(lambda value: value)
    assert normalize_tool(descriptor) is descriptor


@pytest.mark.asyncio
async def test_runtime_tool_has_matching_schema_invocation_and_text_across_harnesses():
    from copilot.tools import ToolInvocation

    from azure_functions_agents.harness.copilot_sdk._copilot_execution import _tool
    from azure_functions_agents.harness.copilot_sdk._copilot_tool_calls import CopilotToolCalls

    effects: list[tuple[int, str]] = []

    def lookup(count: int, label: str = "default") -> dict[str, object]:
        """Look up items."""
        effects.append((count, label))
        return {"count": count, "label": label}

    descriptor = normalize_tool(lookup)
    [maf_tool] = _maf_tools.build_maf_tools((descriptor,))
    copilot_tool = _tool(descriptor, CopilotToolCalls())
    assert copilot_tool.name == maf_tool.name == "lookup"
    assert copilot_tool.description == maf_tool.description == "Look up items."
    assert copilot_tool.parameters == maf_tool.parameters()
    maf_result = await maf_tool.invoke(arguments={"count": "3"})
    copilot_result = await copilot_tool.handler(
        ToolInvocation(
            session_id="session",
            tool_call_id="call",
            tool_name="lookup",
            arguments={"count": "3"},
        )
    )
    assert copilot_result.text_result_for_llm == maf_result[0].text
    assert effects == [(3, "default"), (3, "default")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"max_invocations": 1}, "max_invocations"),
        ({"result_parser": lambda value: value, "secret_option": "top-secret"}, "result_parser"),
    ],
)
async def test_runtime_helper_does_not_silently_strip_unknown_keywords(
    args: dict[str, object], expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(TypeError, match=expected):
        tool(**args)
    with pytest.raises(TypeError, match=expected):
        tool(lambda value: value, **args)
    assert not caplog.records


@pytest.mark.asyncio
async def test_runtime_decorators_accept_authored_callable_objects() -> None:
    class Lookup:
        name = "lookup"

        def __call__(self, value: str) -> str:
            return f"found:{value}"

    authored = Lookup()
    marked = workflow_tool(name="activity")(authored)
    descriptor = tool(marked)

    assert get_workflow_tool_metadata(marked).name == "activity"
    assert descriptor.workflow_metadata.name == "activity"
    assert descriptor.name == "lookup"
    assert await descriptor.invoke(arguments={"value": "item"}) == "found:item"


@pytest.mark.parametrize("description", [None, "Explicit description"])
def test_callable_description_uses_public_metadata_before_docstring(description):
    class Lookup:
        """Docstring fallback."""

        name = "lookup"
        description = "Authored description"

        def __call__(self, value: str) -> str:
            return value

    descriptor = tool(Lookup(), description=description)
    assert descriptor.description == (description or "Authored description")


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
async def test_runtime_tool_copies_preserve_schema_policy_and_workflow_metadata() -> None:
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
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("skills", [(), ("selected", "nested")])
async def test_maf_adapter_uses_skill_descriptor_paths(
    streaming: bool, skills: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_framework

    provider = Mock()
    monkeypatch.setattr(agent_framework, "SkillsProvider", provider)
    monkeypatch.setattr(agent_framework, "create_harness_agent", Mock())
    selected = tuple(SkillDescriptor(name=name, path=Path(name)) for name in skills)
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
            async for event in _maf_execution.run_stream(
                harness, request, display_name=None, **options
            )
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
