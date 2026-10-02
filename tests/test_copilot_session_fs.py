from __future__ import annotations

import asyncio
import errno
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote, unquote, urlsplit

import pytest
import pytest_asyncio
from azure.core.credentials import AccessToken
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.core.pipeline.transport import AsyncHttpResponse, HttpRequest
from azure.storage.blob import BlobProperties, StorageErrorCode

from azure_functions_agents import _copilot_session_fs as fs
from azure_functions_agents._copilot_session_fs import open_session_fs
from azure_functions_agents._native_session_identity import (
    NativeSessionError,
    StorageMode,
    resolve_route,
    session_prefix,
)
from azure_functions_agents._session_storage import BlobStorageSettings, OwnedBlobService


def storage_error(status, code):
    response = AsyncHttpResponse(HttpRequest("GET", "https://fixture.invalid"), None)
    response.status_code = status
    response.headers["x-ms-error-code"] = code.value
    kind = {404: ResourceNotFoundError, 409: ResourceExistsError}.get(status, HttpResponseError)
    return kind("service failed: fixture-secret", response=response)


@dataclass
class MemoryFile:
    content: bytes
    properties: BlobProperties


class MemoryBlobs:
    def __init__(self):
        self.files = {}
        self.container_exists = False
        self.failures = {}
        self.copies = []
        self.services = []
        self.credentials = []


class MemoryBlob:
    def __init__(self, service, name):
        self.service, self.name = service, name
        self.url = "https://fixture.invalid/container/" + quote(name, safe="/")

    def fail(self, operation):
        error = self.service.store.failures.get((operation, self.name))
        if error is not None:
            raise error
        if self.service.closed:
            raise AssertionError("Using a closed test service")

    async def get_blob_properties(self):
        self.fail("stat")
        if self.name not in self.service.store.files:
            raise storage_error(404, StorageErrorCode.BLOB_NOT_FOUND)
        return self.service.store.files[self.name].properties

    async def upload_blob(self, content, *, overwrite, metadata=None):
        self.fail("write")
        files = self.service.store.files
        if self.name in files and not overwrite:
            raise storage_error(409, StorageErrorCode.BLOB_ALREADY_EXISTS)
        now = datetime.now(UTC)
        created = files[self.name].properties.creation_time if self.name in files else now
        properties = BlobProperties()
        properties.name = self.name
        properties.size = len(content)
        properties.last_modified = now
        properties.creation_time = created
        properties.metadata = dict(metadata or {})
        files[self.name] = MemoryFile(content, properties)

    async def set_blob_metadata(self, metadata):
        self.fail("metadata")
        properties = await self.get_blob_properties()
        properties.metadata = dict(metadata)
        properties.last_modified = datetime.now(UTC)

    async def download_blob(self):
        self.fail("read")
        await self.get_blob_properties()
        content = self.service.store.files[self.name].content

        async def readinto(stream):
            return stream.write(content)

        return SimpleNamespace(readinto=readinto)

    async def delete_blob(self):
        self.fail("delete")
        await self.get_blob_properties()
        del self.service.store.files[self.name]

    async def upload_blob_from_url(self, url, *, overwrite, source_authorization=None):
        self.fail("copy")
        source = unquote(urlsplit(url).path).removeprefix("/container/")
        source_file = self.service.store.files[source]
        self.service.store.copies.append((source, self.name, source_authorization))
        await self.upload_blob(
            source_file.content, overwrite=overwrite, metadata=source_file.properties.metadata
        )


class MemoryContainer:
    def __init__(self, service):
        self.service = service

    async def create_container(self):
        error = self.service.store.failures.get(("container", "container"))
        if error is not None:
            raise error
        if self.service.store.container_exists:
            raise storage_error(409, StorageErrorCode.CONTAINER_ALREADY_EXISTS)
        self.service.store.container_exists = True

    def list_blobs(self, *, name_starts_with):
        async def entries():
            error = self.service.store.failures.get(("list", name_starts_with))
            if error is not None:
                raise error
            for name in sorted(self.service.store.files):
                if name.startswith(name_starts_with):
                    yield self.service.store.files[name].properties

        return entries()


class MemoryService:
    def __init__(self, store):
        self.store = store
        self.closed = False
        self.close_count = 0
        store.services.append(self)

    def get_container_client(self, _name):
        return MemoryContainer(self)

    def get_blob_client(self, *, container, blob):
        assert container == "container"
        return MemoryBlob(self, blob)

    async def close(self):
        self.closed = True
        self.close_count += 1


def directory_link(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt" or shutil.which("powershell.exe") is None:
            pytest.skip("Creating directory links is unavailable on this host")
        quoted_link = str(link).replace("'", "''")
        quoted_target = str(target).replace("'", "''")
        subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                f"New-Item -ItemType Junction -Path '{quoted_link}' "
                f"-Target '{quoted_target}' -ErrorAction Stop | Out-Null",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )


@pytest.fixture
def local_route(tmp_path, monkeypatch):
    for name in (
        "AzureWebJobsStorage",
        "AzureWebJobsStorage__blobServiceUri",
        "WEBSITE_OWNER_NAME",
        "WEBSITE_DEPLOYMENT_ID",
        "WEBSITE_SITE_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "sessions"))
    return resolve_route(tmp_path)


@pytest.fixture
def memory_blobs(monkeypatch):
    store = MemoryBlobs()

    async def open_owned(_settings):
        service = MemoryService(store)
        credential = SimpleNamespace(
            close=AsyncMock(),
            get_token=AsyncMock(return_value=AccessToken("fixture-token", 0)),
        )
        store.credentials.append(credential)
        return OwnedBlobService(service, credential)

    monkeypatch.setattr(fs, "open_blob_service", open_owned)
    return store


@pytest_asyncio.fixture(params=["local", "blob"])
async def file_case(request, local_route, memory_blobs):
    route = local_route
    if request.param == "blob":
        route = replace(
            route,
            blob=BlobStorageSettings(
                container_name="container", blob_service_url="https://fixture.invalid"
            ),
        )
    provider = await open_session_fs(route, "agent", "session", conventions="posix")
    try:
        yield provider, route, memory_blobs
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_native_files_are_ordinary_opaque_files_without_an_envelope(local_route):
    provider = await open_session_fs(local_route, "agent", "session", conventions="posix")
    content = "\x00opaque\r\n雪\nnot-json"
    try:
        await provider.write_file("/session-state/arbitrary.sdk", content)
        path = local_route.local_dir / session_prefix(local_route, "agent", "session")
        assert (path / "session-state" / "arbitrary.sdk").read_bytes() == content.encode()
        assert not (path / "state.json").exists()
        assert await provider.read_file("/session-state/arbitrary.sdk") == content
        assert (await provider.stat("/session-state/arbitrary.sdk")).size == len(content.encode())
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_missing_file_and_directory_are_not_empty_success(local_route):
    provider = await open_session_fs(local_route, "agent", "session", conventions="posix")
    try:
        assert not await provider.exists("/session-state/missing")
        for operation in (provider.read_file, provider.stat, provider.readdir):
            with pytest.raises(OSError) as caught:
                await operation("/session-state/missing")
            assert caught.value.errno == errno.ENOENT
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_all_filesystem_operations_preserve_opaque_sdk_files(file_case):
    provider, route, store = file_case
    content = "\x00opaque\r\n雪\nnot-json"
    await provider.mkdir("/session-state", recursive=False)
    await provider.mkdir("/session-state/empty", recursive=False, mode=0o700)
    await provider.write_file("/session-state/tree/arbitrary.sdk", content, mode=0o600)
    await provider.append_file("/session-state/tree/arbitrary.sdk", "\r\nmore")
    assert await provider.read_file("/session-state/tree/arbitrary.sdk") == content + "\r\nmore"
    assert await provider.exists("/session-state/tree/./arbitrary.sdk")
    info = await provider.stat("/session-state/tree/arbitrary.sdk")
    assert info.is_file and not info.is_directory
    assert info.size == len((content + "\r\nmore").encode())
    assert info.mtime.tzinfo is UTC and info.birthtime.tzinfo is UTC
    directory = await provider.stat("/session-state/tree")
    assert directory.is_directory and not directory.is_file
    entries = await provider.readdir_with_types("/session-state")
    assert [(entry.name, entry.type.value) for entry in entries] == [
        ("empty", "directory"), ("tree", "directory")
    ]
    await provider.rename("/session-state/tree/arbitrary.sdk", "/session-state/tree/renamed.sdk")
    assert not await provider.exists("/session-state/tree/arbitrary.sdk")
    await provider.rename("/session-state/tree", "/session-state/moved")
    assert await provider.read_file("/session-state/moved/renamed.sdk") == content + "\r\nmore"
    assert not await provider.exists("/session-state/tree")
    with pytest.raises(OSError) as caught:
        await provider.rm("/session-state/moved", recursive=False, force=False)
    assert caught.value.errno == errno.ENOTEMPTY
    await provider.rm("/session-state/moved", recursive=True, force=False)
    await provider.rm("/session-state/empty", recursive=False, force=False)
    await provider.rm("/session-state", recursive=False, force=False)
    assert await provider.readdir("/") == ["workspace"]
    if route.mode is StorageMode.BLOB:
        assert all(name.startswith("copilot-native/local/agent/session/") for name in store.files)
        assert not any(name.endswith("state.json") for name in store.files)
        assert store.copies
        assert all(authorization == "Bearer fixture-token" for _, _, authorization in store.copies)
        assert not route.local_dir.exists()


@pytest.mark.asyncio
async def test_arbitrary_filenames_and_sizes_have_no_host_format_or_aggregate_cap(file_case):
    provider, route, store = file_case
    content = "x" * (4 * 1024 * 1024 + 1)
    for name in ("state.json", "unexpected-sdk-format.new", "summary-雪.dat"):
        await provider.write_file("/workspace/" + name, content)
        assert await provider.read_file("/workspace/" + name) == content
        assert (await provider.stat("/workspace/" + name)).size == len(content)
    if route.mode is StorageMode.BLOB:
        prefix = session_prefix(route, "agent", "session")
        assert store.files[prefix + "/workspace/state.json"].content == content.encode()
        assert store.files[prefix + "/workspace/summary-雪.dat"].content == content.encode()


@pytest.mark.asyncio
async def test_parallel_appends_are_callback_safe_without_a_turn_protocol(file_case):
    provider, _, _ = file_case
    await provider.write_file("/workspace/journal", "")
    await asyncio.gather(*[
        provider.append_file("/workspace/journal", f"{index},") for index in range(20)
    ])
    assert await provider.read_file("/workspace/journal") == "".join(
        f"{index}," for index in range(20)
    )


@pytest.mark.asyncio
async def test_reopening_reads_acknowledged_files_without_a_host_completion_step(file_case):
    provider, route, _ = file_case
    await provider.write_file("/workspace/journal", "sdk-owned")
    await provider.close()
    reopened = await open_session_fs(route, "agent", "session", conventions="posix")
    try:
        assert await reopened.read_file("/workspace/journal") == "sdk-owned"
        await reopened.append_file("/workspace/journal", "-continued")
        assert await reopened.read_file("/workspace/journal") == "sdk-owned-continued"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_missing_paths_and_existing_file_parents_have_filesystem_errors(file_case):
    provider, _, _ = file_case
    await provider.write_file("/workspace/file", "kept")
    operations = [
        (lambda: provider.read_file("/workspace/missing"), errno.ENOENT),
        (lambda: provider.stat("/workspace/missing"), errno.ENOENT),
        (lambda: provider.readdir("/workspace/missing"), errno.ENOENT),
        (lambda: provider.readdir("/workspace/file"), errno.ENOTDIR),
        (lambda: provider.stat("/workspace/file/child"), errno.ENOTDIR),
        (lambda: provider.write_file("/workspace/file/child", "denied"), errno.ENOTDIR),
        (lambda: provider.append_file("/workspace/file/child", "denied"), errno.ENOTDIR),
        (lambda: provider.mkdir("/workspace/file/child", recursive=True), errno.ENOTDIR),
        (lambda: provider.mkdir("/workspace/missing/child", recursive=False), errno.ENOENT),
        (lambda: provider.mkdir("/workspace/file", recursive=True), errno.EEXIST),
        (lambda: provider.mkdir("/workspace", recursive=False), errno.EEXIST),
        (lambda: provider.rm("/workspace/missing", recursive=False, force=False), errno.ENOENT),
        (lambda: provider.rename("/workspace/missing", "/workspace/target"), errno.ENOENT),
        (lambda: provider.rename("/workspace/file", "/workspace/missing/target"), errno.ENOENT),
        (lambda: provider.read_file("/workspace"), errno.EISDIR),
        (lambda: provider.write_file("/workspace", "denied"), errno.EISDIR),
        (lambda: provider.append_file("/workspace", "denied"), errno.EISDIR),
    ]
    for operation, code in operations:
        with pytest.raises(OSError) as caught:
            await operation()
        assert caught.value.errno == code
    assert not await provider.exists("/workspace/file/child")
    await provider.rm("/workspace/missing", recursive=True, force=True)
    await provider.mkdir("/workspace", recursive=True)
    assert await provider.read_file("/workspace/file") == "kept"


@pytest.mark.asyncio
async def test_rename_replacement_and_directory_rules(file_case):
    provider, _, _ = file_case
    await provider.write_file("/workspace/old", "original")
    await provider.write_file("/workspace/new", "replacement")
    await provider.rename("/workspace/old", "/workspace/new")
    assert await provider.read_file("/workspace/new") == "original"
    await provider.rename("/workspace/new", "/workspace/new")
    assert await provider.read_file("/workspace/new") == "original"
    await provider.write_file("/workspace/directory/child", "nested")
    await provider.mkdir("/workspace/empty", recursive=False)
    await provider.rename("/workspace/directory", "/workspace/empty")
    assert await provider.read_file("/workspace/empty/child") == "nested"
    await provider.rename("/workspace/empty", "/workspace/empty")
    for src, dest, code in (
        ("/workspace/new", "/workspace/empty", errno.EISDIR),
        ("/workspace/empty", "/workspace/new", errno.ENOTDIR),
        ("/workspace/empty", "/workspace/empty/descendant", errno.EINVAL),
        ("/", "/workspace/root", errno.EACCES),
        ("/workspace/new", "/", errno.EACCES),
    ):
        with pytest.raises(OSError) as caught:
            await provider.rename(src, dest)
        assert caught.value.errno == code
    await provider.write_file("/workspace/other/child", "other")
    with pytest.raises(OSError) as caught:
        await provider.rename("/workspace/empty", "/workspace/other")
    assert caught.value.errno == errno.ENOTEMPTY
    with pytest.raises(OSError) as caught:
        await provider.rename("/workspace/missing", "/workspace/missing")
    assert caught.value.errno == errno.ENOENT


@pytest.mark.asyncio
async def test_empty_append_creates_a_file_and_write_truncates_without_changing_birthtime(file_case):
    provider, _, _ = file_case
    await provider.append_file("/workspace/deep/file", "")
    before = await provider.stat("/workspace/deep/file")
    assert before.size == 0
    await provider.append_file("/workspace/deep/file", "雪")
    await provider.write_file("/workspace/deep/file", "x")
    after = await provider.stat("/workspace/deep/file")
    assert after.size == 1
    assert after.mtime >= before.mtime
    if os.name == "nt":
        assert after.birthtime == before.birthtime
    assert await provider.read_file("/workspace/deep/file") == "x"


@pytest.mark.asyncio
async def test_invalid_utf8_surfaces_without_interpreting_or_rewriting_native_bytes(file_case):
    provider, _, _ = file_case
    await provider.backend.write_file("workspace/binary", b"\xfffixture-content", mode=None)
    with pytest.raises(OSError) as caught:
        await provider.read_file("/workspace/binary")
    assert caught.value.errno == errno.EILSEQ
    assert "fixture-content" not in str(caught.value)
    assert await provider.backend.read_file("workspace/binary") == b"\xfffixture-content"
    await provider.write_file("/workspace/other", "still usable")


@pytest.mark.asyncio
async def test_independent_sessions_have_independent_callback_locks(file_case, monkeypatch):
    provider, route, _ = file_case
    other = await open_session_fs(route, "agent", "other", conventions="posix")
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.backend.write_file

    async def held(*args):
        entered.set()
        await release.wait()
        await original(*args)

    monkeypatch.setattr(provider.backend, "write_file", held)
    blocked = asyncio.create_task(provider.write_file("/workspace/file", "held"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.wait_for(other.write_file("/workspace/file", "independent"), timeout=1)
        assert await other.read_file("/workspace/file") == "independent"
        release.set()
        await blocked
        assert await provider.read_file("/workspace/file") == "held"
    finally:
        release.set()
        await blocked
        await other.close()


@pytest.mark.asyncio
async def test_callback_cancellation_propagates_and_does_not_poison_the_adapter(file_case, monkeypatch):
    provider, _, _ = file_case
    entered = asyncio.Event()
    original = provider.backend.write_file

    async def cancelled(*_args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(provider.backend, "write_file", cancelled)
    pending = asyncio.create_task(provider.write_file("/workspace/file", "cancelled"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    monkeypatch.setattr(provider.backend, "write_file", original)
    await provider.write_file("/workspace/file", "retry")
    assert await provider.read_file("/workspace/file") == "retry"


@pytest.mark.asyncio
async def test_closed_adapter_rejects_callbacks_and_closes_only_owned_resources(file_case):
    provider, route, store = file_case
    await provider.close()
    await provider.close()
    with pytest.raises(OSError) as caught:
        await provider.stat("/workspace")
    assert caught.value.errno == errno.EBADF
    if route.mode is StorageMode.BLOB:
        assert [service.close_count for service in store.services] == [1]
        store.credentials[0].close.assert_awaited_once()


@pytest.mark.parametrize(
    ("path", "conventions"),
    [
        ("", "posix"),
        ("../outside", "posix"),
        ("/workspace/a/../escape", "posix"),
        ("/etc/passwd", "posix"),
        ("/workspace/\x00name", "posix"),
        ("/workspace/\x01name", "posix"),
        ("\\session-state\\file", "posix"),
        ("C:\\outside\\file", "windows"),
        ("c:/outside/file", "windows"),
        ("\\\\server\\share\\file", "windows"),
        ("//server/share/file", "windows"),
        ("\\session-state\\..\\escape", "windows"),
        ("\\workspace\\file:stream", "windows"),
        ("\\workspace\\CON", "windows"),
    ],
)
def test_paths_cannot_escape_virtual_roots(path, conventions):
    with pytest.raises(OSError):
        fs.normalize_callback_path(path, conventions)


@pytest.mark.parametrize(
    ("path", "conventions", "workspace", "expected"),
    [
        ("/", "posix", "", "/"),
        ("file", "posix", "", "/workspace/file"),
        ("./deep/file", "posix", "", "/workspace/deep/file"),
        ("/workspace/./file", "posix", "", "/workspace/file"),
        ("\\session-state\\deep\\file", "windows", "", "/session-state/deep/file"),
        ("C:\\app\\ws\\file", "windows", "C:\\app\\ws", "/workspace/file"),
        ("c:/APP/ws/deep/file", "windows", "C:\\app\\ws", "/workspace/deep/file"),
        ("/srv/ws/file", "posix", "/srv/ws", "/workspace/file"),
    ],
)
def test_only_the_exact_current_workspace_is_mapped(path, conventions, workspace, expected):
    assert fs.normalize_callback_path(path, conventions, workspace) == expected


@pytest.mark.parametrize(
    ("path", "conventions", "workspace"),
    [
        ("C:\\app\\ws-old\\file", "windows", "C:\\app\\ws"),
        ("C:\\old-worker\\ws\\file", "windows", "C:\\current-worker\\ws"),
        ("D:\\app\\ws\\file", "windows", "C:\\app\\ws"),
        ("C:\\app\\ws\\..\\escape", "windows", "C:\\app\\ws"),
        ("/SRV/ws/file", "posix", "/srv/ws"),
        ("/old-worker/ws/file", "posix", "/current-worker/ws"),
    ],
)
def test_recorded_unknown_workspaces_are_not_adopted_by_suffix(path, conventions, workspace):
    with pytest.raises(OSError):
        fs.normalize_callback_path(path, conventions, workspace)


@pytest.mark.asyncio
async def test_windows_callbacks_use_the_same_opaque_posix_blob_keys(local_route, memory_blobs):
    route = replace(
        local_route,
        blob=BlobStorageSettings(container_name="container", blob_service_url="https://fixture.invalid"),
    )
    provider = await open_session_fs(
        route, "agent", "session", conventions="windows", workspace_path="C:\\app\\ws"
    )
    try:
        await provider.write_file("\\session-state\\workspace.yaml", "cwd: opaque\r\n")
        await provider.write_file("c:/APP/ws/notes.txt", "workspace")
        await provider.rename(
            "\\session-state\\workspace.yaml", "\\session-state\\renamed.yaml"
        )
        prefix = session_prefix(route, "agent", "session")
        assert memory_blobs.files[prefix + "/session-state/renamed.yaml"].content == b"cwd: opaque\r\n"
        assert memory_blobs.files[prefix + "/workspace/notes.txt"].content == b"workspace"
        assert not any("\\" in name for name in memory_blobs.files)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_local_storage_denies_symlinked_parents_without_touching_the_target(
    local_route, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = local_route.local_dir
    directory_link(link, outside)
    with pytest.raises(OSError) as caught:
        await open_session_fs(local_route, "agent", "session")
    assert caught.value.errno == errno.EACCES
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_blob_failures_are_not_absence_or_a_fallback(local_route, memory_blobs):
    route = replace(
        local_route,
        blob=BlobStorageSettings(container_name="container", blob_service_url="https://fixture.invalid"),
    )
    provider = await open_session_fs(route, "agent", "session", conventions="posix")
    name = session_prefix(route, "agent", "session") + "/workspace/file"
    try:
        await provider.write_file("/workspace/file", "acknowledged")
        memory_blobs.failures[("stat", name)] = storage_error(
            403, StorageErrorCode.AUTHENTICATION_FAILED
        )
        for operation in (provider.stat, provider.exists, provider.read_file):
            with pytest.raises(NativeSessionError) as caught:
                await operation("/workspace/file")
            assert caught.value.errno == errno.EACCES
            assert "fixture-secret" not in str(caught.value)
        memory_blobs.failures.clear()
        memory_blobs.failures[("write", name)] = ServiceRequestError("fixture-secret")
        with pytest.raises(NativeSessionError) as caught:
            await provider.write_file("/workspace/file", "not acknowledged")
        assert caught.value.errno == errno.EIO
        assert memory_blobs.files[name].content == b"acknowledged"
        assert not route.local_dir.exists()
        memory_blobs.failures.clear()
        await provider.write_file("/workspace/file", "retry")
        assert await provider.read_file("/workspace/file") == "retry"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_missing_container_is_not_a_missing_file(local_route, memory_blobs):
    route = replace(
        local_route,
        blob=BlobStorageSettings(container_name="container", blob_service_url="https://fixture.invalid"),
    )
    provider = await open_session_fs(route, "agent", "session", conventions="posix")
    name = session_prefix(route, "agent", "session") + "/workspace/missing"
    try:
        memory_blobs.failures[("stat", name)] = storage_error(404, StorageErrorCode.CONTAINER_NOT_FOUND)
        with pytest.raises(NativeSessionError) as caught:
            await provider.exists("/workspace/missing")
        assert caught.value.errno == errno.EIO
        assert not route.local_dir.exists()
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_blob_copy_delete_failure_surfaces_without_a_host_rollback(local_route, memory_blobs):
    route = replace(
        local_route,
        blob=BlobStorageSettings(container_name="container", blob_service_url="https://fixture.invalid"),
    )
    provider = await open_session_fs(route, "agent", "session", conventions="posix")
    prefix = session_prefix(route, "agent", "session") + "/workspace/"
    try:
        await provider.write_file("/workspace/source", "opaque")
        memory_blobs.failures[("delete", prefix + "source")] = ServiceRequestError("fixture-secret")
        with pytest.raises(NativeSessionError):
            await provider.rename("/workspace/source", "/workspace/target")
        assert memory_blobs.files[prefix + "source"].content == b"opaque"
        assert memory_blobs.files[prefix + "target"].content == b"opaque"
        assert not any("uncertain" in name or "tombstone" in name for name in memory_blobs.files)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_blob_initialization_failure_closes_service_and_credential(local_route, memory_blobs):
    route = replace(
        local_route,
        blob=BlobStorageSettings(container_name="container", blob_service_url="https://fixture.invalid"),
    )
    memory_blobs.failures[("container", "container")] = ServiceRequestError("fixture-secret")
    with pytest.raises(NativeSessionError):
        await open_session_fs(route, "agent", "session")
    assert [service.close_count for service in memory_blobs.services] == [1]
    memory_blobs.credentials[0].close.assert_awaited_once()
    assert not route.local_dir.exists()


@pytest.mark.asyncio
async def test_invalid_configured_blob_does_not_construct_local_files(local_route):
    route = replace(local_route, blob=BlobStorageSettings(connection_string="fixture-secret"))
    with pytest.raises(NativeSessionError) as caught:
        await open_session_fs(route, "agent", "session")
    assert "fixture-secret" not in str(caught.value)
    assert not route.local_dir.exists()


@pytest.mark.asyncio
async def test_callback_wait_and_io_use_the_supplied_request_deadline(file_case, monkeypatch):
    provider, _, _ = file_case
    provider.deadline = asyncio.get_running_loop().time() + 0.1
    entered = asyncio.Event()

    async def held(*_args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(provider.backend, "write_file", held)
    blocked = asyncio.create_task(provider.write_file("/workspace/file", "held"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    queued = asyncio.create_task(provider.stat("/workspace"))
    results = await asyncio.gather(blocked, queued, return_exceptions=True)
    assert all(result.errno == errno.ETIMEDOUT for result in results)
    await provider.close()


@pytest.mark.asyncio
async def test_expired_open_deadline_still_closes_configured_blob_resources(local_route, memory_blobs):
    route = replace(
        local_route,
        blob=BlobStorageSettings(container_name="container", blob_service_url="https://fixture.invalid"),
    )
    with pytest.raises(NativeSessionError) as caught:
        await open_session_fs(
            route, "agent", "session", deadline=asyncio.get_running_loop().time() - 1
        )
    assert caught.value.errno == errno.ETIMEDOUT
    assert memory_blobs.services[0].close_count == 1
    memory_blobs.credentials[0].close.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_close_failure_is_reported_after_credential_close(file_case, monkeypatch):
    provider, route, store = file_case
    if route.mode is StorageMode.LOCAL:
        return
    service = store.services[0]
    monkeypatch.setattr(service, "close", AsyncMock(side_effect=ServiceRequestError("fixture-secret")))
    with pytest.raises(NativeSessionError) as caught:
        await provider.close()
    assert caught.value.errno == errno.EIO
    assert "fixture-secret" not in str(caught.value)
    store.credentials[0].close.assert_awaited_once()
    monkeypatch.setattr(service, "close", AsyncMock())


@pytest.mark.asyncio
async def test_native_storage_neither_imports_nor_probes_maf_history(local_route, monkeypatch):
    history = local_route.local_dir / "agent-sessions" / "agent" / "session.jsonl"
    history.parent.mkdir(parents=True)
    history.write_bytes(b"uninterpreted MAF bytes\x00")
    monkeypatch.setitem(sys.modules, "azure_functions_agents._blob_history", None)
    monkeypatch.setitem(sys.modules, "azure_functions_agents._file_history", None)
    provider = await open_session_fs(local_route, "agent", "session", conventions="posix")
    try:
        await provider.write_file("/workspace/sdk-file", "native")
        assert history.read_bytes() == b"uninterpreted MAF bytes\x00"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_local_callback_denies_links_inside_the_session_tree(local_route, tmp_path):
    provider = await open_session_fs(local_route, "agent", "session", conventions="posix")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept").write_bytes(b"not a session file")
    root = local_route.local_dir / session_prefix(local_route, "agent", "session")
    link = root / "workspace" / "link"
    try:
        directory_link(link, outside)
        for operation in (provider.stat, provider.read_file):
            with pytest.raises(OSError) as caught:
                await operation("/workspace/link/kept")
            assert caught.value.errno == errno.EACCES
        with pytest.raises(OSError) as caught:
            await provider.write_file("/workspace/link/kept", "not written")
        assert caught.value.errno == errno.EACCES
        assert (outside / "kept").read_bytes() == b"not a session file"
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True])
async def test_backend_constructor_failure_closes_both_owned_resources(
    local_route, memory_blobs, monkeypatch, close_fails
):
    route = replace(
        local_route,
        blob=BlobStorageSettings(container_name="container", blob_service_url="https://fixture.invalid"),
    )

    def fail_client(_service, _name):
        raise ValueError("fixture-secret")

    monkeypatch.setattr(MemoryService, "get_container_client", fail_client)
    if close_fails:
        async def fail_close(service):
            service.close_count += 1
            raise ServiceRequestError("fixture-secret")

        monkeypatch.setattr(MemoryService, "close", fail_close)
    with pytest.raises(NativeSessionError) as caught:
        await open_session_fs(route, "agent", "session")
    assert "fixture-secret" not in str(caught.value)
    assert memory_blobs.services[0].close_count == 1
    memory_blobs.credentials[0].close.assert_awaited_once()
