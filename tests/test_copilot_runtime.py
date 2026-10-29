from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from azure.functions.timer import TimerRequest

from azure_functions_agents import runner
from azure_functions_agents._function_tool import tool
from azure_functions_agents._runtime_tool_names import (
    FHS_CANCEL_WORKFLOW_TOOL_NAME,
    FHS_EXECUTE_PYTHON_TOOL_NAME,
    FHS_GET_WORKFLOW_STATUS_TOOL_NAME,
    FHS_LIST_WORKFLOWS_TOOL_NAME,
    FHS_START_WORKFLOW_TOOL_NAME,
    FHS_TERMINATE_WORKFLOW_TOOL_NAME,
)
from azure_functions_agents._tool_descriptor import ApprovalMode, ToolDescriptor
from azure_functions_agents.app import create_function_app
from azure_functions_agents.config import paths
from azure_functions_agents.config.env import EnvVar
from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.config.merge import compose
from azure_functions_agents.config.schema import (
    AgentConfiguration,
    BuiltinEndpointsConfig,
    DynamicSessionsCodeInterpreterConfig,
    SubagentRef,
    TriggerSpec,
    WorkflowConfig,
)
from azure_functions_agents.discovery.mcp import MCPServerDescriptor
from azure_functions_agents.discovery.skills import SkillDescriptor
from azure_functions_agents.discovery.tools import discover_user_tools
from azure_functions_agents.harness import _harness_binding as _harness
from azure_functions_agents.harness import _harness_lifecycle as lifecycle
from azure_functions_agents.harness._harness_binding import (
    AppHarness,
    HarnessKind,
    UnsupportedCapabilityError,
)
from azure_functions_agents.harness._provider_config import ProviderKind
from azure_functions_agents.harness.agent_framework import _maf_execution
from azure_functions_agents.harness.agent_framework._maf_tools import build_maf_tools
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_capabilities as _capabilities,
)
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_execution as _copilot,
)
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_providers as provider_registry,
)
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_runtime as runtime,
)
from azure_functions_agents.harness.copilot_sdk._copilot_providers import (
    FoundryProvider,
    OpenAIProvider,
)
from azure_functions_agents.harness.copilot_sdk._copilot_runtime import CopilotPreviewError
from azure_functions_agents.registration._handlers import make_http_agent_handler
from azure_functions_agents.registration.capabilities import build_capabilities
from tests._harness_helpers import copilot_state, make_copilot_harness
from tests.test_copilot_execution import _fake_client

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "copilot-preview" / "src"


@pytest.fixture
def preview(monkeypatch, tmp_path):
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    monkeypatch.setattr(paths, "_app_root", tmp_path)
    monkeypatch.setenv(_harness.FLAG, "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-not-a-secret")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("WEBSITE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("FUNCTIONS_WORKER_PROCESS_COUNT", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri", raising=False)
    return tmp_path




@pytest.fixture
def runtime_preview(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle, "_SHUTDOWN_CALLBACKS", set())
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri", raising=False)
    return make_copilot_harness(
        tmp_path,
        storage_root=tmp_path / "state",
        provider=OpenAIProvider("not-a-credential"),
    )

def _sample():
    resolved = compose(
        load_agent_specs(SAMPLE)[0], load_global_config(SAMPLE),
        discovered_mcp_names=[], discovered_skill_names=[],
    )
    capabilities = build_capabilities(
        resolved,
        discovered_user_tools=discover_user_tools(SAMPLE).tools,
        discovered_mcp_tools={},
        discovered_skills={},
    )
    return resolved, capabilities


def _state_persisting_client():
    client = _fake_client()
    session = client.create_session.return_value

    async def create_session(**options):
        storage = options["create_session_fs_handler"](session)
        await storage.write_file("/session-state/native", "")
        return session

    client.create_session.side_effect = create_session
    return client


def test_supplied_http_session_creates_then_resumes_after_runtime_restart(
    preview, monkeypatch
):
    import copilot

    app_root = preview / "http-app"
    shutil.copytree(SAMPLE, app_root)
    first_client = _state_persisting_client()
    second_client = _fake_client()
    monkeypatch.setattr(
        copilot, "CopilotClient", Mock(side_effect=[first_client, second_client])
    )

    async def call():
        try:
            first_app = create_function_app(app_root)
            first_handler = next(
                function.get_user_function()
                for function in first_app.get_functions()
                if function.get_function_name() == "agent_main_builtin_chat"
            )
            first = await first_handler(
                SimpleNamespace(
                    headers={"x-ms-session-id": "caller-http"},
                    json=AsyncMock(return_value={"prompt": "first"}),
                )
            )
            assert first.headers["x-ms-session-id"] == "caller-http"

            await lifecycle._shutdown_harnesses()
            _harness._HARNESSES.clear()

            second_app = create_function_app(app_root)
            second_handler = next(
                function.get_user_function()
                for function in second_app.get_functions()
                if function.get_function_name() == "agent_main_builtin_chat"
            )
            second = await second_handler(
                SimpleNamespace(
                    headers={"x-ms-session-id": "caller-http"},
                    json=AsyncMock(return_value={"prompt": "second"}),
                )
            )
            assert second.headers["x-ms-session-id"] == "caller-http"
        finally:
            await lifecycle._shutdown_harnesses()

    asyncio.run(call())
    first_client.create_session.assert_awaited_once()
    assert first_client.create_session.call_args.kwargs["session_id"] == "main-caller-http"
    first_client.resume_session.assert_not_awaited()
    second_client.create_session.assert_not_awaited()
    second_client.resume_session.assert_awaited_once()
    assert second_client.resume_session.call_args.args[0] == "main-caller-http"


def test_normalized_mcp_session_creates_then_resumes_after_runtime_restart(
    preview, monkeypatch
):
    import copilot

    app_root = preview / "mcp-restart-app"
    app_root.mkdir()
    (app_root / "main.agent.md").write_text(
        (SAMPLE / "main.agent.md").read_text(encoding="utf-8").replace(
            "  mcp: false", "  mcp: true"
        ),
        encoding="utf-8",
    )
    first_client = _state_persisting_client()
    second_client = _fake_client()
    monkeypatch.setattr(
        copilot, "CopilotClient", Mock(side_effect=[first_client, second_client])
    )
    context = json.dumps(
        {"arguments": {"prompt": "continue"}, "sessionid": "transport/session"}
    )

    async def call():
        try:
            first_app = create_function_app(app_root)
            first_handler = next(
                function.get_user_function()
                for function in first_app.get_functions()
                if function.get_function_name() == "agent_main_builtin_mcp"
            )
            first = json.loads(await first_handler(context))
            assert first["session_id"].startswith("mcp-")

            await lifecycle._shutdown_harnesses()
            _harness._HARNESSES.clear()

            second_app = create_function_app(app_root)
            second_handler = next(
                function.get_user_function()
                for function in second_app.get_functions()
                if function.get_function_name() == "agent_main_builtin_mcp"
            )
            second = json.loads(await second_handler(context))
            assert second["session_id"] == first["session_id"]
            return first["session_id"]
        finally:
            await lifecycle._shutdown_harnesses()

    session_id = asyncio.run(call())
    first_client.create_session.assert_awaited_once()
    assert first_client.create_session.call_args.kwargs["session_id"] == f"main-{session_id}"
    first_client.resume_session.assert_not_awaited()
    second_client.create_session.assert_not_awaited()
    second_client.resume_session.assert_awaited_once()
    assert second_client.resume_session.call_args.args[0] == f"main-{session_id}"


def test_app_context_freezes_storage_without_initializing_either_provider(preview, monkeypatch):
    from azure_functions_agents.harness.copilot_sdk._copilot_session_identity import StorageMode

    monkeypatch.setenv("AzureWebJobsStorage", "fixture-connection")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER", "first-container")
    first = _harness.get_harness(preview, new_app=True)
    monkeypatch.delenv("AzureWebJobsStorage")
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", "https://fixture.invalid")
    monkeypatch.setenv("AzureWebJobsStorage__clientId", "storage-identity")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER", "second-container")
    second = _harness.get_harness(preview, new_app=True)
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri")
    monkeypatch.setenv(_harness.FLAG, "false")
    assert first is not second
    assert copilot_state(first).session_storage.mode is StorageMode.BLOB
    assert copilot_state(first).session_storage.blob.connection_string == "fixture-connection"
    assert copilot_state(first).session_storage.blob.container_name == "first-container"
    assert copilot_state(second).session_storage.blob.blob_service_url == "https://fixture.invalid"
    assert copilot_state(second).session_storage.blob.client_id == "storage-identity"
    assert copilot_state(second).session_storage.blob.container_name == "second-container"
    assert _harness.get_harness(preview, new_app=True).default_model is None
    assert not copilot_state(first).storage_root.exists()
    assert not copilot_state(second).storage_root.exists()


def test_missing_sdk_is_explicit_without_forcing_a_second_version_check(preview, monkeypatch):
    def missing(_name):
        raise runtime.PackageNotFoundError

    monkeypatch.setattr(runtime, "version", missing)
    with pytest.raises(CopilotPreviewError, match=r"\[copilot\]"):
        _harness.get_harness()
    monkeypatch.setattr(runtime, "version", lambda _: "0.0.0")
    assert _harness.get_harness().name is HarnessKind.COPILOT


def test_provider_selection_error_lists_registered_copilot_providers(preview, monkeypatch):
    monkeypatch.setattr(runtime, "version", lambda _: "0.0.0")
    monkeypatch.setattr(runtime, "resolve_inference_target", Mock(side_effect=RuntimeError))
    monkeypatch.setattr(
        provider_registry,
        "_PROVIDERS",
        {
            ProviderKind.OPENAI: OpenAIProvider,
            ProviderKind.FOUNDRY: FoundryProvider,
        },
    )
    with pytest.raises(
        UnsupportedCapabilityError,
        match=(
            r"AZURE_FUNCTIONS_AGENTS_PROVIDER=openai, or foundry "
            r"with the existing explicit-or-autodetected provider settings\."
        ),
    ):
        runtime.select_copilot_harness(preview)


def test_reasoning_app_settings_are_frozen_on_the_selected_harness(preview, monkeypatch):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", "high")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY", "detailed")

    harness = _harness.get_harness()

    assert harness.name is HarnessKind.COPILOT
    state = copilot_state(harness)
    assert state.reasoning_effort == "high"
    assert state.reasoning_summary == "detailed"


def test_reasoning_effort_preserves_new_sdk_literal_value(preview, monkeypatch):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", "future")
    runtime._reasoning_effort_values.cache_clear()
    monkeypatch.setattr(
        runtime,
        "get_args",
        lambda _: ("low", "medium", "high", "xhigh", "max", "future"),
    )

    try:
        harness = _harness.get_harness(preview, new_app=True)
    finally:
        runtime._reasoning_effort_values.cache_clear()

    assert harness.name is HarnessKind.COPILOT
    assert copilot_state(harness).reasoning_effort == "future"


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        (EnvVar.REASONING_EFFORT, "none", "AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT='none'"),
        (EnvVar.REASONING_SUMMARY, "auto", "AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY='auto'"),
    ],
)
def test_invalid_reasoning_settings_warn_and_omit_override(
    preview, monkeypatch, caplog: pytest.LogCaptureFixture, name, value, expected
):
    monkeypatch.setenv(name, value)

    with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
        harness = _harness.get_harness(preview, new_app=True)

    assert harness.name is HarnessKind.COPILOT
    state = copilot_state(harness)
    if name is EnvVar.REASONING_EFFORT:
        assert state.reasoning_effort is None
    else:
        assert state.reasoning_summary is None
    assert expected in caplog.text
    assert "SDK/provider default applies" in caplog.text


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FUNCTIONS_WORKER_PROCESS_COUNT", ""),
        ("FUNCTIONS_WORKER_PROCESS_COUNT", "   "),
        ("FUNCTIONS_WORKER_PROCESS_COUNT", " 1 "),
        ("FUNCTIONS_WORKER_PROCESS_COUNT", "2"),
        ("WEBSITE_INSTANCE_ID", "   "),
        ("WEBSITE_INSTANCE_ID", "cloud-instance"),
    ],
)
def test_host_owned_worker_and_hosting_settings_are_accepted(preview, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert _harness.get_harness().name is HarnessKind.COPILOT


@pytest.mark.parametrize(
    ("name", "value", "allowed", "diagnostic"),
    [
        (EnvVar.ENABLE_COPILOT, None, True, ""),
        (EnvVar.ENABLE_COPILOT, "", False, "must be true"),
        (EnvVar.ENABLE_COPILOT, "   ", False, "must be true"),
        (EnvVar.ENABLE_COPILOT, "true", True, ""),
        (EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT, None, True, ""),
        (EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT, "", True, ""),
        (EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT, "   ", True, ""),
        (EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT, "1", True, ""),
        (EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT, "2", True, ""),
        (EnvVar.WEBSITE_INSTANCE_ID, None, True, ""),
        (EnvVar.WEBSITE_INSTANCE_ID, "", True, ""),
        (EnvVar.WEBSITE_INSTANCE_ID, "   ", True, ""),
        (EnvVar.WEBSITE_INSTANCE_ID, "cloud-instance", True, ""),
        (EnvVar.REASONING_EFFORT, None, True, ""),
        (EnvVar.REASONING_EFFORT, "", True, ""),
        (EnvVar.REASONING_EFFORT, "   ", True, ""),
        (EnvVar.REASONING_EFFORT, "high", True, ""),
        (EnvVar.REASONING_EFFORT, "none", True, ""),
        (EnvVar.REASONING_SUMMARY, None, True, ""),
        (EnvVar.REASONING_SUMMARY, "", True, ""),
        (EnvVar.REASONING_SUMMARY, "   ", True, ""),
        (EnvVar.REASONING_SUMMARY, "detailed", True, ""),
        (EnvVar.REASONING_SUMMARY, "auto", True, ""),
    ],
)
def test_copilot_environment_edge_semantics(preview, monkeypatch, name, value, allowed, diagnostic):
    monkeypatch.setenv(EnvVar.ENABLE_COPILOT, "true")
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    if allowed:
        harness = _harness.get_harness(preview, new_app=True)
        assert harness.name is (
            HarnessKind.MAF if name is EnvVar.ENABLE_COPILOT and value is None else HarnessKind.COPILOT
        )
    else:
        error_type = ValueError if name is EnvVar.ENABLE_COPILOT else UnsupportedCapabilityError
        with pytest.raises(error_type, match=diagnostic):
            _harness.get_harness(preview, new_app=True)


def test_preview_sample_uses_normal_typed_discovery(preview):
    resolved, capabilities = _sample()
    _harness.validate_agent(_harness.get_harness(), resolved, capabilities)
    assert [item.name for item in capabilities.filtered_user_tools] == ["make_receipt"]
    assert resolved.enabled_mcp_names == resolved.enabled_skills_names == []
    assert [item.name for item in capabilities.web_request_tools] == ["web_request"]
    assert not resolved.builtin_endpoints.debug_chat_ui


def test_preview_config_scenario_indexes_without_native_process(preview):
    root = Path(__file__).parent / "fixtures" / "config_scenarios" / "18_copilot_preview"
    app = create_function_app(root)
    assert app.get_functions()
    assert not copilot_state(_harness.get_harness(root)).storage_root.exists()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("WEBSITE_INSTANCE_ID", "cloud-instance"),
        ("FUNCTIONS_WORKER_PROCESS_COUNT", "8"),
    ],
)
def test_preview_config_scenario_indexes_with_host_owned_environment_settings(
    preview, monkeypatch, setting, value
):
    root = Path(__file__).parent / "fixtures" / "config_scenarios" / "18_copilot_preview"
    monkeypatch.setenv(setting, value)
    app = create_function_app(root)
    assert app.get_functions()
    assert _harness.get_harness(root).name is HarnessKind.COPILOT
    assert not copilot_state(_harness.get_harness(root)).storage_root.exists()


def test_foundry_configuration_is_frozen_without_authentication(preview, monkeypatch):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://fixture.services.ai.azure.com/api/projects/test")
    monkeypatch.setenv("FOUNDRY_MODEL", "deployed-model")
    selected = _harness.get_harness()
    assert copilot_state(selected).provider is not None
    assert copilot_state(selected).provider.kind == "foundry"
    assert selected.default_model == "deployed-model"
    assert "fixture.services.ai.azure.com" not in repr(selected)
    assert not copilot_state(selected).storage_root.exists()


def test_registered_mcp_and_skill_descriptors_are_supported(preview):
    resolved, capabilities = _sample()
    server = MCPServerDescriptor(
        name="selected", url="https://fixture.invalid/mcp", transport="streamable-http",
        headers=(), tools=("lookup",), auth_scope=None, client_id=None,
    )
    approved = SkillDescriptor(name="approved", path=preview / "approved")
    excluded = SkillDescriptor(name="excluded", path=preview / "excluded")
    capabilities = replace(
        capabilities,
        filtered_mcp_tools=(server,),
        enabled_skill_paths=(approved.path,),
        skills=(approved,),
        skill_catalog=(approved, excluded),
    )

    _harness.validate_agent(_harness.get_harness(), resolved, capabilities)

    assert capabilities.skills == (approved,)
    assert capabilities.skill_catalog == (approved, excluded)


def test_direct_preview_accepts_existing_entrypoints(preview):
    resolved, capabilities = _sample()
    candidate = resolved.model_copy(update={
        "builtin_endpoints": BuiltinEndpointsConfig(debug_chat_ui=False, chat_api=True, mcp=True),
    })
    _harness.validate_agent(_harness.get_harness(), candidate, capabilities)
    for candidate in (
        resolved.model_copy(update={"trigger": TriggerSpec(type="queue_trigger")}),
        resolved.model_copy(update={
            "builtin_endpoints": BuiltinEndpointsConfig(debug_chat_ui=True, chat_api=True, mcp=False),
        }),
        resolved.model_copy(update={"subagents": [SubagentRef(agent="specialist")]}),
        resolved.model_copy(update={"workflows": WorkflowConfig(enabled=True)}),
    ):
        _harness.validate_agent(_harness.get_harness(), candidate, capabilities)


def test_direct_preview_accepts_explicit_host_system_tools(preview):
    resolved, capabilities = _sample()
    capabilities = replace(
        capabilities, web_request_tools=[tool(name="web_request")(lambda url: url)]
    )
    resolved = resolved.model_copy(update={
        "sandbox_config": DynamicSessionsCodeInterpreterConfig(
            endpoint="https://fixture.dynamicsessions.io"
        ),
    })
    _harness.validate_agent(_harness.get_harness(), resolved, capabilities)


def test_workflow_management_delegate_namespace_is_not_a_collision(preview):
    resolved, capabilities = _sample()
    resolved = resolved.model_copy(update={
        "subagents": [SubagentRef(agent="billing")],
        "workflows": WorkflowConfig(enabled=True),
    })

    _harness.validate_agent(_harness.get_harness(), resolved, capabilities)


@pytest.mark.parametrize(
    ("configuration", "expected_names"),
    [
        ("tools: true\n", ["web_request"]),
        ("tools: true\nsystem_tools:\n  web_request: false\n", []),
        ("tools: false\n", []),
    ],
)
def test_copilot_web_request_default_and_optouts(preview, tmp_path, configuration, expected_names):
    root = tmp_path / "web-request-policy"
    root.mkdir()
    (root / "main.agent.md").write_text(
        "---\nname: Tool policy\ndescription: Exercise Copilot host-tool policy.\n"
        "builtin_endpoints:\n  chat_api: true\n  debug_chat_ui: false\n  mcp: false\n"
        "mcp: false\nskills: false\nworkflows:\n  enabled: false\n"
        f"{configuration}---\nUse only configured tools.\n",
        encoding="utf-8",
    )
    resolved = compose(
        load_agent_specs(root)[0], load_global_config(root),
        discovered_mcp_names=[], discovered_skill_names=[],
    )
    capabilities = build_capabilities(
        resolved, discovered_user_tools=[], discovered_mcp_tools={}, discovered_skills={},
    )
    _harness.validate_agent(_harness.get_harness(), resolved, capabilities)
    assert [function.name for function in capabilities.web_request_tools or []] == expected_names


def test_sandbox_name_collision_fails_during_registration(preview):
    resolved, capabilities = _sample()
    resolved = resolved.model_copy(update={
        "sandbox_config": DynamicSessionsCodeInterpreterConfig(
            endpoint="https://fixture.dynamicsessions.io"
        ),
    })
    capabilities = replace(
        capabilities,
        filtered_user_tools=[tool(name=FHS_EXECUTE_PYTHON_TOOL_NAME)(lambda code: code)],
    )
    with pytest.raises(UnsupportedCapabilityError, match="collides with an enabled runtime-owned"):
        _harness.validate_agent(_harness.get_harness(), resolved, capabilities)


def test_unresolved_sandbox_endpoint_allows_authored_runtime_name(preview):
    resolved, capabilities = _sample()
    resolved = resolved.model_copy(update={
        "sandbox_config": DynamicSessionsCodeInterpreterConfig(endpoint="$MISSING_POOL"),
    })
    capabilities = replace(
        capabilities,
        filtered_user_tools=[tool(name=FHS_EXECUTE_PYTHON_TOOL_NAME)(lambda code: code)],
    )

    _harness.validate_agent(_harness.get_harness(), resolved, capabilities)


def test_maf_keeps_authored_approval_that_copilot_cannot_adapt(tmp_path):
    resolved, capabilities = _sample()
    descriptor = tool(name="bounded", approval_mode=ApprovalMode.ALWAYS_REQUIRE)(lambda: "ok")
    capabilities = replace(capabilities, filtered_user_tools=(descriptor,))

    _harness.validate_agent(AppHarness(HarnessKind.MAF, tmp_path), resolved, capabilities)

    assert capabilities.filtered_user_tools == (descriptor,)
    assert build_maf_tools(capabilities.filtered_user_tools)[0].approval_mode == "always_require"


def test_retired_maf_compaction_has_no_active_copilot_configuration(preview):
    resolved, capabilities = _sample()
    _harness.validate_agent(_harness.get_harness(), resolved, capabilities)


def test_output_limit_is_preserved_during_copilot_validation(preview):
    resolved, capabilities = _sample()
    candidate = resolved.model_copy(update={"agent_configuration": AgentConfiguration(max_output_tokens=256)})

    _harness.validate_agent(_harness.get_harness(), candidate, capabilities)


def test_standalone_output_limit_reaches_request_boundary(preview, monkeypatch):
    invoke = AsyncMock(return_value=runner.AgentResult("session-id", "native reply"))
    monkeypatch.setattr(_copilot, "run", invoke)
    result = asyncio.run(runner.run_agent(
        "no inference", tools=[], mcp_tools=[],
        agent_configuration=AgentConfiguration(max_output_tokens=256),
    ))
    assert result.content == "native reply"
    invoke.assert_awaited_once()
    request = invoke.await_args.args[1]
    assert request.max_output_tokens == 256


@pytest.mark.parametrize("policy", [{"approval_mode": ApprovalMode.ALWAYS_REQUIRE}])
def test_unsupported_tool_policies_are_not_silently_lost(policy):
    descriptor = tool(name="bounded", **policy)(lambda value: value)
    with pytest.raises(UnsupportedCapabilityError, match="approval"):
        _capabilities.prepare_tools([descriptor])


def test_direct_preview_forks_before_maf_construction_or_blob(preview, monkeypatch):
    harness = _harness.get_harness()
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    maf = AsyncMock(side_effect=AssertionError("MAF construction must not run"))
    monkeypatch.setattr(_maf_execution, "_build_agent_session", maf)
    invoke = AsyncMock(return_value=runner.AgentResult("new-id", "native reply"))
    monkeypatch.setattr(_copilot, "run", invoke)
    result = asyncio.run(runner.run_agent("hello", tools=[], mcp_tools=[]))
    assert result.content == "native reply"
    assert invoke.call_args.args[0] is harness
    assert invoke.call_args.args[1].new_session
    assert not (preview / "state" / "agent-sessions").exists()
    maf.assert_not_called()


def test_standalone_skill_root_does_not_promote_implicit_nested_documents(preview, monkeypatch):
    parent = preview / "skills" / "parent"
    child = parent / "excluded-child"
    child.mkdir(parents=True)
    (parent / "SKILL.md").write_text(
        "---\nname: parent\ndescription: Parent skill\n---\n", encoding="utf-8"
    )
    (child / "SKILL.md").write_text(
        "---\nname: excluded-child\ndescription: Excluded child\n---\n", encoding="utf-8"
    )
    invoke = AsyncMock(return_value=runner.AgentResult("session", "reply"))
    monkeypatch.setattr(_copilot, "run", invoke)
    asyncio.run(
        runner.run_agent(
            "hello", tools=[], mcp_tools=[], skill_paths=[parent],
        )
    )
    request = invoke.call_args.args[1]
    assert [skill.name for skill in request.skills] == ["parent"]
    assert {skill.name for skill in request.skill_catalog} == {"parent"}
    assert request.skills[0].path == parent.resolve()
    assert request.mcp_servers == ()
    assert type(request.tools) is tuple


def test_independently_explicit_roots_outside_app_preserve_nested_ownership(preview, monkeypatch):
    app_root = preview / "app"
    app_root.mkdir()
    parent = preview / "external" / "external-parent"
    child = parent / "external-child"
    child.mkdir(parents=True)
    (parent / "SKILL.md").write_text(
        "---\nname: external-parent\ndescription: Parent\n---\n", encoding="utf-8",
    )
    (child / "SKILL.md").write_text(
        "---\nname: external-child\ndescription: Child\n---\n", encoding="utf-8",
    )
    harness = _harness.get_harness(app_root, new_app=True)
    invoke = AsyncMock(return_value=runner.AgentResult("session", "reply"))
    monkeypatch.setattr(_copilot, "run", invoke)
    original_catalog = runner.describe_skill_catalog
    catalog_calls = []

    def catalog(paths):
        catalog_calls.append(tuple(paths))
        return original_catalog(paths)

    monkeypatch.setattr(runner, "describe_skill_catalog", catalog)

    asyncio.run(runner.run_agent(
        "hello", tools=[], mcp_tools=[], skill_paths=[parent, child], _harness=harness,
    ))

    request = invoke.call_args.args[1]
    assert [skill.name for skill in request.skills] == ["external-parent", "external-child"]
    assert {skill.name for skill in request.skill_catalog} == {
        "external-parent", "external-child",
    }
    assert catalog_calls == [(parent, child)]


def test_maf_standalone_does_not_discover_unrequested_skills(tmp_path, monkeypatch):
    unused = tmp_path / "skills" / "unused"
    unused.mkdir(parents=True)
    (unused / "SKILL.md").write_text(
        "---\nname: invalid--name\ndescription: Not requested\n---\n", encoding="utf-8",
    )
    invoke = AsyncMock(return_value=runner.AgentResult("session", "reply"))
    monkeypatch.setattr(_maf_execution, "run", invoke)

    result = asyncio.run(runner.run_agent(
        "hello", tools=[], mcp_tools=[], skill_paths=None,
        _harness=AppHarness(HarnessKind.MAF, tmp_path),
    ))

    request = invoke.call_args.args[1]
    assert result.content == "reply"
    assert request.skills == request.skill_catalog == ()


def test_direct_preview_composes_host_tools_in_maf_order(preview, monkeypatch):
    user = tool(name="user_tool")(lambda: "user")
    sandbox = tool(name="execute_python")(lambda code: code)
    web = tool(name="web_request")(lambda url: url)
    invoke = AsyncMock(return_value=runner.AgentResult("public-id", "native reply"))
    monkeypatch.setattr(_copilot, "run", invoke)
    result = asyncio.run(runner.run_agent(
        "hello", tools=[user], mcp_tools=[], sandbox_tools=[sandbox], web_request_tools=[web],
    ))
    request = invoke.call_args.args[1]
    assert result.session_id == "public-id"
    assert [function.name for function in request.tools] == ["user_tool", "execute_python", "web_request"]


def test_combined_tool_collision_fails_before_native_startup(preview, monkeypatch):
    invoke = AsyncMock()
    monkeypatch.setattr(_copilot, "run", invoke)
    duplicate = tool(name="web_request")(lambda: "duplicate")
    with pytest.raises(UnsupportedCapabilityError, match="unique custom tool names"):
        asyncio.run(runner.run_agent(
            "hello", tools=[duplicate], mcp_tools=[], web_request_tools=[duplicate],
        ))
    invoke.assert_not_called()


def test_workflow_management_requires_bound_policy(preview):
    from azure_functions_agents.workflows.tools import build_workflow_tools

    durable_client = AsyncMock()
    workflow_tools = build_workflow_tools(
        session_id="public-session", workflow_agent_slug="main", agent_name="main",
        durable_client=durable_client,
    )
    assert [function.name for function in _capabilities.prepare_tools(workflow_tools)] == [
        FHS_START_WORKFLOW_TOOL_NAME,
        FHS_GET_WORKFLOW_STATUS_TOOL_NAME,
        FHS_LIST_WORKFLOWS_TOOL_NAME,
        FHS_CANCEL_WORKFLOW_TOOL_NAME,
        FHS_TERMINATE_WORKFLOW_TOOL_NAME,
    ]
    with pytest.raises(UnsupportedCapabilityError, match="per-agent workflow policy"):
        asyncio.run(runner.run_agent(
            "hello", tools=[], mcp_tools=[], workflow_enabled=True,
            workflow_durable_client=durable_client,
        ))


def test_stream_and_leaf_roles_never_fall_back(preview, monkeypatch):
    monkeypatch.setattr(_maf_execution, "_build_agent_session", AsyncMock(side_effect=AssertionError))
    invoke = AsyncMock(side_effect=CopilotPreviewError("native fixture failure"))
    monkeypatch.setattr(_copilot, "run", invoke)

    async def collect():
        return [json.loads(event.removeprefix("data: ")) async for event in runner.run_agent_stream("hi")]

    assert [event["type"] for event in asyncio.run(collect())] == ["error"]
    resolved, capabilities = _sample()
    with pytest.raises(CopilotPreviewError, match="native fixture failure"):
        asyncio.run(runner.run_leaf_agent_task(
            resolved, capabilities, "hi", timeout=1, execution_role="workflow_subagent",
        ))
    assert invoke.await_count == 2


def test_registered_app_captures_selection_and_newness(preview, monkeypatch):
    requests = []
    validations = []
    validate_agent = _harness.validate_agent

    def validate_once(harness, resolved, capabilities):
        validations.append(resolved.slug)
        return validate_agent(harness, resolved, capabilities)

    async def invoke(harness, request):
        requests.append((harness, request))
        request.session_state.resumable = True
        return runner.AgentResult(request.session_id, "reply")

    monkeypatch.setattr(_harness, "validate_agent", validate_once)
    monkeypatch.setattr(sys.modules[create_function_app.__module__], "validate_agent", validate_once)
    monkeypatch.setattr(_copilot, "run", invoke)
    app = create_function_app(SAMPLE)
    assert validations == ["main"]
    functions = {function.get_function_name(): function.get_user_function() for function in app.get_functions()}
    chat = functions["agent_main_builtin_chat"]
    monkeypatch.setenv(_harness.FLAG, "false")
    _harness.get_harness(preview / "other")
    paths.set_app_root(preview / "other")

    async def call():
        first = await chat(SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "first"})))
        public_id = first.headers["x-ms-session-id"]
        second = await chat(SimpleNamespace(
            headers={"X-Ms-SeSsIoN-Id": f" {public_id} "},
            json=AsyncMock(return_value={"prompt": "second"}),
        ))
        assert second.headers["x-ms-session-id"] == public_id
        assert requests[0][1].new_session is True
        assert requests[1][1].new_session is False
        assert requests[0][0] is requests[1][0]
        assert requests[0][0].app_root == SAMPLE.resolve()
        assert requests[0][0].name == "copilot"
        for name in ("agent_main_builtin_history",):
            response = await functions[name](SimpleNamespace(
                headers={}, json=AsyncMock(return_value={"prompt": "must not run"}),
            ))
            assert response.status_code == 501
        response = await functions["agent_main_builtin_chatstream"](SimpleNamespace(
            headers={}, json=AsyncMock(return_value={"prompt": "stream"}),
        ))
        assert response.status_code == 200
        events = [json.loads(chunk.removeprefix("data: ")) async for chunk in response.body_iterator]
        assert events[-1]["type"] == "done"
        assert requests[-1][1].new_session is True
        assert len(requests) == 3
        assert validations == ["main"]

    asyncio.run(call())


def test_copilot_timer_trigger_and_debug_ui_register_through_shared_runner(preview, monkeypatch):
    app_root = preview / "app"
    app_root.mkdir()
    (app_root / "main.agent.md").write_text(
        "---\n"
        "name: Timer\n"
        "description: Synthetic timer agent.\n"
        "builtin_endpoints:\n  chat_api: true\n  debug_chat_ui: true\n  mcp: false\n"
        "trigger:\n  type: timer_trigger\n  args:\n    schedule: '0 */5 * * * *'\n"
        "mcp: false\nskills: false\ntools: false\n"
        "---\nSummarize the trigger.\n",
        encoding="utf-8",
    )
    requests = []

    async def invoke(harness, request):
        requests.append(request)
        return runner.AgentResult(request.session_id, "reply")

    monkeypatch.setattr(_copilot, "run", invoke)
    app = create_function_app(app_root)
    functions = {f.get_function_name(): f.get_user_function() for f in app.get_functions()}

    async def call():
        await functions["main"](
            TimerRequest(past_due=True, schedule_status={}, schedule={})
        )
        await functions["main"](
            TimerRequest(past_due=False, schedule_status={}, schedule={})
        )
        history = await functions["agent_main_builtin_history"](
            SimpleNamespace(headers={"x-ms-session-id": "caller-session"})
        )
        assert history.status_code == 501
        assert "error" in json.loads(history.body)

    asyncio.run(call())
    assert [request.new_session for request in requests] == [True, True]
    assert requests[0].session_id != requests[1].session_id
    assert '"past_due": true' in requests[0].prompt

def test_registered_copilot_mcp_preserves_prompt_sessions_and_errors(preview, monkeypatch):
    app_root = preview / "mcp-app"
    app_root.mkdir()
    (app_root / "main.agent.md").write_text(
        (SAMPLE / "main.agent.md").read_text(encoding="utf-8").replace(
            "  mcp: false", "  mcp: true"
        ),
        encoding="utf-8",
    )
    requests = []
    fail_resume = False

    async def invoke(harness, request):
        assert harness.name is HarnessKind.COPILOT
        requests.append(request)
        if fail_resume:
            raise CopilotPreviewError("Native resume failed.")
        request.session_state.resumable = True
        return runner.AgentResult(request.session_id, "reply")

    monkeypatch.setattr(_copilot, "run", invoke)
    app = create_function_app(app_root)
    function = next(
        f for f in app.get_functions() if f.get_function_name() == "agent_main_builtin_mcp"
    )
    binding = next(b.get_dict_repr() for b in function.get_bindings()
                   if b.get_dict_repr()["type"] == "mcpToolTrigger")
    assert binding["name"] == "context"
    assert [prop["propertyName"] for prop in json.loads(binding["toolProperties"])] == ["prompt"]
    handler = function.get_user_function()
    # Later flag changes must not switch an already-registered handler to MAF.
    monkeypatch.setenv(_harness.FLAG, "false")

    async def call():
        nonlocal fail_resume
        for arguments in ({}, {"prompt": " "}, {"prompt": 42}):
            response = json.loads(await handler(json.dumps({"arguments": arguments})))
            assert response == {"error": "Missing 'prompt'", "session_id": None}
        assert requests == []
        malformed = json.loads(await handler("{"))
        assert "error" in malformed
        assert requests == []

        first = json.loads(await handler(json.dumps({"arguments": {"prompt": " first "}})))
        assert first["response"] == "reply"
        assert first["session_id"] == requests[0].session_id
        assert requests[0].prompt == "first"
        assert requests[0].new_session is True
        # The extension supplies transport identity, not the returned agent ID.
        # The mocked runner represents an existing native session for that identity.
        transport_context = json.dumps({
            "arguments": {"prompt": "continue"},
            "sessionid": "transport/session",
        })
        mapped = json.loads(await handler(transport_context))
        assert mapped["session_id"].startswith("mcp-")
        assert mapped["session_id"] != first["session_id"]
        assert requests[-1].new_session is False
        repeated = json.loads(await handler(transport_context))
        assert repeated["session_id"] == mapped["session_id"]
        assert requests[-1].session_id == mapped["session_id"]
        assert requests[-1].new_session is False

        fail_resume = True
        calls_before_failure = len(requests)
        failed = json.loads(await handler(transport_context))
        assert failed == {"error": "Native resume failed.", "session_id": mapped["session_id"]}
        assert len(requests) == calls_before_failure + 1
        assert requests[-1].new_session is False
        assert requests[-1].session_id == mapped["session_id"]
        generated_failure = json.loads(await handler(json.dumps({
            "arguments": {"prompt": "new turn"},
        })))
        assert generated_failure == {"error": "Native resume failed.", "session_id": None}
        assert requests[-1].new_session is True

    asyncio.run(call())


def test_registered_stream_invalid_session_header_emits_sse_error(preview, monkeypatch):
    invoke = AsyncMock(side_effect=AssertionError("native stream must not start"))
    monkeypatch.setattr(_copilot, "run", invoke)
    app = create_function_app(SAMPLE)
    stream = next(
        function.get_user_function() for function in app.get_functions()
        if function.get_function_name() == "agent_main_builtin_chatstream"
    )

    async def call():
        response = await stream(
            SimpleNamespace(
                headers={"x-ms-session-id": "bad id!"},
                json=AsyncMock(return_value={"prompt": "stream"}),
            )
        )
        assert response.status_code == 200
        return [
            json.loads(chunk.removeprefix("data: "))
            async for chunk in response.body_iterator
        ]

    events = asyncio.run(call())

    assert [event["type"] for event in events] == ["error"]
    assert "Invalid session_id" in events[0]["content"]
    invoke.assert_not_awaited()


def test_registered_agent_model_override_reaches_copilot_provider(preview, monkeypatch, tmp_path):
    root = tmp_path / "agent-model"
    shutil.copytree(SAMPLE, root)
    agent = root / "main.agent.md"
    agent.write_text(
        agent.read_text(encoding="utf-8").replace(
            "description: A minimal local native-session and custom-tool example.\n",
            "description: A minimal local native-session and custom-tool example.\nmodel: per-agent-model\n",
            1,
        ),
        encoding="utf-8",
    )
    requests = []

    async def invoke(_harness, request):
        requests.append(request)
        request.session_state.resumable = True
        return runner.AgentResult(request.session_id, "reply")

    monkeypatch.setattr(_copilot, "run", invoke)
    app = create_function_app(root)
    chat = next(
        function.get_user_function() for function in app.get_functions()
        if function.get_function_name() == "agent_main_builtin_chat"
    )
    response = asyncio.run(
        chat(SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "hello"})))
    )
    assert response.status_code == 200
    assert requests[0].model == "per-agent-model"


def test_copilot_sandbox_tool_uses_public_http_session_id(preview, monkeypatch):
    from azure_functions_agents.registration import _handlers

    resolved, capabilities = _sample()
    resolved = resolved.model_copy(
        update={
            "sandbox_config": DynamicSessionsCodeInterpreterConfig(
                endpoint="https://fixture.dynamicsessions.io"
            ),
        }
    )
    capabilities = replace(capabilities, _harness=_harness.get_harness())
    requests = []

    def build_sandbox(_resolved, session_id):
        return [tool(name="execute_python")(lambda code: f"{session_id}:{code}")]

    async def invoke(_selected, request):
        requests.append(request)
        request.session_state.resumable = True
        return runner.AgentResult(request.session_id, "reply")

    monkeypatch.setattr(_handlers, "build_sandbox_tools_for_session", build_sandbox)
    monkeypatch.setattr(_copilot, "run", invoke)
    handler = make_http_agent_handler(resolved, capabilities)
    response = asyncio.run(
        handler(SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "calculate"})))
    )
    public_id = response.headers["x-ms-session-id"]
    assert requests[0].session_id == public_id
    assert [function.name for function in requests[0].tools] == [
        "make_receipt",
        "execute_python",
        "web_request",
    ]
    result = asyncio.run(requests[0].tools[1].invoke(arguments={"code": "6 * 7"}))
    assert result == f"{public_id}:6 * 7"
    assert all(type(descriptor) is ToolDescriptor for descriptor in requests[0].tools)


def test_off_import_and_index_do_not_import_sdk_or_launch_process(tmp_path):
    script = """
import platform, sys, subprocess
from pathlib import Path
platform.platform()
platform.processor()
class NoProcess(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        raise AssertionError('Unexpected child process')
subprocess.Popen = NoProcess
import azure_functions_agents as runtime
runtime.create_function_app(Path(sys.argv[1]))
assert not any(name == 'copilot' or name.startswith('copilot.') for name in sys.modules)
assert 'azure_functions_agents.harness.copilot_sdk._copilot_execution' not in sys.modules
assert 'azure_functions_agents.harness.copilot_sdk._copilot_session_fs' not in sys.modules
"""
    env = dict(os.environ, AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT="false")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(SAMPLE)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_owner_construction_is_thread_safe_and_resource_free(runtime_preview):
    with ThreadPoolExecutor(max_workers=8) as pool:
        owners = list(pool.map(lambda _: runtime.get_runtime(runtime_preview), range(32)))
    assert all(owner is owners[0] for owner in owners)
    assert copilot_state(runtime_preview).runtime is owners[0]
    assert owners[0]._client is owners[0]._credential is None
    assert not owners[0].workspace.exists()
    assert not lifecycle._SHUTDOWN_CALLBACKS


def test_replace_and_same_root_bindings_get_distinct_resource_cells(runtime_preview):
    copied = replace(runtime_preview)
    assert copied.app_root == runtime_preview.app_root
    assert copied._resources is not runtime_preview._resources
    assert copied._resources.guard is not runtime_preview._resources.guard
    assert runtime.get_runtime(runtime_preview) is not runtime.get_runtime(copied)
    assert "_resources" not in repr(runtime_preview)


def test_unselected_binding_cannot_acquire_an_owner(tmp_path):
    binding = AppHarness(HarnessKind.MAF, tmp_path)
    with pytest.raises(CopilotPreviewError, match="not selected"):
        runtime.get_runtime(binding)
    assert binding._resources.runner is None


@pytest.fixture
def sdk_timing_logger(monkeypatch):
    sdk_logger = logging.Logger("copilot.client", logging.WARNING)
    sdk_logger.parent = logging.getLogger()
    monkeypatch.setattr(
        runtime, "logging", SimpleNamespace(DEBUG=logging.DEBUG, getLogger=lambda _: sdk_logger)
    )
    monkeypatch.setattr(runtime, "_sdk_timing_users", 0)
    monkeypatch.setattr(runtime, "_sdk_timing_filter", None)
    monkeypatch.setattr(runtime, "_sdk_timing_handlers", [])
    yield sdk_logger
    assert runtime._sdk_timing_users == 0


@pytest.mark.parametrize(("message", "phase", "kind"), [
    ("CopilotClient._start_cli_server subprocess spawned", "subprocess_spawn", "phase"),
    ("CopilotClient._verify_protocol_version protocol handshake complete", "protocol_handshake", "phase"),
    ("CopilotClient._connect_to_server transport setup complete", "transport_connect", "phase"),
    ("CopilotClient.start transport setup complete", "start_transport", "cumulative"),
    ("CopilotClient.start protocol verification complete", "start_protocol", "cumulative"),
    ("CopilotClient.start session filesystem setup complete", "session_filesystem", "phase"),
    ("CopilotClient.start complete", "start_total", "cumulative"),
    ("CopilotClient.create_session local setup complete", "create_local", "overlapping"),
    ("CopilotClient.create_session session creation request completed successfully", "create_rpc", "overlapping"),
    ("CopilotClient.create_session complete", "create_total", "overlapping"),
    ("CopilotClient.resume_session local setup complete", "resume_local", "overlapping"),
    ("CopilotClient.resume_session session resume request completed successfully", "resume_rpc", "overlapping"),
    ("CopilotClient.resume_session complete", "resume_total", "overlapping"),
    ("CopilotClient.stop runtime shutdown complete", "shutdown", "phase"),
])
def test_sdk_timing_exact_allowlist_is_content_free(
    sdk_timing_logger, caplog, message, phase, kind
):
    caplog.set_level(logging.INFO, logger=runtime.logger.name)
    runtime._acquire_sdk_timings()
    try:
        # Same shape as copilot._diagnostics.log_timing, including untrusted extras.
        sdk_timing_logger.debug(
            message,
            extra={"elapsed_ms": 12.5, "session_id": "private", "url": "private",
                   "tools_count": 3, "headers": "private", "prompt": "private"},
            exc_info=(ValueError, ValueError("private"), None),
            stack_info=True,
        )
    finally:
        runtime._release_sdk_timings()
    records = [r for r in caplog.records if r.msg.startswith("Copilot SDK timing:")]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.INFO
    assert (record.phase, record.timing_kind, record.elapsed_ms) == (phase, kind, 12.5)
    assert record.process_id == os.getpid()
    assert record.exc_info is None and record.stack_info is None
    assert not {"session_id", "url", "headers", "prompt", "tools_count",
                "runtime_id", "operation_id"} & record.__dict__.keys()
    assert "private" not in caplog.text


@pytest.mark.parametrize("elapsed", [
    None, "12", True, -1, float("nan"), float("inf"), -float("inf"), 10 ** 400, {},
])
def test_sdk_timing_ignores_invalid_elapsed(sdk_timing_logger, caplog, elapsed):
    caplog.set_level(logging.INFO, logger=runtime.logger.name)
    runtime._acquire_sdk_timings()
    try:
        sdk_timing_logger.debug("CopilotClient.start complete", extra={"elapsed_ms": elapsed})
    finally:
        runtime._release_sdk_timings()
    assert not caplog.records


@pytest.mark.parametrize("level", [logging.NOTSET, logging.DEBUG, logging.INFO, logging.ERROR])
def test_sdk_timing_isolates_debug_and_restores_logging(sdk_timing_logger, caplog, level):
    sdk_timing_logger.setLevel(level)
    original_filter = logging.Filter()
    sdk_timing_logger.addFilter(original_filter)
    local_handler = Mock(spec=logging.Handler, level=logging.DEBUG)
    sdk_timing_logger.addHandler(local_handler)
    old_handlers = list(sdk_timing_logger.handlers)
    old_propagate, old_disabled = sdk_timing_logger.propagate, sdk_timing_logger.disabled
    caplog.set_level(logging.DEBUG)
    runtime._acquire_sdk_timings()
    runtime._acquire_sdk_timings()
    try:
        sdk_timing_logger.debug("private arbitrary diagnostic")
        sdk_timing_logger.debug("CopilotClient.start complete changed", extra={"elapsed_ms": 1})
        sdk_timing_logger.debug("CopilotClient.start complete %s", "private", extra={"elapsed_ms": 1})
        sdk_timing_logger.debug({"private": "message"}, extra={"elapsed_ms": 1})
        runtime._release_sdk_timings()
        sdk_timing_logger.debug("CopilotClient.start complete", extra={"elapsed_ms": 0})
        forwarded = [r for r in caplog.records if r.msg.startswith("Copilot SDK timing:")]
        assert len(forwarded) == 1
        assert forwarded[0].elapsed_ms == 0
        assert all(r.name != "copilot.client" for r in caplog.records)
        local_handler.handle.assert_not_called()
    finally:
        runtime._release_sdk_timings()
    assert sdk_timing_logger.level == level
    assert sdk_timing_logger.filters == [original_filter]
    assert sdk_timing_logger.handlers == old_handlers
    assert (sdk_timing_logger.propagate, sdk_timing_logger.disabled) == (
        old_propagate, old_disabled
    )


def test_sdk_timing_preserves_non_debug_threshold_and_later_settings(sdk_timing_logger, caplog):
    caplog.set_level(logging.DEBUG)
    runtime._acquire_sdk_timings()
    try:
        sdk_timing_logger.info("previously disabled")
        sdk_timing_logger.warning("existing warning")
        sdk_timing_logger.setLevel(logging.ERROR)
    finally:
        runtime._release_sdk_timings()
    assert [r.getMessage() for r in caplog.records] == ["existing warning"]
    assert sdk_timing_logger.level == logging.ERROR


def test_sdk_timing_guards_ancestor_handlers_without_filtering_application_logs(
    sdk_timing_logger, caplog
):
    caplog.set_level(logging.DEBUG)
    child = logging.Logger("copilot.client.child")
    child.parent = sdk_timing_logger
    application = logging.getLogger("sdk-timing-test.application")
    handlers = [*sdk_timing_logger.handlers, *logging.getLogger().handlers]
    original_filters = [(handler, list(handler.filters)) for handler in handlers]
    runtime._acquire_sdk_timings()
    try:
        child.debug("private inherited diagnostic")
        child.info("private newly enabled info")
        application.debug("unrelated application debug")
        child.warning("existing child warning")
    finally:
        runtime._release_sdk_timings()
    assert [r.getMessage() for r in caplog.records] == [
        "unrelated application debug", "existing child warning",
    ]
    assert all(handler.filters == filters for handler, filters in original_filters)


def test_sdk_timing_host_logging_failure_is_nonfatal(sdk_timing_logger, monkeypatch):
    runtime._acquire_sdk_timings()
    monkeypatch.setattr(runtime.logger, "info", Mock(side_effect=RuntimeError("private")))
    try:
        sdk_timing_logger.debug("CopilotClient.start complete", extra={"elapsed_ms": 1})
    finally:
        runtime._release_sdk_timings()


@pytest.mark.asyncio
async def test_sdk_timing_multi_owner_lifecycle(
    runtime_preview, sdk_timing_logger, monkeypatch, caplog
):
    import copilot

    caplog.set_level(logging.INFO, logger=runtime.logger.name)
    owners = [
        runtime.get_runtime(runtime_preview),
        runtime.get_runtime(replace(runtime_preview)),
    ]

    def factory(**_kwargs):
        sdk_timing_logger.debug("CopilotClient.start complete", extra={"elapsed_ms": 1})
        return _fake_client()

    monkeypatch.setattr(copilot, "CopilotClient", factory)
    try:
        await asyncio.gather(*(owner.client() for owner in owners for _ in range(3)))
        assert runtime._sdk_timing_users == 2
        assert len(sdk_timing_logger.filters) == 1
        await owners[0].close()
        await owners[0].close()
        assert runtime._sdk_timing_users == 1
        await owners[1].client()
        assert len([r for r in caplog.records if r.msg.startswith("Copilot SDK timing:")]) == 2
    finally:
        for owner in owners:
            await owner.close()
    assert sdk_timing_logger.filters == []
    assert sdk_timing_logger.level == logging.WARNING
    await owners[0].client()
    await owners[0].close()
    assert sdk_timing_logger.filters == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["construct", "start"])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_sdk_timing_releases_after_startup_failure(
    runtime_preview, sdk_timing_logger, monkeypatch, stage, failure
):
    owner = runtime.get_runtime(runtime_preview)
    client = _fake_client()
    error = failure("private")
    if stage == "construct":
        monkeypatch.setattr(owner, "_build_client", Mock(side_effect=error))
    else:
        monkeypatch.setattr(owner, "_build_client", Mock(return_value=client))
        client.start.side_effect = error
    try:
        with pytest.raises(failure) as raised:
            await owner.client()
        assert raised.value is error or failure is asyncio.CancelledError
    finally:
        await owner.close()
    assert runtime._sdk_timing_users == 0
    assert sdk_timing_logger.filters == []
    assert sdk_timing_logger.level == logging.WARNING
    assert copilot_state(runtime_preview).runtime is None


@pytest.mark.asyncio
async def test_concurrent_startup_shares_one_client(runtime_preview, monkeypatch, caplog):
    import copilot

    started, release = asyncio.Event(), asyncio.Event()
    client = _fake_client()

    async def start():
        started.set()
        await release.wait()

    client.start.side_effect = start
    factory = Mock(return_value=client)
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    caplog.set_level(logging.INFO, logger=runtime.logger.name)
    owner = runtime.get_runtime(runtime_preview)
    requests = [asyncio.create_task(owner.client()) for _ in range(12)]
    try:
        await started.wait()
        release.set()
        assert all(result is client for result in await asyncio.gather(*requests))
        factory.assert_called_once()
        client.start.assert_awaited_once()
        assert await owner.client() is client
        selections = [
            record.selection for record in caplog.records
            if record.msg.startswith("Copilot startup client selection:")
        ]
        assert selections.count("new") == 1
        assert selections.count("join") == 11
        assert selections.count("reuse") == 1
        finished = [
            record for record in caplog.records
            if record.msg.startswith("Copilot startup stage finished:")
        ]
        assert sum(record.stage == "client_construct" for record in finished) == 1
        assert sum(record.stage == "client_start" for record in finished) == 1
        assert sum(record.stage == "client_acquire" for record in finished) == 13
        assert all(record.outcome == "success" for record in finished)
        assert {record.runtime_id for record in finished} == {owner._timing_id}
        assert {record.process_id for record in finished} == {os.getpid()}
    finally:
        release.set()
        await owner.close()
    assert copilot_state(runtime_preview).runtime is None


@pytest.mark.asyncio
async def test_blocking_constructor_runs_off_the_event_loop(runtime_preview, monkeypatch):
    import copilot

    client = _fake_client()
    entered = threading.Event()
    release = threading.Event()
    ticked = asyncio.Event()

    def blocking_factory(**_kwargs):
        entered.set()
        release.wait(timeout=5)
        return client

    async def tick():
        await asyncio.sleep(0)
        ticked.set()

    monkeypatch.setattr(copilot, "CopilotClient", Mock(side_effect=blocking_factory))
    owner = runtime.get_runtime(runtime_preview)
    request = asyncio.create_task(owner.client())
    ticker = asyncio.create_task(tick())
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=5)
        await asyncio.wait_for(ticked.wait(), timeout=1)
        release.set()
        assert await request is client
        client.start.assert_awaited_once()
    finally:
        release.set()
        await ticker
        await owner.close()


@pytest.mark.asyncio
async def test_constructor_cancellation_reuses_one_late_client(
    runtime_preview, monkeypatch, caplog
):
    import copilot

    client = _fake_client()
    entered = threading.Event()
    release = threading.Event()
    factory = Mock()

    def blocking_factory(**_kwargs):
        entered.set()
        release.wait(timeout=5)
        return client

    factory.side_effect = blocking_factory
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    caplog.set_level(logging.INFO, logger=runtime.logger.name)
    owner = runtime.get_runtime(runtime_preview)
    first = asyncio.create_task(owner.client())
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert owner._client is None
        assert owner._client_task is not None
        release.set()
        assert await owner.client() is client
        factory.assert_called_once()
        client.start.assert_awaited_once()
        outcomes = [
            (record.stage, record.outcome) for record in caplog.records
            if record.msg.startswith("Copilot startup stage finished:")
        ]
        assert ("client_acquire", "cancelled") in outcomes
        assert ("client_construct", "success") in outcomes
        assert ("client_start", "success") in outcomes
    finally:
        release.set()
        await owner.close()


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
def test_startup_timing_is_monotonic_content_free_and_preserves_errors(
    runtime_preview, monkeypatch, caplog, failure
):
    owner = runtime.get_runtime(runtime_preview)
    clock = iter([100.0, 100.125])
    monkeypatch.setattr(runtime, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(runtime, "current_operation_id", lambda: "a" * 32)
    caplog.set_level(logging.INFO, logger=runtime.logger.name)
    secret = "sensitive-prompt-token-and-path"
    error = failure(secret) if failure is not None else None

    def measure():
        with owner._timing(runtime._StartupStage.CONSTRUCT):
            if error is not None:
                raise error

    if error is None:
        measure()
    else:
        with pytest.raises(failure) as raised:
            measure()
        assert raised.value is error
    records = [
        record for record in caplog.records
        if record.msg.startswith("Copilot startup stage")
    ]
    assert len(records) == 2
    assert records[1].elapsed_ms == 125
    expected = "success" if error is None else (
        "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
    )
    assert records[1].outcome == expected
    assert all(record.operation_id == "a" * 32 for record in records)
    assert all(record.exc_info is None for record in records)
    assert secret not in caplog.text
    assert str(owner.native_root) not in caplog.text


@pytest.mark.asyncio
async def test_startup_logs_constructor_failure_without_details(
    runtime_preview, monkeypatch, caplog
):
    owner = runtime.get_runtime(runtime_preview)
    caplog.set_level(logging.INFO, logger=runtime.logger.name)
    error = RuntimeError("sensitive-constructor-detail")
    monkeypatch.setattr(owner, "_build_client", Mock(side_effect=error))
    try:
        with pytest.raises(RuntimeError) as raised:
            await owner.client()
        assert raised.value is error
        outcomes = [
            (record.stage, record.outcome) for record in caplog.records
            if record.msg.startswith("Copilot startup stage finished:")
        ]
        assert ("client_construct", "error") in outcomes
        assert ("client_acquire", "error") in outcomes
        assert not any(stage == "client_start" for stage, _ in outcomes)
        assert str(error) not in caplog.text
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_lock_wait_timing_excludes_construction_and_records_cancellation(
    runtime_preview, monkeypatch, caplog
):
    owner = runtime.get_runtime(runtime_preview)
    now = [0.0]
    monkeypatch.setattr(runtime, "perf_counter", lambda: now[0])
    caplog.set_level(logging.INFO, logger=runtime.logger.name)
    owner._client = _fake_client()
    await owner._start_lock.acquire()
    request = asyncio.create_task(owner.client())
    try:
        await asyncio.sleep(0)
        now[0] = 0.250
        owner._start_lock.release()
        assert await request is owner._client
        lock_records = [
            record for record in caplog.records
            if record.msg.startswith("Copilot startup stage finished:")
            and record.stage == "owner_lock_wait"
        ]
        assert len(lock_records) == 1
        assert lock_records[0].elapsed_ms == 250
        assert lock_records[0].outcome == "success"
        await owner._start_lock.acquire()
        cancelled = asyncio.create_task(owner.client())
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert owner._start_lock.locked()
        assert any(
            record.stage == "owner_lock_wait" and record.outcome == "cancelled"
            for record in caplog.records
            if record.msg.startswith("Copilot startup stage finished:")
        )
    finally:
        if owner._start_lock.locked():
            owner._start_lock.release()
        await owner.close()


@pytest.mark.asyncio
async def test_shutdown_waits_for_late_constructor_cleanup_without_duplicate_owner(
    runtime_preview, monkeypatch
):
    import copilot

    client = _fake_client()
    entered = threading.Event()
    release = threading.Event()

    def blocking_factory(**_kwargs):
        entered.set()
        release.wait(timeout=5)
        return client

    monkeypatch.setattr(copilot, "CopilotClient", Mock(side_effect=blocking_factory))
    owner = runtime.get_runtime(runtime_preview)
    startup = asyncio.create_task(owner.client())
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=5)
        close_task = asyncio.create_task(owner.close())
        await asyncio.sleep(0)
        assert runtime.get_runtime(runtime_preview) is owner
        release.set()
        await close_task
        with pytest.raises(asyncio.CancelledError):
            await startup
        client.start.assert_not_awaited()
        client.stop.assert_awaited_once()
        assert copilot_state(runtime_preview).runtime is None
        assert owner._close_callback not in lifecycle._SHUTDOWN_CALLBACKS
    finally:
        release.set()


@pytest.mark.asyncio
async def test_failed_start_cleanup_drops_the_failed_handle(
    runtime_preview, monkeypatch, caplog
):
    import copilot

    client = _fake_client()
    client.start.side_effect = RuntimeError("startup failed")
    client.stop.side_effect = RuntimeError("stop failed")
    client.force_stop.side_effect = RuntimeError("force failed")
    factory = Mock(return_value=client)
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    caplog.set_level(logging.INFO, logger=runtime.logger.name)
    owner = runtime.get_runtime(runtime_preview)
    with pytest.raises(RuntimeError, match="startup failed"):
        await owner.client()
    assert owner._client is None
    assert owner._close_callback in lifecycle._SHUTDOWN_CALLBACKS
    await owner.close()
    assert copilot_state(runtime_preview).runtime is None
    assert not lifecycle._SHUTDOWN_CALLBACKS
    factory.assert_called_once()
    outcomes = [
        (record.stage, record.outcome) for record in caplog.records
        if record.msg.startswith("Copilot startup stage finished:")
    ]
    assert ("client_construct", "success") in outcomes
    assert ("client_start", "error") in outcomes
    assert ("client_acquire", "error") in outcomes
    assert "startup failed" not in caplog.text


@pytest.mark.asyncio
async def test_owner_shutdown_clears_failed_credential_and_visits_every_owner(runtime_preview, monkeypatch):
    import copilot

    failed_credential = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close failed")))
    good_credential = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(
        runtime, "build_async_credential", Mock(side_effect=[failed_credential, good_credential])
    )
    first = runtime.get_runtime(runtime_preview)
    second_binding = replace(runtime_preview)
    second = runtime.get_runtime(second_binding)
    first.credential()
    second.credential()
    client = _fake_client()
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    await second.client()
    with pytest.raises(CopilotPreviewError):
        await lifecycle._shutdown_harnesses()
    failed_credential.close.assert_awaited_once()
    good_credential.close.assert_awaited_once()
    client.stop.assert_awaited_once()
    assert first._credential is None
    assert copilot_state(runtime_preview).runtime is None
    assert first._close_callback not in lifecycle._SHUTDOWN_CALLBACKS
    assert copilot_state(second_binding).runtime is None
    assert not lifecycle._SHUTDOWN_CALLBACKS


@pytest.mark.asyncio
async def test_acquired_owner_registers_a_stable_bound_close_callback(runtime_preview, monkeypatch):
    import copilot

    client = _fake_client()
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    owner = runtime.get_runtime(runtime_preview)
    await owner.client()
    replacement = AsyncMock(side_effect=AssertionError("Mutable owner callback rediscovery"))
    monkeypatch.setattr(owner, "close", replacement)
    await lifecycle._shutdown_harnesses()
    replacement.assert_not_awaited()
    client.stop.assert_awaited_once()
    assert copilot_state(runtime_preview).runtime is None


@pytest.mark.asyncio
async def test_startup_has_a_bound_and_cleanup_retries(runtime_preview, monkeypatch):
    import copilot

    client = _fake_client()

    async def stalled_start():
        await asyncio.sleep(60)

    client.start.side_effect = stalled_start
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    monkeypatch.setattr(runtime, "_START_TIMEOUT_SECONDS", 0.01)
    owner = runtime.get_runtime(runtime_preview)
    with pytest.raises(TimeoutError):
        await owner.client()
    client.stop.assert_awaited_once()
    assert owner._client is None
    client.start.side_effect = None
    assert await owner.client() is client
    await owner.close()


@pytest.mark.asyncio
async def test_failed_start_uses_sdk_stop_and_force_stop_then_retries(runtime_preview, monkeypatch):
    import copilot
    from copilot import RuntimeConnection

    failed = _fake_client()
    failed.start.side_effect = RuntimeError("native failed")
    failed.stop.side_effect = TimeoutError("stop stalled")
    recovered = _fake_client()
    clients = iter([failed, recovered])
    factory = Mock(side_effect=lambda **_: next(clients))
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    owner = runtime.get_runtime(runtime_preview)
    try:
        with pytest.raises(RuntimeError):
            await owner.client()
        failed.stop.assert_awaited_once()
        failed.force_stop.assert_awaited_once()
        assert await owner.client() is recovered
        assert factory.call_count == 2
        assert factory.call_args.kwargs["mode"] == "empty"
        assert "env" not in factory.call_args.kwargs
        connection = factory.call_args.kwargs.get("connection")
        assert connection is None or (
            isinstance(connection, RuntimeConnection) and connection.path is None
        )
        assert "request_handler" not in factory.call_args.kwargs
    finally:
        await lifecycle._shutdown_harnesses()
    recovered.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_closes_every_app_owner_even_if_one_fails(runtime_preview, monkeypatch):
    import copilot

    second_binding = make_copilot_harness(
        runtime_preview.app_root,
        storage_root=copilot_state(runtime_preview).storage_root / "other",
    )
    first = runtime.get_runtime(runtime_preview)
    second = runtime.get_runtime(second_binding)
    assert runtime.get_runtime(runtime_preview) is first
    failed_client, good_client = _fake_client(), _fake_client()
    failed_client.stop.side_effect = RuntimeError("stop failed")
    failed_client.force_stop.side_effect = RuntimeError("force failed")
    monkeypatch.setattr(copilot, "CopilotClient", Mock(side_effect=[failed_client, good_client]))
    await first.client()
    await second.client()
    with pytest.raises(CopilotPreviewError):
        await lifecycle._shutdown_harnesses()
    failed_client.stop.assert_awaited_once()
    failed_client.force_stop.assert_awaited_once()
    good_client.stop.assert_awaited_once()
    assert first._client is None
    assert copilot_state(second_binding).runtime is None
    assert copilot_state(runtime_preview).runtime is None
    assert not lifecycle._SHUTDOWN_CALLBACKS


@pytest.mark.asyncio
async def test_distinct_same_root_app_contexts_never_share_a_native_client(runtime_preview, monkeypatch):
    import copilot

    second = replace(runtime_preview)
    first_client, second_client = _fake_client(), _fake_client()
    factory = Mock(side_effect=[first_client, second_client])
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    first_owner = runtime.get_runtime(runtime_preview)
    second_owner = runtime.get_runtime(second)
    try:
        assert first_owner is not second_owner
        assert await first_owner.client() is first_client
        assert await second_owner.client() is second_client
        assert await first_owner.client() is first_client
        assert factory.call_count == 2
        first_client.start.assert_awaited_once()
        second_client.start.assert_awaited_once()
    finally:
        await lifecycle._shutdown_harnesses()
