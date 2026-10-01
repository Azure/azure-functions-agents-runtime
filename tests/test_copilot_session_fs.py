from __future__ import annotations

import asyncio
import errno
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceModifiedError
from azure.storage.blob import StorageErrorCode
from copilot.generated.rpc import SessionFSErrorCode
from copilot.session_fs_provider import create_session_fs_adapter

from azure_functions_agents import _copilot_session_fs as fs
from azure_functions_agents._copilot_providers import OpenAIProvider
from azure_functions_agents._harness import AppHarness, HarnessKind
from azure_functions_agents._native_session_identity import (
    CorruptSessionError,
    IncompatibleSessionError,
    LeaseLostError,
    PersistenceUnavailableError,
    SessionConflictError,
    StorageMode,
    resolve_route,
)
from tests._blob_sdk_errors import sdk_error

NATIVE_ID = "native-fixture-id"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri", raising=False)
    monkeypatch.delenv("WEBSITE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE", raising=False)
    return AppHarness(
        HarnessKind.COPILOT, tmp_path, tmp_path / "preview", "model", OpenAIProvider("not-a-credential"),
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
        await provider.write_file("/workspace/old/deep/a", "value")
        await provider.rename("/workspace/old", "/workspace/new")
        assert await provider.read_file("/workspace/new/deep/a") == "value"
        assert not await provider.exists("/workspace/old")
        assert "/workspace/old/deep/a" not in owner.envelope.working.files
        assert "/workspace/new/deep/a" in owner.envelope.working.files
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "../outside", "/../outside", "/a/../b", "\\windows", "/a/\x00",
    "/etc/\x01", "C:/outside", "//server/share", None,
])
async def test_invalid_path_latches_even_when_sdk_exists_adapter_swallows_error(harness, path):
    owner = await open_session(harness)
    provider = fs.NativeSessionFs(owner)
    try:
        baseline = await owner.store.load()
        revision = owner.envelope.revision
        handler = create_session_fs_adapter(provider)
        assert not (await handler.exists(SimpleNamespace(path=path))).exists
        with pytest.raises(PersistenceUnavailableError):
            await owner.check()
        with pytest.raises(PersistenceUnavailableError):
            await provider.write_file("/workspace/ok", "blocked")
        assert await owner.store.load() == baseline
        assert owner.envelope.revision == revision
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("conventions", "path"), [
    ("posix", "/etc/passwd"),
    ("posix", "/workspaces/other"),
    ("posix", "outside/./file"),
    ("windows", "\\outside\\file"),
    ("windows", "/etc/passwd"),
])
async def test_denied_paths_are_sdk_results_without_mutation_or_latching(harness, conventions, path):
    owner = await open_session(harness)
    provider = fs.NativeSessionFs(owner, conventions)
    handler = create_session_fs_adapter(provider)
    try:
        await provider.write_file("/workspace/kept", "unchanged")
        baseline = await owner.store.load()
        envelope = fs.serialize(owner.envelope)
        with pytest.raises(OSError) as denied:
            await provider.read_file(path)
        assert denied.value.errno == errno.EACCES
        params = SimpleNamespace(path=path, content="denied", recursive=True, force=True)
        results = [
            (await handler.read_file(params)).error,
            (await handler.stat(params)).error,
            (await handler.readdir(params)).error,
            (await handler.readdir_with_types(params)).error,
            await handler.write_file(params),
            await handler.append_file(params),
            await handler.mkdir(params),
            await handler.rm(params),
            await handler.rename(SimpleNamespace(src="/workspace/kept", dest=path)),
            await handler.rename(SimpleNamespace(src=path, dest="/workspace/kept")),
        ]
        for result in results:
            assert result.code is SessionFSErrorCode.UNKNOWN
            assert f"[Errno {errno.EACCES}]" in result.message
        assert not (await handler.exists(params)).exists
        assert await owner.store.load() == baseline
        assert fs.serialize(owner.envelope) == envelope
        assert owner.failure is None
        assert not owner.failed.is_set()
        await owner.check()
        await provider.write_file("/workspace/after", "still writable")
        await owner.transition(state=fs.SessionState.ACTIVE)
        await owner.complete()
    finally:
        await owner.close()


def test_envelope_cap_accepts_exact_limit_and_rejects_one_byte_over(harness):
    assert fs.MAX_ENVELOPE_BYTES == 4 * 1024 * 1024
    envelope = fs._new_envelope(harness.session_storage, "agent", "session", NATIVE_ID)
    envelope.state = fs.SessionState.PREPARING
    envelope.working = fs.empty_tree()
    content = "x" * (fs.MAX_ENVELOPE_BYTES - 4096)
    file = fs.FileContent(content=content, size_bytes=len(content), birthtime=0, mtime=0)
    envelope.working.files["/workspace/full"] = file
    file.content += "x" * (fs.MAX_ENVELOPE_BYTES - len(fs.serialize(envelope)))
    file.size_bytes = len(file.content)
    assert len(fs.serialize(envelope)) == fs.MAX_ENVELOPE_BYTES
    file.content += "x"
    file.size_bytes += 1
    with pytest.raises(fs.SessionCapacityError, match="use a new ID") as raised:
        fs.serialize(envelope)
    assert raised.value.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["callback", "prepare"])
async def test_envelope_overflow_latches_without_publishing_partial_state(harness, monkeypatch, operation):
    owner = await open_session(harness)
    provider = fs.NativeSessionFs(owner)
    try:
        await provider.write_file("/workspace/kept", "saved")
        if operation == "prepare":
            await owner.transition(state=fs.SessionState.ACTIVE)
            await owner.complete()
        baseline = await owner.store.load()
        envelope = fs.serialize(owner.envelope)
        monkeypatch.setattr(fs, "MAX_ENVELOPE_BYTES", len(envelope))
        if operation == "callback":
            handler = create_session_fs_adapter(provider)
            result = await handler.write_file(SimpleNamespace(
                path="/workspace/overflow", content="x" * len(envelope),
            ))
            assert result.code is SessionFSErrorCode.UNKNOWN
            assert "use a new ID" in result.message
        else:
            with pytest.raises(fs.SessionCapacityError, match="use a new ID"):
                await owner.prepare(new_session=False)
        assert isinstance(owner.failure, fs.SessionCapacityError)
        assert owner.failed.is_set()
        with pytest.raises(fs.SessionCapacityError):
            await owner.check()
        with pytest.raises(fs.SessionCapacityError):
            await provider.write_file("/workspace/kept", "")
        assert await owner.store.load() == baseline
        assert fs.serialize(owner.envelope) == envelope
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_local_snapshot_rollback_preserves_prior_completed_bytes(harness):
    first = await open_session(harness)
    provider = fs.NativeSessionFs(first)
    await provider.write_file("/workspace/events.jsonl", "before")
    await first.transition(state=fs.SessionState.ACTIVE)
    await first.complete()
    await first.close()

    resumed = await open_session(harness, new=False)
    try:
        await resumed.prepare(new_session=False)
        provider = fs.NativeSessionFs(resumed)
        await provider.append_file("/workspace/events.jsonl", "uncommitted")
        assert resumed.envelope.completed.files["/workspace/events.jsonl"].content == "before"
        await resumed.rollback()
    finally:
        await resumed.close()
    restored = await open_session(harness, new=False)
    try:
        assert restored.envelope.state is fs.SessionState.READY
        assert restored.envelope.completed.files["/workspace/events.jsonl"].content == "before"
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_preparing_without_handoff_recovers_only_prior_completed_tree(harness):
    first = await open_session(harness)
    await fs.NativeSessionFs(first).write_file("/workspace/events.jsonl", "completed")
    await first.transition(state=fs.SessionState.ACTIVE)
    await first.complete()
    await first.close()

    interrupted = await open_session(harness, new=False)
    await interrupted.prepare(new_session=False)
    await fs.NativeSessionFs(interrupted).write_file("/workspace/events.jsonl", "uncommitted")
    await interrupted.close()

    recovered = await open_session(harness, new=False)
    try:
        await recovered.recover_preparing()
        assert recovered.envelope.state is fs.SessionState.READY
        assert recovered.envelope.completed.files["/workspace/events.jsonl"].content == "completed"
        assert recovered.envelope.working is None
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_preparing_recovery_rejects_handoff_and_active_state(harness):
    owner = await open_session(harness)
    await owner.transition(state=fs.SessionState.ACTIVE, handoff_may_have_started=True)
    await owner.close()
    reopened = await open_session(harness, new=False)
    try:
        with pytest.raises(IncompatibleSessionError):
            await reopened.recover_preparing()
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_rename_to_same_path_preserves_file_and_directory(harness):
    owner = await open_session(harness)
    provider = fs.NativeSessionFs(owner)
    try:
        await provider.write_file("/workspace/directory/file", "content")
        await provider.rename("/workspace/directory/file", "/workspace/directory/file")
        await provider.rename("/workspace/directory", "/workspace/directory")
        assert await provider.read_file("/workspace/directory/file") == "content"
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_native_startup_missing_path_probe_is_enoent_without_latching(harness):
    owner = await open_session(harness)
    handler = create_session_fs_adapter(fs.NativeSessionFs(owner))
    try:
        missing = await handler.read_file(SimpleNamespace(path="/session-state/workspace.yaml"))
        assert missing.error.code is SessionFSErrorCode.ENOENT
        assert (await handler.stat(SimpleNamespace(path="/session-state"))).error.code is (
            SessionFSErrorCode.ENOENT
        )
        assert (await handler.readdir(SimpleNamespace(path="/session-state"))).error.code is (
            SessionFSErrorCode.ENOENT
        )
        assert (await handler.readdir_with_types(
            SimpleNamespace(path="/session-state")
        )).error.code is SessionFSErrorCode.ENOENT
        assert await handler.mkdir(
            SimpleNamespace(path="/session-state/checkpoints", recursive=True, mode=448)
        ) is None
        await owner.check()
        assert "/session-state/checkpoints" in owner.envelope.working.directories
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "code"), [
    (lambda p: p.rm("/workspace/missing", recursive=False, force=False), SessionFSErrorCode.ENOENT),
    (lambda p: p.rename("/workspace/missing", "/workspace/other"), SessionFSErrorCode.ENOENT),
    (lambda p: p.rename("/workspace/file", "/workspace/missing/target"), SessionFSErrorCode.ENOENT),
    (lambda p: p.mkdir("/workspace/missing/child", False), SessionFSErrorCode.ENOENT),
    (lambda p: p.mkdir("/workspace/file", False), SessionFSErrorCode.UNKNOWN),
    (lambda p: p.write_file("/workspace/directory", "x"), SessionFSErrorCode.UNKNOWN),
    (lambda p: p.write_file("/workspace/file/child", "x"), SessionFSErrorCode.UNKNOWN),
    (lambda p: p.append_file("/workspace/directory", "x"), SessionFSErrorCode.UNKNOWN),
    (lambda p: p.rm("/workspace/directory", recursive=False, force=False), SessionFSErrorCode.UNKNOWN),
    (lambda p: p.rename("/workspace/file", "/workspace/directory"), SessionFSErrorCode.UNKNOWN),
    (lambda p: p.readdir("/workspace/file"), SessionFSErrorCode.UNKNOWN),
])
async def test_filesystem_results_are_not_persisted_or_latched(harness, operation, code):
    owner = await open_session(harness)
    provider = fs.NativeSessionFs(owner)
    try:
        await provider.write_file("/workspace/file", "content")
        await provider.write_file("/workspace/directory/nested", "content")
        revision = owner.envelope.revision
        with pytest.raises(OSError) as raised:
            await operation(provider)
        assert fs_error_code(raised.value) is code
        assert owner.envelope.revision == revision
        await owner.check()
        await provider.write_file("/workspace/after", "still writable")
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_latched_owner_rejects_filesystem_probes_with_latched_failure(harness, monkeypatch):
    owner = await open_session(harness)
    provider = fs.NativeSessionFs(owner)

    async def broken(_data, _etag):
        raise FileNotFoundError("state directory disappeared")

    monkeypatch.setattr(owner.store, "save", broken)
    try:
        with pytest.raises(FileNotFoundError):
            await provider.write_file("/workspace/events.jsonl", "not acknowledged")
        with pytest.raises(PersistenceUnavailableError):
            await owner.check()
        with pytest.raises(PersistenceUnavailableError):
            await provider.read_file("/workspace/missing")
        with pytest.raises(PersistenceUnavailableError):
            await provider.stat("/workspace/missing")
    finally:
        await owner.close()


def fs_error_code(error):
    from copilot.session_fs_provider import _to_session_fs_error

    return _to_session_fs_error(error).code

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
            await provider.write_file("/workspace/events.jsonl", "not acknowledged")
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


@pytest.mark.asyncio
async def test_local_storage_rejects_symlinked_storage_parent(harness):
    root = harness.session_storage.local_dir.parent
    target = root.parent / "redirected"
    target.mkdir()
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks requires elevated privileges on this host.")
    with pytest.raises(PersistenceUnavailableError, match="symlinks"):
        await open_session(harness)


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


async def sdk_create_error(status, code):
    error, request = await sdk_error(
        status, code, lambda client: client.upload_blob(b"{}", overwrite=False)
    )
    assert request.headers["If-None-Match"] == "*"
    return error


@pytest.mark.asyncio
async def test_sdk_reports_leased_blob_create_as_lease_id_missing_not_exists():
    error = await sdk_create_error(412, "LeaseIdMissing")

    assert type(error) is HttpResponseError
    assert error.error_code == StorageErrorCode.LEASE_ID_MISSING


class LeasedCreateBlob(FakeBlob):
    """Real Blob behavior: an unleased create of a leased blob fails before If-None-Match."""

    def __init__(self, lease_error, busy_error=None):
        super().__init__()
        self.lease_error, self.busy_error = lease_error, busy_error

    async def upload_blob(self, data, **kwargs):
        if not kwargs.get("overwrite") and self.current is not None and self.current.active:
            raise self.lease_error
        return await super().upload_blob(data, **kwargs)

    async def acquire_lease(self, **kwargs):
        if self.busy_error is not None and self.current is not None and self.current.active:
            raise self.busy_error
        return await super().acquire_lease(**kwargs)


@pytest.mark.asyncio
async def test_blob_second_client_on_leased_session_reports_busy(harness, monkeypatch):
    busy, _ = await sdk_error(409, "LeaseAlreadyPresent", lambda client: client.acquire_lease())
    blob = LeasedCreateBlob(await sdk_create_error(412, "LeaseIdMissing"), busy)

    async def get_blob(_route, _name):
        return blob, None

    monkeypatch.setattr(fs, "_blob_client", get_blob)
    selected = replace(harness, session_storage=replace(
        harness.session_storage, mode=StorageMode.BLOB
    ))
    owner = await open_session(selected)
    try:
        with pytest.raises(SessionConflictError, match="busy"):
            await fs.NativeSession.open(
                selected, "agent", "session", NATIVE_ID,
                asyncio.get_running_loop().time() + .05,
            )
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_blob_create_non_contention_error_stays_unavailable(harness, monkeypatch):
    blob = LeasedCreateBlob(await sdk_create_error(403, "AuthorizationPermissionMismatch"))
    blob.current = FakeLease(blob)

    async def get_blob(_route, _name):
        return blob, None

    monkeypatch.setattr(fs, "_blob_client", get_blob)
    selected = replace(harness, session_storage=replace(
        harness.session_storage, mode=StorageMode.BLOB
    ))
    with pytest.raises(PersistenceUnavailableError, match="unavailable"):
        await open_session(selected)


@pytest.mark.asyncio
async def test_blob_open_closes_owned_service_and_credential(harness, monkeypatch):
    from azure.storage.blob import aio

    from azure_functions_agents import _credential

    blob = FakeBlob()
    credential = SimpleNamespace(close=AsyncMock())
    service = SimpleNamespace(
        close=AsyncMock(),
        get_container_client=lambda _name: SimpleNamespace(
            create_container=AsyncRead(None)
        ),
        get_blob_client=lambda **_kwargs: blob,
    )
    monkeypatch.setattr(
        _credential, "build_async_credential_with_client_id", lambda _client_id: credential
    )
    monkeypatch.setattr(aio, "BlobServiceClient", lambda **_kwargs: service)
    selected = replace(
        harness,
        session_storage=replace(
            harness.session_storage,
            mode=StorageMode.BLOB,
            connection_string=None,
            blob_uri="https://example.blob.core.windows.net",
        ),
    )

    owner = await open_session(selected)
    await owner.close()

    service.close.assert_awaited_once()
    credential.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_blob_container_is_ensured_once_per_route(harness, monkeypatch):
    from azure.storage.blob import aio

    from azure_functions_agents import _credential

    fs.clear_container_cache()
    create_container = AsyncMock(return_value=None)
    credential = SimpleNamespace(close=AsyncMock())
    service = SimpleNamespace(
        close=AsyncMock(),
        get_container_client=lambda _name: SimpleNamespace(create_container=create_container),
        get_blob_client=lambda **_kwargs: FakeBlob(),
    )
    monkeypatch.setattr(
        _credential, "build_async_credential_with_client_id", lambda _client_id: credential
    )
    monkeypatch.setattr(aio, "BlobServiceClient", lambda **_kwargs: service)
    selected = replace(
        harness,
        session_storage=replace(
            harness.session_storage,
            mode=StorageMode.BLOB,
            connection_string=None,
            blob_uri="https://example.blob.core.windows.net",
        ),
    )

    for _ in range(2):
        owner = await open_session(selected)
        await owner.close()
    create_container.assert_awaited_once()

    from azure_functions_agents.client_manager import shutdown_client_manager

    await shutdown_client_manager()
    owner = await open_session(selected)
    await owner.close()
    assert create_container.await_count == 2


@pytest.mark.asyncio
async def test_blob_open_failure_closes_owned_service_and_credential(harness, monkeypatch):
    from azure.storage.blob import aio

    from azure_functions_agents import _credential

    closed = []
    credential = SimpleNamespace(close=AsyncMock(side_effect=lambda: closed.append("credential")))
    service = SimpleNamespace(
        close=AsyncMock(side_effect=lambda: closed.append("service")),
        get_container_client=lambda _name: SimpleNamespace(
            create_container=AsyncMock(side_effect=RuntimeError("container unavailable"))
        ),
    )
    monkeypatch.setattr(
        _credential, "build_async_credential_with_client_id", lambda _client_id: credential
    )
    monkeypatch.setattr(aio, "BlobServiceClient", lambda **_kwargs: service)
    selected = replace(
        harness,
        session_storage=replace(
            harness.session_storage,
            mode=StorageMode.BLOB,
            connection_string=None,
            blob_uri="https://example.blob.core.windows.net",
        ),
    )

    with pytest.raises(PersistenceUnavailableError):
        await open_session(selected)
    with pytest.raises(PersistenceUnavailableError):
        await open_session(selected)

    assert closed == ["service", "credential", "service", "credential"]


@pytest.mark.asyncio
async def test_blob_release_does_not_close_external_blob():
    blob = FakeBlob()
    blob.close = AsyncMock()

    await fs._BlobStore(blob).release()

    blob.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_blob_release_reports_close_error_and_still_closes_credential(
    harness, monkeypatch
):
    from azure.storage.blob import aio

    from azure_functions_agents import _credential

    blob = FakeBlob()
    credential = SimpleNamespace(close=AsyncMock())
    service = SimpleNamespace(
        close=AsyncMock(side_effect=RuntimeError("service close failed")),
        get_container_client=lambda _name: SimpleNamespace(
            create_container=AsyncRead(None)
        ),
        get_blob_client=lambda **_kwargs: blob,
    )
    monkeypatch.setattr(
        _credential, "build_async_credential_with_client_id", lambda _client_id: credential
    )
    monkeypatch.setattr(aio, "BlobServiceClient", lambda **_kwargs: service)
    selected = replace(
        harness,
        session_storage=replace(
            harness.session_storage,
            mode=StorageMode.BLOB,
            connection_string=None,
            blob_uri="https://example.blob.core.windows.net",
        ),
    )
    owner = await open_session(selected)

    with pytest.raises(RuntimeError, match="service close failed"):
        await owner.close()

    credential.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_blob_lease_etag_single_put_stale_owner_and_renew(harness, monkeypatch):
    blob = FakeBlob()

    async def get_blob(_route, _name):
        return blob, None

    monkeypatch.setattr(fs, "_blob_client", get_blob)
    monkeypatch.setattr(fs, "LEASE_SECONDS", .06)
    selected = replace(harness, session_storage=replace(
        harness.session_storage, mode=StorageMode.BLOB
    ))
    owner = await open_session(selected)
    try:
        provider = fs.NativeSessionFs(owner)
        await provider.append_file("/workspace/events.jsonl", "first")
        await provider.append_file("/workspace/events.jsonl", " second")
        assert blob.current.renewed == 0
        await asyncio.sleep(.03)
        assert blob.current.renewed > 0
        assert all(record["lease"] is blob.current and "etag" in record for record in blob.writes[1:])
        assert all(record.get("overwrite") is True for record in blob.writes[1:])
        blob.current.active = False
        with pytest.raises(LeaseLostError):
            await provider.write_file("/workspace/events.jsonl", "stale")
        with pytest.raises(LeaseLostError):
            await owner.complete()
        assert blob.current.released == 0
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_blob_renewal_loss_latches_without_stale_release(harness, monkeypatch):
    blob = FakeBlob()

    async def get_blob(_route, _name):
        return blob, None

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


@pytest.mark.asyncio
async def test_blob_renewal_loss_during_put_never_acknowledges_mutation(harness, monkeypatch):
    blob = FakeBlob()

    async def get_blob(_route, _name):
        return blob, None

    monkeypatch.setattr(fs, "_blob_client", get_blob)
    selected = replace(harness, session_storage=replace(
        harness.session_storage, mode=StorageMode.BLOB
    ))
    owner = await open_session(selected)
    original = owner.envelope.revision

    async def lost_renewal(data, **kwargs):
        result = await FakeBlob.upload_blob(blob, data, **kwargs)
        owner.store.failure = LeaseLostError("renewal failed")
        owner.store.failed.set()
        return result

    monkeypatch.setattr(blob, "upload_blob", lost_renewal)
    try:
        with pytest.raises(LeaseLostError):
            await fs.NativeSessionFs(owner).write_file("/workspace/file", "value")
        assert owner.envelope.revision == original
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_windows_conventions_callbacks_persist_canonical_posix_paths(harness):
    """The pinned runtime emits host-convention separators; persisted keys stay POSIX."""
    owner = await open_session(harness)
    provider = fs.NativeSessionFs(owner, "windows")
    try:
        await provider.mkdir("\\session-state", False)
        await provider.write_file("\\session-state\\workspace.yaml", "cwd: host")
        await provider.mkdir("/session-state", False)
        assert await provider.exists("/session-state/workspace.yaml")
        assert await provider.read_file("\\session-state\\workspace.yaml") == "cwd: host"
        await provider.write_file("\\session-state\\workspace.yaml.tmp", "cwd: host")
        await provider.rename(
            "\\session-state\\workspace.yaml.tmp", "\\session-state\\workspace.yaml"
        )
        assert sorted(owner.envelope.working.files) == ["/session-state/workspace.yaml"]
        assert sorted(owner.envelope.working.directories) == [
            "/",
            "/session-state",
            "/workspace",
        ]
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_windows_conventions_reject_traversal_drive_and_unc_paths(harness):
    paths = (
        "\\session-state\\..\\..\\outside",
        "C:\\session-state",
        "c:/session-state",
        "\\\\server\\share",
        "//server/share",
        "\\session-state\\a\x00b",
    )
    for index, path in enumerate(paths):
        owner = await open_session(harness, session_id=f"invalid-{index}")
        provider = fs.NativeSessionFs(owner, "windows")
        try:
            baseline = await owner.store.load()
            handler = create_session_fs_adapter(provider)
            assert not (await handler.exists(SimpleNamespace(path=path))).exists
            with pytest.raises(PersistenceUnavailableError):
                await owner.check()
            assert await owner.store.load() == baseline
        finally:
            await owner.close()


@pytest.mark.asyncio
async def test_posix_conventions_stay_strict_regardless_of_host(harness):
    """Default conventions keep cross-platform determinism: backslashes are never paths."""
    owner = await open_session(harness)
    provider = fs.NativeSessionFs(owner)
    assert provider.conventions == "posix"
    try:
        handler = create_session_fs_adapter(provider)
        assert not (await handler.exists(SimpleNamespace(path="\\session-state"))).exists
        with pytest.raises(PersistenceUnavailableError):
            await owner.check()
    finally:
        await owner.close()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("\\session-state\\files", "/session-state/files"),
        ("/session-state", "/session-state"),
        ("\\session-state\\.\\plan.md", "/session-state/plan.md"),
    ],
)
def test_normalize_callback_path_translates_only_windows_separators(path, expected):
    assert fs.normalize_callback_path(path, "windows") == expected
    if "\\" in path:
        with pytest.raises(ValueError):
            fs.normalize_callback_path(path, "posix")


@pytest.mark.parametrize(
    "path", ["C:\\x", "c:/x", "\\\\server\\share", "\\a\\..\\..\\b", "\\a\x00"]
)
def test_normalize_callback_path_rejects_host_qualified_and_escaping_paths(path):
    with pytest.raises(ValueError):
        fs.normalize_callback_path(path, "windows")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:\\app\\ws", "/workspace"),
        ("C:\\app\\ws\\", "/workspace"),
        ("c:/APP/ws/notes.md", "/workspace/notes.md"),
        ("C:\\app\\ws\\sub\\a.txt", "/workspace/sub/a.txt"),
    ],
)
def test_normalize_callback_path_maps_declared_workspace_aliases(path, expected):
    """The runtime resolves the recorded host cwd through SessionFs on resume."""
    assert fs.normalize_callback_path(path, "windows", ("C:\\app\\ws",)) == expected


@pytest.mark.parametrize(
    "path",
    [
        "C:\\app\\ws2",
        "C:\\app\\wsother\\a.txt",
        "D:\\app\\ws",
        "C:\\app\\ws\\..\\..\\escape",
        "\\\\server\\share\\ws",
    ],
)
def test_normalize_callback_path_keeps_non_alias_host_paths_closed(path):
    """Sibling prefixes, other roots, and traversal out of the alias must still fail closed."""
    with pytest.raises(ValueError):
        fs.normalize_callback_path(path, "windows", ("C:\\app\\ws",))


def test_normalize_callback_path_ignores_aliases_under_posix_conventions():
    """POSIX hosts compare exactly: no case folding and no separator translation."""
    assert fs.normalize_callback_path("/srv/ws/a.txt", "posix", ("/srv/ws",)) == "/workspace/a.txt"
    with pytest.raises(OSError) as denied:
        fs.normalize_callback_path("/SRV/ws/a.txt", "posix", ("/srv/ws",))
    assert denied.value.errno == errno.EACCES
    with pytest.raises(ValueError):
        fs.normalize_callback_path("\\srv\\ws\\a.txt", "posix", ("/srv/ws",))


@pytest.mark.parametrize(
    "path",
    ["/srv/ws/a.txt", "/etc/passwd", "/home/agent/notes.md", "/workspaces/other", "/var"],
)
def test_normalize_callback_path_rejects_paths_outside_the_virtual_roots(path):
    """Only the virtual roots and declared aliases are addressable."""
    with pytest.raises(OSError) as denied:
        fs.normalize_callback_path(path, "posix")
    assert denied.value.errno == errno.EACCES


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "/"),
        ("/workspace", "/workspace"),
        ("/workspace/./notes.md", "/workspace/notes.md"),
        ("/session-state", "/session-state"),
        ("/session-state/files/a.txt", "/session-state/files/a.txt"),
    ],
)
def test_normalize_callback_path_keeps_the_virtual_roots_addressable(path, expected):
    assert fs.normalize_callback_path(path, "posix") == expected


def test_host_path_conventions_matches_the_running_host():
    import os

    expected = "windows" if os.name == "nt" else "posix"
    assert expected == fs.HOST_PATH_CONVENTIONS
