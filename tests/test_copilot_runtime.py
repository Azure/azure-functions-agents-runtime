from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from azure_functions_agents.harness import _harness_lifecycle as lifecycle
from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_execution as execution,
)
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_runtime as runtime,
)
from azure_functions_agents.harness.copilot_sdk._copilot_preview import CopilotPreviewError
from tests.test_copilot_execution import _fake_client, _request
from tests.test_copilot_execution import preview as preview


def test_owner_construction_is_thread_safe_and_resource_free(preview):
    with ThreadPoolExecutor(max_workers=8) as pool:
        owners = list(pool.map(lambda _: runtime.get_runtime(preview), range(32)))
    assert all(owner is owners[0] for owner in owners)
    assert preview._resources.runtime is owners[0]
    assert owners[0]._client is owners[0]._credential is owners[0]._loop is None
    assert not owners[0]._filesystems
    assert not owners[0].workspace.exists()
    assert not lifecycle._SHUTDOWN_CALLBACKS


def test_replace_and_same_root_bindings_get_distinct_resource_cells(preview):
    copied = replace(preview)
    assert copied.app_root == preview.app_root
    assert copied._resources is not preview._resources
    assert copied._resources.guard is not preview._resources.guard
    assert runtime.get_runtime(preview) is not runtime.get_runtime(copied)
    assert "_resources" not in repr(preview)


def test_unselected_binding_cannot_acquire_an_owner(tmp_path):
    binding = AppHarness(HarnessKind.MAF, tmp_path)
    with pytest.raises(CopilotPreviewError, match="not selected"):
        runtime.get_runtime(binding)
    assert binding._resources.runtime is None


@pytest.mark.asyncio
async def test_concurrent_startup_shares_one_client(preview, monkeypatch):
    import copilot

    started, release = asyncio.Event(), asyncio.Event()
    client = _fake_client()

    async def start():
        started.set()
        await release.wait()

    client.start.side_effect = start
    factory = Mock(return_value=client)
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    owner = runtime.get_runtime(preview)
    requests = [asyncio.create_task(owner.client()) for _ in range(12)]
    try:
        await started.wait()
        release.set()
        assert all(result is client for result in await asyncio.gather(*requests))
        factory.assert_called_once()
        client.start.assert_awaited_once()
    finally:
        release.set()
        await owner.close()
    assert preview._resources.runtime is None


@pytest.mark.asyncio
async def test_cross_loop_rejection_precedes_credentials_filesystem_and_start(preview, monkeypatch):
    import copilot

    client = _fake_client()
    factory = Mock(return_value=client)
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    owner = runtime.get_runtime(preview)
    owner.admit_loop()
    open_files = AsyncMock(side_effect=AssertionError("Cross-loop filesystem acquisition"))
    credential = Mock(side_effect=AssertionError("Cross-loop credential acquisition"))
    monkeypatch.setattr(execution, "open_session_fs", open_files)
    monkeypatch.setattr(runtime, "build_async_credential", credential)

    async def other_loop():
        with pytest.raises(CopilotPreviewError, match="runtime owner"):
            await execution.run(preview, _request())

    await asyncio.to_thread(asyncio.run, other_loop())
    open_files.assert_not_awaited()
    credential.assert_not_called()
    factory.assert_not_called()
    await owner.close()

    async def fresh_lifetime():
        fresh = runtime.get_runtime(preview)
        assert fresh is not owner
        assert await fresh.client() is client
        await fresh.close()

    await asyncio.to_thread(asyncio.run, fresh_lifetime())
    client.start.assert_awaited_once()
    client.stop.assert_awaited_once()
    assert preview._resources.runtime is None


@pytest.mark.asyncio
async def test_failed_start_cleanup_retains_process_for_retry(preview, monkeypatch):
    import copilot

    client = _fake_client()
    client.start.side_effect = RuntimeError("startup failed")
    client.stop.side_effect = RuntimeError("stop failed")
    client.force_stop.side_effect = RuntimeError("force failed")
    factory = Mock(return_value=client)
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    owner = runtime.get_runtime(preview)
    with pytest.raises(RuntimeError, match="startup failed"):
        await owner.client()
    assert owner._failed_client is client
    assert owner._close_callback in lifecycle._SHUTDOWN_CALLBACKS
    with pytest.raises(CopilotPreviewError):
        await owner.close()
    assert preview._resources.runtime is owner
    assert owner._failed_client is client
    client.stop.side_effect = None
    await lifecycle._shutdown_harnesses()
    assert preview._resources.runtime is None
    assert not lifecycle._SHUTDOWN_CALLBACKS
    factory.assert_called_once()


@pytest.mark.asyncio
async def test_owner_shutdown_keeps_failed_credential_and_visits_every_owner(preview, monkeypatch):
    import copilot

    failed_credential = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close failed")))
    good_credential = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(
        runtime, "build_async_credential", Mock(side_effect=[failed_credential, good_credential])
    )
    first = runtime.get_runtime(preview)
    second_binding = replace(preview)
    second = runtime.get_runtime(second_binding)
    first.credential()
    second.credential()
    client = _fake_client()
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    await second.client()
    with pytest.raises(CopilotPreviewError):
        await lifecycle._shutdown_harnesses()
    failed_credential.close.assert_awaited_once()
    good_credential.close.assert_awaited_once()
    client.stop.assert_awaited_once()
    assert first._credential is failed_credential
    assert preview._resources.runtime is first
    assert first._close_callback in lifecycle._SHUTDOWN_CALLBACKS
    assert second_binding._resources.runtime is None
    failed_credential.close.side_effect = None
    await lifecycle._shutdown_harnesses()
    assert preview._resources.runtime is None
    assert not lifecycle._SHUTDOWN_CALLBACKS


@pytest.mark.asyncio
async def test_acquired_owner_registers_a_stable_bound_close_callback(preview, monkeypatch):
    import copilot

    client = _fake_client()
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    owner = runtime.get_runtime(preview)
    await owner.client()
    replacement = AsyncMock(side_effect=AssertionError("Mutable owner callback rediscovery"))
    monkeypatch.setattr(owner, "close", replacement)
    await lifecycle._shutdown_harnesses()
    replacement.assert_not_awaited()
    client.stop.assert_awaited_once()
    assert preview._resources.runtime is None


@pytest.mark.asyncio
async def test_startup_has_a_bound_and_cleanup_retries(preview, monkeypatch):
    import copilot

    client = _fake_client()

    async def stalled_start():
        await asyncio.sleep(60)

    client.start.side_effect = stalled_start
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    monkeypatch.setattr(runtime, "_START_TIMEOUT_SECONDS", 0.01)
    owner = runtime.get_runtime(preview)
    with pytest.raises(TimeoutError):
        await owner.client()
    client.stop.assert_awaited_once()
    assert owner._failed_client is None
    client.start.side_effect = None
    assert await owner.client() is client
    await owner.close()


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
    owner = runtime.get_runtime(preview)
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
        await lifecycle._shutdown_harnesses()
    recovered.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_closes_every_app_owner_even_if_one_fails(preview, monkeypatch):
    import copilot

    second_binding = replace(preview, storage_root=preview.storage_root / "other")
    first = runtime.get_runtime(preview)
    second = runtime.get_runtime(second_binding)
    assert runtime.get_runtime(preview) is first
    failed_client, good_client = _fake_client(), _fake_client()
    failed_client.stop.side_effect = RuntimeError("stop failed")
    failed_client.force_stop.side_effect = RuntimeError("force failed")
    monkeypatch.setattr(copilot, "CopilotClient", Mock(side_effect=[failed_client, good_client]))
    await first.client()
    await second.client()
    with pytest.raises(CopilotPreviewError):
        await lifecycle._shutdown_harnesses()
    failed_client.stop.assert_awaited_once()
    failed_client.force_stop.assert_awaited_once()
    good_client.stop.assert_awaited_once()
    assert preview._resources.runtime is first
    assert first._client is failed_client
    assert second_binding._resources.runtime is None
    failed_client.stop.side_effect = None
    await lifecycle._shutdown_harnesses()
    assert preview._resources.runtime is None


@pytest.mark.asyncio
async def test_distinct_same_root_app_contexts_never_share_a_native_client(preview, monkeypatch):
    import copilot

    second = replace(preview)
    first_client, second_client = _fake_client(), _fake_client()
    factory = Mock(side_effect=[first_client, second_client])
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    first_owner = runtime.get_runtime(preview)
    second_owner = runtime.get_runtime(second)
    try:
        assert first_owner is not second_owner
        assert await first_owner.client() is first_client
        assert await second_owner.client() is second_client
        assert await first_owner.client() is first_client
        assert factory.call_count == 2
        first_client.start.assert_awaited_once()
        second_client.start.assert_awaited_once()
    finally:
        await lifecycle._shutdown_harnesses()
