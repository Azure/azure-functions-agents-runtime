"""Async Azure Blob byte operations adapting flat storage to session files."""

from __future__ import annotations

import errno
from collections.abc import Sequence
from contextlib import AsyncExitStack
from io import BytesIO
from typing import TYPE_CHECKING

from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.core.pipeline.transport import AsyncHttpResponse as TransportResponse
from azure.core.rest import AsyncHttpResponse as RestResponse
from azure.storage.blob import BlobProperties, StorageErrorCode
from azure.storage.blob.aio import BlobClient
from copilot.generated.rpc import DebugCollectLogsEntryKind, SessionFSReaddirWithTypesEntry
from copilot.session_fs_provider import SessionFsFileInfo

from .._session_storage import OwnedBlobService, open_blob_service
from ._copilot_session_identity import CopilotSessionError, StorageRoute, _path_error

if TYPE_CHECKING:
    from ._copilot_session_fs import SessionFileBackend

STORAGE_TOKEN_SCOPE = "https://storage.azure.com/.default"
DIRECTORY_METADATA = {"hdi_isfolder": "true"}


def _has_storage_code(error: HttpResponseError, code: StorageErrorCode) -> bool:
    # Azure Core's exception response protocol omits headers.
    response = error.response
    return isinstance(response, (RestResponse, TransportResponse)) and (
        response.headers.get("x-ms-error-code") == code.value
    )


def _blob_info(properties: BlobProperties, *, directory: bool) -> SessionFsFileInfo:
    return SessionFsFileInfo(
        is_file=not directory,
        is_directory=directory,
        size=0 if directory else properties.size,
        mtime=properties.last_modified,
        birthtime=properties.creation_time,
    )


class BlobSessionFileBackend:
    def __init__(self, owned: OwnedBlobService, container: str, prefix: str) -> None:
        self.owned = owned
        self.container = owned.service.get_container_client(container)
        self.container_name = container
        self.prefix = prefix + "/"

    def _name(self, path: str) -> str:
        return self.prefix + path

    def _directory_name(self, path: str) -> str:
        return self._name(path) + "/" if path else self.prefix

    def _blob(self, name: str) -> BlobClient:
        return self.owned.service.get_blob_client(container=self.container_name, blob=name)

    async def initialize(self) -> None:
        try:
            await self.container.create_container()
        except ResourceExistsError as error:
            if not _has_storage_code(error, StorageErrorCode.CONTAINER_ALREADY_EXISTS):
                raise
        await self.mkdir("", recursive=True, mode=None)

    async def _properties(self, name: str) -> BlobProperties | None:
        try:
            return await self._blob(name).get_blob_properties()
        except ResourceNotFoundError as error:
            if not _has_storage_code(error, StorageErrorCode.BLOB_NOT_FOUND):
                raise
            return None

    async def _lookup(self, path: str) -> SessionFsFileInfo | None:
        if path:
            properties = await self._properties(self._name(path))
            if properties is not None:
                return _blob_info(properties, directory=False)
        directory = self._directory_name(path)
        properties = await self._properties(directory)
        if properties is not None:
            return _blob_info(properties, directory=True)
        async for child in self.container.list_blobs(name_starts_with=directory):
            return _blob_info(child, directory=True)
        return None

    async def _check_parents(self, path: str) -> None:
        parts = path.split("/")
        for length in range(1, len(parts)):
            if await self._properties(self._name("/".join(parts[:length]))) is not None:
                raise _path_error(errno.ENOTDIR)

    async def stat(self, path: str) -> SessionFsFileInfo:
        await self._check_parents(path)
        info = await self._lookup(path)
        if info is None:
            raise _path_error(errno.ENOENT)
        return info

    async def _require_directory(self, path: str) -> None:
        if not (await self.stat(path)).is_directory:
            raise _path_error(errno.ENOTDIR)

    async def read_file(self, path: str) -> bytes:
        if (await self.stat(path)).is_directory:
            raise _path_error(errno.EISDIR)
        downloader = await self._blob(self._name(path)).download_blob()
        content = BytesIO()
        await downloader.readinto(content)
        return content.getvalue()

    async def _touch_parent(self, path: str) -> None:
        parent = path.rpartition("/")[0]
        marker = self._blob(self._directory_name(parent))
        try:
            await marker.set_blob_metadata(DIRECTORY_METADATA)
        except ResourceNotFoundError as error:
            if not _has_storage_code(error, StorageErrorCode.BLOB_NOT_FOUND):
                raise
            await marker.upload_blob(b"", overwrite=False, metadata=DIRECTORY_METADATA)

    async def write_file(self, path: str, content: bytes, mode: int | None) -> None:
        del mode
        await self._check_parents(path)
        info = await self._lookup(path)
        if info is not None and info.is_directory:
            raise _path_error(errno.EISDIR)
        await self.mkdir(path.rpartition("/")[0], recursive=True, mode=None)
        await self._blob(self._name(path)).upload_blob(content, overwrite=True)
        if info is None:
            await self._touch_parent(path)

    async def append_file(self, path: str, content: bytes, mode: int | None) -> None:
        try:
            previous = await self.read_file(path)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
            previous = b""
        await self.write_file(path, previous + content, mode)

    async def mkdir(self, path: str, recursive: bool, mode: int | None) -> None:
        del mode
        await self._check_parents(path)
        info = await self._lookup(path)
        if info is not None:
            if recursive and info.is_directory:
                return
            raise _path_error(errno.EEXIST)
        parent = path.rpartition("/")[0]
        if path:
            if recursive:
                await self.mkdir(parent, recursive=True, mode=None)
            else:
                await self._require_directory(parent)
        await self._blob(self._directory_name(path)).upload_blob(
            b"", overwrite=False, metadata=DIRECTORY_METADATA
        )
        if path:
            await self._touch_parent(path)

    async def readdir_with_types(self, path: str) -> Sequence[SessionFSReaddirWithTypesEntry]:
        await self._require_directory(path)
        directory = self._directory_name(path)
        entries: dict[str, DebugCollectLogsEntryKind] = {}
        async for blob in self.container.list_blobs(name_starts_with=directory):
            relative = blob.name.removeprefix(directory)
            if not relative:
                continue
            name, separator, _ = relative.partition("/")
            entries[name] = (
                DebugCollectLogsEntryKind.DIRECTORY
                if separator
                else DebugCollectLogsEntryKind.FILE
            )
        return [
            SessionFSReaddirWithTypesEntry(name=name, type=kind)
            for name, kind in sorted(entries.items())
        ]

    async def rm(self, path: str, recursive: bool, force: bool) -> None:
        if not path:
            raise _path_error(errno.EACCES)
        try:
            info = await self.stat(path)
        except OSError as error:
            if force and error.errno == errno.ENOENT:
                return
            raise
        if info.is_file:
            await self._blob(self._name(path)).delete_blob()
        else:
            if not recursive and await self.readdir_with_types(path):
                raise _path_error(errno.ENOTEMPTY)
            directory = self._directory_name(path)
            names = [
                blob.name
                async for blob in self.container.list_blobs(name_starts_with=directory)
            ]
            for name in names:
                await self._blob(name).delete_blob()
        await self._touch_parent(path)

    async def _copy_delete(self, src: str, dest: str) -> None:
        source, target = self._blob(src), self._blob(dest)
        if self.owned.credential is None:
            # Account-key request authorization does not authorize a private copy-source URL.
            downloader = await source.download_blob()
            content = BytesIO()
            await downloader.readinto(content)
            await target.upload_blob(
                content.getvalue(), overwrite=True, metadata=downloader.properties.metadata
            )
        else:
            token = await self.owned.credential.get_token(STORAGE_TOKEN_SCOPE)
            await target.upload_blob_from_url(
                source.url, overwrite=True, source_authorization=f"Bearer {token.token}"
            )
        await source.delete_blob()

    async def rename(self, src: str, dest: str) -> None:
        if not src or not dest:
            raise _path_error(errno.EACCES)
        source = await self.stat(src)
        if src == dest:
            return
        if source.is_directory and dest.startswith(src + "/"):
            raise _path_error(errno.EINVAL)
        await self._require_directory(dest.rpartition("/")[0])
        target = await self._lookup(dest)
        if target is not None:
            if source.is_directory != target.is_directory:
                raise _path_error(errno.ENOTDIR if source.is_directory else errno.EISDIR)
            if target.is_directory and await self.readdir_with_types(dest):
                raise _path_error(errno.ENOTEMPTY)
        if source.is_file:
            await self._copy_delete(self._name(src), self._name(dest))
        else:
            directory = self._directory_name(src)
            names = [
                blob.name
                async for blob in self.container.list_blobs(name_starts_with=directory)
            ]
            for name in names:
                await self._copy_delete(
                    name, self._directory_name(dest) + name.removeprefix(directory)
                )
        await self._touch_parent(src)
        await self._touch_parent(dest)

    async def close(self) -> None:
        await self.owned.close()


async def open_blob_backend(route: StorageRoute, prefix: str) -> SessionFileBackend:
    if route.blob is None:
        raise CopilotSessionError(errno.EINVAL, "Blob session storage is not configured.")
    try:
        async with AsyncExitStack() as cleanup:
            owned = await open_blob_service(route.blob)
            cleanup.push_async_callback(owned.close)
            backend = BlobSessionFileBackend(owned, route.blob.container_name, prefix)
            cleanup.pop_all()
            return backend
    except (AzureError, ValueError):
        raise CopilotSessionError(
            errno.EINVAL, "Blob session storage configuration could not be opened."
        ) from None
