from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import Mock

import pytest

from azure_functions_agents.harness import _harness_execution as execution
from azure_functions_agents.harness._provider_config import InferenceTarget


@pytest.fixture(autouse=True)
def isolated_locks(monkeypatch):
    monkeypatch.setattr(execution, "_SESSION_LOCKS", {})
    monkeypatch.setattr(execution, "_SESSION_LOCKS_GUARD", asyncio.Lock())


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("openai", "openai"), ("azure_openai", "openai"), ("foundry", None), (None, None)],
)
def test_publisher_mapping(provider, expected):
    assert execution._model_publisher(provider) == expected


def test_backend_neutral_usage_emits_once(caplog):
    recorder = execution._AgentUsageRecorder(
        agent_name="billing",
        execution_role="workflow_subagent",
        inference_target=InferenceTarget("azure_openai", "fixture-model"),
    )
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        recorder.emit_counts(input_tokens=3, output_tokens=None)
        recorder.emit_counts(input_tokens=99, output_tokens=99)
    records = [
        record for record in caplog.records
        if record.getMessage().startswith("Agent token usage: ")
    ]
    assert len(records) == 1
    serialized = records[0].getMessage().removeprefix("Agent token usage: ")
    payload = {
        "agent_name": "billing",
        "event_name": "agent_token_usage",
        "execution_role": "workflow_subagent",
        "input_tokens": 3,
        "model": "fixture-model",
        "model_publisher": "openai",
        "output_tokens": None,
        "provider": "azure_openai",
    }
    assert json.loads(serialized) == payload
    assert serialized == json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def test_failed_logging_is_not_retried_or_propagated(monkeypatch):
    log = Mock(side_effect=RuntimeError("fixture logging failure"))
    monkeypatch.setattr(execution.logger, "info", log)
    recorder = execution._AgentUsageRecorder(agent_name="main", execution_role="primary")
    recorder.emit_counts(input_tokens=None, output_tokens=None)
    recorder.emit_counts(input_tokens=4, output_tokens=2)
    log.assert_called_once()


@pytest.mark.asyncio
async def test_lock_identity_uses_only_existing_agent_and_session_key():
    lock = await execution._get_session_lock("session", "billing")
    assert await execution._get_session_lock("session", "billing") is lock
    assert await execution._get_session_lock("session", "support") is not lock
    assert await execution._get_session_lock("other", "billing") is not lock
    assert execution._SESSION_LOCKS[("billing", "session")] is lock


@pytest.mark.asyncio
async def test_bounded_lock_timeout_does_not_release_the_active_turn():
    loop = asyncio.get_running_loop()
    lock = await execution._get_session_lock("session", "billing")
    await lock.acquire()
    try:
        with pytest.raises(TimeoutError):
            async with execution._session_lock_bounded_by(
                "session", loop.time() + 0.01, agent_slug="billing"
            ):
                pytest.fail("A competing turn cannot enter.")
        assert lock.locked()
    finally:
        lock.release()
    async with execution._session_lock_bounded_by(
        "session", loop.time() + 1, agent_slug="billing"
    ):
        assert lock.locked()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_cancellation_releases_only_an_acquired_lock():
    started = asyncio.Event()

    async def turn():
        async with execution._session_lock_bounded_by(
            "session", asyncio.get_running_loop().time() + 10, agent_slug="billing"
        ):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(turn())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (await execution._get_session_lock("session", "billing")).locked()
