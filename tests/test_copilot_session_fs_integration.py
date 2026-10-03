from __future__ import annotations

import errno
import os
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import pytest_asyncio
from azure.core.credentials import AccessToken
from azure.core.exceptions import ServiceRequestError
from azure.storage.blob.aio import BlobServiceClient
from copilot.generated import rpc
from copilot.session_fs_provider import create_session_fs_adapter

from azure_functions_agents import _copilot_session_fs as fs
from azure_functions_agents._copilot_session_fs import open_session_fs
from azure_functions_agents._native_session_identity import (
    NativeSessionError,
    StorageRoute,
    resolve_route,
    session_prefix,
)
from azure_functions_agents._session_storage import (
    BlobStorageSettings,
    OwnedBlobService,
    open_blob_service,
)
from tests._blob_sdk_errors import ScriptedBlobTransport
from tests.test_copilot_session_fs import file_case as file_case
from tests.test_copilot_session_fs import local_route as local_route
from tests.test_copilot_session_fs import memory_blobs as memory_blobs

SDK_SESSION = "sdk-session"
CONNECTION = "AZURE_FUNCTIONS_AGENTS_TEST_BLOB_CONNECTION_STRING"
SERVICE_URI = "AZURE_FUNCTIONS_AGENTS_TEST_BLOB_SERVICE_URI"
CONTAINER = "AZURE_FUNCTIONS_AGENTS_TEST_BLOB_CONTAINER"
OPT_IN = "AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB"


class BufferedBlobStream:
    def __init__(self, response):
        self.response = response
        self.content_length = int(response.headers["Content-Length"])

    def __aiter__(self):
        return self.response.iter_bytes()


class PrivateRenameTransport(ScriptedBlobTransport):
    """Serve one private source blob and reject unauthenticated source-URL copies."""

    def __init__(self, content, metadata, upload_fails):
        super().__init__()
        self.content, self.metadata, self.upload_fails = content, metadata, upload_fails

    async def send(self, request, **kwargs):
        if request.method == "GET":
            if not self.content and "x-ms-range" in request.headers:
                status, code = 416, "InvalidRange"
            else:
                status, code = (206 if self.content else 200), None
        elif request.method == "PUT":
            if "x-ms-copy-source" in request.headers:
                status, code = 403, "CannotVerifyCopySource"
            else:
                status, code = (503, "ServerBusy") if self.upload_fails else (201, None)
        else:
            assert request.method == "DELETE"
            status, code = 202, None
        self.responses.append((status, code))
        response = await super().send(request, **kwargs)
        if request.method == "GET" and code is None:
            response.headers.update({
                "Content-Length": str(len(self.content)),
                "Content-Type": "application/octet-stream",
                "Last-Modified": "Fri, 02 Oct 2026 21:00:00 GMT",
                "x-ms-creation-time": "Fri, 02 Oct 2026 21:00:00 GMT",
                "x-ms-blob-type": "BlockBlob",
                "ETag": '"fixture-etag"',
            })
            if self.content:
                response.headers["Content-Range"] = f"bytes 0-{len(self.content) - 1}/{len(self.content)}"
            response.headers.update({f"x-ms-meta-{key}": value for key, value in self.metadata.items()})
            response._content = self.content
            response._stream_download_generator = (
                lambda _pipeline, current, **_kwargs: BufferedBlobStream(current)
            )
        return response


@pytest.mark.asyncio
async def test_real_sdk_handler_reports_missing_file(tmp_path, monkeypatch):
    for name in ("AzureWebJobsStorage", "AzureWebJobsStorage__blobServiceUri"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "sessions"))
    provider = await open_session_fs(resolve_route(tmp_path), "agent", "session")
    try:
        handler = create_session_fs_adapter(provider)
        result = await handler.read_file(
            rpc.SessionFSReadFileRequest(path="/workspace/missing", session_id=SDK_SESSION)
        )
        assert result.error.code is rpc.SessionFSErrorCode.ENOENT
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_real_sdk_create_resume_factory_registers_callbacks_before_rpc(file_case, tmp_path, resume):
    from copilot import CopilotClient, RuntimeConnection

    from azure_functions_agents._copilot_capabilities import permission_handler
    from azure_functions_agents._skill_policy import SkillPolicy

    provider, _, _ = file_case
    workspace = tmp_path / "host-workspace"
    workspace.mkdir()
    on_permission_request = permission_handler(
        SkillPolicy.create(approved=(), discovered=(), working_directory=workspace)
    )
    provider.workspace_path = str(workspace)
    provider.conventions = fs.HOST_PATH_CONVENTIONS
    client = CopilotClient(
        connection=RuntimeConnection.for_stdio(path=sys.executable),
        mode="empty",
        use_logged_in_user=False,
        telemetry=None,
        session_fs={
            "initial_working_directory": str(workspace),
            "session_state_path": fs.SESSION_STATE_ROOT,
            "conventions": fs.HOST_PATH_CONVENTIONS,
        },
    )
    methods, factory_sessions = [], []
    content = "\x00opaque\r\nSDK callback"
    native_id = "main.sdk-session"

    def factory(session):
        factory_sessions.append(session.session_id)
        return provider

    async def request(method, payload, **_kwargs):
        methods.append(method)
        if method in {"session.create", "session.resume", "session.detach"}:
            session = client._sessions[payload["sessionId"]]
            handler = session._client_session_apis.session_fs
            assert handler is not None
            if method == "session.detach":
                read = await handler.read_file(rpc.SessionFSReadFileRequest(
                    path=str(workspace / "opaque.sdk"), session_id=native_id
                ))
                assert read.error is None and read.content == content
                return {"success": True}
            assert factory_sessions == [native_id]
            error = await handler.write_file(rpc.SessionFSWriteFileRequest(
                path=str(workspace / "opaque.sdk"), session_id=native_id, content=content
            ))
            assert error is None
            return {"sessionId": native_id, "workspacePath": str(workspace)}
        assert method == "session.options.update"
        return rpc.SessionUpdateOptionsResult(success=True).to_dict()

    transport = SimpleNamespace(request=AsyncMock(side_effect=request), stop=AsyncMock())
    client._client = transport
    client._state = "connected"
    session = None
    try:
        options = {
            "model": "offline",
            "tools": [],
            "available_tools": [],
            "create_session_fs_handler": factory,
            "on_permission_request": on_permission_request,
        }
        if resume:
            session = await client.resume_session(native_id, **options)
        else:
            session = await client.create_session(session_id=native_id, **options)
        assert session.session_id == native_id
        assert await provider.read_file("/workspace/opaque.sdk") == content
        await session.disconnect()
        assert methods == [
            "session.resume" if resume else "session.create",
            "session.options.update",
            "session.detach",
        ]
        assert "session.getMetadata" not in methods
        assert "session.send" not in methods
    finally:
        if session is not None:
            await session.disconnect()
        await provider.close()
        await client.force_stop()


@pytest.mark.asyncio
async def test_real_sdk_handler_exercises_each_required_callback(file_case):
    provider, _, _ = file_case
    handler = create_session_fs_adapter(provider)
    content = "\x00opaque\r\n雪"
    assert await handler.mkdir(rpc.SessionFSMkdirRequest(
        path="/session-state/empty", session_id=SDK_SESSION, recursive=True, mode=0o700
    )) is None
    assert await handler.write_file(rpc.SessionFSWriteFileRequest(
        path="/session-state/opaque.sdk", session_id=SDK_SESSION, content=content, mode=0o600
    )) is None
    assert await handler.append_file(rpc.SessionFSAppendFileRequest(
        path="/session-state/opaque.sdk", session_id=SDK_SESSION, content="\r\nmore"
    )) is None
    read = await handler.read_file(rpc.SessionFSReadFileRequest(
        path="/session-state/opaque.sdk", session_id=SDK_SESSION
    ))
    assert read.error is None and read.content == content + "\r\nmore"
    stat = await handler.stat(rpc.SessionFSStatRequest(
        path="/session-state/opaque.sdk", session_id=SDK_SESSION
    ))
    assert stat.error is None
    assert stat.is_file and not stat.is_directory
    assert stat.size == len((content + "\r\nmore").encode())
    assert (await handler.exists(rpc.SessionFSExistsRequest(
        path="/session-state/opaque.sdk", session_id=SDK_SESSION
    ))).exists
    listing = await handler.readdir(rpc.SessionFSReaddirRequest(
        path="/session-state", session_id=SDK_SESSION
    ))
    assert listing.error is None and listing.entries == ["empty", "opaque.sdk"]
    typed = await handler.readdir_with_types(rpc.SessionFSReaddirWithTypesRequest(
        path="/session-state", session_id=SDK_SESSION
    ))
    assert typed.error is None
    assert [(entry.name, entry.type.value) for entry in typed.entries] == [
        ("empty", "directory"), ("opaque.sdk", "file")
    ]
    assert await handler.rename(rpc.SessionFSRenameRequest(
        src="/session-state/opaque.sdk", dest="/session-state/renamed.sdk", session_id=SDK_SESSION
    )) is None
    assert await handler.rm(rpc.SessionFSRmRequest(
        path="/session-state", session_id=SDK_SESSION, recursive=True, force=False
    )) is None
    missing = await handler.stat(rpc.SessionFSStatRequest(
        path="/session-state", session_id=SDK_SESSION
    ))
    assert missing.error.code is rpc.SessionFSErrorCode.ENOENT


@pytest.mark.asyncio
async def test_real_sdk_maps_denied_paths_and_directory_errors_without_poisoning(file_case):
    provider, _, _ = file_case
    handler = create_session_fs_adapter(provider)
    for path in ("/workspace", "/etc/passwd", "/workspace/../escape"):
        result = await handler.read_file(rpc.SessionFSReadFileRequest(
            path=path, session_id=SDK_SESSION
        ))
        assert result.error.code is rpc.SessionFSErrorCode.UNKNOWN
    result = await handler.write_file(rpc.SessionFSWriteFileRequest(
        path="/etc/passwd", session_id=SDK_SESSION, content="not written"
    ))
    assert result.code is rpc.SessionFSErrorCode.UNKNOWN
    assert await handler.write_file(rpc.SessionFSWriteFileRequest(
        path="/workspace/allowed", session_id=SDK_SESSION, content="allowed"
    )) is None
    assert await provider.read_file("/workspace/allowed") == "allowed"


@pytest.mark.asyncio
async def test_pinned_sdk_exists_masks_errors_but_the_provider_does_not(file_case, monkeypatch):
    provider, _, _ = file_case
    handler = create_session_fs_adapter(provider)
    original = provider.backend.stat
    monkeypatch.setattr(provider.backend, "stat", AsyncMock(
        side_effect=ServiceRequestError("fixture-secret")
    ))
    with pytest.raises(NativeSessionError) as caught:
        await provider.exists("/workspace/file")
    assert caught.value.errno == errno.EIO
    result = await handler.exists(rpc.SessionFSExistsRequest(
        path="/workspace/file", session_id=SDK_SESSION
    ))
    assert result.exists is False
    stat = await handler.stat(rpc.SessionFSStatRequest(
        path="/workspace/file", session_id=SDK_SESSION
    ))
    assert stat.error.code is rpc.SessionFSErrorCode.UNKNOWN
    assert "fixture-secret" not in stat.error.message
    monkeypatch.setattr(provider.backend, "stat", original)
    assert await provider.exists("/workspace")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [(404, "BlobNotFound", None), (404, "ContainerNotFound", errno.EIO), (403, "AuthenticationFailed", errno.EACCES)],
)
async def test_real_blob_sdk_error_pipeline_distinguishes_file_absence(status, code, expected):
    transport = ScriptedBlobTransport((status, code))
    service = BlobServiceClient(
        "https://fixture.invalid", transport=transport, retry_total=0
    )
    backend = fs._BlobFileBackend(OwnedBlobService(service), "container", "owned-prefix")
    provider = fs.NativeSessionFs(backend)
    try:
        if expected is None:
            assert await provider._call(lambda: backend._properties("owned-prefix/file")) is None
        else:
            with pytest.raises(NativeSessionError) as caught:
                await provider._call(lambda: backend._properties("owned-prefix/file"))
            assert caught.value.errno == expected
        assert len(transport.requests) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_real_blob_sdk_identity_rename_copies_then_deletes_without_leases():
    transport = ScriptedBlobTransport((201, None), (202, None))
    service = BlobServiceClient("https://fixture.invalid", transport=transport, retry_total=0)
    credential = SimpleNamespace(
        get_token=AsyncMock(return_value=AccessToken("fixture-token", 0)), close=AsyncMock()
    )
    backend = fs._BlobFileBackend(OwnedBlobService(service, credential), "container", "owned-prefix")
    provider = fs.NativeSessionFs(backend)
    try:
        await provider._call(lambda: backend._copy_delete("owned-prefix/source", "owned-prefix/target"))
        copy, delete = transport.requests
        assert copy.method == "PUT" and delete.method == "DELETE"
        assert unquote(urlsplit(copy.url).path) == "/container/owned-prefix/target"
        assert copy.headers["x-ms-copy-source"] == "https://fixture.invalid/container/owned-prefix/source"
        assert parse_qs(urlsplit(copy.url).query).get("comp") is None
        assert "x-ms-lease-id" not in copy.headers and "x-ms-lease-id" not in delete.headers
        assert copy.headers["x-ms-copy-source-authorization"] == "Bearer fixture-token"
        credential.get_token.assert_awaited_once_with(fs.STORAGE_TOKEN_SCOPE)
    finally:
        await provider.close()
    credential.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("upload_fails", [False, True])
@pytest.mark.parametrize(
    ("content", "metadata", "suffix"),
    [
        ("\x00opaque\r\n雪".encode(), {"fixture": "file"}, "file"),
        (b"", fs.DIRECTORY_METADATA | {"fixture": "directory"}, "directory/"),
    ],
)
async def test_real_account_key_blob_sdk_rename_preserves_bytes_metadata_and_source_on_failure(
    content, metadata, suffix, upload_fails
):
    transport = PrivateRenameTransport(content, metadata, upload_fails)
    service = BlobServiceClient.from_connection_string(
        "DefaultEndpointsProtocol=https;AccountName=fixture;"
        "AccountKey=Zml4dHVyZS1rZXk=;EndpointSuffix=core.windows.net",
        transport=transport, retry_total=0,
    )
    backend = fs._BlobFileBackend(OwnedBlobService(service), "container", "owned-prefix")
    provider = fs.NativeSessionFs(backend)
    source, target = f"owned-prefix/source/{suffix}", f"owned-prefix/target/{suffix}"
    try:
        if upload_fails:
            with pytest.raises(NativeSessionError) as caught:
                await provider._call(lambda: backend._copy_delete(source, target))
            assert caught.value.errno == errno.EIO
        else:
            await provider._call(lambda: backend._copy_delete(source, target))
        downloads = 1 if content else 2
        assert [request.method for request in transport.requests] == (
            ["GET"] * downloads + ["PUT"] + ([] if upload_fails else ["DELETE"])
        )
        for request in transport.requests:
            assert request.headers["Authorization"].startswith("SharedKey fixture:")
            assert "x-ms-copy-source" not in request.headers
            assert "x-ms-lease-id" not in request.headers
            assert "sig" not in parse_qs(urlsplit(request.url).query)
        upload = transport.requests[downloads]
        assert unquote(urlsplit(upload.url).path) == "/container/" + target
        assert upload.body == content
        assert {
            key.removeprefix("x-ms-meta-"): value
            for key, value in upload.headers.items()
            if key.startswith("x-ms-meta-")
        } == metadata
        assert all(
            unquote(urlsplit(request.url).path) == "/container/" + source
            for request in transport.requests[:downloads]
        )
        if not upload_fails:
            assert unquote(urlsplit(transport.requests[-1].url).path) == "/container/" + source
    finally:
        await provider.close()


def integration_route(environ: Mapping[str, str], local_dir: Path) -> StorageRoute | None:
    container = environ.get(CONTAINER, "").strip()
    connection = environ.get(CONNECTION, "").strip()
    uri = environ.get(SERVICE_URI, "").strip()
    if environ.get(OPT_IN) != "1" or not container or not (connection or uri):
        return None
    if connection and uri:
        raise ValueError(f"Set exactly one of {CONNECTION} or {SERVICE_URI}.")
    if uri:
        parts = urlsplit(uri)
        if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
            raise ValueError(f"{SERVICE_URI} requires an https service URI without SAS.")
    return StorageRoute(
        local_dir=local_dir,
        correlation_key="integration-" + uuid.uuid4().hex,
        blob=BlobStorageSettings(
            container_name=container,
            connection_string=connection or None,
            blob_service_url=uri or None,
        ),
    )


def test_live_blob_route_requires_explicit_opt_in_and_safe_target(tmp_path):
    base = {OPT_IN: "1", CONTAINER: "disposable", SERVICE_URI: "https://fixture.invalid"}
    assert integration_route({}, tmp_path) is None
    assert integration_route({**base, OPT_IN: "true"}, tmp_path) is None
    assert integration_route({OPT_IN: "1"}, tmp_path) is None
    assert integration_route(base, tmp_path).blob.blob_service_url == "https://fixture.invalid"
    assert integration_route({
        OPT_IN: "1", CONTAINER: "disposable", CONNECTION: "fixture-connection"
    }, tmp_path).blob.connection_string == "fixture-connection"
    with pytest.raises(ValueError, match="exactly one"):
        integration_route({**base, CONNECTION: "fixture-secret"}, tmp_path)
    with pytest.raises(ValueError) as caught:
        integration_route({**base, SERVICE_URI: "https://fixture.invalid/?sig=fixture-secret"}, tmp_path)
    assert "fixture-secret" not in str(caught.value)


@pytest_asyncio.fixture
async def live_blob_case(tmp_path):
    route = integration_route(os.environ, tmp_path / "unused-local")
    if route is None:
        pytest.skip("Real Blob tests require explicit opt-in and a pre-existing disposable container.")
    prefix = session_prefix(route, "agent", "session") + "/"
    owned = await open_blob_service(route.blob)
    container = owned.service.get_container_client(route.blob.container_name)
    try:
        await container.get_container_properties()
        yield route
    finally:
        try:
            names = [blob.name async for blob in container.list_blobs(name_starts_with=prefix)]
            assert all(name.startswith(prefix) for name in names)
            for name in names:
                await owned.service.get_blob_client(
                    container=route.blob.container_name, blob=name
                ).delete_blob()
        finally:
            await owned.close()


@pytest.mark.asyncio
async def test_opt_in_blob_files_continue_across_adapter_clients(live_blob_case):
    route = live_blob_case
    provider = await open_session_fs(route, "agent", "session", conventions="posix")
    try:
        await provider.write_file("/workspace/uninterpreted.sdk", "\x00opaque\r\n雪")
        await provider.append_file("/workspace/uninterpreted.sdk", "\ncontinued")
        await provider.rename("/workspace/uninterpreted.sdk", "/workspace/renamed.sdk")
    finally:
        await provider.close()
    reopened = await open_session_fs(route, "agent", "session", conventions="posix")
    try:
        assert await reopened.read_file("/workspace/renamed.sdk") == "\x00opaque\r\n雪\ncontinued"
        assert (await reopened.stat("/workspace/renamed.sdk")).size == len(
            "\x00opaque\r\n雪\ncontinued".encode()
        )
        assert [(entry.name, entry.type.value) for entry in await reopened.readdir_with_types("/workspace")] == [
            ("renamed.sdk", "file")
        ]
        await reopened.rm("/workspace/renamed.sdk", recursive=False, force=False)
    finally:
        await reopened.close()
