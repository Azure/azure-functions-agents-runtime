"""Real pinned native runtime, synthetic HTTP provider. This is NOT live model evidence."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import get_args, get_type_hints
from unittest.mock import AsyncMock

import httpx
import pytest
from azure.core.credentials import AccessToken

from azure_functions_agents import _copilot, _harness, runner, shutdown_client_manager
from azure_functions_agents.app import create_function_app
from azure_functions_agents.config import paths

pytestmark = pytest.mark.skipif(
    os.environ.get("AZURE_FUNCTIONS_AGENTS_TEST_NATIVE_COPILOT") != "1",
    reason="Explicit native-runtime qualification only; never downloads or calls a real provider.",
)

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "copilot-preview" / "src"
CACHE = Path(__file__).resolve().parents[1] / ".tmp-validation" / "runtime-1.0.85"
SENTINEL = "not-a-credential-native-persistence-sentinel"
AUTH_FAILURE_SENTINEL = "sentinel-private-native-provider-403"


def _tag(user: str) -> str:
    match = re.search(r"""tag\s+\\*['"]([A-Za-z0-9_-]+)\\*['"]""", user)
    assert match is not None, "Synthetic provider needs the first-turn tag"
    return match[1]


def _responses_fixture(body, item, request):
    response = {
        "id": "resp_fixture", "object": "response", "created_at": 0, "model": body["model"],
        "status": "completed", "output": [item],
        "usage": {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17},
    }
    if not body.get("stream"):
        return httpx.Response(200, json=response, request=request)
    started = dict(response, status="in_progress", output=[])
    events = [{"type": "response.created", "response": started}]
    empty_item = dict(item, arguments="") if item["type"] == "function_call" else dict(item, content=[])
    events.append({"type": "response.output_item.added", "output_index": 0, "item": empty_item})
    if item["type"] == "function_call":
        events.extend([
            {"type": "response.function_call_arguments.delta", "item_id": item["id"],
             "output_index": 0, "delta": item["arguments"]},
            {"type": "response.function_call_arguments.done", "item_id": item["id"],
             "output_index": 0, "arguments": item["arguments"]},
        ])
    else:
        part = item["content"][0]
        events.extend([
            {"type": "response.content_part.added", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "part": dict(part, text="")},
            {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "delta": part["text"]},
            {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "text": part["text"]},
            {"type": "response.content_part.done", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "part": part},
        ])
    events.extend([
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ])
    content = "".join(
        f"event: {event['type']}\ndata: {json.dumps(dict(event, sequence_number=index))}\n\n"
        for index, event in enumerate(events)
    )
    return httpx.Response(
        200, content=content, headers={"content-type": "text/event-stream"}, request=request
    )


@pytest.fixture(
    params=[
        "openai",
        "azure_openai_entra_versionless",
        "azure_openai_api_key_versioned",
        "foundry",
    ]
)
def native(monkeypatch, tmp_path, request):
    import copilot
    from copilot._cli_version import get_runtime_platform
    from copilot.copilot_request_handler import CopilotRequestHandler
    from copilot.session import CopilotSession, ProviderConfig

    sdk_client = copilot.CopilotClient
    bundle = CACHE / "prebuilds" / get_runtime_platform()
    wrapper = "copilot-runtime.exe" if os.name == "nt" else "copilot-runtime"
    if not all((bundle / name).is_file() for name in (
        wrapper, "runtime.node", ".hostless-runtime-assets-v2"
    )):
        pytest.skip("Pinned native assets are not cached; never fetch them in tests.")
    monkeypatch.setenv("COPILOT_CLI_EXTRACT_DIR", str(CACHE))
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.delenv("COPILOT_CLI_PATH", raising=False)
    monkeypatch.delenv("COPILOT_SDK_DEFAULT_CONNECTION", raising=False)

    captured = []
    clients = []
    created = []
    metadata_lookups = []
    resumed = []
    histories = []
    stalled = asyncio.Event()
    cancelled = asyncio.Event()
    provider_types = get_type_hints(ProviderConfig)
    provider_case = request.param
    provider_name = (
        "azure_openai" if provider_case.startswith("azure_openai_") else provider_case
    )
    azure_api_key = provider_case == "azure_openai_api_key_versioned"
    azure_api_version = "2024-10-21" if azure_api_key else None

    def check_options(options):
        provider = options["provider"]
        assert provider["type"] in get_args(provider_types["type"])
        assert provider["type"] == ("azure" if provider_name == "azure_openai" else "openai")
        assert provider["wire_api"] in get_args(provider_types["wire_api"])
        assert provider["wire_api"] == (
            "responses" if provider_name == "foundry" else "completions"
        )
        assert provider["model_id"] == "gpt-4.1-mini"
        assert provider["wire_model"] == "gpt-4.1-mini"
        assert options["model"] == "gpt-4.1-mini"
        assert options["available_tools"] == ["custom:make_receipt"]
        if provider_name == "openai":
            assert provider["base_url"] == "https://api.openai.com/v1"
        elif provider_name == "foundry":
            assert (
                provider["base_url"]
                == "https://fixture.services.ai.azure.com/api/projects/test/openai/v1"
            )
        else:
            assert provider["base_url"] == "https://fixture.openai.azure.com"
            if azure_api_version is None:
                assert "azure" not in provider
            else:
                assert provider["azure"] == {"api_version": azure_api_version}
            if azure_api_key:
                assert provider["api_key"] == SENTINEL
                assert "bearer_token_provider" not in provider
            else:
                assert "api_key" not in provider
                assert callable(provider["bearer_token_provider"])

    class ObservedClient(sdk_client):
        async def get_session_metadata(self, session_id):
            metadata_lookups.append(session_id)
            return await super().get_session_metadata(session_id)

        async def create_session(self, **options):
            check_options(options)
            created.append(options["session_id"])
            return await super().create_session(**options)

        async def resume_session(self, session_id, **options):
            check_options(options)
            resumed.append(session_id)
            return await super().resume_session(session_id, **options)

    original_send_and_wait = CopilotSession.send_and_wait

    async def record_events(session, *args, **kwargs):
        response = await original_send_and_wait(session, *args, **kwargs)
        histories.append(await session.get_events())
        return response

    monkeypatch.setattr(CopilotSession, "send_and_wait", record_events)

    class SyntheticProvider(CopilotRequestHandler):
        async def send_request(self, request, context):
            expected_route = {
                "openai": ("api.openai.com", "/v1/chat/completions", {}),
                "azure_openai_entra_versionless": (
                    "fixture.openai.azure.com",
                    "/openai/v1/chat/completions",
                    {},
                ),
                "azure_openai_api_key_versioned": (
                    "fixture.openai.azure.com",
                    "/openai/deployments/gpt-4.1-mini/chat/completions",
                    {"api-version": "2024-10-21"},
                ),
                "foundry": (
                    "fixture.services.ai.azure.com",
                    "/api/projects/test/openai/v1/responses",
                    {},
                ),
            }[provider_case]
            assert request.url.host == expected_route[0]
            assert request.url.path == expected_route[1]
            assert dict(request.url.params) == expected_route[2]
            headers = dict(request.headers)
            bearer = "Bearer " + SENTINEL
            expected_auth = {
                "openai": ("authorization", bearer),
                "azure_openai_entra_versionless": ("authorization", bearer),
                "azure_openai_api_key_versioned": ("api-key", SENTINEL),
                "foundry": ("authorization", bearer),
            }[provider_case]
            raw_header_names = {name.decode("ascii") for name, _value in request.headers.raw}
            assert expected_auth[0] in raw_header_names
            assert headers[expected_auth[0]] == expected_auth[1]
            if provider_case == "azure_openai_api_key_versioned":
                assert "authorization" not in headers
                assert "authorization" not in raw_header_names
            body = json.loads(await request.aread())
            captured.append(body)
            responses = request.url.path.endswith("/responses")
            if responses:
                assert body.get("store") is False, "SDK Responses must disable provider retention"
            assert body["model"] == "gpt-4.1-mini"
            tools = [item["name"] if responses else item["function"]["name"] for item in body.get("tools", [])]
            assert tools == ["make_receipt"], f"Unexpected model-visible tools: {tools}"
            declaration = body["tools"][0] if responses else body["tools"][0]["function"]
            assert "harmless demonstration tag" in declaration["description"]
            assert declaration["parameters"]["properties"]["tag"]["type"] == "string"
            assert declaration["parameters"]["required"] == ["tag"]
            messages = body["input"] if responses else body["messages"]
            users = [item for item in messages if item.get("role") == "user"]
            user = str(users[-1]["content"])
            if "PROVIDER_AUTH_FAILURE" in user:
                return httpx.Response(
                    403,
                    json={"error": {"message": AUTH_FAILURE_SENTINEL}},
                    request=request,
                )
            if "STALL_NATIVE_TEST" in user:
                stalled.set()
                await asyncio.wait_for(context.cancel_event.wait(), timeout=20)
                cancelled.set()
                return httpx.Response(499, request=request)
            results = [
                item for item in messages
                if item.get("type") == "function_call_output" or item.get("role") == "tool"
            ]
            if responses:
                if results:
                    item = {"id": "msg_fixture", "type": "message", "role": "assistant",
                            "status": "completed", "content": [{
                                "type": "output_text", "text": results[-1]["output"], "annotations": [],
                            }]}
                else:
                    item = {"id": "fc_fixture", "type": "function_call", "call_id": "call_receipt",
                            "name": "make_receipt", "arguments": json.dumps({"tag": _tag(user)}),
                            "status": "completed"}
                return _responses_fixture(body, item, request)
            if results:
                message = {"role": "assistant", "content": results[-1]["content"]}
                finish = "stop"
            else:
                message = {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "call_receipt", "type": "function",
                        "function": {"name": "make_receipt", "arguments": json.dumps({"tag": _tag(user)})},
                    }],
                }
                finish = "tool_calls"
            usage = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
            if body.get("stream"):
                delta = dict(message)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [dict(delta["tool_calls"][0], index=0)]
                chunks = [
                    {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                     "model": body["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                     "model": body["model"], "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                     "usage": usage},
                ]
                content = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
                return httpx.Response(
                    200, content=content, headers={"content-type": "text/event-stream"}, request=request
                )
            return httpx.Response(200, json={
                "id": "fixture", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage,
            }, request=request)

        async def open_websocket(self, context):
            raise AssertionError("The preview must use HTTP, not a WebSocket provider")

    provider = SyntheticProvider()

    def create_client(**kwargs):
        assert kwargs["mode"] == "empty"
        assert kwargs["use_logged_in_user"] is False
        assert kwargs["log_level"] == "none"
        assert "env" not in kwargs
        assert "request_handler" not in kwargs
        connection = kwargs.get("connection")
        assert connection is None or connection.path is None
        client = ObservedClient(**kwargs, request_handler=provider)
        clients.append(client)
        return client

    app_root = tmp_path / "app"
    shutil.copytree(SAMPLE, app_root)
    monkeypatch.setattr(copilot, "CopilotClient", create_client)
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    monkeypatch.setattr(_copilot, "_RUNTIMES", {})
    monkeypatch.setattr(paths, "_app_root", app_root)
    monkeypatch.setenv(_harness.FLAG, "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider_name)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fixture.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")
    if azure_api_key:
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", SENTINEL)
    else:
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    if azure_api_version is None:
        monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
    else:
        monkeypatch.setenv("AZURE_OPENAI_API_VERSION", azure_api_version)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://fixture.services.ai.azure.com/api/projects/test")
    monkeypatch.setenv("FOUNDRY_MODEL", "gpt-4.1-mini")
    credential = SimpleNamespace(
        get_token=AsyncMock(return_value=AccessToken(SENTINEL, 9999999999)), close=AsyncMock()
    )
    monkeypatch.setattr(_copilot, "build_async_credential", lambda: credential)
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-auth-must-not-be-forwarded")
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    for key in ("WEBSITE_INSTANCE_ID", "FUNCTIONS_WORKER_PROCESS_COUNT",
                "AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT", "AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY"):
        monkeypatch.delenv(key, raising=False)
    return SimpleNamespace(
        root=app_root, state=tmp_path / "state", captured=captured,
        clients=clients, created=created, metadata_lookups=metadata_lookups,
        resumed=resumed, histories=histories,
        stalled=stalled, cancelled=cancelled, provider=provider_name,
        auth="api_key" if azure_api_key else "entra", credential=credential,
    )


@pytest.mark.asyncio
async def test_real_native_markdown_tool_and_cold_runtime_resume(native):
    from copilot.session_events import (
        AssistantMessageData,
        AssistantTurnEndData,
        ToolExecutionCompleteData,
    )

    app = create_function_app(native.root)
    functions = {item.get_function_name(): item.get_user_function() for item in app.get_functions()}
    chat = functions["agent_main_builtin_chat"]
    try:
        first = await chat(SimpleNamespace(
            headers={}, json=AsyncMock(return_value={"prompt": "Call make_receipt with tag 'native-one'."}),
        ))
        assert first.status_code == 200, first.body.decode()
        result = json.loads(first.body)
        assert len(result["tool_calls"]) == 1
        receipt = result["tool_calls"][0]["result"]
        assert receipt in result["response"]
        assert len(native.clients) == 1
        assert len(native.created) == 1
        assert native.metadata_lookups == native.created
        assert any(isinstance(event.data, AssistantTurnEndData) for event in native.histories[0])
        assert any(
            isinstance(event.data, AssistantMessageData) and receipt in event.data.content
            for event in native.histories[0]
        )
        assert any(
            isinstance(event.data, ToolExecutionCompleteData) and event.data.success
            for event in native.histories[0]
        )
        await shutdown_client_manager()
        followup = await chat(SimpleNamespace(
            headers={"x-ms-session-id": result["session_id"]},
            json=AsyncMock(return_value={"prompt": "Recall the previous receipt, without tools."}),
        ))
        assert followup.status_code == 200, followup.body.decode()
        second = json.loads(followup.body)
        assert second["session_id"] == result["session_id"]
        assert receipt in second["response"]
        assert second["tool_calls"] == []
        assert len(native.clients) == 2
        assert native.resumed == native.created
        assert native.metadata_lookups == native.created
        assert len(native.captured) == 3
        prior = native.captured[-1].get("messages") or native.captured[-1]["input"]
        assert prior[-1]["role"] == "user"
        assert "native-one" not in str(prior[-1])
        assert any(item.get("role") == "tool" or item.get("type") == "function_call_output" for item in prior)
        assert any(isinstance(event.data, AssistantTurnEndData) for event in native.histories[1])
        if native.provider in {"azure_openai", "foundry"} and native.auth == "entra":
            native.credential.get_token.assert_awaited()
        else:
            native.credential.get_token.assert_not_awaited()
        for path in native.state.rglob("*"):
            if path.is_file():
                assert SENTINEL.encode() not in path.read_bytes(), (
                    f"Credential sentinel was persisted in {path.relative_to(native.state)}"
                )
    finally:
        await shutdown_client_manager()


@pytest.mark.asyncio
async def test_native_provider_auth_failure_is_sanitized_through_public_route(
    native, monkeypatch, caplog
):
    maf = AsyncMock(side_effect=AssertionError("MAF fallback must not run"))
    monkeypatch.setattr(runner, "_build_agent_session", maf)
    app = create_function_app(native.root)
    chat = next(
        item.get_user_function()
        for item in app.get_functions()
        if item.get_function_name() == "agent_main_builtin_chat"
    )
    try:
        response = await chat(
            SimpleNamespace(
                headers={},
                json=AsyncMock(return_value={"prompt": "PROVIDER_AUTH_FAILURE"}),
            )
        )
        body = response.body.decode()
        assert response.status_code == 500
        assert "provider" in body.lower() or "auth" in body.lower()
        assert AUTH_FAILURE_SENTINEL not in body
        assert SENTINEL not in body
        assert AUTH_FAILURE_SENTINEL not in caplog.text
        assert SENTINEL not in caplog.text
        maf.assert_not_awaited()
    finally:
        await shutdown_client_manager()


@pytest.mark.asyncio
async def test_native_startup_failure_preserves_completed_session(native, monkeypatch):
    app = create_function_app(native.root)
    chat = next(
        item.get_user_function() for item in app.get_functions()
        if item.get_function_name() == "agent_main_builtin_chat"
    )
    try:
        first = await chat(SimpleNamespace(
            headers={}, json=AsyncMock(return_value={"prompt": "Call make_receipt with tag 'startup-fault'."}),
        ))
        assert first.status_code == 200, first.body.decode()
        public_id = json.loads(first.body)["session_id"]
        await shutdown_client_manager()
        with monkeypatch.context() as patch:
            patch.setenv("COPILOT_CLI_EXTRACT_DIR", str(native.state / "missing-sdk-bundle"))
            failed = await chat(SimpleNamespace(
                headers={"x-ms-session-id": public_id},
                json=AsyncMock(return_value={"prompt": "Recall."}),
            ))
            assert failed.status_code == 500
            assert "not-a-credential" not in failed.body.decode()
        assert len(native.captured) == 2
        resumed = await chat(SimpleNamespace(
            headers={"x-ms-session-id": public_id}, json=AsyncMock(return_value={"prompt": "Recall."}),
        ))
        assert resumed.status_code == 200, resumed.body.decode()
        assert native.resumed == native.created
    finally:
        await shutdown_client_manager()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "empty", "malformed"])
async def test_missing_or_corrupt_sdk_journal_never_starts_over(native, damage):
    app = create_function_app(native.root)
    chat = next(
        item.get_user_function() for item in app.get_functions()
        if item.get_function_name() == "agent_main_builtin_chat"
    )
    try:
        first = await chat(SimpleNamespace(
            headers={}, json=AsyncMock(return_value={"prompt": "Call make_receipt with tag 'durable'."}),
        ))
        assert first.status_code == 200, first.body.decode()
        public_id = json.loads(first.body)["session_id"]
        await shutdown_client_manager()
        journals = list(native.state.rglob("events.jsonl"))
        assert len(journals) == 1, "SDK did not persist the completed conversation"
        journal = journals[0]
        if damage == "missing":
            journal.unlink()
        else:
            journal.write_text("" if damage == "empty" else "malformed\n", encoding="utf-8")
        failed = await chat(SimpleNamespace(
            headers={"x-ms-session-id": public_id},
            json=AsyncMock(return_value={"prompt": "Recall the receipt."}),
        ))
        assert failed.status_code == 500, failed.body.decode()
        error = json.loads(failed.body)["error"]
        assert "not-a-credential" not in error
        assert "session" in error.lower() or "conversation" in error.lower()
        assert len(native.captured) == 2
        assert len(native.created) == 1
    finally:
        await shutdown_client_manager()


@pytest.mark.asyncio
async def test_authored_preview_http_trigger_creates_and_resumes(native):
    agent = native.root / "main.agent.md"
    agent.write_text(
        agent.read_text(encoding="utf-8").replace(
            "tools: true\n",
            "tools: true\ninput_schema:\n  type: object\n  required: [prompt]\n"
            "  properties:\n    prompt:\n      type: string\n",
            1,
        ),
        encoding="utf-8",
    )
    app = create_function_app(native.root)
    authored = next(
        item for item in app.get_functions() if item.get_function_name() == "main"
    )
    assert "preview" in [
        binding.route for binding in authored.get_bindings()
        if getattr(binding, "route", None) is not None
    ]
    preview = authored.get_user_function()
    try:
        invalid_new = await preview(SimpleNamespace(
            headers={}, json=AsyncMock(return_value={"prompt": 123}),
        ))
        assert invalid_new.status_code == 400
        assert "x-ms-session-id" not in invalid_new.headers
        assert native.clients == []
        first = await preview(SimpleNamespace(
            headers={},
            json=AsyncMock(return_value={"prompt": 'Call make_receipt with tag "authored-one".'}),
        ))
        assert first.status_code == 200, first.body.decode()
        receipt = first.body.decode()
        assert receipt.startswith("receipt-")
        public_id = first.headers["x-ms-session-id"]
        invalid_existing = await preview(SimpleNamespace(
            headers={"x-ms-session-id": public_id},
            json=AsyncMock(return_value={"prompt": 123}),
        ))
        assert invalid_existing.status_code == 400
        assert invalid_existing.headers["x-ms-session-id"] == public_id
        followup = await preview(SimpleNamespace(
            headers={"x-ms-session-id": public_id},
            json=AsyncMock(return_value={"prompt": "Recall the previous receipt, without tools."}),
        ))
        assert followup.status_code == 200, followup.body.decode()
        assert followup.headers["x-ms-session-id"] == public_id
        assert followup.body.decode() == receipt
        assert len(native.created) == len(native.resumed) == 1
    finally:
        await shutdown_client_manager()


@pytest.mark.asyncio
async def test_real_native_request_cancellation_does_not_kill_peer(native):
    create_function_app(native.root)
    tools = runner.discover_user_tools(native.root).tools
    slow = asyncio.create_task(runner.run_agent(
        "STALL_NATIVE_TEST", tools=tools, mcp_tools=[], timeout=45,
    ))
    try:
        await asyncio.wait_for(native.stalled.wait(), timeout=30)
        client = native.clients[0]
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        await asyncio.wait_for(native.cancelled.wait(), timeout=10)
        peer = await runner.run_agent(
            "Call make_receipt with tag 'unaffected-peer'.", tools=tools, mcp_tools=[], timeout=45,
        )
        assert peer.content.startswith("receipt-")
        assert len(peer.tool_calls) == 1
        assert native.clients == [client]
    finally:
        if not slow.done():
            slow.cancel()
        await shutdown_client_manager()
