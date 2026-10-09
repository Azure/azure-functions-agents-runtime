from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from azure.functions.timer import TimerRequest

from azure_functions_agents import runner
from azure_functions_agents._function_tool import tool
from azure_functions_agents._tool_descriptor import ToolDescriptor
from azure_functions_agents.app import create_function_app
from azure_functions_agents.client_manager import (
    ClientManager,
    MAFClientManager,
    get_client_manager,
    set_client_manager,
)
from azure_functions_agents.config import paths
from azure_functions_agents.config.env import EnvVar
from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.config.merge import compose
from azure_functions_agents.config.schema import (
    AgentConfiguration,
    AgentFrameworkCompactionConfig,
    AgentFrameworkConfiguration,
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
from azure_functions_agents.harness._harness_binding import (
    AppHarness,
    HarnessKind,
    UnsupportedCapabilityError,
)
from azure_functions_agents.harness.agent_framework import _maf_execution
from azure_functions_agents.harness.agent_framework._maf_tools import build_maf_tools
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_execution as _copilot,
)
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_preview as _preview,
)
from azure_functions_agents.harness.copilot_sdk._copilot_preview import CopilotPreviewError
from azure_functions_agents.registration._handlers import make_http_agent_handler
from azure_functions_agents.registration.capabilities import build_capabilities

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "copilot-preview" / "src"


class _ReplacedMAFClientManager(MAFClientManager):
    pass


@pytest.fixture
def replace_client_manager():
    original = get_client_manager()
    try:
        yield set_client_manager
    finally:
        set_client_manager(original)


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
    assert first.session_storage.mode is StorageMode.BLOB
    assert first.session_storage.blob.connection_string == "fixture-connection"
    assert first.session_storage.blob.container_name == "first-container"
    assert second.session_storage.blob.blob_service_url == "https://fixture.invalid"
    assert second.session_storage.blob.client_id == "storage-identity"
    assert second.session_storage.blob.container_name == "second-container"
    assert _harness.get_harness(preview, new_app=True).session_storage is None
    assert not first.storage_root.exists()
    assert not second.storage_root.exists()


def test_missing_sdk_is_explicit_without_forcing_a_second_version_check(preview, monkeypatch):
    def missing(_name):
        raise _preview.PackageNotFoundError

    monkeypatch.setattr(_preview, "version", missing)
    with pytest.raises(CopilotPreviewError, match=r"\[copilot\]"):
        _harness.get_harness()
    monkeypatch.setattr(_preview, "version", lambda _: "0.0.0")
    assert _harness.get_harness().name is HarnessKind.COPILOT


@pytest.mark.parametrize(
    ("name", "value", "diagnostic"),
    [
        ("WEBSITE_INSTANCE_ID", "cloud-instance", "Azure hosting is not qualified"),
        ("FUNCTIONS_WORKER_PROCESS_COUNT", "2", "FUNCTIONS_WORKER_PROCESS_COUNT=1"),
        ("FUNCTIONS_WORKER_PROCESS_COUNT", " ", "FUNCTIONS_WORKER_PROCESS_COUNT=1"),
        ("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", "high", "REASONING_EFFORT"),
    ],
)
def test_unsupported_app_settings(preview, monkeypatch, name, value, diagnostic):
    monkeypatch.setenv(name, value)
    with pytest.raises((UnsupportedCapabilityError, ValueError), match=diagnostic):
        _harness.get_harness()


def test_single_worker_hosting_is_accepted(preview, monkeypatch):
    monkeypatch.setenv("FUNCTIONS_WORKER_PROCESS_COUNT", " 1 ")
    assert _harness.get_harness().name is HarnessKind.COPILOT


@pytest.mark.parametrize(
    ("name", "value", "allowed", "diagnostic"),
    [
        (EnvVar.ENABLE_COPILOT, None, True, ""),
        (EnvVar.ENABLE_COPILOT, "", False, "must be true"),
        (EnvVar.ENABLE_COPILOT, "   ", False, "must be true"),
        (EnvVar.ENABLE_COPILOT, "true", True, ""),
        (EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT, None, True, ""),
        (EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT, "", False, "one|=1"),
        (EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT, "   ", False, "one|=1"),
        (EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT, "1", True, ""),
        (EnvVar.WEBSITE_INSTANCE_ID, None, True, ""),
        (EnvVar.WEBSITE_INSTANCE_ID, "", True, ""),
        (EnvVar.WEBSITE_INSTANCE_ID, "   ", False, "local execution"),
        (EnvVar.WEBSITE_INSTANCE_ID, "cloud-instance", False, "local execution"),
        (EnvVar.REASONING_EFFORT, None, True, ""),
        (EnvVar.REASONING_EFFORT, "", True, ""),
        (EnvVar.REASONING_EFFORT, "   ", False, "REASONING_EFFORT"),
        (EnvVar.REASONING_EFFORT, "high", False, "REASONING_EFFORT"),
        (EnvVar.REASONING_SUMMARY, None, True, ""),
        (EnvVar.REASONING_SUMMARY, "", True, ""),
        (EnvVar.REASONING_SUMMARY, "   ", False, "REASONING_SUMMARY"),
        (EnvVar.REASONING_SUMMARY, "detailed", False, "REASONING_SUMMARY"),
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
    assert not _harness.get_harness(root).storage_root.exists()


def test_custom_manager_is_rejected_before_function_app_construction(
    preview, monkeypatch, replace_client_manager,
):
    import azure_functions_agents.app as app_module

    class CustomManager(ClientManager):
        def resolve_model(self, requested):
            return requested or "custom"

        def build_chat_client(self, model):
            raise AssertionError("Custom MAF client must not be constructed")

    replace_client_manager(CustomManager())
    app_constructor = Mock(side_effect=AssertionError("FunctionApp must not be constructed"))
    monkeypatch.setattr(app_module.func, "FunctionApp", app_constructor)
    with pytest.raises(UnsupportedCapabilityError, match=r"ClientManager.*MAF-only"):
        create_function_app(SAMPLE)
    app_constructor.assert_not_called()


@pytest.mark.parametrize("manager", [MAFClientManager(), _ReplacedMAFClientManager()])
def test_replaced_or_subclassed_builtin_manager_is_rejected(
    preview, manager, replace_client_manager,
):
    replace_client_manager(manager)
    with pytest.raises(UnsupportedCapabilityError, match=r"MAFClientManager.*MAF-only"):
        _harness.get_harness(preview, new_app=True)


def test_flag_off_does_not_reject_custom_manager(tmp_path, monkeypatch, replace_client_manager):
    class CustomManager(ClientManager):
        def resolve_model(self, requested):
            return requested or "custom"

        def build_chat_client(self, model):
            return object()

    custom = CustomManager()
    replace_client_manager(custom)
    monkeypatch.setenv(_harness.FLAG, "false")
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    assert _harness.get_harness(tmp_path).name is HarnessKind.MAF
    assert get_client_manager() is custom


def test_foundry_configuration_is_frozen_without_authentication(preview, monkeypatch):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://fixture.services.ai.azure.com/api/projects/test")
    monkeypatch.setenv("FOUNDRY_MODEL", "deployed-model")
    selected = _harness.get_harness()
    assert selected.provider is not None
    assert selected.provider.kind == "foundry"
    assert selected.default_model == "deployed-model"
    assert "fixture.services.ai.azure.com" not in repr(selected)
    assert not selected.storage_root.exists()


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
        filtered_user_tools=[tool(name="execute_python")(lambda code: code)],
    )
    with pytest.raises(UnsupportedCapabilityError, match="unique custom tool names"):
        _harness.validate_agent(_harness.get_harness(), resolved, capabilities)


def test_maf_keeps_authored_approval_that_copilot_cannot_adapt(tmp_path):
    resolved, capabilities = _sample()
    descriptor = tool(name="bounded", approval_mode="always_require")(lambda: "ok")
    capabilities = replace(capabilities, filtered_user_tools=(descriptor,))

    _harness.validate_agent(AppHarness(HarnessKind.MAF, tmp_path), resolved, capabilities)

    assert capabilities.filtered_user_tools == (descriptor,)
    assert build_maf_tools(capabilities.filtered_user_tools)[0].approval_mode == "always_require"


def test_maf_only_configuration_is_not_silently_discarded(preview):
    with pytest.raises(UnsupportedCapabilityError, match="agent_framework"):
        _preview.validate_configuration(AgentConfiguration(
            agent_framework=AgentFrameworkConfiguration(
                compaction=AgentFrameworkCompactionConfig(max_context_window_tokens=100)
            )
        ))
    _preview.validate_configuration(AgentConfiguration(agent_framework=AgentFrameworkConfiguration()))
    _preview.validate_configuration(AgentConfiguration(agent_framework=None))


def test_unsupported_output_limit_fails_before_native_execution(preview):
    with pytest.raises(UnsupportedCapabilityError, match="max_output_tokens"):
        _preview.validate_configuration(AgentConfiguration(max_output_tokens=256))


def test_standalone_output_limit_fails_before_native_execution(preview, monkeypatch):
    invoke = AsyncMock()
    monkeypatch.setattr(_copilot, "run", invoke)
    with pytest.raises(UnsupportedCapabilityError, match="max_output_tokens"):
        asyncio.run(runner.run_agent(
            "no inference", tools=[], mcp_tools=[],
            agent_configuration=AgentConfiguration(max_output_tokens=256),
        ))
    invoke.assert_not_called()


@pytest.mark.parametrize("policy", [{"approval_mode": "always_require"}])
def test_unsupported_tool_policies_are_not_silently_lost(policy):
    descriptor = tool(name="bounded", **policy)(lambda value: value)
    with pytest.raises(UnsupportedCapabilityError, match="approval"):
        _preview.prepare_tools([descriptor])


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
    assert [function.name for function in _preview.prepare_tools(workflow_tools)] == [
        "start_workflow", "get_workflow_status", "list_workflows", "cancel_workflow", "terminate_workflow",
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
            assert response == {"error": "Missing 'prompt'"}
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
        assert failed == {"error": "Native resume failed."}
        assert len(requests) == calls_before_failure + 1
        assert requests[-1].new_session is False
        assert requests[-1].session_id == mapped["session_id"]

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
