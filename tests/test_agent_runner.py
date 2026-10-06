from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from azure_functions_agents import runner
from azure_functions_agents.config.schema import AgentConfiguration
from azure_functions_agents.harness import _agent_runner
from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
from azure_functions_agents.harness.copilot_sdk import _copilot_execution, _copilot_runtime
from azure_functions_agents.registration.capabilities import AgentCapabilities


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
