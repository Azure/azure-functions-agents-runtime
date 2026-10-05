from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from azure_functions_agents.harness import _harness_lifecycle as lifecycle


@pytest.fixture(autouse=True)
def isolate_callbacks(monkeypatch):
    monkeypatch.setattr(lifecycle, "_SHUTDOWN_CALLBACKS", set())


@pytest.mark.asyncio
async def test_registration_is_idempotent_and_shutdown_calls_each_owner_once():
    callback = AsyncMock()
    lifecycle._register_shutdown(callback)
    lifecycle._register_shutdown(callback)

    assert {callback} == lifecycle._SHUTDOWN_CALLBACKS
    await lifecycle._shutdown_harnesses()
    callback.assert_awaited_once_with()
    assert {callback} == lifecycle._SHUTDOWN_CALLBACKS


def test_unregistration_is_idempotent():
    callback = AsyncMock()
    lifecycle._register_shutdown(callback)
    lifecycle._unregister_shutdown(callback)
    lifecycle._unregister_shutdown(callback)

    assert not lifecycle._SHUTDOWN_CALLBACKS


@pytest.mark.asyncio
async def test_shutdown_visits_every_owner_and_retains_failed_cleanup_for_retry():
    visited = []
    failure = RuntimeError("fixture close failure")

    async def successful():
        visited.append("successful")
        lifecycle._unregister_shutdown(successful)

    async def failing():
        visited.append("failing")
        raise failure

    lifecycle._register_shutdown(successful)
    lifecycle._register_shutdown(failing)
    with pytest.raises(RuntimeError, match="fixture close failure") as error:
        await lifecycle._shutdown_harnesses()

    assert error.value is failure
    assert sorted(visited) == ["failing", "successful"]
    assert {failing} == lifecycle._SHUTDOWN_CALLBACKS
    with pytest.raises(RuntimeError, match="fixture close failure"):
        await lifecycle._shutdown_harnesses()
    assert visited.count("failing") == 2
    assert visited.count("successful") == 1


@pytest.mark.asyncio
async def test_shutdown_still_visits_other_owners_when_one_is_cancelled():
    visited = []

    async def cancelled():
        visited.append("cancelled")
        raise asyncio.CancelledError

    async def successful():
        visited.append("successful")

    lifecycle._register_shutdown(cancelled)
    lifecycle._register_shutdown(successful)
    with pytest.raises(asyncio.CancelledError):
        await lifecycle._shutdown_harnesses()

    assert sorted(visited) == ["cancelled", "successful"]


@pytest.mark.asyncio
async def test_shutdown_uses_a_snapshot_when_callbacks_register_new_owners():
    deferred = AsyncMock()

    async def original():
        lifecycle._unregister_shutdown(original)
        lifecycle._register_shutdown(deferred)

    lifecycle._register_shutdown(original)
    await lifecycle._shutdown_harnesses()

    deferred.assert_not_awaited()
    assert {deferred} == lifecycle._SHUTDOWN_CALLBACKS
    await lifecycle._shutdown_harnesses()
    deferred.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_shutdown_without_acquired_owners_is_a_noop():
    await lifecycle._shutdown_harnesses()

    assert not lifecycle._SHUTDOWN_CALLBACKS
