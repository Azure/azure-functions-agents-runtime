"""Real pinned native runtime, synthetic HTTP provider. This is NOT live model evidence."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
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
SENTINEL = "not-a-credential-native-persistence-sentinel"


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


@pytest.fixture(params=["openai", "foundry"])
def native(monkeypatch, tmp_path, request):
    import copilot
    from copilot.copilot_request_handler import CopilotRequestHandler

    sdk_client = copilot.CopilotClient
    captured = []
    stalled = asyncio.Event()
    cancelled = asyncio.Event()

    class SyntheticProvider(CopilotRequestHandler):
        async def send_request(self, request, context):
            assert request.url.host in {"api.openai.com", "fixture.services.ai.azure.com"}, "Unexpected destination"
            assert request.headers.get("authorization") == f"Bearer {SENTINEL}"
            body = json.loads(await request.aread())
            captured.append(body)
            assert body.get("store", False) is False, "Provider response retention was enabled"
            responses = request.url.path.endswith("/responses")
            if responses:
                assert body.get("store") is False, "Responses must explicitly disable provider retention"
            tools = [item["name"] if responses else item["function"]["name"] for item in body.get("tools", [])]
            assert tools == ["make_receipt"], f"Unexpected model-visible tools: {tools}"
            declaration = body["tools"][0] if responses else body["tools"][0]["function"]
            assert "harmless demonstration tag" in declaration["description"]
            assert declaration["parameters"]["properties"]["tag"]["type"] == "string"
            assert declaration["parameters"]["required"] == ["tag"]
            messages = body["input"] if responses else body["messages"]
            users = [item for item in messages if item.get("role") == "user"]
            user = str(users[-1]["content"])
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
                    tag = re.search(r"tag '([^']+)'", user)
                    assert tag is not None
                    item = {"id": "fc_fixture", "type": "function_call", "call_id": "call_receipt",
                            "name": "make_receipt", "arguments": json.dumps({"tag": tag[1]}),
                            "status": "completed"}
                return _responses_fixture(body, item, request)
            if results:
                message = {"role": "assistant", "content": results[-1]["content"]}
                finish = "stop"
            else:
                tag = re.search(r"tag '([^']+)'", user)
                assert tag is not None, "Synthetic provider needs the first-turn tag"
                message = {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "call_receipt", "type": "function",
                        "function": {"name": "make_receipt", "arguments": json.dumps({"tag": tag[1]})},
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
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "GITHUB_TOKEN" not in kwargs["env"]
        assert kwargs["request_handler"] is not None
        return sdk_client(**kwargs)

    async def forward(_owner, request, context):
        return await provider.send_request(request, context)

    app_root = tmp_path / "app"
    shutil.copytree(SAMPLE, app_root)
    monkeypatch.setattr(copilot, "CopilotClient", create_client)
    monkeypatch.setattr(_copilot._NativeRuntime, "_forward_http", forward)
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    monkeypatch.setattr(_copilot, "_RUNTIMES", {})
    monkeypatch.setattr(paths, "_app_root", app_root)
    monkeypatch.setenv(_harness.FLAG, "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", request.param)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
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
        stalled=stalled, cancelled=cancelled, provider=request.param,
    )


@pytest.mark.asyncio
async def test_real_native_markdown_tool_and_cold_runtime_resume(native):
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
        harness = _harness.get_harness(native.root)
        owner = _copilot._runtime(harness)
        process = owner._process
        assert process is not None and process.poll() is None
        await shutdown_client_manager()
        assert process.poll() is not None
        followup = await chat(SimpleNamespace(
            headers={"x-ms-session-id": result["session_id"]},
            json=AsyncMock(return_value={"prompt": "Recall the previous receipt, without tools."}),
        ))
        assert followup.status_code == 200, followup.body.decode()
        second = json.loads(followup.body)
        assert second["session_id"] == result["session_id"]
        assert receipt in second["response"]
        assert second["tool_calls"] == []
        assert len(native.captured) == 3
        for body in native.captured:
            assert body.get("max_output_tokens", body.get("max_completion_tokens", body.get("max_tokens"))) == 256
        prior = native.captured[-1].get("messages") or native.captured[-1]["input"]
        assert prior[-1]["role"] == "user"
        assert "native-one" not in str(prior[-1])
        assert any(item.get("role") == "tool" or item.get("type") == "function_call_output" for item in prior)
        assert not (native.state / "agent-sessions").exists()
    finally:
        await shutdown_client_manager()
    files = [path for path in native.state.rglob("*") if path.is_file()]
    assert files
    assert all(SENTINEL.encode() not in path.read_bytes() for path in files)


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
        original_path = _copilot._runtime_path
        monkeypatch.setattr(_copilot, "_runtime_path", lambda: (_ for _ in ()).throw(
            _harness.CopilotPreviewError("Injected missing native bundle")
        ))
        failed = await chat(SimpleNamespace(
            headers={"x-ms-session-id": public_id}, json=AsyncMock(return_value={"prompt": "Recall."}),
        ))
        assert failed.status_code == 500
        assert len(native.captured) == 2
        monkeypatch.setattr(_copilot, "_runtime_path", original_path)
        resumed = await chat(SimpleNamespace(
            headers={"x-ms-session-id": public_id}, json=AsyncMock(return_value={"prompt": "Recall."}),
        ))
        assert resumed.status_code == 200, resumed.body.decode()
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
        owner = _copilot._runtime(_harness.get_harness())
        process = owner._process
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        assert process is not None and process.poll() is None
        peer = await runner.run_agent(
            "Call make_receipt with tag 'unaffected-peer'.", tools=tools, mcp_tools=[], timeout=45,
        )
        assert peer.content.startswith("receipt-")
        assert len(peer.tool_calls) == 1
        assert process.poll() is None
        assert native.cancelled.is_set()
    finally:
        if not slow.done():
            slow.cancel()
        await shutdown_client_manager()
