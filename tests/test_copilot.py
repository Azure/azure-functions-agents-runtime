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

from azure_functions_agents import _copilot, _harness
from azure_functions_agents._harness import (
    AppHarness,
    CopilotPreviewError,
    HarnessKind,
    HarnessRequest,
    ProviderKind,
    UnsupportedCapabilityError,
)
from azure_functions_agents.app import create_function_app
from azure_functions_agents.config import paths

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "copilot-preview" / "src"


@pytest.fixture
def preview(monkeypatch, tmp_path):
    monkeypatch.setattr(_copilot, "_RUNTIMES", {})
    return AppHarness(
        HarnessKind.COPILOT, tmp_path, tmp_path / "state", "gpt-4.1-mini", ProviderKind.OPENAI
    )


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
    ("provider", "wire_api"),
    [(ProviderKind.OPENAI, "completions"), (ProviderKind.FOUNDRY, "responses")],
)
async def test_provider_options_use_sdk_declared_types(preview, monkeypatch, provider, wire_api):
    import copilot
    from copilot.session import ProviderConfig

    selected = replace(
        preview, provider=provider, endpoint="https://fixture.services.ai.azure.com/api/projects/test"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-credential")
    credential = SimpleNamespace(
        get_token=AsyncMock(return_value=AccessToken("not-a-credential", 9999999999)),
        close=AsyncMock(),
    )
    monkeypatch.setattr(_copilot, "build_async_credential", lambda: credential)
    client = _fake_client()
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    try:
        result = await _copilot.run(selected, _request())
        assert result.content == "synthetic reply"
        options = client.create_session.call_args.kwargs
        provider_options = options["provider"]
        allowed = get_type_hints(ProviderConfig)
        assert provider_options["type"] in get_args(allowed["type"])
        assert provider_options["type"] == "openai"
        assert provider_options["wire_api"] in get_args(allowed["wire_api"])
        assert provider_options["wire_api"] == wire_api
        assert options["available_tools"] == []
        assert "max_output_tokens" not in provider_options
    finally:
        await _copilot.shutdown()


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
