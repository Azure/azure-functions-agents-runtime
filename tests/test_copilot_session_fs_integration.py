"""Opt-in tests against a disposable, pre-existing Blob container (including Azurite)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import pytest
import pytest_asyncio
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.storage.blob.aio import BlobServiceClient

from azure_functions_agents import _copilot_session_fs as fs
from azure_functions_agents._harness import AppHarness, HarnessKind, ProviderKind
from azure_functions_agents._native_session_identity import (
    LeaseLostError,
    SessionConflictError,
    StorageMode,
    StorageRoute,
    opaque_key,
    state_name,
)

CONNECTION = "AZURE_FUNCTIONS_AGENTS_TEST_BLOB_CONNECTION_STRING"
CONTAINER = "AZURE_FUNCTIONS_AGENTS_TEST_BLOB_CONTAINER"
OPT_IN = "AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB"
NATIVE_ID = "integration-native-id"


@pytest_asyncio.fixture
async def blob_case(tmp_path):
    if os.environ.get(OPT_IN) != "1" or not all(
        os.environ.get(name) for name in (CONNECTION, CONTAINER)
    ):
        pytest.skip(
            f"Real Blob tests require {OPT_IN}=1 and explicit {CONNECTION} and {CONTAINER} "
            "(a pre-existing disposable container; Azurite is supported)."
        )
    route = StorageRoute(
        mode=StorageMode.BLOB,
        app_key=opaque_key("a", "integration-" + uuid.uuid4().hex),
        local_dir=tmp_path / "unused-local",
        container=os.environ[CONTAINER],
        identity_key=uuid.uuid4().hex,
        connection_string=os.environ[CONNECTION],
    )
    service = BlobServiceClient.from_connection_string(route.connection_string)
    names: set[str] = set()
    try:
        # Never let NativeSession.open create a container in an unintended account.
        await service.get_container_client(route.container).get_container_properties()
        def harness(session_id: str) -> AppHarness:
            names.add(state_name(route, "agent", session_id))
            return AppHarness(
                HarnessKind.COPILOT, tmp_path, tmp_path / "preview", "model",
                ProviderKind.OPENAI, session_storage=route,
            )

        yield harness, service, route
    finally:
        try:
            for name in names:
                assert name.startswith(f"copilot-native/v1/{route.app_key}/")
                blob = service.get_blob_client(route.container, name)
                try:
                    await blob.delete_blob(delete_snapshots="include")
                except HttpResponseError as exc:
                    if isinstance(exc, ResourceNotFoundError):
                        continue
                    if exc.status_code not in {409, 412}:
                        raise
                    await blob.break_lease(lease_break_period=0)
                    with contextlib.suppress(ResourceNotFoundError):
                        await blob.delete_blob(delete_snapshots="include")
        finally:
            await service.close()


async def open_owner(harness, session_id="conversation", *, new=True, timeout=5):
    owner = await fs.NativeSession.open(
        harness(session_id), "agent", session_id, NATIVE_ID,
        asyncio.get_running_loop().time() + timeout,
    )
    if new:
        await owner.prepare(new_session=True)
    return owner


@pytest.mark.asyncio
async def test_real_blob_two_clients_exclude_same_owner(blob_case):
    harness, _, _ = blob_case
    owner = await open_owner(harness)
    try:
        with pytest.raises(SessionConflictError, match="busy"):
            await open_owner(harness, new=False, timeout=.3)
        independent = await open_owner(harness, session_id="independent")
        await independent.close()
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_real_blob_lease_break_blocks_stale_writer(blob_case):
    harness, service, route = blob_case
    owner = await open_owner(harness)
    try:
        await fs.NativeSessionFs(owner).write_file("/journal", "committed")
        await owner.transition(state=fs.SessionState.ACTIVE)
        await owner.complete()
    finally:
        await owner.close()

    stale = await open_owner(harness, new=False)
    try:
        await stale.prepare(new_session=False)
        name = state_name(route, "agent", "conversation")
        await service.get_blob_client(route.container, name).break_lease(lease_break_period=0)
        with pytest.raises(LeaseLostError):
            await fs.NativeSessionFs(stale).write_file("/journal", "stale")
        with pytest.raises(LeaseLostError):
            await stale.complete()
    finally:
        await stale.close()

    restored = await open_owner(harness, new=False)
    try:
        await restored.recover_preparing()
        assert restored.envelope.completed.files["/journal"].content == "committed"
    finally:
        await restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "rename"])
async def test_real_blob_changed_etag_rejects_interrupted_mutation(blob_case, operation):
    harness, service, route = blob_case
    owner = await open_owner(harness)
    try:
        provider = fs.NativeSessionFs(owner)
        await provider.write_file("/old/deep/file", "before")
        baseline = fs.serialize(owner.envelope)
        revision = owner.envelope.revision
        name = state_name(route, "agent", "conversation")
        # A distinct client advances the ETag under the same lease, without changing content.
        # The original owner must not acknowledge its subsequent stale conditional Put.
        blob = service.get_blob_client(route.container, name)
        await blob.upload_blob(baseline, overwrite=True, lease=owner.store.lease.id)
        with pytest.raises(LeaseLostError):
            if operation == "write":
                await provider.write_file("/old/deep/file", "after")
            else:
                await provider.rename("/old", "/new")
        assert owner.envelope.revision == revision
        with pytest.raises(LeaseLostError):
            await owner.check()
    finally:
        await owner.close()

    # Failed owners intentionally never release their lease; break this test-owned lease.
    await service.get_blob_client(route.container, name).break_lease(lease_break_period=0)
    reopened = await open_owner(harness, new=False)
    try:
        assert reopened.envelope.owner_epoch > owner.envelope.owner_epoch
        assert reopened.envelope.state is fs.SessionState.PREPARING
        assert reopened.envelope.working.files["/old/deep/file"].content == "before"
        assert "/new/deep/file" not in reopened.envelope.working.files
        await reopened.recover_preparing()
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_real_blob_completed_tree_restores_from_new_client(blob_case):
    harness, _, _ = blob_case
    first = await open_owner(harness)
    try:
        await fs.NativeSessionFs(first).write_file("/sdk/journal.jsonl", "turn-1")
        await first.transition(state=fs.SessionState.ACTIVE)
        await first.complete()
        completed_revision = first.envelope.revision
    finally:
        await first.close()

    second = await open_owner(harness, new=False)
    try:
        assert not (second.route.local_dir / state_name(
            second.route, "agent", "conversation"
        )).exists()
        assert second.envelope.revision > completed_revision
        assert second.envelope.state is fs.SessionState.READY
        assert second.envelope.completed.files["/sdk/journal.jsonl"].content == "turn-1"
        await second.prepare(new_session=False)
        assert await fs.NativeSessionFs(second).read_file("/sdk/journal.jsonl") == "turn-1"
        await second.rollback()
    finally:
        await second.close()
