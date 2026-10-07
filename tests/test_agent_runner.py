from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from azure_functions_agents import runner
from azure_functions_agents._tool_descriptor import ToolDescriptor
from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.config.merge import compose
from azure_functions_agents.config.schema import AgentConfiguration
from azure_functions_agents.discovery.mcp import MCPServerDescriptor, discover_mcp_servers
from azure_functions_agents.discovery.skills import (
    SkillDescriptor,
    describe_skill_catalog,
    discover_skills,
)
from azure_functions_agents.discovery.tools import discover_user_tools
from azure_functions_agents.harness import _agent_runner
from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
from azure_functions_agents.harness.agent_framework import _maf_execution
from azure_functions_agents.harness.copilot_sdk import _copilot_execution, _copilot_runtime
from azure_functions_agents.registration.capabilities import AgentCapabilities, build_capabilities


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HarnessKind.MAF, HarnessKind.COPILOT])
@pytest.mark.parametrize("skills_disabled", [False, True])
async def test_shared_scenario_inventory_is_filtered_once_and_forwarded_unchanged(
    kind, skills_disabled, monkeypatch
):
    root = Path(__file__).parent / "fixtures" / "config_scenarios" / "21_skill_root_boundaries"
    discovered = discover_skills(root)
    tools = discover_user_tools(root).tools
    servers = discover_mcp_servers(root).servers
    spec = load_agent_specs(root, strict=True)[0]
    if skills_disabled:
        spec = spec.model_copy(update={"skills": False})
    resolved = compose(
        spec,
        load_global_config(root),
        discovered_mcp_names=list(servers),
        discovered_skill_names=list(discovered.skills),
    )
    capabilities = build_capabilities(
        resolved,
        discovered_user_tools=tools,
        discovered_mcp_tools=servers,
        discovered_skills=discovered.skills,
        discovered_skill_descriptors=discovered.descriptors,
    )
    before = capabilities
    execution = AsyncMock(return_value=runner.AgentResult("session", "reply"))
    backend = _maf_execution if kind is HarnessKind.MAF else _copilot_execution
    monkeypatch.setattr(backend, "run", execution)
    no_parse = Mock(side_effect=AssertionError("Filtered descriptors must not be rediscovered"))
    monkeypatch.setattr(runner, "describe_skill_catalog", no_parse)
    harness = AppHarness(kind, root, default_model="fixture-model")

    await _agent_runner.get_agent_runner(harness).run_agent(
        "prompt",
        deadline=123.0,
        tools=capabilities.filtered_user_tools,
        mcp_tools=capabilities.filtered_mcp_tools,
        skills=capabilities.skills,
        skill_catalog=capabilities.skill_catalog,
    )

    request = execution.call_args.args[1]
    assert set(discovered.skills) == {"parent", "grouped", "excluded"}
    assert {skill.name for skill in request.skills} == (
        set() if skills_disabled else {"parent", "grouped"}
    )
    assert request.skills == capabilities.skills
    assert request.skill_catalog == discovered.descriptors
    assert request.tools == capabilities.filtered_user_tools
    assert [tool.name for tool in request.tools] == ["local_tool"]
    assert request.mcp_servers == capabilities.filtered_mcp_tools
    assert [server.name for server in request.mcp_servers] == ["selected"]
    assert capabilities == before
    no_parse.assert_not_called()


def test_get_agent_runner_caches_per_binding_resource_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("azure_functions_agents.harness.agent_framework._maf_runner")
    first = object()
    create_runner = Mock(return_value=first)
    module.create_runner = create_runner
    monkeypatch.setitem(sys.modules, module.__name__, module)

    harness = AppHarness(HarnessKind.MAF, Path.cwd())

    assert _agent_runner.get_agent_runner(harness) is first
    assert _agent_runner.get_agent_runner(harness) is first
    create_runner.assert_called_once_with(harness)


def test_get_agent_runner_keeps_same_root_clones_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("azure_functions_agents.harness.agent_framework._maf_runner")
    created: list[object] = []

    def create_runner(_harness: AppHarness) -> object:
        runner_object = object()
        created.append(runner_object)
        return runner_object

    module.create_runner = create_runner
    monkeypatch.setitem(sys.modules, module.__name__, module)

    original = AppHarness(HarnessKind.MAF, Path.cwd())
    clone = replace(original)

    assert _agent_runner.get_agent_runner(original) is created[0]
    assert _agent_runner.get_agent_runner(clone) is created[1]
    assert created[0] is not created[1]


@pytest.mark.asyncio
async def test_public_runner_shims_dispatch_all_three_operations_through_bound_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = AppHarness(HarnessKind.MAF, Path.cwd())
    seen: dict[str, dict[str, object]] = {}

    class _BoundRunner:
        async def run_agent(self, prompt: str, **kwargs: object) -> runner.AgentResult:
            seen["run_agent"] = {"prompt": prompt, **kwargs}
            return runner.AgentResult("session", "answer")

        def run_agent_stream(self, prompt: str, **kwargs: object):
            seen["run_agent_stream"] = {"prompt": prompt, **kwargs}

            async def _stream():
                yield "data: ok\n\n"

            return _stream()

        async def run_leaf_agent_task(self, resolved, capabilities, task: str, **kwargs: object) -> str:
            seen["run_leaf_agent_task"] = {
                "resolved": resolved,
                "capabilities": capabilities,
                "task": task,
                **kwargs,
            }
            return "leaf"

    monkeypatch.setattr(runner, "get_harness", lambda: harness)
    monkeypatch.setattr(runner, "get_agent_runner", lambda selected: _BoundRunner())

    result = await runner.run_agent("prompt", timeout=2.0, _session_is_new=True)
    stream = [chunk async for chunk in runner.run_agent_stream("prompt", timeout=3.0)]
    resolved = SimpleNamespace(slug="billing")
    capabilities = AgentCapabilities(_harness=harness)
    leaf = await runner.run_leaf_agent_task(
        resolved,
        capabilities,
        "work",
        timeout=1.0,
        execution_role="delegate",
    )

    assert result.content == "answer"
    assert stream == ["data: ok\n\n"]
    assert leaf == "leaf"
    assert seen["run_agent"]["session_is_new"] is True
    assert seen["run_agent"]["timeout"] == 2.0
    assert seen["run_agent_stream"]["timeout"] == 3.0
    assert seen["run_leaf_agent_task"]["execution_role"] == "delegate"


@pytest.mark.asyncio
async def test_copilot_unsupported_configuration_rejects_before_native_acquisition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = AppHarness(HarnessKind.COPILOT, tmp_path)
    get_runtime = Mock(side_effect=AssertionError("native runtime should not start"))
    native_run = AsyncMock(side_effect=AssertionError("native run should not start"))
    monkeypatch.setattr(_copilot_runtime, "get_runtime", get_runtime)
    monkeypatch.setattr(_copilot_execution, "run", native_run)

    events = [
        json.loads(chunk.removeprefix("data: ").strip())
        async for chunk in runner.run_agent_stream(
            "prompt", timeout=1.0, _harness=harness,
            agent_configuration=AgentConfiguration(max_output_tokens=32),
        )
    ]
    assert [event["type"] for event in events] == ["error"]

    with pytest.raises(ValueError, match="max_output_tokens"):
        await runner.run_leaf_agent_task(
            SimpleNamespace(slug="billing", agent_configuration=AgentConfiguration(max_output_tokens=32)),
            AgentCapabilities(_harness=harness),
            "work",
            timeout=1.0,
            execution_role="workflow_subagent",
        )

    get_runtime.assert_not_called()
    native_run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HarnessKind.MAF, HarnessKind.COPILOT])
async def test_cached_facade_forwards_neutral_capabilities_to_selected_execution(
    kind, monkeypatch, tmp_path
):
    harness = AppHarness(kind, tmp_path, default_model="fixture-model")
    selected = _agent_runner.get_agent_runner(harness)
    assert _agent_runner.get_agent_runner(harness) is selected
    descriptor = ToolDescriptor.create(
        name="local_tool", description="Local tool", func=lambda value: {"value": value}
    )
    server = MCPServerDescriptor.create(
        name="selected", url="https://fixture.invalid/mcp", tools=["lookup"]
    )
    approved = SkillDescriptor.create(
        name="approved", path=tmp_path / "approved"
    )
    excluded = SkillDescriptor.create(
        name="excluded", path=tmp_path / "excluded"
    )
    execution = AsyncMock(return_value=runner.AgentResult("session", "answer"))
    backend = _maf_execution if kind is HarnessKind.MAF else _copilot_execution
    monkeypatch.setattr(backend, "run", execution)

    result = await selected.run_agent(
        "prompt",
        instructions="instructions",
        deadline=123.0,
        tools=(descriptor,),
        mcp_tools=(server,),
        skills=(approved,),
        skill_catalog=(approved, excluded),
        session_id="session",
        session_is_new=True,
    )

    assert result.content == "answer"
    actual_harness, request = execution.call_args.args
    assert actual_harness is harness
    assert request.tools == (descriptor,)
    assert request.mcp_servers == (server,)
    assert request.skills == (approved,)
    assert request.skill_catalog == (approved, excluded)
    assert request.session_id == "session"
    assert request.new_session is True
    assert request.deadline == 123.0
    assert request.model == "fixture-model"
    assert request.instructions == "instructions"
    assert _agent_runner.get_agent_runner(harness) is selected


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["delegate", "workflow_subagent"])
async def test_cached_leaf_facade_binds_immutable_capabilities_to_its_app_root(
    role, monkeypatch, tmp_path
):
    harness = AppHarness(HarnessKind.MAF, tmp_path / "bound-root")
    selected = _agent_runner.get_agent_runner(harness)
    descriptor = ToolDescriptor.create(name="local", description="Local", func=lambda: "ok")
    server = MCPServerDescriptor.create(name="remote", url="https://fixture.invalid/mcp")
    skill = SkillDescriptor.create(
        name="approved", path=tmp_path / "approved"
    )
    capabilities = AgentCapabilities(
        filtered_user_tools=(descriptor,), filtered_mcp_tools=(server,),
        skills=(skill,), skill_catalog=(skill,),
    )
    execution = AsyncMock(return_value="leaf reply")
    monkeypatch.setattr(_maf_execution, "run_leaf_agent_task", execution)
    resolved = SimpleNamespace(slug="leaf")

    assert await selected.run_leaf_agent_task(
        resolved, capabilities, "task", timeout=1, execution_role=role
    ) == "leaf reply"

    actual_resolved, actual_capabilities, task = execution.call_args.args
    assert actual_resolved is resolved
    assert task == "task"
    assert actual_capabilities._harness is harness
    assert capabilities._harness is None
    assert actual_capabilities.filtered_user_tools == capabilities.filtered_user_tools
    assert actual_capabilities.filtered_mcp_tools == capabilities.filtered_mcp_tools
    assert actual_capabilities.skills == capabilities.skills
    assert actual_capabilities.skill_catalog == capabilities.skill_catalog
    assert execution.call_args.kwargs == {"timeout": 1, "execution_role": role}
    assert _agent_runner.get_agent_runner(harness) is selected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HarnessKind.MAF, HarnessKind.COPILOT])
@pytest.mark.parametrize(
    ("paths", "expected_calls"),
    [
        (None, []),
        ((), [()]),
    ],
)
async def test_cached_facade_preserves_none_vs_empty_skill_path_semantics(
    kind, paths, expected_calls, monkeypatch, tmp_path
):
    harness = AppHarness(kind, tmp_path, default_model="fixture-model")
    selected = _agent_runner.get_agent_runner(harness)
    observed = []
    parser_calls = []
    original = runner.describe_skill_catalog

    def parser(received):
        parser_calls.append(tuple(received))
        return original(received)

    async def execute(actual_harness, request, **kwargs):
        observed.append((actual_harness, request))
        return runner.AgentResult("session", "reply")

    monkeypatch.setattr(runner, "describe_skill_catalog", parser)
    monkeypatch.setattr(
        _maf_execution if kind is HarnessKind.MAF else _copilot_execution, "run", execute
    )

    result = await selected.run_agent(
        "prompt", tools=(), mcp_tools=(), skill_paths=paths, deadline=123.0
    )

    assert result.content == "reply"
    assert observed[0][0] is harness
    assert observed[0][1].skills == ()
    assert observed[0][1].skill_catalog == ()
    assert parser_calls == expected_calls
    assert _agent_runner.get_agent_runner(harness) is selected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HarnessKind.MAF, HarnessKind.COPILOT])
async def test_cached_facade_expands_nested_skill_paths_once_before_selected_execution(
    kind, monkeypatch, tmp_path
):
    harness = AppHarness(kind, tmp_path, default_model="fixture-model")
    selected = _agent_runner.get_agent_runner(harness)
    observed = []
    parser_calls = []
    original = describe_skill_catalog
    collection = tmp_path / "collection"
    parent = collection / "parent"
    child = parent / "nested"
    for directory, name in ((parent, "parent"), (child, "nested")):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\n---\nInstructions.\n",
            encoding="utf-8",
        )
    paths = (collection, child, collection)
    expected = original(paths)
    no_read = Mock(side_effect=AssertionError("Shared skill expansion must not read SKILL.md"))

    def parser(received):
        parser_calls.append(tuple(received))
        return original(received)

    async def execute(actual_harness, request, **kwargs):
        observed.append((actual_harness, request))
        return runner.AgentResult("session", "reply")

    monkeypatch.setattr(Path, "read_text", no_read)
    monkeypatch.setattr(Path, "open", no_read)
    monkeypatch.setattr("builtins.open", no_read)
    monkeypatch.setattr(runner, "describe_skill_catalog", parser)
    monkeypatch.setattr(
        _maf_execution if kind is HarnessKind.MAF else _copilot_execution, "run", execute
    )
    result = await selected.run_agent(
        "prompt", tools=(), mcp_tools=(), skill_paths=paths, deadline=123.0
    )

    assert result.content == "reply"
    assert observed[0][0] is harness
    assert observed[0][1].skills == expected
    assert observed[0][1].skill_catalog == expected
    assert parser_calls == [paths]
    no_read.assert_not_called()
    assert _agent_runner.get_agent_runner(harness) is selected


@pytest.mark.parametrize("kind", [HarnessKind.MAF, HarnessKind.COPILOT])
def test_real_facades_keep_same_root_app_and_clone_ownership_independent(kind, tmp_path, monkeypatch):
    first = AppHarness(kind, tmp_path)
    second = AppHarness(kind, tmp_path)
    clone = replace(first)
    facades = [_agent_runner.get_agent_runner(app) for app in (first, second, clone)]
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", "false" if kind is HarnessKind.COPILOT else "true")

    assert len({id(facade) for facade in facades}) == 3
    assert len({id(facade._backend) for facade in facades}) == 3
    for app, facade in zip((first, second, clone), facades, strict=True):
        assert _agent_runner.get_agent_runner(app) is facade
        assert app._resources.runner is facade
        assert facade._backend._harness is app
        assert app._resources.runtime is None
