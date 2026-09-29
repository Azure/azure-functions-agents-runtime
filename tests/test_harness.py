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

from azure_functions_agents import _harness, runner
from azure_functions_agents._copilot_providers import (
    _PROVIDERS,
    AzureOpenAIProvider,
    FoundryProvider,
    OpenAIProvider,
)
from azure_functions_agents._function_tool import FunctionTool
from azure_functions_agents._harness import (
    CopilotPreviewError,
    HarnessKind,
    ProviderKind,
    UnsupportedCapabilityError,
)
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
from azure_functions_agents.config.schema import AgentConfiguration, AgentFrameworkConfiguration
from azure_functions_agents.discovery.tools import discover_user_tools
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
    return tmp_path


@pytest.mark.parametrize("value", [None, "false", "FALSE", "fAlSe", "0", " false "])
def test_flag_off(value):
    assert not _harness._flag_enabled(value)


@pytest.mark.parametrize("value", ["true", "TRUE", "tRuE", "1", " true "])
def test_flag_on(value):
    assert _harness._flag_enabled(value)


@pytest.mark.parametrize("value", ["", " ", "\t", "yes", "no", "on", "2", "secret-looking-input"])
def test_invalid_flag_is_explicit_and_does_not_echo_value(value):
    with pytest.raises(ValueError, match="must be true, false, 1, or 0") as error:
        _harness._flag_enabled(value)
    if value.strip() and value not in {"no", "on"}:
        assert value not in str(error.value)


def test_selection_is_once_per_app_and_shared_with_standalone(preview, monkeypatch):
    selected = _harness.get_harness()
    monkeypatch.setenv(_harness.FLAG, "false")
    assert _harness.get_harness(preview) is selected
    assert selected.name == "copilot"
    assert _harness.get_harness(preview / "another").name == "maf"
    assert not selected.storage_root.exists()


def test_separate_app_construction_captures_a_fresh_binding(preview, monkeypatch):
    standalone = _harness.get_harness(preview)
    old = _harness.get_harness(preview, new_app=True)
    monkeypatch.setenv(_harness.FLAG, "false")
    new = _harness.get_harness(preview, new_app=True)
    assert old.name == "copilot"
    assert new.name == "maf"
    assert old is not new
    assert _harness.get_harness(preview) is standalone
    assert standalone.name == "copilot"


def test_missing_sdk_is_explicit_without_forcing_a_second_version_check(preview, monkeypatch):
    def missing(_name):
        raise _harness.PackageNotFoundError

    monkeypatch.setattr(_harness, "version", missing)
    with pytest.raises(CopilotPreviewError, match=r"\[copilot\]"):
        _harness.get_harness()
    monkeypatch.setattr(_harness, "version", lambda _: "0.0.0")
    assert _harness.get_harness().name is HarnessKind.COPILOT


@pytest.mark.parametrize(
    ("name", "value", "diagnostic"),
    [
        ("FUNCTIONS_WORKER_PROCESS_COUNT", "2", "one|=1"),
        ("WEBSITE_INSTANCE_ID", "cloud-instance", "local execution"),
        ("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", "high", "REASONING_EFFORT"),
    ],
)
def test_unsupported_app_settings(preview, monkeypatch, name, value, diagnostic):
    monkeypatch.setenv(name, value)
    with pytest.raises(UnsupportedCapabilityError, match=diagnostic):
        _harness.get_harness()


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


def test_preview_sample_uses_normal_typed_discovery(preview):
    resolved, capabilities = _sample()
    _harness.validate_agent(_harness.get_harness(), resolved, capabilities)
    assert [item.name for item in capabilities.filtered_user_tools] == ["make_receipt"]
    assert resolved.enabled_mcp_names == resolved.enabled_skills_names == []
    assert capabilities.web_request_tools == []
    assert not resolved.builtin_endpoints.debug_chat_ui


def test_preview_config_scenario_indexes_without_native_process(preview):
    root = Path(__file__).parent / "fixtures" / "config_scenarios" / "18_copilot_preview"
    app = create_function_app(root)
    assert app.get_functions()
    assert not _harness.get_harness(root).storage_root.exists()


@pytest.mark.parametrize("endpoint", [
    "", "http://fixture.services.ai.azure.com/api/projects/test",
    "https://fixture.services.ai.azure.com/api/projects/test?api-key=do-not-log",
    "https://user:do-not-log@fixture.services.ai.azure.com/api/projects/test",
    "https://fixture.services.ai.azure.com:do-not-log/api/projects/test",
    "https://fixture.example/api/projects/test",
])
def test_invalid_foundry_endpoint_never_echoes_credentials(preview, monkeypatch, endpoint):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", endpoint)
    with pytest.raises(UnsupportedCapabilityError) as error:
        _harness.get_harness()
    assert "do-not-log" not in str(error.value)


@pytest.mark.parametrize(
    ("provider", "settings", "expected_model"),
    [
        (
            "openai",
            {"OPENAI_API_KEY": "sentinel-not-a-secret"},
            "runtime-model",
        ),
        (
            "azure_openai",
            {
                "AZURE_OPENAI_ENDPOINT": "https://fixture.openai.azure.com",
                "AZURE_OPENAI_DEPLOYMENT": "azure-deployment",
            },
            "azure-deployment",
        ),
        (
            "foundry",
            {
                "FOUNDRY_PROJECT_ENDPOINT": (
                    "https://fixture.services.ai.azure.com/api/projects/test"
                ),
                "FOUNDRY_MODEL": "foundry-deployment",
            },
            "foundry-deployment",
        ),
    ],
)
def test_provider_target_is_resolved_without_maf_client_construction(
    preview,
    monkeypatch,
    provider,
    settings,
    expected_model,
):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "runtime-model")
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    maf = Mock(side_effect=AssertionError("MAF client construction must not run"))
    monkeypatch.setattr(MAFClientManager, "build_chat_client_with_target", maf)

    selected = _harness.get_harness()

    assert selected.provider is not None
    assert selected.provider.kind == provider
    assert selected.default_model == expected_model
    maf.assert_not_called()


@pytest.mark.parametrize(
    ("provider", "settings", "diagnostic"),
    [
        ("openai", {}, "OPENAI_API_KEY"),
        (
            "azure_openai",
            {"AZURE_OPENAI_ENDPOINT": "https://fixture.openai.azure.com/path"},
            "host-only",
        ),
        (
            "azure_openai",
            {
                "AZURE_OPENAI_ENDPOINT": "https://fixture.openai.azure.com",
                "AZURE_OPENAI_API_VERSION": "2024-10-21?api-key=do-not-log",
            },
            "API_VERSION",
        ),
        ("unsupported", {}, "openai.*azure_openai.*foundry"),
    ],
)
def test_invalid_provider_settings_are_sanitized(
    preview,
    monkeypatch,
    provider,
    settings,
    diagnostic,
):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(UnsupportedCapabilityError, match=diagnostic) as error:
        _harness.get_harness()

    assert "do-not-log" not in str(error.value)


def test_copilot_provider_registry_freezes_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fixture.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-secret")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://fixture.services.ai.azure.com/api/projects/test",
    )

    openai = _PROVIDERS[ProviderKind.OPENAI].from_environment()
    azure = _PROVIDERS[ProviderKind.AZURE_OPENAI].from_environment()
    foundry = _PROVIDERS[ProviderKind.FOUNDRY].from_environment()

    assert isinstance(openai, OpenAIProvider)
    assert isinstance(azure, AzureOpenAIProvider)
    assert isinstance(foundry, FoundryProvider)
    assert azure.api_version == "2024-10-21"
    assert azure.auth_label == "AZURE_OPENAI_API_KEY"
    for provider in (openai, azure, foundry):
        rendered = repr(provider)
        assert "secret" not in rendered
        assert "fixture" not in rendered


def test_azure_provider_auth_mode_is_frozen(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fixture.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "frozen-secret")
    provider = AzureOpenAIProvider.from_environment()
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)

    assert provider.auth_label == "AZURE_OPENAI_API_KEY"
    assert provider.api_key == "frozen-secret"


def test_custom_manager_is_rejected_before_function_app_construction(
    preview,
    monkeypatch,
    replace_client_manager,
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

    with pytest.raises(UnsupportedCapabilityError, match=r"custom ClientManager.*MAF"):
        create_function_app(SAMPLE)

    app_constructor.assert_not_called()


@pytest.mark.parametrize("manager", [MAFClientManager(), _ReplacedMAFClientManager()])
def test_replaced_or_subclassed_builtin_manager_is_rejected(
    preview, manager, replace_client_manager
):
    replace_client_manager(manager)

    with pytest.raises(UnsupportedCapabilityError, match=r"custom ClientManager.*MAF"):
        _harness.get_harness(preview, new_app=True)


def test_flag_off_does_not_reject_custom_manager(
    tmp_path, monkeypatch, replace_client_manager
):
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


@pytest.mark.parametrize("field", ["web_request_tools", "filtered_mcp_tools", "enabled_skill_paths"])
def test_enabled_unsupported_capabilities_fail(preview, field):
    resolved, capabilities = _sample()
    capabilities = replace(capabilities, **{field: [object()]})
    with pytest.raises(UnsupportedCapabilityError):
        _harness.validate_agent(_harness.get_harness(), resolved, capabilities)


def test_maf_only_configuration_is_not_silently_discarded(preview):
    with pytest.raises(UnsupportedCapabilityError, match="agent_framework"):
        _harness.validate_configuration(
            AgentConfiguration(agent_framework=AgentFrameworkConfiguration())
        )


def test_unsupported_output_limit_fails_before_native_execution(preview):
    with pytest.raises(UnsupportedCapabilityError, match="max_output_tokens"):
        _harness.validate_configuration(AgentConfiguration(max_output_tokens=256))


def test_standalone_output_limit_fails_before_native_execution(preview, monkeypatch):
    from azure_functions_agents import _copilot

    invoke = AsyncMock()
    monkeypatch.setattr(_copilot, "run", invoke)

    with pytest.raises(UnsupportedCapabilityError, match="max_output_tokens"):
        asyncio.run(
            runner.run_agent(
                "no inference",
                tools=[],
                mcp_tools=[],
                agent_configuration=AgentConfiguration(max_output_tokens=256),
            )
        )

    invoke.assert_not_called()


@pytest.mark.parametrize("policy", [
    {"max_invocations": 1},
    {"max_invocation_exceptions": 1},
    {"approval_mode": "always_require"},
    {"result_parser": str},
    {"func": None},
])
def test_unsupported_tool_policies_are_not_silently_lost(policy):
    options = {"name": "bounded", "func": lambda value: value, **policy}
    with pytest.raises(UnsupportedCapabilityError, match="simple FunctionTool"):
        _harness.prepare_tools([FunctionTool(**options)])


def test_direct_preview_forks_before_maf_construction_or_blob(preview, monkeypatch):
    from azure_functions_agents import _copilot

    harness = _harness.get_harness()
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    maf = AsyncMock(side_effect=AssertionError("MAF construction must not run"))
    monkeypatch.setattr(runner, "_build_agent_session", maf)
    invoke = AsyncMock(return_value=runner.AgentResult("new-id", "native reply"))
    monkeypatch.setattr(_copilot, "run", invoke)
    result = asyncio.run(runner.run_agent("hello", tools=[], mcp_tools=[]))
    assert result.content == "native reply"
    assert invoke.call_args.args[0] is harness
    assert invoke.call_args.args[1].new_session
    assert not (preview / "state" / "agent-sessions").exists()
    maf.assert_not_called()


def test_standalone_unsupported_call_fails_before_backend(preview, monkeypatch):
    from azure_functions_agents import _copilot

    invoke = AsyncMock()
    monkeypatch.setattr(_copilot, "run", invoke)
    with pytest.raises(UnsupportedCapabilityError, match="web_request"):
        asyncio.run(runner.run_agent("hello", tools=[], mcp_tools=[], web_request_tools=[object()]))
    invoke.assert_not_called()


def test_stream_and_leaf_roles_never_fall_back(preview, monkeypatch):
    monkeypatch.setattr(runner, "_build_agent_session", AsyncMock(side_effect=AssertionError))

    async def collect():
        return [json.loads(event.removeprefix("data: ")) async for event in runner.run_agent_stream("hi")]

    assert [event["type"] for event in asyncio.run(collect())] == ["error"]
    resolved, capabilities = _sample()
    with pytest.raises(UnsupportedCapabilityError, match="workflow_subagent"):
        asyncio.run(runner.run_leaf_agent_task(
            resolved, capabilities, "hi", timeout=1, execution_role="workflow_subagent"
        ))


def test_registered_app_captures_selection_and_newness(preview, monkeypatch):
    from azure_functions_agents import _copilot

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
    monkeypatch.setattr(
        sys.modules[create_function_app.__module__], "validate_agent", validate_once
    )
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
        for name in ("agent_main_builtin_chatstream", "agent_main_builtin_history"):
            response = await functions[name](SimpleNamespace(
                headers={}, json=AsyncMock(return_value={"prompt": "must not run"})
            ))
            assert response.status_code == 501
        assert len(requests) == 2
        assert validations == ["main"]

    asyncio.run(call())


def test_registered_agent_model_override_reaches_copilot_provider(
    preview, monkeypatch, tmp_path
):
    from azure_functions_agents import _copilot

    root = tmp_path / "agent-model"
    shutil.copytree(SAMPLE, root)
    agent = root / "main.agent.md"
    agent.write_text(
        agent.read_text(encoding="utf-8").replace(
            "description: A minimal local native-session and custom-tool example.\n",
            "description: A minimal local native-session and custom-tool example.\n"
            "model: per-agent-model\n",
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
        function.get_user_function()
        for function in app.get_functions()
        if function.get_function_name() == "agent_main_builtin_chat"
    )
    response = asyncio.run(
        chat(SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "hello"})))
    )

    assert response.status_code == 200
    assert requests[0].model == "per-agent-model"


def test_off_import_and_index_do_not_import_sdk_or_launch_process(tmp_path):
    script = """
import sys, subprocess, platform
from pathlib import Path
platform.processor()
class NoProcess(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        raise AssertionError('Unexpected child process')
subprocess.Popen = NoProcess
import azure_functions_agents as runtime
runtime.create_function_app(Path(sys.argv[1]))
assert not any(name == 'copilot' or name.startswith('copilot.') for name in sys.modules)
assert 'azure_functions_agents._copilot' not in sys.modules
"""
    env = dict(os.environ, AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT="false")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(SAMPLE)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
