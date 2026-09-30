from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from azure_functions_agents import _harness, runner
from azure_functions_agents._function_tool import FunctionTool
from azure_functions_agents._harness import (
    AppHarness,
    CopilotPreviewError,
    HarnessKind,
    UnsupportedCapabilityError,
)
from azure_functions_agents.app import create_function_app
from azure_functions_agents.config import paths
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
from azure_functions_agents.discovery.tools import discover_user_tools
from azure_functions_agents.registration._handlers import make_http_agent_handler
from azure_functions_agents.registration.capabilities import build_capabilities

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "copilot-preview" / "src"


@pytest.fixture
def preview(monkeypatch, tmp_path):
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    monkeypatch.setattr(paths, "_app_root", tmp_path)
    monkeypatch.setenv(_harness.FLAG, "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "gpt-4.1-mini")
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
        ("AZURE_FUNCTIONS_AGENTS_PROVIDER", "azure_openai", "supports.*PROVIDER"),
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
    assert [item.name for item in capabilities.web_request_tools] == ["web_request"]
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


def test_foundry_configuration_is_frozen_without_authentication(preview, monkeypatch):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://fixture.services.ai.azure.com/api/projects/test")
    monkeypatch.setenv("FOUNDRY_MODEL", "deployed-model")
    selected = _harness.get_harness()
    assert selected.provider == "foundry"
    assert selected.default_model == "deployed-model"
    assert "fixture.services.ai.azure.com" not in repr(selected)
    assert not selected.storage_root.exists()


@pytest.mark.parametrize("field", ["filtered_mcp_tools", "enabled_skill_paths"])
def test_enabled_unsupported_capabilities_fail(preview, field):
    resolved, capabilities = _sample()
    capabilities = replace(capabilities, **{field: [object()]})
    with pytest.raises(UnsupportedCapabilityError):
        _harness.validate_agent(_harness.get_harness(), resolved, capabilities)


def test_direct_preview_keeps_other_roles_and_surfaces_rejected(preview):
    resolved, capabilities = _sample()
    cases = [
        (
            "non_http_trigger",
            resolved.model_copy(update={"trigger": TriggerSpec(type="queue_trigger")}),
        ),
        (
            "debug_chat_ui",
            resolved.model_copy(
                update={
                    "builtin_endpoints": BuiltinEndpointsConfig(
                        debug_chat_ui=True,
                        chat_api=True,
                        mcp=False,
                    )
                }
            ),
        ),
        (
            "mcp_endpoint",
            resolved.model_copy(
                update={
                    "builtin_endpoints": BuiltinEndpointsConfig(
                        debug_chat_ui=False,
                        chat_api=True,
                        mcp=True,
                    )
                }
            ),
        ),
        (
            "subagents",
            resolved.model_copy(update={"subagents": [SubagentRef(agent="specialist")]}),
        ),
        (
            "workflows",
            resolved.model_copy(update={"workflows": WorkflowConfig(enabled=True)}),
        ),
    ]

    for diagnostic, candidate in cases:
        with pytest.raises(UnsupportedCapabilityError, match=diagnostic):
            _harness.validate_agent(_harness.get_harness(), candidate, capabilities)


def test_direct_preview_accepts_explicit_host_system_tools(preview):
    resolved, capabilities = _sample()
    web_request = FunctionTool(name="web_request", func=lambda url: url)
    capabilities = replace(capabilities, web_request_tools=[web_request])
    resolved = resolved.model_copy(
        update={
            "sandbox_config": DynamicSessionsCodeInterpreterConfig(
                endpoint="https://fixture.dynamicsessions.io"
            ),
        }
    )

    _harness.validate_agent(_harness.get_harness(), resolved, capabilities)


@pytest.mark.parametrize(
    ("configuration", "expected_names"),
    [
        ("tools: true\n", ["web_request"]),
        ("tools: true\nsystem_tools:\n  web_request: false\n", []),
        ("tools: false\n", []),
    ],
)
def test_copilot_web_request_default_and_optouts(
    preview,
    tmp_path,
    configuration,
    expected_names,
):
    root = tmp_path / "web-request-policy"
    root.mkdir()
    (root / "main.agent.md").write_text(
        "---\n"
        "name: Tool policy\n"
        "description: Exercise Copilot host-tool policy.\n"
        "builtin_endpoints:\n"
        "  chat_api: true\n"
        "  debug_chat_ui: false\n"
        "  mcp: false\n"
        "mcp: false\n"
        "skills: false\n"
        "workflows:\n"
        "  enabled: false\n"
        f"{configuration}"
        "---\n"
        "Use only configured tools.\n",
        encoding="utf-8",
    )
    resolved = compose(
        load_agent_specs(root)[0],
        load_global_config(root),
        discovered_mcp_names=[],
        discovered_skill_names=[],
    )
    capabilities = build_capabilities(
        resolved,
        discovered_user_tools=[],
        discovered_mcp_tools={},
        discovered_skills={},
    )

    _harness.validate_agent(_harness.get_harness(), resolved, capabilities)

    assert [function.name for function in capabilities.web_request_tools or []] == expected_names


def test_sandbox_name_collision_fails_during_registration(preview):
    resolved, capabilities = _sample()
    resolved = resolved.model_copy(
        update={
            "sandbox_config": DynamicSessionsCodeInterpreterConfig(
                endpoint="https://fixture.dynamicsessions.io"
            ),
        }
    )
    capabilities = replace(
        capabilities,
        filtered_user_tools=[FunctionTool(name="execute_python", func=lambda code: code)],
    )

    with pytest.raises(UnsupportedCapabilityError, match="unique custom tool names"):
        _harness.validate_agent(_harness.get_harness(), resolved, capabilities)


def test_maf_keeps_tool_objects_that_copilot_cannot_adapt(tmp_path):
    resolved, capabilities = _sample()
    maf_only = FunctionTool(name="bounded", func=lambda: "ok", max_invocations=1)
    capabilities = replace(capabilities, filtered_user_tools=[maf_only])

    _harness.validate_agent(AppHarness(HarnessKind.MAF, tmp_path), resolved, capabilities)

    assert capabilities.filtered_user_tools == [maf_only]


def test_maf_only_configuration_is_not_silently_discarded(preview):
    with pytest.raises(UnsupportedCapabilityError, match="agent_framework"):
        _harness.validate_configuration(
            AgentConfiguration(agent_framework=AgentFrameworkConfiguration(
                compaction=AgentFrameworkCompactionConfig(max_context_window_tokens=100)
            ))
        )
    _harness.validate_configuration(AgentConfiguration(agent_framework=AgentFrameworkConfiguration()))
    _harness.validate_configuration(AgentConfiguration(agent_framework=None))


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
    with pytest.raises(UnsupportedCapabilityError, match="skills"):
        asyncio.run(
            runner.run_agent(
                "hello",
                tools=[],
                mcp_tools=[],
                skill_paths=[preview / "unsupported-skill"],
            )
        )
    invoke.assert_not_called()


def test_direct_preview_composes_host_tools_in_maf_order(preview, monkeypatch):
    from azure_functions_agents import _copilot

    user = FunctionTool(name="user_tool", func=lambda: "user")
    sandbox = FunctionTool(name="execute_python", func=lambda code: code)
    web = FunctionTool(name="web_request", func=lambda url: url)
    invoke = AsyncMock(return_value=runner.AgentResult("public-id", "native reply"))
    monkeypatch.setattr(_copilot, "run", invoke)

    result = asyncio.run(
        runner.run_agent(
            "hello",
            tools=[user],
            mcp_tools=[],
            sandbox_tools=[sandbox],
            web_request_tools=[web],
        )
    )

    request = invoke.call_args.args[1]
    assert result.session_id == "public-id"
    assert [function.name for function in request.tools] == [
        "user_tool",
        "execute_python",
        "web_request",
    ]


def test_combined_tool_collision_fails_before_native_startup(preview, monkeypatch):
    from azure_functions_agents import _copilot

    invoke = AsyncMock()
    monkeypatch.setattr(_copilot, "run", invoke)
    duplicate = FunctionTool(name="web_request", func=lambda: "duplicate")

    with pytest.raises(UnsupportedCapabilityError, match="unique custom tool names"):
        asyncio.run(
            runner.run_agent(
                "hello",
                tools=[duplicate],
                mcp_tools=[],
                web_request_tools=[duplicate],
            )
        )

    invoke.assert_not_called()


def test_workflow_management_tools_qualify_for_adapter_but_workflows_stay_rejected(
    preview,
):
    from azure_functions_agents.workflows.tools import build_workflow_tools

    durable_client = AsyncMock()
    workflow_tools = build_workflow_tools(
        session_id="public-session",
        workflow_agent_slug="main",
        agent_name="main",
        durable_client=durable_client,
    )

    assert [function.name for function in _harness.prepare_tools(workflow_tools)] == [
        "start_workflow",
        "get_workflow_status",
        "list_workflows",
        "cancel_workflow",
        "terminate_workflow",
    ]
    with pytest.raises(UnsupportedCapabilityError, match="workflows"):
        asyncio.run(
            runner.run_agent(
                "hello",
                tools=[],
                mcp_tools=[],
                workflow_enabled=True,
                workflow_durable_client=durable_client,
            )
        )


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


def test_copilot_sandbox_tool_uses_public_http_session_id(preview, monkeypatch):
    from azure_functions_agents import _copilot
    from azure_functions_agents.registration import _handlers

    resolved, capabilities = _sample()
    resolved = resolved.model_copy(
        update={
            "sandbox_config": DynamicSessionsCodeInterpreterConfig(
                endpoint="https://fixture.dynamicsessions.io"
            ),
        }
    )
    capabilities._harness = _harness.get_harness()
    requests = []

    def build_sandbox(_resolved, session_id):
        return [
            FunctionTool(
                name="execute_python",
                func=lambda code: f"{session_id}:{code}",
            )
        ]

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
    contents = asyncio.run(requests[0].tools[1].invoke(arguments={"code": "6 * 7"}))
    assert contents[0].text == f"{public_id}:6 * 7"


def test_off_import_and_index_do_not_import_sdk_or_launch_process(tmp_path):
    script = """
import platform, sys, subprocess
from pathlib import Path
platform.platform()
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
