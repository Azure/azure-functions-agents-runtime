from __future__ import annotations

import asyncio
import errno
import shutil
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import get_args, get_type_hints
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from azure.core.credentials import AccessToken
from pydantic import BaseModel, Field

from azure_functions_agents._function_tool import tool, workflow_tool
from azure_functions_agents.app import create_function_app
from azure_functions_agents.client_manager import (
    ClientManager,
    get_client_manager,
    set_client_manager,
)
from azure_functions_agents.config import paths
from azure_functions_agents.harness import (
    _harness_binding as _harness,
)
from azure_functions_agents.harness import (
    _harness_execution as _execution,
)
from azure_functions_agents.harness import (
    _harness_lifecycle as _lifecycle,
)
from azure_functions_agents.harness._harness_binding import (
    AppHarness,
    HarnessKind,
    HarnessRequest,
    ProviderKind,
    UnsupportedCapabilityError,
)
from azure_functions_agents.harness.agent_framework import _maf_execution
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_execution as _copilot,
)
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_preview as _preview,
)
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_runtime as _runtime,
)
from azure_functions_agents.harness.copilot_sdk._copilot_preview import CopilotPreviewError
from azure_functions_agents.harness.copilot_sdk._copilot_providers import (
    AzureOpenAIProvider,
    FoundryProvider,
    OpenAIProvider,
)
from azure_functions_agents.harness.copilot_sdk._copilot_session_fs import open_session_fs
from azure_functions_agents.harness.copilot_sdk._copilot_session_identity import (
    CopilotSessionError,
    resolve_route,
)
from azure_functions_agents.harness.copilot_sdk._copilot_tool_calls import CopilotToolCalls

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "copilot-preview" / "src"


@pytest.fixture
def preview(monkeypatch, tmp_path):
    monkeypatch.setattr(_lifecycle, "_SHUTDOWN_CALLBACKS", set())
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri", raising=False)
    return AppHarness(
        HarnessKind.COPILOT,
        tmp_path,
        tmp_path / "state",
        "gpt-4.1-mini",
        OpenAIProvider("not-a-credential"),
        session_storage=resolve_route(tmp_path),
    )


@pytest.fixture
def replace_client_manager():
    original = get_client_manager()
    try:
        yield set_client_manager
    finally:
        set_client_manager(original)


def _request(*, new_session=True):
    return HarnessRequest(
        prompt="hello", instructions="Be helpful.", agent_slug="main", session_id="example",
        new_session=new_session, model="gpt-4.1-mini", tools=(), max_output_tokens=None,
        deadline=asyncio.get_running_loop().time() + 10,
    )


def _sdk_event(event_type, data):
    from copilot.session_events import SessionEvent

    return SessionEvent(data=data, id=uuid4(), timestamp=datetime.now(UTC), type=event_type)


def _fake_client():
    from copilot.session_events import (
        AssistantMessageData,
        AssistantTurnEndData,
        SessionEventType,
        UserMessageData,
    )

    session = AsyncMock()
    session.unsubscribe = Mock()
    session.handlers = []

    def on(handler):
        session.handlers.append(handler)

        def unsubscribe():
            session.handlers.remove(handler)

        session.unsubscribe.side_effect = unsubscribe
        return session.unsubscribe

    session.on = Mock(side_effect=on)
    session.rpc = SimpleNamespace(tools=SimpleNamespace(
        get_current_metadata=AsyncMock(return_value=SimpleNamespace(tools=[]))
    ))
    session.send_and_wait.return_value = _sdk_event(
        SessionEventType.ASSISTANT_MESSAGE,
        AssistantMessageData(content="synthetic reply", message_id="fixture"),
    )
    session.get_events.return_value = [
        _sdk_event(SessionEventType.USER_MESSAGE, UserMessageData(content="hello")),
        _sdk_event(SessionEventType.ASSISTANT_TURN_END, AssistantTurnEndData(turn_id="fixture")),
    ]
    return SimpleNamespace(
        start=AsyncMock(), stop=AsyncMock(), force_stop=AsyncMock(),
        get_session_metadata=AsyncMock(return_value=None),
        create_session=AsyncMock(return_value=session),
        resume_session=AsyncMock(return_value=session),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("new_session", [False, True])
async def test_sdk_owns_create_and_resume_without_history_probes(preview, monkeypatch, new_session):
    import copilot

    client = _fake_client()
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        result = await _copilot.run(preview, _request(new_session=new_session))
        assert result.content == "synthetic reply"
        if new_session:
            client.create_session.assert_awaited_once()
            client.resume_session.assert_not_awaited()
            session = client.create_session.return_value
        else:
            client.resume_session.assert_awaited_once()
            client.create_session.assert_not_awaited()
            session = client.resume_session.return_value
        client.get_session_metadata.assert_not_awaited()
        session.rpc.tools.get_current_metadata.assert_awaited_once()
        session.get_events.assert_not_awaited()
        session.disconnect.assert_awaited_once()
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_sdk_factory_receives_the_open_opaque_filesystem(preview, monkeypatch):
    import copilot
    from copilot.session_fs_provider import SessionFsProvider

    client = _fake_client()
    session = client.create_session.return_value
    providers = []
    content = "\x00opaque\r\nnot a host document"
    order = []

    async def create_session(**options):
        provider = options["create_session_fs_handler"](session)
        assert isinstance(provider, SessionFsProvider)
        assert Path(provider.workspace_path).is_dir()
        providers.append(provider)
        await provider.write_file("/session-state/unfamiliar.sdk", content)
        order.append("create")
        return session

    async def disconnect():
        assert await providers[0].read_file("/session-state/unfamiliar.sdk") == content
        order.append("disconnect")

    client.create_session.side_effect = create_session
    session.disconnect.side_effect = disconnect
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        await _copilot.run(preview, _request())
        assert order == ["create", "disconnect"]
        with pytest.raises(OSError) as caught:
            await providers[0].read_file("/session-state/unfamiliar.sdk")
        assert caught.value.errno == errno.EBADF
        reopened = await open_session_fs(preview.session_storage, "main", "example")
        try:
            assert await reopened.read_file("/session-state/unfamiliar.sdk") == content
        finally:
            await reopened.close()
        options = client.create_session.call_args.kwargs
        assert "continue_pending_work" not in options
        assert "infinite_sessions" not in options
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_same_session_resume_resets_native_calls_and_unsubscribes_each_turn(
    preview, monkeypatch
):
    import copilot
    from copilot.session_events import (
        AssistantMessageData,
        SessionEventType,
        ToolExecutionCompleteData,
        ToolExecutionCompleteResult,
        ToolExecutionStartData,
    )

    from azure_functions_agents.discovery.mcp import MCPServerDescriptor
    from azure_functions_agents.registration._handlers import _tool_error_count

    client = _fake_client()
    session = client.create_session.return_value
    turn = 0

    async def send(*args, **kwargs):
        nonlocal turn
        turn += 1
        [observer] = session.handlers
        assert observer(_sdk_event(
            SessionEventType.TOOL_EXECUTION_START,
            ToolExecutionStartData(
                tool_call_id="reused-id", tool_name="lookup", arguments={"turn": turn}
            ),
        )) is None
        observer(_sdk_event(
            SessionEventType.TOOL_EXECUTION_COMPLETE,
            ToolExecutionCompleteData(
                tool_call_id="reused-id",
                success=turn == 1,
                result=ToolExecutionCompleteResult(
                    content="first-result" if turn == 1 else "private denied output"
                ),
            ),
        ))
        final = _sdk_event(
            SessionEventType.ASSISTANT_MESSAGE,
            AssistantMessageData(content=f"reply-{turn}", message_id=f"reply-{turn}"),
        )
        observer(final)
        return final

    session.send_and_wait.side_effect = send
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    server = MCPServerDescriptor.create(name="selected", url="https://fixture.invalid/mcp")
    request = replace(_request(), mcp_servers=(server,))
    try:
        first = await _copilot.run(preview, request)
        assert session.handlers == []
        second = await _copilot.run(preview, replace(request, new_session=False))

        assert first.content == "reply-1"
        assert second.content == "reply-2"
        assert first.model == second.model == "gpt-4.1-mini"
        assert first.content_intermediate == second.content_intermediate == []
        assert len(first.tool_calls) == len(second.tool_calls) == 1
        assert first.tool_calls[0]["arguments"] == {"turn": 1}
        assert second.tool_calls[0]["arguments"] == {"turn": 2}
        assert first.tool_calls[0]["result"] == "first-result"
        assert first.tool_calls[0]["success"] is True
        assert second.tool_calls[0]["success"] is False
        assert "turn_id" not in first.tool_calls[0]
        assert "turn_id" not in second.tool_calls[0]
        assert _tool_error_count(first.tool_calls) == 0
        assert _tool_error_count(second.tool_calls) == 1
        assert "first-result" not in repr(second.tool_calls)
        assert "private denied output" not in repr(second.tool_calls)
        assert client.create_session.call_args.kwargs["session_id"] == (
            client.resume_session.call_args.args[0]
        )
        assert session.on.call_count == session.unsubscribe.call_count == 2
        assert session.disconnect.await_count == 2
        assert session.handlers == []
    finally:
        await _lifecycle._shutdown_harnesses()


def test_native_sdk_identity_is_readable_and_agent_scoped():
    assert _copilot._copilot_session_id("main", "example") == "main-example"
    assert _copilot._copilot_session_id("main", "one.two-three") == "main-one.two-three"
    assert _copilot._copilot_session_id("billing", "shared") != _copilot._copilot_session_id("support", "shared")


def test_tool_result_text_preserves_plain_text_and_rejects_sdk_content():
    from agent_framework import Content

    assert _copilot.tool_result_text("first\nsecond") == "first\nsecond"
    with pytest.raises(TypeError, match="SDK objects"):
        _copilot.tool_result_text([
            Content("text", text="first"),
            Content("text", text="second"),
        ])


async def _invoke_native_tool(function, arguments):
    from copilot.tools import ToolInvocation

    [descriptor] = _preview.prepare_tools([function])
    calls = CopilotToolCalls()
    native_tool = _copilot._tool(descriptor, calls)
    assert native_tool.handler is not None
    result = await native_tool.handler(
        ToolInvocation(
            session_id="native-session",
            tool_call_id="call-1",
            tool_name=descriptor.name,
            arguments=arguments,
        )
    )
    return result, calls.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
async def test_tool_adapter_runs_sync_and_async_callables(is_async):
    effects = []

    if is_async:

        async def candidate(value: str) -> str:
            await asyncio.sleep(0)
            effects.append(value)
            return f"async:{value}"

    else:

        def candidate(value: str) -> str:
            effects.append(value)
            return f"sync:{value}"

    [function] = _preview.prepare_tools([tool(candidate)])
    result, calls = await _invoke_native_tool(function, {"value": "ok"})

    assert result.result_type == "success"
    assert result.text_result_for_llm == f"{'async' if is_async else 'sync'}:ok"
    assert effects == ["ok"]
    assert calls[0]["result"] == result.text_result_for_llm
    assert calls[0]["success"] is True


class _PositiveInput(BaseModel):
    count: int = Field(gt=0)


@pytest.mark.asyncio
async def test_tool_adapter_validates_pydantic_before_effect():
    effects = []

    @tool(schema=_PositiveInput)
    def validated(params: _PositiveInput) -> str:
        effects.append(params.count)
        return str(params.count)

    result, calls = await _invoke_native_tool(validated, {"count": 0})

    assert result.result_type == "failure"
    assert effects == []
    assert calls[0]["result"] == '{"error":"Custom tool failed or returned unsupported content."}'
    assert calls[0]["success"] is False


@pytest.mark.asyncio
async def test_tool_adapter_supports_both_workflow_decorator_orders():
    def tool_outer(value: str) -> str:
        return f"outer:{value}"

    def workflow_outer(value: str) -> str:
        return f"workflow:{value}"

    first = tool(name="tool_outer")(workflow_tool(tool_outer))
    second = workflow_tool(tool(name="workflow_outer")(workflow_outer))
    prepared = _preview.prepare_tools([first, second])

    first_result, _ = await _invoke_native_tool(prepared[0], {"value": "ok"})
    second_result, _ = await _invoke_native_tool(prepared[1], {"value": "ok"})

    assert first_result.text_result_for_llm == "outer:ok"
    assert second_result.text_result_for_llm == "workflow:ok"


@pytest.mark.asyncio
async def test_tool_adapter_returns_recoverable_failure():
    @tool
    def broken() -> str:
        raise RuntimeError("private tool detail")

    result, calls = await _invoke_native_tool(broken, {})

    assert result.result_type == "failure"
    assert "private tool detail" not in result.text_result_for_llm
    assert calls[0]["result"] == result.text_result_for_llm
    assert calls[0]["success"] is False


@pytest.mark.asyncio
async def test_tool_adapter_propagates_cancellation():
    started = asyncio.Event()

    @tool
    async def wait_forever() -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    from copilot.tools import ToolInvocation

    [descriptor] = _preview.prepare_tools([wait_forever])
    calls = CopilotToolCalls()
    native_tool = _copilot._tool(descriptor, calls)
    assert native_tool.handler is not None
    task = asyncio.create_task(
        native_tool.handler(
            ToolInvocation(
                session_id="native-session",
                tool_call_id="call-cancel",
                tool_name="wait_forever",
                arguments={},
            )
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls.calls == [
        {
            "type": "tool_start",
            "tool_call_id": "call-cancel",
            "tool_name": "wait_forever",
            "arguments": {},
        }
    ]


@pytest.mark.asyncio
async def test_sdk_receives_only_combined_custom_catalog(preview, monkeypatch):
    import copilot

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    functions = _preview.prepare_tools([
        tool(name="user_tool")(lambda: "user"),
        tool(name="execute_python")(lambda code: code),
        tool(name="web_request")(lambda url: url),
    ])
    client = _fake_client()
    client.create_session.return_value.rpc.tools.get_current_metadata.return_value = SimpleNamespace(
        tools=[SimpleNamespace(name=function.name) for function in functions]
    )
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        await _copilot.run(preview, replace(_request(), tools=functions))
        options = client.create_session.call_args.kwargs
        assert options["available_tools"] == [
            "custom:user_tool",
            "custom:execute_python",
            "custom:web_request",
        ]
        assert [item.name for item in options["tools"]] == [
            "user_tool",
            "execute_python",
            "web_request",
        ]
        assert options["enable_config_discovery"] is False
        assert options["tool_search"]["enabled"] is False
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_custom_only_catalog_mismatch_fails_before_observer_or_send(preview, monkeypatch):
    import copilot
    from copilot.rpc import CurrentToolMetadata

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    function = tool(name="allowed_tool")(lambda: "ok")
    functions = _preview.prepare_tools([function])
    client = _fake_client()
    session = client.create_session.return_value
    session.rpc.tools.get_current_metadata.return_value = SimpleNamespace(
        tools=[
            CurrentToolMetadata(description="", name="allowed_tool"),
            CurrentToolMetadata(description="", name="shell"),
        ]
    )
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        with pytest.raises(CopilotPreviewError, match="catalog differs"):
            await _copilot.run(preview, replace(_request(), tools=functions))
        options = client.create_session.call_args.kwargs
        assert options["available_tools"] == ["custom:allowed_tool"]
        assert options["enable_skills"] is False
        assert options["enable_config_discovery"] is False
        session.rpc.tools.get_current_metadata.assert_awaited_once()
        session.on.assert_not_called()
        session.send_and_wait.assert_not_awaited()
        session.disconnect.assert_awaited_once()
    finally:
        await _lifecycle._shutdown_harnesses()


def _skill_inventory(root):
    from azure_functions_agents.discovery.skills import SkillDescriptor

    approved = root / "skills" / "approved"
    excluded = approved / "nested"
    excluded.mkdir(parents=True)
    (approved / "SKILL.md").write_text(
        "---\nname: approved\ndescription: Approved description.\n---\napproved content\n",
        encoding="utf-8",
    )
    (approved / "reference.txt").write_text("approved resource", encoding="utf-8")
    (excluded / "SKILL.md").write_text(
        "---\nname: excluded\ndescription: Excluded description.\n---\nexcluded content\n",
        encoding="utf-8",
    )
    return tuple(
        SkillDescriptor.create(name=name, path=path)
        for name, path in (
            ("approved", approved),
            ("excluded", excluded),
        )
    )


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_create_resume_keep_filtered_capabilities_and_refresh_static_mcp_headers(
    preview, monkeypatch,
):
    import copilot

    from azure_functions_agents import _mcp_auth
    from azure_functions_agents._tool_descriptor import ToolDescriptor
    from azure_functions_agents.discovery.mcp import MCPServerDescriptor

    credential = SimpleNamespace(get_token=Mock(side_effect=[
        AccessToken("first-token-sentinel", 9999999999),
        AccessToken("second-token-sentinel", 9999999999),
    ]))
    monkeypatch.setattr(_mcp_auth, "build_credential", Mock(return_value=credential))
    servers = tuple(
        MCPServerDescriptor.create(name=name, url=f"https://{name}.example/mcp", **options)
        for name, options in (
            ("all", {
                "auth_scope": "https://remote.example/.default",
                "headers": {"Authorization": "static-sentinel", "X-Static": "unchanged"},
            }),
            ("none", {"tools": []}),
            ("selected", {"tools": ["lookup"], "transport": "streamable-http"}),
        )
    )
    host_tool = ToolDescriptor.create(name="host_tool", description="Host tool", func=lambda: "ok")
    skills = _skill_inventory(preview.app_root)
    request = replace(
        _request(),
        session_id="one.two-three",
        tools=(host_tool,),
        mcp_servers=servers,
        skills=skills[:1],
        skill_catalog=skills,
    )
    client = _fake_client()
    client.resume_session.return_value = _fake_client().resume_session.return_value
    factory = Mock(return_value=client)
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    try:
        first = await _copilot.run(preview, request)
        second = await _copilot.run(preview, replace(request, new_session=False))
        assert first.session_id == second.session_id == "one.two-three"
        created = client.create_session.call_args.kwargs
        resumed = client.resume_session.call_args.kwargs
        assert created["session_id"] == client.resume_session.call_args.args[0] == (
            _copilot._copilot_session_id("main", "one.two-three")
        )
        for options in (created, resumed):
            assert set(options["mcp_servers"]) == {"all", "none", "selected"}
            assert options["mcp_servers"]["all"]["tools"] == ["*"]
            assert options["mcp_servers"]["none"]["tools"] == []
            assert options["mcp_servers"]["selected"]["tools"] == ["lookup"]
            assert options["mcp_servers"]["selected"]["type"] == "http"
            assert options["mcp_servers"]["selected"]["url"] == "https://selected.example/mcp"
            assert options["skill_directories"] == [str(skills[0].path)]
            assert options["disabled_skills"] == ["excluded"]
            assert options["included_builtin_skills"] == []
            assert options["enable_skills"] is True
            assert options["available_tools"] == [
                "custom:host_tool", "mcp:*", "builtin:skill", "builtin:view", "builtin:bash",
            ]
            assert options["working_directory"] == factory.call_args.kwargs[
                "session_fs"
            ]["initial_working_directory"]
            assert options["enable_config_discovery"] is False
            assert options["enable_session_telemetry"] is False
            assert options["request_extensions"] is False
            assert options["system_message"] == {"mode": "replace", "content": request.instructions}
        created_all_headers = dict(created["mcp_servers"]["all"]["headers"])
        resumed_all_headers = dict(resumed["mcp_servers"]["all"]["headers"])
        assert created_all_headers["X-Static"] == "unchanged"
        assert resumed_all_headers["X-Static"] == "unchanged"
        assert created_all_headers["Authorization"] == "Bearer first-token-sentinel"
        assert resumed_all_headers["Authorization"] == "Bearer second-token-sentinel"
        assert created["mcp_servers"] is not resumed["mcp_servers"]
        assert created["mcp_servers"]["all"]["headers"] is not resumed["mcp_servers"]["all"]["headers"]
        assert servers[0].headers == (("Authorization", "static-sentinel"), ("X-Static", "unchanged"))
        assert credential.get_token.call_count == 2
        assert all(
            args.args == ("https://remote.example/.default",)
            for args in credential.get_token.call_args_list
        )
        client.create_session.assert_awaited_once()
        client.resume_session.assert_awaited_once()
        client.get_session_metadata.assert_not_awaited()
        for session in (client.create_session.return_value, client.resume_session.return_value):
            session.disconnect.assert_awaited_once()
            session.get_events.assert_not_awaited()
            session.rpc.tools.get_current_metadata.assert_not_awaited()
        assert factory.call_args.kwargs["telemetry"] is None
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_session", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
async def test_native_skill_exposure_matches_approved_inventory_on_create_and_resume(
    preview, monkeypatch, new_session, enabled,
):
    import copilot
    from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionDeniedByRules
    from copilot.session_events import PermissionRequestRead, PermissionRequestUrl

    skills = _skill_inventory(preview.app_root)
    read_text = Path.read_text

    def forbid_skill_reads(path, *args, **kwargs):
        if path.name == "SKILL.md":
            raise AssertionError("The host must not read SDK-owned skill content")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbid_skill_reads)
    client = _fake_client()
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))

    async def read_resource_during_turn(*_args, **_kwargs):
        session_call = client.create_session.call_args if new_session else client.resume_session.call_args
        storage = session_call.kwargs["create_session_fs_handler"](None)
        resource = str(skills[0].path / "reference.txt")
        if enabled:
            assert await storage.exists(resource)
            assert await storage.read_file(resource) == Path(resource).read_text(encoding="utf-8")
        else:
            with pytest.raises(OSError):
                await storage.read_file(resource)
        return client.create_session.return_value.send_and_wait.return_value

    client.create_session.return_value.send_and_wait.side_effect = read_resource_during_turn
    try:
        await _copilot.run(preview, replace(
            _request(new_session=new_session),
            skills=skills[:1] if enabled else (),
            skill_catalog=skills,
        ))
        session_call = client.create_session.call_args if new_session else client.resume_session.call_args
        options = session_call.kwargs
        assert options["enable_skills"] is enabled
        assert options["skill_directories"] == ([str(skills[0].path)] if enabled else [])
        assert options["disabled_skills"] == (["excluded"] if enabled else ["approved", "excluded"])
        assert options["available_tools"] == (
            ["builtin:skill", "builtin:view", "builtin:bash"] if enabled else []
        )
        assert options["included_builtin_skills"] == []
        handler = options["on_permission_request"]
        resource = str(skills[0].path / "reference.txt")
        read = handler(
            PermissionRequestRead(intention="resource", path=resource),
            {"session_id": "main.example"},
        )
        assert read.kind == (
            PermissionDecisionApproveOnce.kind if enabled else PermissionDecisionDeniedByRules.kind
        )
        denied = handler(
            PermissionRequestUrl(intention="skill", url="https://remote.example/"),
            {"session_id": "main.example"},
        )
        assert denied.kind == PermissionDecisionDeniedByRules.kind
        assert options["system_message"] == {"mode": "replace", "content": "Be helpful."}
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_session", [False, True])
async def test_mcp_authentication_failure_stops_before_native_session_without_retry(
    preview, monkeypatch, new_session,
):
    import copilot

    from azure_functions_agents.discovery.mcp import MCPServerDescriptor
    from azure_functions_agents.harness.copilot_sdk import _copilot_capabilities

    materialize = Mock(side_effect=RuntimeError("private auth sentinel"))
    monkeypatch.setattr(_copilot_capabilities, "materialize_mcp_headers", materialize)
    factory = Mock(side_effect=AssertionError("no native process or fallback"))
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    providers = []
    original_open = _copilot.open_session_fs

    async def observe_open(*args, **kwargs):
        provider = await original_open(*args, **kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(_copilot, "open_session_fs", observe_open)
    server = MCPServerDescriptor.create(
        name="selected", url="https://remote.example/mcp", auth_scope="https://remote.example/.default"
    )
    try:
        with pytest.raises(CopilotPreviewError, match="MCP authentication") as caught:
            await _copilot.run(preview, replace(
                _request(new_session=new_session), mcp_servers=(server,)
            ))
        assert "private auth sentinel" not in str(caught.value)
        materialize.assert_called_once_with(server)
        factory.assert_not_called()
        assert len(providers) == 1
        assert providers[0]._closed
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_session", [False, True])
async def test_mcp_session_failure_does_not_drop_capabilities_reset_or_retry(
    preview, monkeypatch, new_session,
):
    import copilot

    from azure_functions_agents.discovery.mcp import MCPServerDescriptor

    server = MCPServerDescriptor.create(
        name="selected", url="https://remote.example/mcp", tools=["lookup"]
    )
    client = _fake_client()
    method = client.create_session if new_session else client.resume_session
    method.side_effect = RuntimeError("private MCP connection detail")
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        with pytest.raises(CopilotPreviewError, match="No fallback") as caught:
            await _copilot.run(preview, replace(
                _request(new_session=new_session), mcp_servers=(server,)
            ))
        assert "private MCP connection detail" not in str(caught.value)
        method.assert_awaited_once()
        other = client.resume_session if new_session else client.create_session
        other.assert_not_awaited()
        assert method.call_args.kwargs["mcp_servers"]["selected"]["tools"] == ["lookup"]
        assert method.call_args.kwargs["available_tools"] == ["mcp:*"]
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_native_skill_mcp_denials_and_custom_calls_feed_existing_result_metrics(
    preview, monkeypatch,
):
    import copilot
    from copilot.rpc import PermissionDecisionDeniedByRules
    from copilot.session_events import (
        PermissionRequestRead,
        SessionEventType,
        SkillInvokedData,
        ToolExecutionCompleteData,
        ToolExecutionCompleteError,
        ToolExecutionCompleteResult,
        ToolExecutionStartData,
    )
    from copilot.tools import ToolInvocation

    from azure_functions_agents._tool_descriptor import ToolDescriptor
    from azure_functions_agents.discovery.mcp import MCPServerDescriptor
    from azure_functions_agents.registration._handlers import _set_run_result_attributes

    skills = _skill_inventory(preview.app_root)
    host_tool = ToolDescriptor.create(name="host_tool", description="Host tool", func=lambda: "host result")
    server = MCPServerDescriptor.create(name="selected", url="https://remote.example/mcp")
    client = _fake_client()
    session = client.create_session.return_value

    async def send(*_args, **_kwargs):
        options = client.create_session.call_args.kwargs
        [on_event] = session.handlers
        assert "on_event" not in options

        def emit(event_type, data):
            on_event(_sdk_event(event_type, data))

        emit(SessionEventType.TOOL_EXECUTION_START, ToolExecutionStartData(
            tool_call_id="skill", tool_name="skill", arguments={"name": "approved"},
        ))
        emit(SessionEventType.SKILL_INVOKED, SkillInvokedData(
            content="private skill instructions", name="approved", path=str(skills[0].path),
            allowed_tools=["bash", "edit", "*"],
        ))
        emit(SessionEventType.TOOL_EXECUTION_COMPLETE, ToolExecutionCompleteData(
            tool_call_id="skill", success=True,
            result=ToolExecutionCompleteResult(content="Loaded approved."),
        ))
        denied = options["on_permission_request"](
            PermissionRequestRead(
                intention="claims an approved skill", path=str(skills[1].path / "SKILL.md"),
                tool_call_id="denied-view",
            ),
            {"session_id": "main.example"},
        )
        assert denied.kind == PermissionDecisionDeniedByRules.kind
        emit(SessionEventType.TOOL_EXECUTION_START, ToolExecutionStartData(
            tool_call_id="denied-view", tool_name="view", arguments={"path": "excluded"},
        ))
        for _duplicate in range(2):
            emit(SessionEventType.TOOL_EXECUTION_COMPLETE, ToolExecutionCompleteData(
                tool_call_id="denied-view", success=False,
                error=ToolExecutionCompleteError(message="private native denial detail"),
            ))
        emit(SessionEventType.TOOL_EXECUTION_START, ToolExecutionStartData(
            tool_call_id="mcp", tool_name="remote_lookup", arguments={"query": "safe"},
            mcp_server_name="selected", mcp_tool_name="lookup",
        ))
        emit(SessionEventType.TOOL_EXECUTION_COMPLETE, ToolExecutionCompleteData(
            tool_call_id="mcp", success=True,
            result=ToolExecutionCompleteResult(content='{"remote":"result"}'),
        ))
        emit(SessionEventType.TOOL_EXECUTION_START, ToolExecutionStartData(
            tool_call_id="custom", tool_name="host_tool", arguments={},
        ))
        [native_tool] = options["tools"]
        await native_tool.handler(ToolInvocation(
            session_id=_copilot._copilot_session_id("main", "one.two-three"),
            tool_call_id="custom",
            tool_name="host_tool",
            arguments={},
        ))
        emit(SessionEventType.TOOL_EXECUTION_COMPLETE, ToolExecutionCompleteData(
            tool_call_id="custom", success=True,
            result=ToolExecutionCompleteResult(content="SDK wrapper duplicate"),
        ))
        emit(SessionEventType.TOOL_EXECUTION_COMPLETE, ToolExecutionCompleteData(
            tool_call_id="completion-only-bash", success=False,
            error=ToolExecutionCompleteError(message="private shell detail"),
        ))
        return session.send_and_wait.return_value

    session.send_and_wait.side_effect = send
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        result = await _copilot.run(preview, replace(
            _request(), tools=(host_tool,), mcp_servers=(server,),
            skills=skills[:1], skill_catalog=skills,
        ))
        assert [call["tool_call_id"] for call in result.tool_calls] == [
            "skill", "denied-view", "mcp", "custom", "completion-only-bash",
        ]
        assert result.tool_calls[3]["result"] == "host result"
        assert "private" not in repr(result.tool_calls)
        span = Mock()
        _set_run_result_attributes(span, result)
        span.set_attribute.assert_any_call("af.agent.tool_call_count", 5)
        span.set_attribute.assert_any_call("af.agent.tool_error_count", 2)
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
@pytest.mark.parametrize("permission_callback", [False, True])
async def test_prompt_refusal_or_permission_request_alone_does_not_invent_tool_execution(
    preview, monkeypatch, permission_callback,
):
    import copilot
    from copilot.rpc import PermissionDecisionDeniedByRules
    from copilot.session_events import PermissionRequestRead

    client = _fake_client()
    session = client.create_session.return_value

    async def send(*_args, **_kwargs):
        if permission_callback:
            options = client.create_session.call_args.kwargs
            decision = options["on_permission_request"](
                PermissionRequestRead(intention="a claim", path="unapproved.txt"),
                {"session_id": "main.example"},
            )
            assert decision.kind == PermissionDecisionDeniedByRules.kind
        return session.send_and_wait.return_value

    session.send_and_wait.side_effect = send
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        result = await _copilot.run(preview, _request())
        assert result.tool_calls == []
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_native_custom_tools_preserve_json_results_and_fresh_parameter_schemas():
    from azure_functions_agents._tool_descriptor import ToolDescriptor

    descriptor = ToolDescriptor.create(
        name="json_tool",
        description="A JSON result.",
        func=lambda value: {"value": value, "items": [True, None]},
    )
    registered_parameters = descriptor.parameters()
    first = _copilot._tool(descriptor, CopilotToolCalls())
    second = _copilot._tool(descriptor, CopilotToolCalls())
    first.parameters["properties"]["value"]["description"] = "SDK-side mutation"

    assert second.parameters == registered_parameters
    assert descriptor.parameters() == registered_parameters
    result, calls = await _invoke_native_tool(descriptor, {"value": "ok"})
    assert result.result_type == "success"
    assert result.text_result_for_llm == '{"value": "ok", "items": [true, null]}'
    assert calls[0]["result"] == result.text_result_for_llm


@pytest.mark.asyncio
async def test_native_custom_tool_sdk_result_is_a_sanitized_recoverable_failure():
    from copilot.tools import ToolResult

    from azure_functions_agents._tool_descriptor import ToolDescriptor

    descriptor = ToolDescriptor.create(
        name="unsupported_result",
        description="Returns an unsupported SDK object.",
        func=lambda: ToolResult(text_result_for_llm="private SDK result detail"),
    )
    result, calls = await _invoke_native_tool(descriptor, {})

    assert result.result_type == "failure"
    assert result.text_result_for_llm == '{"error":"Custom tool failed or returned unsupported content."}'
    assert calls[0]["result"] == result.text_result_for_llm
    assert "private SDK result detail" not in repr(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "wire_api", "provider_type"),
    [
        (OpenAIProvider("not-a-credential"), "responses", "openai"),
        (AzureOpenAIProvider("https://fixture.openai.azure.com"), "responses", "azure"),
        (FoundryProvider("https://fixture.services.ai.azure.com/api/projects/test"), "responses", "openai"),
    ],
)
async def test_provider_options_use_sdk_declared_types(
    preview, monkeypatch, provider, wire_api, provider_type
):
    import copilot
    from copilot.session import ProviderConfig

    selected = replace(preview, provider=provider)
    credential = SimpleNamespace(
        get_token=AsyncMock(return_value=AccessToken("not-a-credential", 9999999999)),
        close=AsyncMock(),
    )
    monkeypatch.setattr(_runtime, "build_async_credential", lambda: credential)
    client = _fake_client()
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        result = await _copilot.run(selected, _request())
        resumed = await _copilot.run(selected, _request(new_session=False))
        assert result.content == "synthetic reply"
        assert resumed.content == "synthetic reply"
        assert result.model == "gpt-4.1-mini"
        allowed = get_type_hints(ProviderConfig)
        for options in (
            client.create_session.call_args.kwargs,
            client.resume_session.call_args.kwargs,
        ):
            provider_options = options["provider"]
            assert provider_options["type"] in get_args(allowed["type"])
            assert provider_options["type"] == provider_type
            assert provider_options["wire_api"] in get_args(allowed["wire_api"])
            assert provider_options["wire_api"] == wire_api
            assert provider_options["model_id"] == "gpt-4.1-mini"
            assert provider_options["wire_model"] == "gpt-4.1-mini"
            assert options["model"] == "gpt-4.1-mini"
            assert options["available_tools"] == []
            assert "max_output_tokens" not in provider_options
            if provider.kind is ProviderKind.AZURE_OPENAI:
                assert "azure" not in provider_options
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_concurrent_entra_failure_diagnostic_is_request_scoped(preview, monkeypatch):
    import copilot
    from copilot.session_events import AssistantMessageData, SessionEvent, SessionEventType

    selected = replace(
        preview,
        provider=AzureOpenAIProvider("https://fixture.openai.azure.com"),
    )
    credential = SimpleNamespace(
        get_token=AsyncMock(side_effect=RuntimeError("sentinel-private-credential-detail")),
        close=AsyncMock(),
    )
    token_failed = asyncio.Event()
    release_failure = asyncio.Event()
    client = _fake_client()

    async def create_session(**options):
        session = _fake_client().create_session.return_value
        callback = options["provider"]["bearer_token_provider"]

        async def send_and_wait(prompt, **_kwargs):
            if prompt == "bad":
                with pytest.raises(CopilotPreviewError):
                    await callback(SimpleNamespace())
                token_failed.set()
                await release_failure.wait()
                raise RuntimeError("SDK masked the token callback failure")
            await token_failed.wait()
            release_failure.set()
            return SessionEvent(
                data=AssistantMessageData(content="synthetic reply", message_id="fixture"),
                id=uuid4(),
                timestamp=datetime.now(UTC),
                type=SessionEventType.ASSISTANT_MESSAGE,
            )

        session.send_and_wait.side_effect = send_and_wait
        return session

    client.create_session.side_effect = create_session
    monkeypatch.setattr(_runtime, "build_async_credential", Mock(return_value=credential))
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    bad_request = replace(_request(), prompt="bad", session_id="bad")
    good_request = replace(_request(), prompt="good", session_id="good")
    try:
        bad_result, good_result = await asyncio.gather(
            _copilot.run(selected, bad_request),
            _copilot.run(selected, good_request),
            return_exceptions=True,
        )
    finally:
        await _lifecycle._shutdown_harnesses()

    assert isinstance(bad_result, CopilotPreviewError)
    assert "Azure OpenAI" in str(bad_result)
    assert "Entra token" in str(bad_result)
    assert "sentinel-private-credential-detail" not in str(bad_result)
    assert good_result.content == "synthetic reply"


def _registered_azure_chat(monkeypatch, tmp_path, *, blob_setting=None):
    root = tmp_path / "app"
    shutil.copytree(SAMPLE, root)
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    monkeypatch.setattr(paths, "_app_root", root)
    monkeypatch.setenv(_harness.FLAG, "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fixture.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri", raising=False)
    if blob_setting is not None:
        monkeypatch.setenv(*blob_setting)
    monkeypatch.delenv("WEBSITE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("FUNCTIONS_WORKER_PROCESS_COUNT", raising=False)
    app = create_function_app(root)
    return next(
        function.get_user_function()
        for function in app.get_functions()
        if function.get_function_name() == "agent_main_builtin_chat"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["AzureWebJobsStorage", "AzureWebJobsStorage__blobServiceUri"])
async def test_registered_blob_configuration_failure_is_safe_without_start_or_fallback(
    monkeypatch, tmp_path, caplog, setting,
):
    import copilot

    from azure_functions_agents import _credential

    sentinel = "fixture-private-storage-configuration"
    value = sentinel if setting == "AzureWebJobsStorage" else f"https:///{sentinel}"
    provider_credential = SimpleNamespace(
        get_token=AsyncMock(side_effect=AssertionError("Provider auth must not run")), close=AsyncMock()
    )
    storage_credential = SimpleNamespace(
        get_token=AsyncMock(side_effect=AssertionError("Storage auth must not run")), close=AsyncMock()
    )
    monkeypatch.setattr(_runtime, "build_async_credential", lambda: provider_credential)
    monkeypatch.setattr(
        _credential, "build_async_credential_with_client_id", lambda _client_id: storage_credential
    )
    native = Mock(side_effect=AssertionError("Native startup must not run"))
    maf = AsyncMock(side_effect=AssertionError("MAF fallback must not run"))
    monkeypatch.setattr(copilot, "CopilotClient", native)
    monkeypatch.setattr(_maf_execution, "_build_agent_session", maf)
    chat = _registered_azure_chat(monkeypatch, tmp_path, blob_setting=(setting, value))
    monkeypatch.setenv(_harness.FLAG, "false")
    try:
        response = await chat(
            SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "no inference"}))
        )
        assert response.status_code == 503
        assert "Blob session storage configuration" in response.body.decode()
        assert sentinel not in response.body.decode()
        assert sentinel not in caplog.text
        assert not (tmp_path / "state" / "copilot-native").exists()
        native.assert_not_called()
        maf.assert_not_awaited()
        provider_credential.get_token.assert_not_awaited()
        storage_credential.get_token.assert_not_awaited()
    finally:
        await _lifecycle._shutdown_harnesses()
    provider_credential.close.assert_awaited_once()
    if setting == "AzureWebJobsStorage__blobServiceUri":
        storage_credential.close.assert_awaited_once()
    else:
        storage_credential.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_credential_constructor_failure_is_sanitized_at_public_route(
    monkeypatch, tmp_path, caplog
):
    import copilot

    sentinel = "sentinel-private-credential-constructor"
    build = Mock(side_effect=RuntimeError(sentinel))
    native = Mock(side_effect=AssertionError("Native runtime must not start"))
    maf = AsyncMock(side_effect=AssertionError("MAF fallback must not run"))
    monkeypatch.setattr(_runtime, "build_async_credential", build)
    monkeypatch.setattr(copilot, "CopilotClient", native)
    monkeypatch.setattr(_maf_execution, "_build_agent_session", maf)
    chat = _registered_azure_chat(monkeypatch, tmp_path)
    try:
        response = await chat(
            SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "hello"}))
        )
    finally:
        await _lifecycle._shutdown_harnesses()

    body = response.body.decode()
    assert response.status_code == 500
    assert "Azure OpenAI" in body
    assert "Azure credential" in body
    assert sentinel not in body
    assert sentinel not in caplog.text
    build.assert_called_once_with()
    native.assert_not_called()
    maf.assert_not_awaited()


@pytest.mark.asyncio
async def test_credential_callback_failure_is_sanitized_at_public_route(
    monkeypatch, tmp_path, caplog
):
    import copilot

    sentinel = "sentinel-private-token-callback"
    credential = SimpleNamespace(
        get_token=AsyncMock(side_effect=RuntimeError(sentinel)),
        close=AsyncMock(),
    )
    client = _fake_client()

    async def create_session(**options):
        await options["provider"]["bearer_token_provider"](SimpleNamespace())
        raise AssertionError("Credential callback failure must stop session creation")

    client.create_session.side_effect = create_session
    maf = AsyncMock(side_effect=AssertionError("MAF fallback must not run"))
    monkeypatch.setattr(_runtime, "build_async_credential", Mock(return_value=credential))
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    monkeypatch.setattr(_maf_execution, "_build_agent_session", maf)
    chat = _registered_azure_chat(monkeypatch, tmp_path)
    try:
        response = await chat(
            SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "hello"}))
        )
    finally:
        await _lifecycle._shutdown_harnesses()

    body = response.body.decode()
    assert response.status_code == 500
    assert "Azure OpenAI" in body
    assert "Entra token" in body
    assert sentinel not in body
    assert sentinel not in caplog.text
    credential.close.assert_awaited_once()
    maf.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_auth_failure_is_sanitized_at_public_route(
    monkeypatch, tmp_path, caplog
):
    import copilot

    sentinel = "sentinel-private-provider-403"
    client = _fake_client()
    session = client.create_session.return_value
    session.rpc.tools.get_current_metadata.return_value = SimpleNamespace(
        tools=[SimpleNamespace(name="make_receipt"), SimpleNamespace(name="web_request")]
    )
    session.send_and_wait.side_effect = RuntimeError(sentinel)
    maf = AsyncMock(side_effect=AssertionError("MAF fallback must not run"))
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sentinel-private-api-key")
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    monkeypatch.setattr(_maf_execution, "_build_agent_session", maf)
    chat = _registered_azure_chat(monkeypatch, tmp_path)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sentinel-private-api-key")
    try:
        response = await chat(
            SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "hello"}))
        )
    finally:
        await _lifecycle._shutdown_harnesses()

    body = response.body.decode()
    assert response.status_code == 500
    assert "provider authentication" in body
    assert sentinel not in body
    assert "sentinel-private-api-key" not in body
    assert sentinel not in caplog.text
    assert "sentinel-private-api-key" not in caplog.text
    maf.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_config_is_resupplied_on_resume(preview, monkeypatch):
    import copilot

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    client = _fake_client()
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        await _copilot.run(preview, _request())
        await _copilot.run(preview, _request(new_session=False))
    finally:
        await _lifecycle._shutdown_harnesses()

    created = client.create_session.call_args.kwargs["provider"]
    resumed = client.resume_session.call_args.kwargs["provider"]
    assert created["model_id"] == resumed["model_id"] == "gpt-4.1-mini"
    assert created["wire_model"] == resumed["wire_model"] == "gpt-4.1-mini"
    assert created["wire_api"] == resumed["wire_api"] == "responses"


@pytest.mark.asyncio
async def test_late_custom_manager_replacement_fails_before_native_execution(
    preview, monkeypatch, replace_client_manager
):
    class CustomManager(ClientManager):
        def resolve_model(self, requested):
            return requested or "custom"

        def build_chat_client(self, model):
            raise AssertionError("Custom MAF client must not be constructed")

    replace_client_manager(CustomManager())
    native = Mock(side_effect=AssertionError("Native runtime must not be acquired"))
    monkeypatch.setattr(_copilot, "get_runtime", native)

    with pytest.raises(UnsupportedCapabilityError, match=r"ClientManager.*MAF-only"):
        await _copilot.run(preview, _request())

    native.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["session not found", "malformed session state"])
async def test_sdk_resume_failure_is_safe_and_never_creates_a_session(preview, monkeypatch, failure):
    import copilot

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    client = _fake_client()
    client.resume_session.side_effect = RuntimeError(f"{failure}: private-sdk-details")
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        with pytest.raises(CopilotPreviewError) as error:
            await _copilot.run(preview, _request(new_session=False))
        assert "private-sdk-details" not in str(error.value)
        assert "session" in str(error.value).lower() or "conversation" in str(error.value).lower()
        client.resume_session.assert_awaited_once()
        client.create_session.assert_not_awaited()
    finally:
        await _lifecycle._shutdown_harnesses()


async def _sdk_file(preview):
    provider = await open_session_fs(preview.session_storage, "main", "example")
    try:
        return await provider.read_file("/session-state/uninterpreted.sdk")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_resume_retry_preserves_opaque_sdk_files(preview, monkeypatch):
    import copilot

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    client = _fake_client()
    provider = await open_session_fs(preview.session_storage, "main", "example")
    try:
        await provider.write_file("/session-state/uninterpreted.sdk", "\x00opaque\r\n")
    finally:
        await provider.close()
    session = client.resume_session.return_value
    client.resume_session.side_effect = [RuntimeError("temporary native error"), session]
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        with pytest.raises(CopilotPreviewError):
            await _copilot.run(preview, _request(new_session=False))
        assert await _sdk_file(preview) == "\x00opaque\r\n"

        result = await _copilot.run(preview, _request(new_session=False))
        assert result.content == "synthetic reply"
        assert client.resume_session.await_count == 2
        client.create_session.assert_not_awaited()
        client.get_session_metadata.assert_not_awaited()
        session.get_events.assert_not_awaited()
        assert await _sdk_file(preview) == "\x00opaque\r\n"
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_create_rpc_failure_preserves_sdk_writes_without_host_rollback(preview, monkeypatch):
    import copilot

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    client = _fake_client()
    session = client.create_session.return_value
    async def fail_after_sdk_write(**options):
        provider = options["create_session_fs_handler"](session)
        await provider.write_file("/session-state/uninterpreted.sdk", "SDK owns this write")
        raise RuntimeError("private create failure")

    client.create_session.side_effect = fail_after_sdk_write
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        with pytest.raises(CopilotPreviewError):
            await _copilot.run(preview, _request())
        assert await _sdk_file(preview) == "SDK owns this write"

        client.create_session.side_effect = None
        result = await _copilot.run(preview, _request())
        assert result.content == "synthetic reply"
        assert client.create_session.await_count == 2
        client.get_session_metadata.assert_not_awaited()
        assert await _sdk_file(preview) == "SDK owns this write"
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_startup_failure_closes_the_open_filesystem(preview, monkeypatch):
    import copilot

    client = _fake_client()
    client.start.side_effect = RuntimeError("private startup failure")
    providers = []
    original_open = _copilot.open_session_fs

    async def observe_open(*args, **kwargs):
        provider = await original_open(*args, **kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(_copilot, "open_session_fs", observe_open)
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        with pytest.raises(CopilotPreviewError):
            await _copilot.run(preview, _request())
        assert len(providers) == 1
        assert providers[0]._closed
        client.create_session.assert_not_awaited()
        client.stop.assert_awaited_once()
        client.get_session_metadata.assert_not_awaited()
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_public_run_reports_startup_failure_without_cleanup_masking(
    preview, monkeypatch, cleanup_fails
):
    import copilot

    client = _fake_client()
    client.start.side_effect = RuntimeError("private startup failure")
    if cleanup_fails:
        client.stop.side_effect = RuntimeError("private stop failure")
        client.force_stop.side_effect = RuntimeError("private force-stop failure")
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        with pytest.raises(CopilotPreviewError, match="runtime startup failed"):
            await _copilot.run(preview, _request())
        client.stop.assert_awaited_once()
        if cleanup_fails:
            client.force_stop.assert_awaited_once()
        else:
            client.force_stop.assert_not_awaited()
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_public_run_distinguishes_startup_timeout_from_request_deadline(
    preview, monkeypatch, cleanup_fails
):
    import copilot

    client = _fake_client()

    async def stalled_start():
        await asyncio.sleep(60)

    client.start.side_effect = stalled_start
    if cleanup_fails:
        client.stop.side_effect = RuntimeError("private stop failure")
        client.force_stop.side_effect = RuntimeError("private force-stop failure")
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    monkeypatch.setattr(_runtime, "_START_TIMEOUT_SECONDS", 0.01)
    try:
        with pytest.raises(CopilotPreviewError, match="startup timed out"):
            await _copilot.run(preview, _request())
        client.stop.assert_awaited_once()
        if cleanup_fails:
            client.force_stop.assert_awaited_once()
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_filesystem_failure_stops_before_native_start_without_fallback(preview, monkeypatch):
    import copilot

    native = Mock(side_effect=AssertionError("Native startup must not run"))
    maf = AsyncMock(side_effect=AssertionError("MAF fallback must not run"))
    monkeypatch.setattr(copilot, "CopilotClient", native)
    monkeypatch.setattr(_maf_execution, "_build_agent_session", maf)
    monkeypatch.setattr(_copilot, "open_session_fs", AsyncMock(
        side_effect=CopilotSessionError(errno.EIO, "Configured storage failed.")
    ))
    try:
        with pytest.raises(CopilotSessionError) as caught:
            await _copilot.run(preview, _request())
        assert caught.value.errno == errno.EIO
        native.assert_not_called()
        maf.assert_not_awaited()
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["send", "catalog"])
async def test_detach_failure_does_not_mask_the_original_failure(preview, monkeypatch, boundary):
    import copilot

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    client = _fake_client()
    session = client.create_session.return_value
    original_failure = CopilotPreviewError("original failure")
    providers = []
    original_open = _copilot.open_session_fs

    async def observe_open(*args, **kwargs):
        provider = await original_open(*args, **kwargs)
        providers.append(provider)
        return provider

    if boundary == "send":
        session.send_and_wait.side_effect = original_failure
    else:
        session.rpc.tools.get_current_metadata.return_value = SimpleNamespace(tools=None)
    session.disconnect.side_effect = RuntimeError("disconnect failed")
    monkeypatch.setattr(_copilot, "open_session_fs", observe_open)
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        diagnostic = "original failure" if boundary == "send" else "model-visible tool catalog"
        with pytest.raises(CopilotPreviewError, match=diagnostic) as caught:
            await _copilot.run(preview, _request())
        if boundary == "send":
            assert caught.value is original_failure
            session.send_and_wait.assert_awaited_once()
            session.abort.assert_awaited_once()
            session.unsubscribe.assert_called_once()
        else:
            session.send_and_wait.assert_not_awaited()
            session.abort.assert_not_awaited()
            session.on.assert_not_called()
        session.disconnect.assert_awaited_once()
        assert providers[0]._closed
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_detach_failure_alone_still_fails_the_turn(preview, monkeypatch):
    import copilot

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    client = _fake_client()
    providers = []
    original_open = _copilot.open_session_fs

    async def observe_open(*args, **kwargs):
        provider = await original_open(*args, **kwargs)
        providers.append(provider)
        return provider

    client.create_session.return_value.disconnect.side_effect = RuntimeError("disconnect failed")
    monkeypatch.setattr(_copilot, "open_session_fs", observe_open)
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        with pytest.raises(CopilotPreviewError, match="could not be disconnected"):
            await _copilot.run(preview, _request())
        assert providers[0]._closed
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_maf_history_does_not_block_or_get_loaded_by_copilot(preview, monkeypatch):
    import copilot

    history = preview.session_storage.local_dir / "agent-sessions" / "main" / "example.jsonl"
    history.parent.mkdir(parents=True)
    history.write_bytes(b"unrelated MAF-owned history")
    maf_history = Mock(side_effect=AssertionError("MAF history must not be constructed"))
    monkeypatch.setattr(_maf_execution, "_build_history_provider", maf_history)
    client = _fake_client()
    client.get_session_metadata.return_value = SimpleNamespace(session_id="existing")
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        await _copilot.run(preview, _request())
        client.get_session_metadata.assert_not_awaited()
        client.create_session.assert_awaited_once()
        client.resume_session.assert_not_awaited()
        maf_history.assert_not_called()
        assert history.read_bytes() == b"unrelated MAF-owned history"
    finally:
        await _lifecycle._shutdown_harnesses()


def test_portable_output_limit_is_rejected_during_registration(tmp_path, monkeypatch):
    import copilot

    root = tmp_path / "app"
    shutil.copytree(SAMPLE, root)
    agent = root / "main.agent.md"
    agent.write_text(
        agent.read_text(encoding="utf-8").replace(
            "tools: true\n", "tools: true\nagent_configuration:\n  max_output_tokens: 256\n", 1
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    monkeypatch.setattr(paths, "_app_root", root)
    monkeypatch.setenv(_harness.FLAG, "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-not-a-secret")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("WEBSITE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("FUNCTIONS_WORKER_PROCESS_COUNT", raising=False)
    factory = Mock(side_effect=AssertionError("Native runtime must not start"))
    monkeypatch.setattr(copilot, "CopilotClient", factory)

    with pytest.raises(UnsupportedCapabilityError, match="max_output_tokens"):
        create_function_app(root)
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_native_client_uses_an_existing_host_working_directory(preview, monkeypatch):
    """The pinned runtime validates the session cwd against the real host filesystem."""
    import copilot

    from azure_functions_agents.harness.copilot_sdk._copilot_session_paths import (
        HOST_PATH_CONVENTIONS,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    client = _fake_client()
    factory = Mock(return_value=client)
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    try:
        await _copilot.run(preview, _request(new_session=True))
    finally:
        await _lifecycle._shutdown_harnesses()

    session_fs = factory.call_args.kwargs["session_fs"]
    working_directory = Path(session_fs["initial_working_directory"])
    assert working_directory.is_absolute()
    assert working_directory.is_dir()
    assert session_fs["conventions"] == HOST_PATH_CONVENTIONS
    assert session_fs["session_state_path"] == "/session-state"


@pytest.mark.asyncio
async def test_same_session_wait_is_bounded_while_other_sessions_remain_concurrent(preview, monkeypatch):
    import copilot

    monkeypatch.setattr(_execution, "_SESSION_LOCKS", {})
    monkeypatch.setattr(_execution, "_SESSION_LOCKS_GUARD", asyncio.Lock())
    started, release = asyncio.Event(), asyncio.Event()
    client = _fake_client()
    sessions = []

    async def resume_session(_session_id, **_options):
        session = _fake_client().resume_session.return_value
        sessions.append(session)
        if len(sessions) == 1:
            async def wait_for_release(*_args, **_kwargs):
                started.set()
                await release.wait()
                return _fake_client().create_session.return_value.send_and_wait.return_value

            session.send_and_wait.side_effect = wait_for_release
        return session

    client.resume_session.side_effect = resume_session
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    first = asyncio.create_task(_copilot.run(preview, _request(new_session=False)))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        waiting = replace(
            _request(new_session=False), deadline=asyncio.get_running_loop().time() + 0.05
        )
        with pytest.raises(CopilotPreviewError, match="deadline"):
            await asyncio.wait_for(_copilot.run(preview, waiting), timeout=1)
        assert client.resume_session.await_count == 1
        sessions[0].abort.assert_not_awaited()

        for request in (
            replace(_request(new_session=False), session_id="other"),
            replace(_request(new_session=False), agent_slug="billing"),
        ):
            result = await asyncio.wait_for(_copilot.run(preview, request), timeout=1)
            assert result.content == "synthetic reply"
        assert not first.done()
        release.set()
        assert (await first).content == "synthetic reply"
        assert (await _copilot.run(preview, _request(new_session=False))).content == "synthetic reply"
        assert client.resume_session.await_count == 4
        assert {call.args[0] for call in client.resume_session.await_args_list} == {
            "main-example", "main-other", "billing-example",
        }
    finally:
        release.set()
        await first
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_turn_aborts_disconnects_and_closes_only_its_adapter(
    preview, monkeypatch, cancel,
):
    import copilot

    started = asyncio.Event()
    order = []
    client = _fake_client()
    session = client.create_session.return_value
    original_open = _copilot.open_session_fs

    async def wait_forever(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    async def abort():
        order.append("abort")

    async def disconnect():
        order.append("disconnect")

    async def observe_open(*args, **kwargs):
        provider = await original_open(*args, **kwargs)
        original_close = provider.close

        async def close():
            order.append("close")
            await original_close()

        provider.close = close
        return provider

    session.send_and_wait.side_effect = wait_forever
    session.abort.side_effect = abort
    session.disconnect.side_effect = disconnect
    monkeypatch.setattr(_copilot, "open_session_fs", observe_open)
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    request = _request()
    if not cancel:
        request = replace(request, deadline=asyncio.get_running_loop().time() + 0.1)
    task = asyncio.create_task(_copilot.run(preview, request))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(CopilotPreviewError, match="deadline"):
                await asyncio.wait_for(task, timeout=2)
        assert order == ["abort", "disconnect", "close"]
        client.stop.assert_not_awaited()
        session.send_and_wait.side_effect = None
        peer = await _copilot.run(preview, replace(_request(), session_id="peer"))
        assert peer.content == "synthetic reply"
        client.start.assert_awaited_once()
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_failed_adapter_close_is_reported_without_owner_retention(preview, monkeypatch):
    import copilot

    client = _fake_client()
    original_open = _copilot.open_session_fs
    providers = []

    async def open_with_close_fault(*args, **kwargs):
        provider = await original_open(*args, **kwargs)
        provider.backend.close = AsyncMock(side_effect=[
            OSError(errno.EIO, "private close failure"), None,
        ])
        providers.append(provider)
        return provider

    monkeypatch.setattr(_copilot, "open_session_fs", open_with_close_fault)
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    owner = _runtime.get_runtime(preview)
    with pytest.raises(CopilotPreviewError, match="storage"):
        await _copilot.run(preview, _request())
    assert owner._client is client
    client.create_session.return_value.disconnect.assert_awaited_once()
    providers[0].backend.close.assert_awaited()
    assert providers[0].backend.close.await_count == 1
    assert not providers[0]._closed
    assert preview._resources.runtime is owner
    monkeypatch.setattr(_copilot, "open_session_fs", original_open)
    peer = await _copilot.run(preview, replace(_request(), session_id="peer"))
    assert peer.content == "synthetic reply"
    await _lifecycle._shutdown_harnesses()
    client.stop.assert_awaited_once()
    assert preview._resources.runtime is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["success", "sdk_timeout", "send_failure", "empty", "no_reply", "late_token"]
)
async def test_send_scope_observes_usage_once_and_unsubscribes(preview, monkeypatch, outcome):
    import copilot
    from copilot.session_events import (
        AssistantMessageData,
        AssistantUsageData,
        SessionEvent,
        SessionEventType,
    )

    def event(kind, data):
        return SessionEvent(data=data, id=uuid4(), timestamp=datetime.now(UTC), type=kind)

    selected = preview
    if outcome == "late_token":
        selected = replace(preview, provider=AzureOpenAIProvider("https://fixture.openai.azure.com"))
        monkeypatch.setattr(_runtime, "build_async_credential", Mock(return_value=SimpleNamespace(
            get_token=AsyncMock(side_effect=RuntimeError("private token failure")),
            close=AsyncMock(),
        )))
    client = _fake_client()
    session = client.create_session.return_value
    recorder = Mock()
    monkeypatch.setattr(_execution, "_AgentUsageRecorder", Mock(return_value=recorder))
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))

    async def metadata():
        assert session.handlers == []
        session.on.assert_not_called()
        return SimpleNamespace(tools=[])

    async def send(*_args, **_kwargs):
        assert len(session.handlers) == 1
        for data in (
            AssistantUsageData(model="fixture", input_tokens=2, output_tokens=3),
            AssistantUsageData(model="fixture", input_tokens=5, output_tokens=7),
        ):
            session.handlers[0](event(SessionEventType.ASSISTANT_USAGE, data))
        session.handlers[0](event(
            SessionEventType.ASSISTANT_MESSAGE,
            AssistantMessageData(content="intermediate", message_id="intermediate"),
        ))
        if outcome == "sdk_timeout":
            raise TimeoutError("SDK wait expired without aborting")
        if outcome == "send_failure":
            raise RuntimeError("private send failure")
        if outcome == "late_token":
            callback = client.create_session.call_args.kwargs["provider"]["bearer_token_provider"]
            with pytest.raises(CopilotPreviewError):
                await callback(SimpleNamespace())
        if outcome == "no_reply":
            return None
        final = event(
            SessionEventType.ASSISTANT_MESSAGE,
            AssistantMessageData(
                content="   " if outcome == "empty" else "final reply", message_id="final",
            ),
        )
        session.handlers[0](final)
        return final

    async def disconnect():
        assert session.handlers == []

    session.rpc.tools.get_current_metadata.side_effect = metadata
    session.send_and_wait.side_effect = send
    session.disconnect.side_effect = disconnect
    try:
        if outcome == "success":
            result = await _copilot.run(selected, _request())
            assert result.content == "final reply"
            assert result.content_intermediate == ["intermediate"]
        else:
            diagnostic = {
                "sdk_timeout": "deadline",
                "send_failure": "No fallback",
                "empty": "no final model reply",
                "no_reply": "no final model reply",
                "late_token": "Entra token",
            }[outcome]
            with pytest.raises(CopilotPreviewError, match=diagnostic):
                await _copilot.run(selected, _request())
        assert "on_event" not in client.create_session.call_args.kwargs
        session.on.assert_called_once()
        session.unsubscribe.assert_called_once()
        recorder.emit_counts.assert_called_once_with(input_tokens=7, output_tokens=10)
        assert session.abort.await_count == (outcome in {"sdk_timeout", "send_failure"})
        session.disconnect.assert_awaited_once()
        client.stop.assert_not_awaited()
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["filesystem", "create", "catalog", "subscribe"])
async def test_failures_before_send_do_not_publish_usage_or_abort(preview, monkeypatch, boundary):
    import copilot

    client = _fake_client()
    session = client.create_session.return_value
    recorder = Mock()
    monkeypatch.setattr(_execution, "_AgentUsageRecorder", Mock(return_value=recorder))
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    if boundary == "filesystem":
        monkeypatch.setattr(_copilot, "open_session_fs", AsyncMock(
            side_effect=CopilotSessionError(errno.EIO, "Configured storage failed."),
        ))
    elif boundary == "create":
        client.create_session.side_effect = RuntimeError("private create failure")
    elif boundary == "catalog":
        session.rpc.tools.get_current_metadata.return_value = SimpleNamespace(tools=None)
    else:
        session.on.side_effect = RuntimeError("private observer failure")
    try:
        with pytest.raises((CopilotPreviewError, CopilotSessionError)):
            await _copilot.run(preview, _request())
        session.send_and_wait.assert_not_awaited()
        session.abort.assert_not_awaited()
        recorder.emit_counts.assert_not_called()
        session.unsubscribe.assert_not_called()
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_cleanup_failures_preserve_original_request_cancellation(preview, monkeypatch):
    import copilot

    client = _fake_client()
    session = client.create_session.return_value
    original = asyncio.CancelledError("request cancelled")
    session.send_and_wait.side_effect = original
    session.abort.side_effect = RuntimeError("private abort failure")
    session.disconnect.side_effect = RuntimeError("private detach failure")
    opened = []
    original_open = _copilot.open_session_fs

    async def open_with_failure(*args, **kwargs):
        provider = await original_open(*args, **kwargs)
        provider.backend.close = AsyncMock(side_effect=[OSError(errno.EIO, "private close failure"), None])
        opened.append(provider)
        return provider

    monkeypatch.setattr(_copilot, "open_session_fs", open_with_failure)
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    owner = _runtime.get_runtime(preview)
    with pytest.raises(asyncio.CancelledError) as caught:
        await _copilot.run(preview, _request())
    assert caught.value is original
    session.abort.assert_awaited_once()
    session.unsubscribe.assert_called_once()
    session.disconnect.assert_awaited_once()
    assert owner._client is client
    assert opened[0].backend.close.await_count == 1
    client.stop.assert_not_awaited()
    await _lifecycle._shutdown_harnesses()
    assert not opened[0]._closed
    assert preview._resources.runtime is None


@pytest.mark.asyncio
@pytest.mark.parametrize("send_fails", [False, True])
async def test_observer_cleanup_failure_does_not_replace_send_failure(preview, monkeypatch, send_fails):
    import copilot

    client = _fake_client()
    session = client.create_session.return_value
    unsubscribe = Mock(side_effect=RuntimeError("private unsubscribe failure"))
    session.on = Mock(return_value=unsubscribe)
    original = CopilotPreviewError("Original request failure.")
    if send_fails:
        session.send_and_wait.side_effect = original
    recorder = Mock()
    monkeypatch.setattr(_execution, "_AgentUsageRecorder", Mock(return_value=recorder))
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        with pytest.raises(CopilotPreviewError) as caught:
            await _copilot.run(preview, _request())
        if send_fails:
            assert caught.value is original
        else:
            assert "private unsubscribe" not in str(caught.value)
        unsubscribe.assert_called_once()
        recorder.emit_counts.assert_called_once_with(input_tokens=None, output_tokens=None)
        session.disconnect.assert_awaited_once()
        assert session.abort.await_count == send_fails
    finally:
        await _lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_real_sdk_wait_timeout_requires_the_host_abort():
    from copilot.session import CopilotSession

    client = SimpleNamespace(request=AsyncMock(return_value={"messageId": "fixture-message"}))
    session = CopilotSession("fixture-session", client)
    with pytest.raises(TimeoutError):
        await session.send_and_wait("offline prompt", timeout=0.01)
    assert [call.args[0] for call in client.request.await_args_list] == ["session.send"]
    assert not session._event_handlers
    await _copilot._abort(session)
    assert [call.args[0] for call in client.request.await_args_list] == [
        "session.send", "session.abort",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "send_failure", "setup_failure", "cancelled"])
async def test_invocation_owned_cleanup_unsubscribes_before_disconnect_and_filesystem_close(
    preview, monkeypatch, outcome
):
    import copilot
    from copilot.session_events import (
        AssistantUsageData,
        SessionEventType,
        ToolExecutionCompleteData,
        ToolExecutionCompleteResult,
        ToolExecutionStartData,
    )

    client = _fake_client()
    session = client.create_session.return_value
    order = []
    opened = []
    original_open = _copilot.open_session_fs
    original_on = session.on.side_effect
    final = session.send_and_wait.return_value
    recorder = Mock()
    original_failure = CopilotPreviewError("original send failure")
    original_cancel = asyncio.CancelledError("original cancellation")

    async def observe_open(*args, **kwargs):
        provider = await original_open(*args, **kwargs)
        original_close = provider.close

        async def close():
            assert session.handlers == []
            order.append("filesystem_close")
            await original_close()

        provider.close = close
        opened.append(provider)
        return provider

    def subscribe(handler):
        order.append("subscribe")
        unsubscribe = original_on(handler)

        def release():
            order.append("unsubscribe")
            unsubscribe()

        return release

    async def send(*args, **kwargs):
        order.append("send")
        [observer] = session.handlers
        observer(_sdk_event(
            SessionEventType.ASSISTANT_USAGE,
            AssistantUsageData(model="fixture", input_tokens=2, output_tokens=3),
        ))
        observer(_sdk_event(
            SessionEventType.TOOL_EXECUTION_START,
            ToolExecutionStartData(
                tool_call_id="denied-view", tool_name="view", arguments={"path": "denied"}
            ),
        ))
        observer(_sdk_event(
            SessionEventType.TOOL_EXECUTION_COMPLETE,
            ToolExecutionCompleteData(
                tool_call_id="denied-view", success=False,
                result=ToolExecutionCompleteResult(content="private denied detail"),
            ),
        ))
        if outcome == "send_failure":
            raise original_failure
        if outcome == "cancelled":
            raise original_cancel
        observer(final)
        return final

    async def abort():
        order.append("abort")

    async def disconnect():
        assert session.handlers == []
        assert not opened[0]._closed
        order.append("disconnect")

    session.on = Mock(side_effect=subscribe)
    session.send_and_wait.side_effect = send
    session.abort.side_effect = abort
    session.disconnect.side_effect = disconnect
    if outcome == "setup_failure":
        session.rpc.tools.get_current_metadata.return_value = SimpleNamespace(tools=None)
    monkeypatch.setattr(_copilot, "open_session_fs", observe_open)
    monkeypatch.setattr(_execution, "_AgentUsageRecorder", Mock(return_value=recorder))
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        if outcome == "success":
            result = await _copilot.run(preview, _request())
            assert len(result.tool_calls) == 1
            assert result.tool_calls[0]["tool_call_id"] == "denied-view"
            assert result.tool_calls[0]["success"] is False
            assert "private denied detail" not in repr(result.tool_calls)
        else:
            error = asyncio.CancelledError if outcome == "cancelled" else CopilotPreviewError
            with pytest.raises(error) as caught:
                await _copilot.run(preview, _request())
            if outcome == "send_failure":
                assert caught.value is original_failure
            elif outcome == "cancelled":
                assert caught.value is original_cancel
        expected = (
            ["disconnect", "filesystem_close"]
            if outcome == "setup_failure"
            else ["subscribe", "send"]
            + (["abort"] if outcome in {"send_failure", "cancelled"} else [])
            + ["unsubscribe", "disconnect", "filesystem_close"]
        )
        assert order == expected
        assert opened[0]._closed
        if outcome == "setup_failure":
            recorder.emit_counts.assert_not_called()
        else:
            recorder.emit_counts.assert_called_once_with(input_tokens=2, output_tokens=3)
        client.stop.assert_not_awaited()
        assert preview._resources.runtime._client is client
    finally:
        await _lifecycle._shutdown_harnesses()
