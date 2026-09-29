from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
from copilot.session_fs_provider import create_session_fs_adapter

from azure_functions_agents import _copilot_session_fs as fs
from azure_functions_agents._harness import AppHarness, HarnessKind, ProviderKind
from azure_functions_agents._native_session_identity import (
    CorruptSessionError,
    IncompatibleSessionError,
    LeaseLostError,
    PersistenceUnavailableError,
    SessionConflictError,
    StorageMode,
    resolve_route,
)

NATIVE_ID = "native-fixture-id"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri", raising=False)
    monkeypatch.delenv("WEBSITE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE", raising=False)
    return AppHarness(
        HarnessKind.COPILOT, tmp_path, tmp_path / "preview", "model", ProviderKind.OPENAI,
        session_storage=resolve_route(tmp_path),
    )


async def open_session(harness, session_id="session", new=True):
    owner = await fs.NativeSession.open(
        harness, "agent", session_id, NATIVE_ID,
        asyncio.get_running_loop().time() + 2,
    )
    if new:
        await owner.prepare(new_session=True)
    return owner


@pytest.mark.asyncio
async def test_all_session_fs_operations_are_single_tree_and_read_after_ack(harness):
    owner = await open_session(harness)
    provider = fs.NativeSessionFs(owner)
    try:
        await provider.mkdir("/workspace", False)
        await provider.write_file("/workspace/events.jsonl", "☃")
        assert await provider.exists("/workspace/./events.jsonl")
        assert await provider.read_file("/workspace/events.jsonl") == "☃"
        assert (await provider.stat("/workspace/events.jsonl")).size == 3
        await asyncio.gather(*[
            provider.append_file("/workspace/events.jsonl", str(i)) for i in range(20)
        ])
        assert await provider.read_file("/workspace/events.jsonl") == "☃" + "".join(
            str(i) for i in range(20)
        )
        assert await provider.readdir("/workspace") == ["events.jsonl"]
        assert (await provider.readdir_with_types("/workspace"))[0].type.value == "file"
        await provider.rename("/workspace/events.jsonl", "/workspace/previous.jsonl")
        assert not await provider.exists("/workspace/events.jsonl")
        assert await provider.exists("/workspace/previous.jsonl")
        await provider.rm("/workspace/previous.jsonl", recursive=False, force=False)
        await provider.rm("/workspace", recursive=False, force=False)
        assert await provider.readdir("/") == []
        await owner.transition(state=fs.SessionState.ACTIVE)
        await owner.complete()
        assert owner.envelope.working is None
        assert owner.envelope.completed is not None
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_directory_rename_is_atomic_and_complete(harness):
    owner = await open_session(harness)
    provider = fs.NativeSessionFs(owner)
    try:
        await provider.write_file("/old/deep/a", "value")
        await provider.rename("/old", "/new")
        assert await provider.read_file("/new/deep/a") == "value"
        assert not await provider.exists("/old")
        assert "/old/deep/a" not in owner.envelope.working.files
        assert "/new/deep/a" in owner.envelope.working.files
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside", "/../outside", "/a/../b", "\\windows", "/a/\x00"])
async def test_invalid_path_latches_even_when_sdk_exists_adapter_swallows_error(harness, path):
    owner = await open_session(harness)
    provider = fs.NativeSessionFs(owner)
    try:
        handler = create_session_fs_adapter(provider)
        assert not (await handler.exists(SimpleNamespace(path=path))).exists
        with pytest.raises(PersistenceUnavailableError):
            await owner.check()
        with pytest.raises(PersistenceUnavailableError):
            await provider.write_file("/ok", "blocked")
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_local_snapshot_rollback_preserves_prior_completed_bytes(harness):
    first = await open_session(harness)
    provider = fs.NativeSessionFs(first)
    await provider.write_file("/events.jsonl", "before")
    await first.transition(state=fs.SessionState.ACTIVE)
    await first.complete()
    await first.close()

    resumed = await open_session(harness, new=False)
    try:
        await resumed.prepare(new_session=False)
        provider = fs.NativeSessionFs(resumed)
        await provider.append_file("/events.jsonl", "uncommitted")
        assert resumed.envelope.completed.files["/events.jsonl"].content == "before"
        await resumed.rollback()
    finally:
        await resumed.close()
    restored = await open_session(harness, new=False)
    try:
        assert restored.envelope.state is fs.SessionState.READY
        assert restored.envelope.completed.files["/events.jsonl"].content == "before"
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_atomic_write_failure_latches_and_does_not_ack(harness, monkeypatch):
    owner = await open_session(harness)
    revision = owner.envelope.revision
    provider = fs.NativeSessionFs(owner)

    async def broken(_data, _etag):
        raise OSError("injected failure")

    monkeypatch.setattr(owner.store, "save", broken)
    try:
        with pytest.raises(OSError):
            await provider.write_file("/events.jsonl", "not acknowledged")
        assert owner.envelope.revision == revision
        with pytest.raises(PersistenceUnavailableError):
            await owner.check()
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_local_lock_conflict_and_independent_session(harness):
    owner = await open_session(harness)
    try:
        with pytest.raises(SessionConflictError, match="busy"):
            await fs.NativeSession.open(
                harness, "agent", "session", NATIVE_ID,
                asyncio.get_running_loop().time() + .05,
            )
        other = await open_session(harness, session_id="independent")
        await other.close()
    finally:
        await owner.close()


def test_envelope_strict_integrity_version_identity_and_tombstone(harness):
    route = harness.session_storage
    empty = fs._new_envelope(route, "agent", "session", NATIVE_ID)
    data = fs.serialize(empty)
    assert fs.deserialize(data, route, "agent", "session", NATIVE_ID).state is fs.SessionState.EMPTY
    with pytest.raises(IncompatibleSessionError):
        fs.deserialize(data, route, "agent", "another", NATIVE_ID)
    for broken in (
        data.replace(b'"revision":0', b'"revision":1'),
        data.replace(b'"revision":0', b'"revision":0,"revision":0'),
        b"{}",
        fs.serialize(empty).replace(b'"schema_version":1', b'"schema_version":2'),
    ):
        with pytest.raises((CorruptSessionError, IncompatibleSessionError)):
            fs.deserialize(broken, route, "agent", "session", NATIVE_ID)
    tombstone = empty.model_copy(update={"state": fs.SessionState.DELETED})
    assert fs.deserialize(fs.serialize(tombstone), route, "agent", "session", NATIVE_ID).state is fs.SessionState.DELETED


@pytest.mark.asyncio
async def test_active_uncertain_deleted_and_preparing_never_auto_resume(harness):
    for state in (fs.SessionState.PREPARING, fs.SessionState.ACTIVE, fs.SessionState.UNCERTAIN):
        owner = await open_session(harness, session_id=state.value)
        await owner.transition(state=state)
        await owner.close()
        resumed = await open_session(harness, session_id=state.value, new=False)
        try:
            with pytest.raises(IncompatibleSessionError, match="not safely resumable"):
                await resumed.prepare(new_session=False)
        finally:
            await resumed.close()
    owner = await open_session(harness, session_id="deleted")
    await owner.delete()
    await owner.delete()
    assert owner.envelope.working is owner.envelope.completed is None
    await owner.close()
    tomb = await open_session(harness, session_id="deleted", new=False)
    try:
        with pytest.raises(IncompatibleSessionError):
            await tomb.prepare(new_session=True)
    finally:
        await tomb.close()


class FakeLease:
    def __init__(self, blob):
        self.blob = blob
        self.active = True
        self.renewed = 0
        self.released = 0

    async def renew(self):
        if not self.active:
            raise RuntimeError("expired")
        self.renewed += 1

    async def release(self):
        self.released += 1
        self.active = False


class FakeBlob:
    def __init__(self):
        self.data = None
        self.etag = 0
        self.current = None
        self.writes = []

    async def upload_blob(self, data, **kwargs):
        if not kwargs.get("overwrite"):
            if self.data is not None:
                raise ResourceExistsError("exists")
        else:
            lease = kwargs["lease"]
            if lease is not self.current or not lease.active or kwargs["etag"] != str(self.etag):
                raise ResourceModifiedError("stale", status_code=412)
        self.data = data
        self.etag += 1
        self.writes.append(kwargs)
        return {"etag": str(self.etag)}

    async def acquire_lease(self, **_kwargs):
        if self.current is not None and self.current.active:
            raise ResourceExistsError("busy", status_code=409)
        self.current = FakeLease(self)
        return self.current

    async def download_blob(self, **kwargs):
        assert kwargs["lease"] is self.current
        return SimpleNamespace(
            readall=AsyncRead(self.data), properties={"etag": str(self.etag)},
        )


class AsyncRead:
    def __init__(self, data):
        self.data = data

    async def __call__(self):
        return self.data


@pytest.mark.asyncio
async def test_blob_lease_etag_single_put_stale_owner_and_renew(harness, monkeypatch):
    blob = FakeBlob()

    async def get_blob(_route, _name):
        return blob

    monkeypatch.setattr(fs, "_blob_client", get_blob)
    monkeypatch.setattr(fs, "LEASE_SECONDS", .06)
    selected = replace(harness, session_storage=replace(
        harness.session_storage, mode=StorageMode.BLOB
    ))
    owner = await open_session(selected)
    try:
        provider = fs.NativeSessionFs(owner)
        await provider.append_file("/events.jsonl", "first")
        await provider.append_file("/events.jsonl", " second")
        assert blob.current.renewed == 0
        await asyncio.sleep(.03)
        assert blob.current.renewed > 0
        assert all(record["lease"] is blob.current and "etag" in record for record in blob.writes[1:])
        assert all(record.get("overwrite") is True for record in blob.writes[1:])
        blob.current.active = False
        with pytest.raises(LeaseLostError):
            await provider.write_file("/events.jsonl", "stale")
        with pytest.raises(LeaseLostError):
            await owner.complete()
        assert blob.current.released == 0
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_blob_renewal_loss_latches_without_stale_release(harness, monkeypatch):
    blob = FakeBlob()

    async def get_blob(_route, _name):
        return blob

    monkeypatch.setattr(fs, "_blob_client", get_blob)
    monkeypatch.setattr(fs, "LEASE_SECONDS", .03)
    selected = replace(harness, session_storage=replace(
        harness.session_storage, mode=StorageMode.BLOB
    ))
    owner = await open_session(selected)
    try:
        blob.current.active = False
        await asyncio.wait_for(owner.store.failed.wait(), timeout=.5)
        with pytest.raises(LeaseLostError):
            await owner.check()
    finally:
        await owner.close()
    assert blob.current.released == 0
