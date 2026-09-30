from __future__ import annotations

import asyncio
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

from azure_functions_agents import _copilot, _harness, runner
from azure_functions_agents._copilot_providers import (
    AzureOpenAIProvider,
    FoundryProvider,
    OpenAIProvider,
)
from azure_functions_agents._function_tool import tool, workflow_tool
from azure_functions_agents._harness import (
    AppHarness,
    CopilotPreviewError,
    HarnessKind,
    HarnessRequest,
    ProviderKind,
    UnsupportedCapabilityError,
)
from azure_functions_agents.app import create_function_app
from azure_functions_agents.client_manager import (
    ClientManager,
    get_client_manager,
    set_client_manager,
)
from azure_functions_agents.config import paths

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "copilot-preview" / "src"


@pytest.fixture
def preview(monkeypatch, tmp_path):
    monkeypatch.setattr(_copilot, "_RUNTIMES", {})
    return AppHarness(
        HarnessKind.COPILOT,
        tmp_path,
        tmp_path / "state",
        "gpt-4.1-mini",
        OpenAIProvider("not-a-credential"),
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
        new_session=new_session, model="gpt-4.1-mini", tools=[], max_output_tokens=None,
        deadline=asyncio.get_running_loop().time() + 10,
    )


def _fake_client():
    from copilot.session_events import (
        AssistantMessageData,
        AssistantTurnEndData,
        SessionEvent,
        SessionEventType,
        UserMessageData,
    )

    def event(event_type, data):
        return SessionEvent(data=data, id=uuid4(), timestamp=datetime.now(UTC), type=event_type)

    session = AsyncMock()
    session.rpc = SimpleNamespace(tools=SimpleNamespace(
        get_current_metadata=AsyncMock(return_value=SimpleNamespace(tools=[]))
    ))
    session.send_and_wait.return_value = event(
        SessionEventType.ASSISTANT_MESSAGE,
        AssistantMessageData(content="synthetic reply", message_id="fixture"),
    )
    session.get_events.return_value = [
        event(SessionEventType.USER_MESSAGE, UserMessageData(content="hello")),
        event(SessionEventType.ASSISTANT_TURN_END, AssistantTurnEndData(turn_id="fixture")),
    ]
    return SimpleNamespace(
        start=AsyncMock(), stop=AsyncMock(), force_stop=AsyncMock(),
        get_session_metadata=AsyncMock(return_value=None),
        create_session=AsyncMock(return_value=session),
        resume_session=AsyncMock(return_value=session),
    )


async def _invoke_native_tool(function, arguments):
    from copilot.tools import ToolInvocation

    calls = []
    native_tool = _copilot._tool(function, calls)
    assert native_tool.handler is not None
    result = await native_tool.handler(
        ToolInvocation(
            session_id="native-session",
            tool_call_id="call-1",
            tool_name=function.name,
            arguments=arguments,
        )
    )
    return result, calls


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

    [function] = _harness.prepare_tools([candidate])
    result, calls = await _invoke_native_tool(function, {"value": "ok"})

    assert result.result_type == "success"
    assert result.text_result_for_llm == f"{'async' if is_async else 'sync'}:ok"
    assert effects == ["ok"]
    assert calls[0]["result"] == result.text_result_for_llm


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


@pytest.mark.asyncio
async def test_tool_adapter_supports_both_workflow_decorator_orders():
    def tool_outer(value: str) -> str:
        return f"outer:{value}"

    def workflow_outer(value: str) -> str:
        return f"workflow:{value}"

    first = tool(name="tool_outer")(workflow_tool(tool_outer))
    second = workflow_tool(tool(name="workflow_outer")(workflow_outer))
    prepared = _harness.prepare_tools([first, second])

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


@pytest.mark.asyncio
async def test_tool_adapter_propagates_cancellation():
    started = asyncio.Event()

    @tool
    async def wait_forever() -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    from copilot.tools import ToolInvocation

    calls = []
    native_tool = _copilot._tool(wait_forever, calls)
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
    assert calls == [
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
    from copilot.generated.rpc import CurrentToolMetadata

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    functions = [
        tool(name="user_tool")(lambda: "user"),
        tool(name="execute_python")(lambda code: code),
        tool(name="web_request")(lambda url: url),
    ]
    client = _fake_client()
    client.create_session.return_value.rpc.tools.get_current_metadata.return_value = SimpleNamespace(
        tools=[
            CurrentToolMetadata(description="", name=function.name)
            for function in functions
        ]
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
        await _copilot.shutdown()


@pytest.mark.asyncio
async def test_sdk_rejects_ambient_catalog_before_prompt(preview, monkeypatch):
    import copilot
    from copilot.generated.rpc import CurrentToolMetadata

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    function = tool(name="allowed_tool")(lambda: "ok")
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
        with pytest.raises(CopilotPreviewError, match="tool catalog differs"):
            await _copilot.run(preview, replace(_request(), tools=[function]))
        session.send_and_wait.assert_not_awaited()
    finally:
        await _copilot.shutdown()


@pytest.mark.asyncio
async def test_failed_start_uses_sdk_stop_and_force_stop_then_retries(preview, monkeypatch):
    import copilot
    from copilot import RuntimeConnection

    failed = _fake_client()
    failed.start.side_effect = RuntimeError("native failed")
    failed.stop.side_effect = TimeoutError("stop stalled")
    recovered = _fake_client()
    clients = iter([failed, recovered])
    factory = Mock(side_effect=lambda **_: next(clients))
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    owner = _copilot._runtime(preview)
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
        await _copilot.shutdown()
    recovered.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_closes_every_app_owner_even_if_one_fails(preview, monkeypatch):
    first = _copilot._runtime(preview)
    second = _copilot._runtime(replace(preview, storage_root=preview.storage_root / "other"))
    assert _copilot._runtime(preview) is first
    failed_close = AsyncMock(side_effect=RuntimeError("stop failed"))
    good_close = AsyncMock()
    monkeypatch.setattr(first, "close", failed_close)
    monkeypatch.setattr(second, "close", good_close)
    with suppress(Exception):
        await _copilot.shutdown()
    failed_close.assert_awaited_once()
    good_close.assert_awaited_once()
    assert not _copilot._RUNTIMES


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
    monkeypatch.setattr(_copilot, "build_async_credential", lambda: credential)
    client = _fake_client()
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        result = await _copilot.run(selected, _request())
        resumed = await _copilot.run(selected, _request(new_session=False))
        assert result.content == "synthetic reply"
        assert resumed.content == "synthetic reply"
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
        await _copilot.shutdown()


@pytest.mark.asyncio
async def test_openai_provider_callback_uses_frozen_startup_key(preview, monkeypatch):
    owner = _copilot._runtime(preview)
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-one")
    provider = _copilot._provider(preview, owner, "per-agent-model")

    assert provider == {
        "type": "openai",
        "wire_api": "responses",
        "base_url": "https://api.openai.com/v1",
        "model_id": "per-agent-model",
        "wire_model": "per-agent-model",
        "bearer_token_provider": provider["bearer_token_provider"],
    }
    callback = provider["bearer_token_provider"]
    assert await callback(SimpleNamespace()) == "not-a-credential"
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-two")
    assert await callback(SimpleNamespace()) == "not-a-credential"


def test_azure_api_key_provider_avoids_credential_construction(preview, monkeypatch):
    selected = replace(
        preview,
        provider=AzureOpenAIProvider(
            "https://fixture.openai.azure.com",
            "2024-10-21",
            "sentinel-not-a-secret",
        ),
    )
    monkeypatch.setattr(
        _copilot,
        "build_async_credential",
        Mock(side_effect=AssertionError("Azure API key must not build a credential")),
    )

    provider = _copilot._provider(selected, _copilot._runtime(selected), "azure-deployment")

    assert provider == {
        "type": "azure",
        "wire_api": "responses",
        "base_url": "https://fixture.openai.azure.com",
        "model_id": "azure-deployment",
        "wire_model": "azure-deployment",
        "azure": {"api_version": "2024-10-21"},
        "api_key": "sentinel-not-a-secret",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "endpoint", "scope"),
    [
        (
            AzureOpenAIProvider,
            "https://fixture.openai.azure.com",
            "https://cognitiveservices.azure.com/.default",
        ),
        (
            FoundryProvider,
            "https://fixture.services.ai.azure.com/api/projects/test",
            "https://ai.azure.com/.default",
        ),
    ],
)
async def test_entra_callbacks_refresh_overlap_and_close(
    preview, monkeypatch, provider, endpoint, scope
):
    selected = replace(preview, provider=provider(endpoint))
    issued = 0

    async def get_token(received_scope):
        nonlocal issued
        assert received_scope == scope
        issued += 1
        current = issued
        await asyncio.sleep(0)
        return AccessToken(f"sentinel-token-{current}", 9999999999)

    credential = SimpleNamespace(get_token=AsyncMock(side_effect=get_token), close=AsyncMock())
    build = Mock(return_value=credential)
    monkeypatch.setattr(_copilot, "build_async_credential", build)
    owner = _copilot._runtime(selected)
    config = _copilot._provider(selected, owner, "deployment")
    callback = config["bearer_token_provider"]

    first = await callback(SimpleNamespace())
    second = await callback(SimpleNamespace())
    overlapped = await asyncio.gather(callback(SimpleNamespace()), callback(SimpleNamespace()))
    await owner.close()

    assert first == "sentinel-token-1"
    assert second == "sentinel-token-2"
    assert set(overlapped) == {"sentinel-token-3", "sentinel-token-4"}
    assert credential.get_token.await_count == 4
    build.assert_called_once_with()
    credential.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_entra_callback_failure_is_sanitized(preview, monkeypatch):
    selected = replace(
        preview,
        provider=AzureOpenAIProvider("https://fixture.openai.azure.com"),
    )
    credential = SimpleNamespace(
        get_token=AsyncMock(side_effect=RuntimeError("sentinel-private-credential-detail")),
        close=AsyncMock(),
    )
    monkeypatch.setattr(_copilot, "build_async_credential", Mock(return_value=credential))
    owner = _copilot._runtime(selected)
    callback = _copilot._provider(selected, owner, "deployment")["bearer_token_provider"]
    try:
        with pytest.raises(CopilotPreviewError, match=r"Azure OpenAI.*Entra") as error:
            await callback(SimpleNamespace())
        assert "sentinel-private-credential-detail" not in str(error.value)
    finally:
        await owner.close()


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
    monkeypatch.setattr(_copilot, "build_async_credential", Mock(return_value=credential))
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
        await _copilot.shutdown()

    assert isinstance(bad_result, CopilotPreviewError)
    assert "Azure OpenAI" in str(bad_result)
    assert "Entra token" in str(bad_result)
    assert "sentinel-private-credential-detail" not in str(bad_result)
    assert good_result.content == "synthetic reply"


def _registered_azure_chat(monkeypatch, tmp_path):
    root = tmp_path / "app"
    shutil.copytree(SAMPLE, root)
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    monkeypatch.setattr(_copilot, "_RUNTIMES", {})
    monkeypatch.setattr(paths, "_app_root", root)
    monkeypatch.setenv(_harness.FLAG, "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fixture.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WEBSITE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("FUNCTIONS_WORKER_PROCESS_COUNT", raising=False)
    app = create_function_app(root)
    return next(
        function.get_user_function()
        for function in app.get_functions()
        if function.get_function_name() == "agent_main_builtin_chat"
    )


@pytest.mark.asyncio
async def test_credential_constructor_failure_is_sanitized_at_public_route(
    monkeypatch, tmp_path, caplog
):
    import copilot

    sentinel = "sentinel-private-credential-constructor"
    build = Mock(side_effect=RuntimeError(sentinel))
    native = Mock(side_effect=AssertionError("Native runtime must not start"))
    maf = AsyncMock(side_effect=AssertionError("MAF fallback must not run"))
    monkeypatch.setattr(_copilot, "build_async_credential", build)
    monkeypatch.setattr(copilot, "CopilotClient", native)
    monkeypatch.setattr(runner, "_build_agent_session", maf)
    chat = _registered_azure_chat(monkeypatch, tmp_path)
    try:
        response = await chat(
            SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "hello"}))
        )
    finally:
        await _copilot.shutdown()

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
    monkeypatch.setattr(_copilot, "build_async_credential", Mock(return_value=credential))
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    monkeypatch.setattr(runner, "_build_agent_session", maf)
    chat = _registered_azure_chat(monkeypatch, tmp_path)
    try:
        response = await chat(
            SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "hello"}))
        )
    finally:
        await _copilot.shutdown()

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
    monkeypatch.setattr(runner, "_build_agent_session", maf)
    chat = _registered_azure_chat(monkeypatch, tmp_path)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sentinel-private-api-key")
    try:
        response = await chat(
            SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "hello"}))
        )
    finally:
        await _copilot.shutdown()

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
        await _copilot.shutdown()

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
    monkeypatch.setattr(_copilot, "_runtime", native)

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
        await _copilot.shutdown()


@pytest.mark.asyncio
async def test_sdk_transient_resume_failure_can_retry_same_session(preview, monkeypatch):
    import copilot

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    client = _fake_client()
    session = client.resume_session.return_value
    client.resume_session.side_effect = [RuntimeError("temporary native error"), session]
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        with pytest.raises(CopilotPreviewError, match="could not resume"):
            await _copilot.run(preview, _request(new_session=False))
        retry = await _copilot.run(preview, _request(new_session=False))
        assert retry.session_id == "example"
        assert retry.content == "synthetic reply"
        assert client.resume_session.await_count == 2
        client.create_session.assert_not_awaited()
    finally:
        await _copilot.shutdown()


@pytest.mark.asyncio
async def test_sdk_metadata_rejects_existing_id_before_create(preview, monkeypatch):
    import copilot

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    client = _fake_client()
    client.get_session_metadata.return_value = SimpleNamespace(session_id="existing")
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        with pytest.raises(CopilotPreviewError, match="already exists"):
            await _copilot.run(preview, _request())
        client.get_session_metadata.assert_awaited_once()
        client.create_session.assert_not_awaited()
        client.resume_session.assert_not_awaited()
    finally:
        await _copilot.shutdown()


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


def test_sdk_events_require_a_completed_uninterrupted_turn():
    from copilot.session_events import (
        AssistantTurnEndData,
        SessionErrorData,
        SessionEvent,
        SessionEventType,
        SessionIdleData,
        UserMessageData,
    )

    def event(event_type, data):
        return SessionEvent(data=data, id=uuid4(), timestamp=datetime.now(UTC), type=event_type)

    user = event(SessionEventType.USER_MESSAGE, UserMessageData(content="hello"))
    finished = event(
        SessionEventType.ASSISTANT_TURN_END, AssistantTurnEndData(turn_id="finished")
    )
    aborted = event(SessionEventType.SESSION_IDLE, SessionIdleData(aborted=True))
    error = event(
        SessionEventType.SESSION_ERROR,
        SessionErrorData(error_type="provider", message="synthetic failure"),
    )

    assert _copilot._completed_turn([user, finished])
    assert not _copilot._completed_turn([])
    assert not _copilot._completed_turn([user])
    assert not _copilot._completed_turn([user, finished, user])
    assert not _copilot._completed_turn([user, aborted, finished])
    assert not _copilot._completed_turn([user, finished, error])
