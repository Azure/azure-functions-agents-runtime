from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from azure_functions_agents.harness._session_storage import BlobStorageSettings
from azure_functions_agents.harness.copilot_sdk import _copilot_session_fs as fs
from azure_functions_agents.harness.copilot_sdk._copilot_session_blob import (
    BlobSessionFileBackend,
    open_blob_backend,
)
from tests.test_copilot_session_fs import (
    MemoryBlob,
    MemoryFile,
)
from tests.test_copilot_session_fs import local_route as local_route
from tests.test_copilot_session_fs import memory_blobs as memory_blobs


@pytest.mark.asyncio
async def test_blob_factory_structurally_conforms_without_protocol_defaults(local_route, memory_blobs):
    route = replace(local_route, blob=BlobStorageSettings(
        container_name="container", connection_string="fixture-connection",
    ))
    backend = await open_blob_backend(route, "owned-prefix")
    assert type(backend) is BlobSessionFileBackend
    assert fs.SessionFileBackend not in BlobSessionFileBackend.__mro__
    await backend.initialize()
    await backend.write_file("opaque", b"\x00\xff\r\n", None)
    assert await backend.read_file("opaque") == b"\x00\xff\r\n"
    assert isinstance(memory_blobs.files["owned-prefix/opaque"], MemoryFile)
    await backend.close()


@pytest.mark.asyncio
async def test_private_rename_reads_bytes_metadata_then_uploads_before_deleting(
    local_route, memory_blobs, monkeypatch,
):
    route = replace(local_route, blob=BlobStorageSettings(
        container_name="container", connection_string="fixture-connection",
    ))
    backend = await open_blob_backend(route, "owned-prefix")
    await backend.initialize()
    await backend.write_file("source", b"\x00\xff\r\n", None)
    memory_blobs.files["owned-prefix/source"].properties.metadata = {"opaque": "metadata"}
    order = []
    download, upload, delete = MemoryBlob.download_blob, MemoryBlob.upload_blob, MemoryBlob.delete_blob

    async def read(blob):
        order.append("read")
        return await download(blob)

    async def write(blob, *args, **kwargs):
        order.append("upload")
        assert "owned-prefix/source" in memory_blobs.files
        return await upload(blob, *args, **kwargs)

    async def remove(blob):
        order.append("delete")
        assert memory_blobs.files["owned-prefix/target"].properties.metadata == {"opaque": "metadata"}
        return await delete(blob)

    monkeypatch.setattr(MemoryBlob, "download_blob", read)
    monkeypatch.setattr(MemoryBlob, "upload_blob", write)
    monkeypatch.setattr(MemoryBlob, "delete_blob", remove)
    await backend._copy_delete("owned-prefix/source", "owned-prefix/target")
    assert order == ["read", "upload", "delete"]
    assert memory_blobs.files["owned-prefix/target"].content == b"\x00\xff\r\n"
    assert "owned-prefix/source" not in memory_blobs.files
    await backend.close()


@pytest.mark.asyncio
async def test_entra_rename_preserves_source_bearer(local_route, memory_blobs):
    route = replace(local_route, blob=BlobStorageSettings(
        container_name="container", blob_service_url="https://fixture.invalid",
    ))
    backend = await open_blob_backend(route, "owned-prefix")
    await backend.initialize()
    await backend.write_file("source", b"bytes", None)
    await backend._copy_delete("owned-prefix/source", "owned-prefix/target")
    assert memory_blobs.copies == [
        ("owned-prefix/source", "owned-prefix/target", "Bearer fixture-token")
    ]
    backend.owned.credential.get_token.assert_awaited_once_with("https://storage.azure.com/.default")
    await backend.close()


@pytest.mark.asyncio
async def test_failed_blob_close_retains_provider_for_retry(local_route, memory_blobs):
    route = replace(local_route, blob=BlobStorageSettings(
        container_name="container", blob_service_url="https://fixture.invalid",
    ))
    provider = await fs.open_session_fs(route, "agent", "session")
    close = AsyncMock(side_effect=[OSError("failed close"), None])
    provider.backend.close = close
    with pytest.raises(OSError):
        await provider.close()
    assert not provider._closed
    await provider.close()
    assert provider._closed
    assert close.await_count == 2
