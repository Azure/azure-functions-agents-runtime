from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from azure_functions_agents._agent_run_diagnostics import AgentExecutionSurface
from azure_functions_agents.config.schema import AgentConfiguration
from azure_functions_agents.harness import _harness_binding as binding
from azure_functions_agents.harness._provider_config import ProviderKind
from azure_functions_agents.registration.capabilities import AgentCapabilities
from tests._harness_helpers import copilot_state, make_copilot_harness


@pytest.fixture
def binding_root(monkeypatch):
    root = (Path(__file__).resolve().parents[1] / "binding-fixture").resolve()
    monkeypatch.setattr(binding, "_HARNESSES", {})
    monkeypatch.setattr(binding, "get_app_root", lambda: root)
    monkeypatch.delenv(binding.FLAG, raising=False)
    return root


@pytest.fixture
def preview_module(monkeypatch):
    runtime_module = ModuleType("azure_functions_agents.harness.copilot_sdk._copilot_runtime")
    capabilities_module = ModuleType("azure_functions_agents.harness.copilot_sdk._copilot_capabilities")
    select_copilot_harness = Mock(
        side_effect=lambda root: make_copilot_harness(root)
    )
    validate_copilot_agent = Mock()
    runtime_module.select_copilot_harness = select_copilot_harness
    capabilities_module.validate_copilot_agent = validate_copilot_agent
    monkeypatch.setitem(sys.modules, runtime_module.__name__, runtime_module)
    monkeypatch.setitem(sys.modules, capabilities_module.__name__, capabilities_module)
    return SimpleNamespace(
        select_copilot_harness=select_copilot_harness,
        validate_copilot_agent=validate_copilot_agent,
    )


@pytest.mark.parametrize("value", ["false", "FALSE", "fAlSe", "0", " false "])
def test_flag_off(value):
    assert not binding._flag_enabled(value)


@pytest.mark.parametrize("value", [None, "true", "TRUE", "tRuE", "1", " true "])
def test_flag_on(value):
    assert binding._flag_enabled(value)


@pytest.mark.parametrize("value", ["", " ", "\t", "yes", "no", "on", "2", "secret-looking-input"])
def test_invalid_flag_is_explicit_and_does_not_echo_value(value):
    with pytest.raises(ValueError, match="must be true, false, 1, or 0") as error:
        binding._flag_enabled(value)
    if value.strip() and value not in {"no", "on"}:
        assert value not in str(error.value)


def test_provider_kind_is_sdk_neutral():
    assert binding.ProviderKind is ProviderKind


def test_agent_run_attributes_include_execution_surface(binding_root):
    harness = binding.AppHarness(binding.HarnessKind.MAF, binding_root)

    attributes = binding.agent_run_attributes_for_harness(
        harness,
        agent_name="internal",
        display_name="Internal",
        trigger_type=None,
        session_id="session-one",
        model="model-one",
        execution_surface=AgentExecutionSurface.HOSTED_SKILL_BINDING,
    )

    assert attributes["af.agent.harness"] == "maf"
    assert attributes["af.agent.execution_surface"] == "hosted_skill_binding"


def test_app_binding_is_frozen_and_identity_distinct(binding_root):
    first = binding.AppHarness(binding.HarnessKind.MAF, binding_root)
    second = binding.AppHarness(binding.HarnessKind.MAF, binding_root)

    assert first != second
    assert first._resources is not second._resources
    assert first._resources.guard is not second._resources.guard
    assert first._resources.runner is second._resources.runner is None
    assert "_resources" not in repr(first)
    with pytest.raises(FrozenInstanceError):
        first.name = binding.HarnessKind.COPILOT


def test_resource_cell_has_only_a_guard_and_runner(binding_root):
    assert [item.name for item in fields(binding._HarnessResources)] == ["guard", "runner"]
    assert [item.name for item in fields(binding.AppHarness)] == [
        "name", "app_root", "_create_runner", "_resources",
    ]
    resource_field = next(item for item in fields(binding.AppHarness) if item.name == "_resources")
    assert not resource_field.init
    assert not resource_field.repr
    assert not resource_field.compare

    harness = binding.AppHarness(binding.HarnessKind.MAF, binding_root)
    assert harness._resources.guard.acquire(blocking=False)
    harness._resources.guard.release()
    assert harness._resources.runner is None
    with pytest.raises(TypeError, match="_resources"):
        binding.AppHarness(binding.HarnessKind.MAF, binding_root, _resources=harness._resources)


def test_replace_gets_fresh_resources_without_changing_settings(binding_root):
    original = make_copilot_harness(
        binding_root,
        storage_root=binding_root / "state",
        default_model="fixture-model",
    )
    owner = Mock()
    copilot_state(original).runtime = owner
    clone = replace(original)

    assert clone.name is original.name
    assert clone.app_root is original.app_root
    assert clone._create_runner is original._create_runner
    assert clone._resources is not original._resources
    assert clone._resources.guard is not original._resources.guard
    assert clone._resources.runner is None
    assert copilot_state(clone).storage_root is copilot_state(original).storage_root
    assert clone.default_model == original.default_model
    assert copilot_state(clone).runtime is None
    assert copilot_state(original).runtime is owner


def test_request_retains_frozen_neutral_descriptors_and_standalone_skill_paths():
    tools = ()
    request = binding.HarnessRequest(
        prompt="hello",
        instructions=None,
        agent_slug="fixture",
        session_id="session",
        new_session=True,
        model="fixture-model",
        tools=tools,
        max_output_tokens=None,
        deadline=123.0,
    )

    assert [item.name for item in fields(request)] == [
        "prompt", "instructions", "agent_slug", "session_id", "new_session",
        "model", "tools", "max_output_tokens", "deadline",
        "mcp_servers", "skills", "skill_catalog",
        "execution_role", "event_sink",
        "session_state",
    ]
    assert request.mcp_servers == request.skills == request.skill_catalog == ()
    assert request.execution_role == "primary"
    assert request.event_sink is None
    assert request.tools is tools
    assert replace(request) == request
    with pytest.raises(FrozenInstanceError):
        request.model = "replacement"


def test_default_selection_uses_copilot(binding_root, preview_module):
    harness = binding.get_harness()

    assert harness.name is binding.HarnessKind.COPILOT
    assert harness.app_root == binding_root
    preview_module.select_copilot_harness.assert_called_once_with(binding_root)


@pytest.mark.parametrize("value", ["false", "0"])
def test_explicit_false_selection_uses_maf(binding_root, preview_module, monkeypatch, value):
    monkeypatch.setenv(binding.FLAG, value)

    harness = binding.get_harness()

    assert harness.name is binding.HarnessKind.MAF
    assert harness.app_root == binding_root
    preview_module.select_copilot_harness.assert_not_called()


def test_selection_canonicalizes_root_and_reuses_standalone(binding_root, preview_module):
    selected = binding.get_harness(binding_root / "nested" / "..")

    assert selected.app_root == binding_root
    assert binding.get_harness(binding_root) is selected
    assert {binding_root: selected} == binding._HARNESSES


def test_selection_is_once_per_app_and_shared_with_standalone(
    binding_root, preview_module, monkeypatch
):
    monkeypatch.setenv(binding.FLAG, "true")
    selected = binding.get_harness()
    monkeypatch.setenv(binding.FLAG, "false")

    assert selected.name is binding.HarnessKind.COPILOT
    assert binding.get_harness(binding_root) is selected
    assert binding.get_harness(binding_root / "another").name is binding.HarnessKind.MAF
    preview_module.select_copilot_harness.assert_called_once_with(binding_root)


def test_separate_app_construction_captures_a_fresh_binding(
    binding_root, preview_module, monkeypatch
):
    monkeypatch.setenv(binding.FLAG, "true")
    standalone = binding.get_harness(binding_root)
    first_app = binding.get_harness(binding_root, new_app=True)
    second_app = binding.get_harness(binding_root, new_app=True)
    monkeypatch.setenv(binding.FLAG, "false")
    new_app = binding.get_harness(binding_root, new_app=True)

    assert first_app is not second_app
    assert first_app._resources is not second_app._resources
    assert standalone is not first_app
    assert first_app.name is second_app.name is binding.HarnessKind.COPILOT
    assert new_app.name is binding.HarnessKind.MAF
    assert binding.get_harness(binding_root) is standalone
    assert preview_module.select_copilot_harness.call_count == 3


def test_invalid_flag_fails_without_caching_or_selecting(
    binding_root, preview_module, monkeypatch
):
    monkeypatch.setenv(binding.FLAG, "")
    with pytest.raises(ValueError, match="must be true"):
        binding.get_harness(binding_root)

    assert not binding._HARNESSES
    preview_module.select_copilot_harness.assert_not_called()


def test_preview_selection_failure_does_not_cache_or_fallback(
    binding_root, preview_module, monkeypatch
):
    preview_module.select_copilot_harness.side_effect = binding.UnsupportedCapabilityError(
        "fixture qualification failure"
    )
    with pytest.raises(binding.UnsupportedCapabilityError, match="qualification"):
        binding.get_harness(binding_root)
    assert not binding._HARNESSES

    selected = make_copilot_harness(binding_root)
    preview_module.select_copilot_harness.side_effect = None
    preview_module.select_copilot_harness.return_value = selected
    assert binding.get_harness(binding_root) is selected


def test_concurrent_standalone_selection_creates_one_binding(
    binding_root, preview_module, monkeypatch
):
    monkeypatch.setenv(binding.FLAG, "true")
    with ThreadPoolExecutor(max_workers=4) as executor:
        selections = list(executor.map(binding.get_harness, [binding_root] * 12))

    assert all(selected is selections[0] for selected in selections)
    preview_module.select_copilot_harness.assert_called_once_with(binding_root)


def test_validation_skips_preview_for_maf(binding_root, preview_module):
    harness = binding.AppHarness(binding.HarnessKind.MAF, binding_root)
    binding.validate_agent(harness, Mock(), AgentCapabilities())

    preview_module.select_copilot_harness.assert_not_called()


def test_validation_passes_the_bound_context_to_preview(binding_root, preview_module):
    harness = make_copilot_harness(binding_root)
    resolved = Mock(agent_configuration=AgentConfiguration(), model="fixture-model")
    capabilities = AgentCapabilities()
    binding.validate_agent(harness, resolved, capabilities)

    assert copilot_state(harness)._harness is harness


def test_bind_harness_reuses_an_existing_binding_without_validation(binding_root, monkeypatch):
    harness = binding.AppHarness(binding.HarnessKind.MAF, binding_root)
    capabilities = AgentCapabilities(_harness=harness)
    select = Mock()
    validate = Mock()
    monkeypatch.setattr(binding, "get_harness", select)
    monkeypatch.setattr(binding, "validate_agent", validate)

    assert binding.bind_harness(Mock(), capabilities) is harness
    select.assert_not_called()
    validate.assert_not_called()


def test_bind_harness_returns_validated_context_without_mutating_capabilities(binding_root, monkeypatch):
    harness = binding.AppHarness(binding.HarnessKind.MAF, binding_root)
    resolved = Mock()
    capabilities = AgentCapabilities()
    validate = Mock(side_effect=binding.UnsupportedCapabilityError("fixture failure"))
    monkeypatch.setattr(binding, "get_harness", Mock(return_value=harness))
    monkeypatch.setattr(binding, "validate_agent", validate)

    with pytest.raises(binding.UnsupportedCapabilityError, match="fixture failure"):
        binding.bind_harness(resolved, capabilities)
    assert capabilities._harness is None

    validate.side_effect = None
    assert binding.bind_harness(resolved, capabilities) is harness
    assert capabilities._harness is None
    validate.assert_called_with(harness, resolved, capabilities)


@pytest.mark.parametrize("preview", [False, True])
def test_selection_runner_validation_and_cleanup_do_not_import_sdks(preview):
    script = """
import asyncio
import os
import sys

blocked = ("agent_framework", "copilot")
for name in blocked:
    sys.modules[name] = None
for name in (
    "WEBSITE_INSTANCE_ID", "FUNCTIONS_WORKER_PROCESS_COUNT",
    "AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", "AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY",
    "AzureWebJobsStorage", "AzureWebJobsStorage__blobServiceUri",
):
    os.environ.pop(name, None)
os.environ["AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT"] = sys.argv[1]
os.environ["AZURE_FUNCTIONS_AGENTS_PROVIDER"] = "openai"
os.environ["OPENAI_API_KEY"] = "fixture-only"
os.environ["AZURE_FUNCTIONS_AGENTS_MODEL"] = "fixture-model"

import azure_functions_agents
import azure_functions_agents.harness
from azure_functions_agents.harness import _harness_binding as binding
from azure_functions_agents.harness import _harness_lifecycle as lifecycle
from azure_functions_agents.harness._agent_runner import get_agent_runner
from azure_functions_agents.config.schema import AgentConfiguration
from azure_functions_agents.registration.capabilities import AgentCapabilities
from types import SimpleNamespace

harness = binding.get_harness(new_app=True)
runner = get_agent_runner(harness)
assert get_agent_runner(harness) is runner
binding.validate_agent(
    harness, SimpleNamespace(agent_configuration=AgentConfiguration(), model=None, workflows=None,
                             sandbox_config=None, tools_disabled=False, trigger=None,
                             builtin_endpoints=SimpleNamespace(
                                 debug_chat_ui=False, chat_api=False, mcp=False,
                             )),
    AgentCapabilities(),
)
assert not lifecycle._SHUTDOWN_CALLBACKS
asyncio.run(lifecycle._shutdown_harnesses())
assert all(sys.modules[name] is None for name in blocked)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(preview).lower()],
        capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
