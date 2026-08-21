"""Execution-level FHA V0 capability and composition-boundary coverage."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest
from agent_framework import (
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    MCPStreamableHTTPTool,
    Message,
)

from azure_functions_agents import app as app_module
from azure_functions_agents._function_tool import tool
from azure_functions_agents.app import create_function_app
from azure_functions_agents.client_manager import (
    ClientManager,
    InferenceTarget,
    get_client_manager,
    set_client_manager,
)
from azure_functions_agents.config.paths import get_app_root, set_app_root
from azure_functions_agents.discovery.mcp import clear_mcp_cache
from azure_functions_agents.execution.foundry_responses_binding import (
    FHA_BINDING_ENV_NAMES,
)
from azure_functions_agents.foundry_responses.fha_model_catalog_gate import (
    FhaModelCatalogError,
    compile_fha_v0_project,
)
from azure_functions_agents.foundry_responses.fha_private_history import (
    FhaHistoryFactory,
    FhaResponsesRequestEnvelope,
)
from azure_functions_agents.foundry_responses.fha_resilient_responses_entrypoint import (
    execute_fha_v0_stage,
)
from azure_functions_agents.foundry_responses.fha_runtime_projection import (
    FhaRuntimeProjection,
    load_fha_runtime_projection,
)
from azure_functions_agents.registration.catalog import AgentCatalog

_PROJECT_ENDPOINT = "https://project.services.ai.azure.com/api/projects/fha-capability"
_MODEL = "fha-capability-model"
_MCP_URL = "https://mcp.example.test/fha-capability"
_MANAGED_IDENTITY_CLIENT_ID = "11111111-2222-3333-4444-555555555555"


def _bootstrap_module() -> ModuleType:
    path = Path(__file__).parents[1] / "eng" / "scripts" / "bootstrap_foundry_responses_fha.py"
    spec = importlib.util.spec_from_file_location(
        "bootstrap_foundry_responses_fha_capability_parity",
        path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bootstrap_arguments(module: ModuleType, application_root: Path, stage_root: Path) -> Any:
    return module.BootstrapArguments(
        application_root=application_root,
        stage_root=stage_root,
        subscription_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        function_app_name="fha-capability-app",
        function_app_slot=None,
        resource_group="fha-capability-rg",
        setup_principal_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        project_endpoint=_PROJECT_ENDPOINT,
        project_resource_id=(
            "/subscriptions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            "/resourceGroups/fha-capability-rg/providers/Microsoft.CognitiveServices/"
            "accounts/fha-capability/projects/fha-capability"
        ),
        model_deployment_name=_MODEL,
        runtime_pin="azurefunctions-agents-runtime==0.1.0",
        agentserver_core_pin="azure-ai-agentserver-core==2.1.0b1",
        agentserver_responses_pin="azure-ai-agentserver-responses==2.1.0b1",
    )


def _write_agent(root: Path, filename: str, frontmatter: str, body: str) -> None:
    (root / filename).write_text(
        f"---\n{frontmatter.strip()}\n---\n{body.strip()}\n",
        encoding="utf-8",
    )


def _write_capable_app(root: Path) -> None:
    root.mkdir()
    (root / "agents.config.yaml").write_text(
        "system_tools:\n  web_request: false\n",
        encoding="utf-8",
    )
    _write_agent(
        root,
        "coordinator.agent.md",
        """
name: Capability Coordinator
description: Coordinates only FHA V0-supported local capabilities.
trigger:
  type: http_trigger
  args:
    route: fha-capability
subagents:
  - agent: specialist
""",
        "Use the available local capabilities and delegate only when needed.",
    )
    _write_agent(
        root,
        "specialist.agent.md",
        """
name: Endpointless Specialist
description: Handles the delegated local task.
tools: false
mcp: false
skills: false
system_tools:
  web_request: false
""",
        "Return a concise delegated result.",
    )
    tools_directory = root / "tools"
    tools_directory.mkdir()
    (tools_directory / "local_lookup.py").write_text(
        """
from azure_functions_agents import tool

invocations: list[str] = []


@tool
def local_lookup(query: str) -> str:
    \"\"\"Return a deterministic local result.\"\"\"
    invocations.append(query)
    return f"local:{query}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    skill_directory = root / "skills" / "brief-reader"
    skill_directory.mkdir(parents=True)
    (skill_directory / "SKILL.md").write_text(
        """
---
name: brief-reader
description: Reads the local FHA capability reference.
---
Use the local reference when answering the coordinator.
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (skill_directory / "reference.md").write_text(
        "skill-resource-visible",
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "remote": {
                        "type": "streamable-http",
                        "url": _MCP_URL,
                        "tools": ["mcp_lookup"],
                        "auth": {
                            "scope": "https://mcp.example.test/.default",
                            "client_id": _MANAGED_IDENTITY_CLIENT_ID,
                        },
                        "headers": {"Accept": "application/json"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _enabled_builtin_endpoints(resolved: Any) -> tuple[str, ...]:
    return tuple(
        name
        for name in ("debug_chat_ui", "chat_api", "mcp")
        if getattr(resolved.builtin_endpoints, name)
    )


def _tool_names(tools: Sequence[Any] | None) -> tuple[str, ...]:
    return tuple(sorted(str(tool.name) for tool in tools or []))


def _catalog_snapshot(catalog: AgentCatalog) -> dict[str, dict[str, object]]:
    return {
        slug: {
            "model": entry.resolved.model,
            "trigger": entry.resolved.trigger.type if entry.resolved.trigger else None,
            "builtin_endpoints": _enabled_builtin_endpoints(entry.resolved),
            "user_tools": _tool_names(entry.capabilities.filtered_user_tools),
            "skills": tuple(sorted(path.name for path in entry.capabilities.enabled_skill_paths)),
            "mcp": _tool_names(entry.capabilities.filtered_mcp_tools),
            "subagents": tuple(ref.agent for ref in entry.resolved.subagents),
        }
        for slug, entry in catalog.items()
    }


def _projection_snapshot(projection: FhaRuntimeProjection) -> dict[str, dict[str, object]]:
    return {
        entry.slug: {
            "model": entry.model,
            "trigger": entry.trigger,
            "builtin_endpoints": entry.builtin_endpoints,
            "user_tools": entry.capabilities.user_tools,
            "skills": entry.capabilities.skills,
            "mcp": entry.capabilities.mcp,
            "subagents": entry.capabilities.subagents,
        }
        for entry in projection.catalog
    }


def _assert_system_surfaces_absent(catalog: AgentCatalog) -> None:
    for entry in catalog.values():
        assert entry.capabilities.web_request_tools == []
        assert entry.capabilities.filtered_workflow_tools == []
        assert entry.resolved.sandbox_config is None
        assert entry.resolved.web_request_config is None
        assert entry.resolved.workflows is None


@pytest.fixture
def _restore_runtime_globals() -> Iterator[None]:
    original_client_manager = get_client_manager()
    original_app_root = get_app_root()
    yield
    set_client_manager(original_client_manager)
    set_app_root(original_app_root)
    clear_mcp_cache()


def test_fha_v0_capabilities_remain_canonical_across_bootstrap_function_and_hosted_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _restore_runtime_globals: None,
) -> None:
    application_root = tmp_path / "application"
    _write_capable_app(application_root)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://ambient.example.test/project")
    monkeypatch.setenv("FOUNDRY_MODEL", "ambient-model-must-not-win")
    monkeypatch.setenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "ambient-deployment-must-not-win")

    standalone = compile_fha_v0_project(
        application_root,
        project_endpoint=_PROJECT_ENDPOINT,
        default_model=_MODEL,
    )
    assert not (application_root / "tools" / "__pycache__").exists()
    bootstrap = _bootstrap_module()
    plan = bootstrap.build_bootstrap_plan(
        _bootstrap_arguments(bootstrap, application_root, tmp_path / "staged")
    )

    assert plan.projection == standalone.projection
    assert plan.artifact.projection_path.read_text(encoding="utf-8") == standalone.projection.serialize()
    assert load_fha_runtime_projection(plan.artifact.projection_path) == standalone.projection
    assert _catalog_snapshot(standalone.catalog) == _projection_snapshot(standalone.projection)
    _assert_system_surfaces_absent(standalone.catalog)
    [remote_mcp] = plan.projection.mcp_servers
    assert remote_mcp.name == "remote"
    assert remote_mcp.url == _MCP_URL
    assert remote_mcp.allowed_tools == ("mcp_lookup",)
    assert remote_mcp.auth_scope == "https://mcp.example.test/.default"
    assert remote_mcp.managed_identity_client_id == _MANAGED_IDENTITY_CLIENT_ID
    assert remote_mcp.headers == (("Accept", "application/json"),)
    assert "secret" not in plan.projection.serialize().casefold()
    assert "authorization" not in plan.projection.serialize().casefold()

    binding_settings = bootstrap._binding_settings(
        plan,
        agent_name=plan.managed_agent_name,
        agent_version="1",
    )
    assert set(binding_settings) == set(FHA_BINDING_ENV_NAMES)
    assert len(binding_settings) == 8
    for name, value in binding_settings.items():
        monkeypatch.setenv(name, value)

    captured_function_catalog: dict[str, AgentCatalog] = {}
    register_agent = app_module.register_agent

    def capture_function_catalog(*args: Any, **kwargs: Any) -> Any:
        catalog = kwargs["catalog"]
        assert isinstance(catalog, Mapping)
        captured_function_catalog["catalog"] = catalog
        return register_agent(*args, **kwargs)

    monkeypatch.setattr(app_module, "resolve_function_app_identity", lambda: plan.app_identity)
    monkeypatch.setattr(app_module, "register_agent", capture_function_catalog)

    function_app = create_function_app(application_root)

    assert function_app is not None
    function_catalog = captured_function_catalog["catalog"]
    assert _catalog_snapshot(function_catalog) == _projection_snapshot(plan.projection)
    _assert_system_surfaces_absent(function_catalog)
    assert all(entry.resolved.model == _MODEL for entry in function_catalog.values())

    captured_hosted_catalog: dict[str, AgentCatalog] = {}

    class CapturedHost:
        def response_handler(self, handler: Any) -> Any:
            self.handler = handler
            return handler

    def capture_hosted_catalog(catalog: AgentCatalog) -> CapturedHost:
        captured_hosted_catalog["catalog"] = catalog
        return CapturedHost()

    from azure_functions_agents.foundry_responses import (
        fha_resilient_responses_entrypoint as entrypoint_module,
    )

    monkeypatch.setattr(
        entrypoint_module,
        "create_fha_resilient_responses_host",
        capture_hosted_catalog,
    )
    entrypoint_path = plan.artifact.entrypoint_path
    namespace = {
        "__file__": str(entrypoint_path),
        "__name__": "fha_capability_hosted_entrypoint",
    }
    exec(compile(entrypoint_path.read_text(encoding="utf-8"), str(entrypoint_path), "exec"), namespace)

    hosted_catalog = captured_hosted_catalog["catalog"]
    assert _catalog_snapshot(hosted_catalog) == _projection_snapshot(plan.projection)
    _assert_system_surfaces_absent(hosted_catalog)
    assert all(entry.resolved.model == _MODEL for entry in hosted_catalog.values())
    assert namespace["app"].__class__ is CapturedHost
    assert os.environ["FOUNDRY_PROJECT_ENDPOINT"] == _PROJECT_ENDPOINT
    assert os.environ["FOUNDRY_MODEL"] == _MODEL
    assert os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"] == _MODEL


class _PreconnectedRemoteMcp(MCPStreamableHTTPTool):
    """A local stand-in for the authored remote MCP server after connection."""

    def __init__(self, invocations: list[str]) -> None:
        super().__init__(
            name="remote",
            url=_MCP_URL,
            allowed_tools=["mcp_lookup"],
            load_tools=False,
            load_prompts=False,
        )
        self.is_connected = True

        @tool
        def mcp_lookup(query: str) -> str:
            """Return a deterministic MCP result."""
            invocations.append(query)
            return f"mcp:{query}"

        self._functions = [mcp_lookup]


class _ScriptedChatClient(FunctionInvocationLayer[Any], BaseChatClient[Any]):
    additional_properties: ClassVar[dict[str, Any]] = {}

    def __init__(self, calls: Sequence[tuple[str | None, dict[str, str] | str]]) -> None:
        super().__init__()
        self._calls = list(calls)
        self.call_count = 0
        self.function_result_text: list[str] = []

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        del stream, options, kwargs
        self.function_result_text.extend(
            str(content.result)
            for message in messages
            for content in message.contents
            if getattr(content, "type", "") == "function_result"
        )
        name, payload = self._calls[self.call_count]
        self.call_count += 1
        content = (
            Content.from_text(payload)
            if name is None
            else Content.from_function_call(
                call_id=f"call-{self.call_count}",
                name=name,
                arguments=payload,
            )
        )

        async def response() -> ChatResponse[Any]:
            return ChatResponse(
                messages=[Message("assistant", [content])],
                finish_reason="stop" if name is None else "tool_calls",
            )

        return response()


class _ScriptedClientManager(ClientManager):
    def __init__(self, clients: Sequence[_ScriptedChatClient]) -> None:
        self._clients = list(clients)
        self.models: list[str | None] = []

    def resolve_model(self, requested: str | None) -> str:
        return requested or "local-fake-model"

    def build_chat_client(self, model: str | None) -> _ScriptedChatClient:
        self.models.append(model)
        return self._clients.pop(0)

    def build_chat_client_with_target(
        self,
        model: str | None,
    ) -> tuple[_ScriptedChatClient, InferenceTarget]:
        return self.build_chat_client(model), InferenceTarget("local-fake", self.resolve_model(model))


@pytest.mark.asyncio
async def test_fha_v0_stage_executes_compiled_tools_skill_mcp_and_one_level_delegate(
    tmp_path: Path,
    _restore_runtime_globals: None,
) -> None:
    application_root = tmp_path / "application"
    _write_capable_app(application_root)
    bootstrap = _bootstrap_module()
    plan = bootstrap.build_bootstrap_plan(
        _bootstrap_arguments(bootstrap, application_root, tmp_path / "staged")
    )
    compilation = compile_fha_v0_project(
        plan.artifact.stage_root,
        project_endpoint=plan.projection.project_endpoint,
        default_model=plan.projection.default_model,
        expected_projection=plan.projection,
    )
    assert compilation.projection == plan.projection
    coordinator = compilation.catalog["coordinator"]
    local_tool = coordinator.capabilities.filtered_user_tools[0]
    mcp_invocations: list[str] = []
    coordinator.capabilities.filtered_mcp_tools = [_PreconnectedRemoteMcp(mcp_invocations)]

    coordinator_client = _ScriptedChatClient(
        [
            (
                "read_skill_resource",
                {"skill_name": "brief-reader", "resource_name": "reference.md"},
            ),
            ("local_lookup", {"query": "needle"}),
            ("mcp_lookup", {"query": "needle"}),
            ("delegate_specialist", {"task": "Return the delegated local result."}),
            (None, "hosted capability execution complete"),
        ]
    )
    specialist_client = _ScriptedChatClient([(None, "specialist delegated result")])
    client_manager = _ScriptedClientManager([coordinator_client, specialist_client])
    set_client_manager(client_manager)

    result = await execute_fha_v0_stage(
        FhaResponsesRequestEnvelope(
            agent_slug="coordinator",
            history_scope="o1-" + ("a" * 52),
            runtime_session_id="capability-session",
            runtime_run_id="a" * 32,
            prompt="Exercise the local FHA capabilities.",
        ),
        catalog=compilation.catalog,
        history_factory=FhaHistoryFactory(home_directory=tmp_path / "history"),
    )

    assert result == "hosted capability execution complete"
    assert local_tool.func.__globals__["invocations"] == ["needle"]
    assert mcp_invocations == ["needle"]
    assert coordinator_client.call_count == 5
    assert specialist_client.call_count == 1
    assert client_manager.models == [_MODEL, _MODEL]
    assert any("skill-resource-visible" in value for value in coordinator_client.function_result_text)
    assert any("specialist delegated result" in value for value in coordinator_client.function_result_text)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("system-tool", "web_request"),
        ("sandbox-system-tool", "Dynamic Sessions"),
        ("workflow", "workflows"),
        ("local-mcp", "MCP configuration"),
        ("nested-delegation", "nested delegation"),
    ],
)
def test_fha_v0_rejects_excluded_system_local_mcp_and_nested_surfaces(
    tmp_path: Path,
    kind: str,
    expected: str,
) -> None:
    application_root = tmp_path / "application"
    _write_capable_app(application_root)

    if kind == "system-tool":
        (application_root / "agents.config.yaml").write_text(
            "system_tools:\n  web_request: true\n",
            encoding="utf-8",
        )
    elif kind == "sandbox-system-tool":
        (application_root / "agents.config.yaml").write_text(
            """
system_tools:
  web_request: false
  dynamic_sessions_code_interpreter:
    endpoint: https://sessions.example.test
""".strip()
            + "\n",
            encoding="utf-8",
        )
    elif kind == "workflow":
        _write_agent(
            application_root,
            "coordinator.agent.md",
            """
name: Capability Coordinator
description: Coordinates only FHA V0-supported local capabilities.
trigger:
  type: http_trigger
  args:
    route: fha-capability
workflows:
  enabled: false
subagents:
  - agent: specialist
""",
            "Use the available local capabilities.",
        )
    elif kind == "local-mcp":
        (application_root / "mcp.json").write_text(
            json.dumps({"servers": {"local": {"type": "stdio", "command": "python"}}}),
            encoding="utf-8",
        )
    else:
        _write_agent(
            application_root,
            "specialist.agent.md",
            """
name: Endpointless Specialist
description: Attempts prohibited nested delegation.
tools: false
mcp: false
skills: false
system_tools:
  web_request: false
subagents:
  - agent: leaf
""",
            "Delegate further.",
        )
        _write_agent(
            application_root,
            "leaf.agent.md",
            """
name: Leaf
description: Nested specialist.
tools: false
mcp: false
skills: false
system_tools:
  web_request: false
""",
            "Handle the nested task.",
        )

    with pytest.raises(FhaModelCatalogError, match=expected):
        compile_fha_v0_project(
            application_root,
            project_endpoint=_PROJECT_ENDPOINT,
            default_model=_MODEL,
        )
