from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from azure_functions_agents import _copilot, runner
from azure_functions_agents._harness import AppHarness, CopilotPreviewError, HarnessRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["foundry", "openai"])
async def test_provider_transport_enforces_cap_and_retention_per_turn(tmp_path, monkeypatch, provider):
    owner = _copilot._NativeRuntime(tmp_path)
    harness = AppHarness(
        "copilot", tmp_path, tmp_path,
        provider=provider, endpoint="https://fixture.services.ai.azure.com/api/projects/test",
    )
    forwarded = AsyncMock(return_value=httpx.Response(200))
    monkeypatch.setattr(owner, "_forward_http", forwarded)
    context = SimpleNamespace(session_id="native-one")
    handler = _copilot._request_handler(owner)
    try:
        with owner.inference_turn("native-one", harness, 123):
            url = owner._inference["native-one"].url
            await handler.send_request(
                httpx.Request("POST", url, json={"store": True, "model": "example"}),
                context,
            )
            sent = forwarded.call_args.args[0]
            body = json.loads(sent.content)
            assert body["store"] is False
            limit = "max_output_tokens" if provider == "foundry" else "max_completion_tokens"
            assert body[limit] == 123
            assert sent.headers["content-length"] == str(len(sent.content))
            with pytest.raises(CopilotPreviewError, match="outside the active provider"):
                await handler.send_request(
                    httpx.Request("POST", "https://unexpected.example/responses", json={}), context
                )
        assert owner._inference == {}
        with pytest.raises(CopilotPreviewError, match="outside the active provider"):
            await handler.send_request(httpx.Request("POST", url, json={}), context)
        assert forwarded.await_count == 1
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_failed_start_and_sdk_cleanup_still_terminate_owned_handle(tmp_path, monkeypatch):
    import copilot

    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    client = SimpleNamespace(
        _cli_process=process,
        start=AsyncMock(side_effect=RuntimeError("native failed")),
        stop=AsyncMock(side_effect=TimeoutError),
    )
    monkeypatch.setattr(copilot, "CopilotClient", lambda **_: client)
    monkeypatch.setattr(_copilot, "_runtime_path", lambda: tmp_path / "native.exe")
    owner = _copilot._NativeRuntime(tmp_path)
    try:
        with pytest.raises(TimeoutError):
            await owner.client()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=2)
        assert owner.state._owner is None
        assert owner._client is None
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_lock_wait_timeout_does_not_mark_or_misdiagnose_session(tmp_path, monkeypatch):
    monkeypatch.setattr(_copilot, "_RUNTIMES", {})
    harness = AppHarness("copilot", tmp_path, tmp_path / "state", provider="openai")
    owner = _copilot._runtime(harness)
    native_id = owner.state.native_id("main", "existing")
    lock = await runner._get_session_lock(native_id, "main")
    await lock.acquire()
    try:
        with pytest.raises(CopilotPreviewError, match="did not mark the session unfinished"):
            await _copilot.run(harness, HarnessRequest(
                prompt="never sent", instructions=None, agent_slug="main", session_id="existing",
                new_session=False, model="unused", tools=[], max_output_tokens=None,
                deadline=asyncio.get_running_loop().time() + 0.01,
            ))
        assert not harness.storage_root.exists()
    finally:
        lock.release()
        await owner.close()
