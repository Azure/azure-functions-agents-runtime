"""Opaque local/Blob files behind the SDK's SessionFs callbacks."""

from __future__ import annotations

import asyncio
import errno
import ntpath
import os
import re
import shutil
import stat
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Protocol

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
from copilot.session import SessionFsConventions
from copilot.session_fs_provider import SessionFsFileInfo, SessionFsProvider

from ._native_session_identity import NativeSessionError, StorageRoute, session_prefix
from ._session_storage import OwnedBlobService, open_blob_service

HOST_PATH_CONVENTIONS: SessionFsConventions = "windows" if os.name == "nt" else "posix"
WORKSPACE_ROOT = "/workspace"
SESSION_STATE_ROOT = "/session-state"
STORAGE_TOKEN_SCOPE = "https://storage.azure.com/.default"
DIRECTORY_METADATA = {"hdi_isfolder": "true"}


def _workspace_relative(
    path: str, workspace_path: str, conventions: SessionFsConventions
) -> str | None:
    if not workspace_path:
        return None
    subject = path.replace("\\", "/") if conventions == "windows" else path
    prefix = (
        workspace_path.replace("\\", "/")
        if conventions == "windows"
        else workspace_path
    ).rstrip("/")
    compared = subject.casefold() if conventions == "windows" else subject
    against = prefix.casefold() if conventions == "windows" else prefix
    if compared == against:
        return ""
    if prefix and compared.startswith(against + "/"):
        return subject[len(prefix) + 1 :]
    return None


def normalize_callback_path(
    path: str, conventions: SessionFsConventions, workspace_path: str = ""
) -> str:
    """Contain SDK paths to its virtual roots or the exact current workspace."""
    if not path or "\x00" in path:
        raise OSError(errno.EINVAL, "Invalid native filesystem path.")
    relative = _workspace_relative(path, workspace_path, conventions)
    if relative is not None:
        path = f"{WORKSPACE_ROOT}/{relative}"
    elif path.startswith(("\\\\", "//")) or re.match(r"[A-Za-z]:", path):
        raise OSError(errno.EACCES, "Native filesystem path is outside its roots.")
    if conventions == "windows":
        path = path.replace("\\", "/")
    elif "\\" in path:
        raise OSError(errno.EINVAL, "Invalid native filesystem path.")
    if not path.startswith("/"):
        path = f"{WORKSPACE_ROOT}/{path}"
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if any(
        part == ".."
        or any(ord(character) < 32 for character in part)
        or (conventions == "windows" and ntpath.isreserved(part))
        for part in parts
    ):
        raise OSError(errno.EACCES, "Invalid or escaping native filesystem path.")
    normalized = "/" + "/".join(parts)
    if normalized != "/" and not any(
        normalized == root or normalized.startswith(root + "/")
        for root in (WORKSPACE_ROOT, SESSION_STATE_ROOT)
    ):
        raise OSError(errno.EACCES, "Native filesystem path is outside its roots.")
    return normalized


class SessionFileBackend(Protocol):
    """The file operations required by one session-scoped SDK provider."""

    async def initialize(self) -> None: ...
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, content: bytes, mode: int | None) -> None: ...
    async def append_file(self, path: str, content: bytes, mode: int | None) -> None: ...
    async def stat(self, path: str) -> SessionFsFileInfo: ...
    async def mkdir(self, path: str, recursive: bool, mode: int | None) -> None: ...
    async def readdir_with_types(self, path: str) -> Sequence[SessionFSReaddirWithTypesEntry]: ...
    async def rm(self, path: str, recursive: bool, force: bool) -> None: ...
    async def rename(self, src: str, dest: str) -> None: ...
    async def close(self) -> None: ...


def _path_error(code: int) -> OSError:
    return OSError(code, "Native session filesystem operation failed.")


class _LocalFileBackend:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def _path(self, relative: str) -> Path:
        target = self.root.joinpath(*relative.split("/")) if relative else self.root
        for entry in (*reversed(target.parents), target):
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or entry.is_junction():
                raise _path_error(errno.EACCES)
            if entry != target and not stat.S_ISDIR(info.st_mode):
                raise _path_error(errno.ENOTDIR)
        if os.name == "nt" and any(ntpath.isreserved(part) for part in target.parts[1:]):
            raise _path_error(errno.EINVAL)
        return target

    async def initialize(self) -> None:
        await self.mkdir("", recursive=True, mode=None)

    async def read_file(self, path: str) -> bytes:
        return await asyncio.to_thread(self._read_file, path)

    def _read_file(self, path: str) -> bytes:
        target = self._path(path)
        info = target.stat()
        if stat.S_ISDIR(info.st_mode):
            raise _path_error(errno.EISDIR)
        if not stat.S_ISREG(info.st_mode):
            raise _path_error(errno.EINVAL)
        return target.read_bytes()

    async def write_file(self, path: str, content: bytes, mode: int | None) -> None:
        await asyncio.to_thread(self._write_file, path, content, mode, False)

    async def append_file(self, path: str, content: bytes, mode: int | None) -> None:
        await asyncio.to_thread(self._write_file, path, content, mode, True)

    def _write_file(self, path: str, content: bytes, mode: int | None, append: bool) -> None:
        target = self._path(path)
        try:
            info = target.stat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISDIR(info.st_mode):
                raise _path_error(errno.EISDIR)
            if not stat.S_ISREG(info.st_mode):
                raise _path_error(errno.EINVAL)
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        if os.name == "nt":
            flags |= os.O_BINARY
        with os.fdopen(os.open(target, flags, mode if mode is not None else 0o666), "wb") as file:
            file.write(content)

    async def stat(self, path: str) -> SessionFsFileInfo:
        return await asyncio.to_thread(self._stat, path)

    def _stat(self, path: str) -> SessionFsFileInfo:
        info = self._path(path).stat()
        try:
            birthtime = info.st_birthtime
        except AttributeError:
            birthtime = info.st_ctime
        return SessionFsFileInfo(
            is_file=stat.S_ISREG(info.st_mode),
            is_directory=stat.S_ISDIR(info.st_mode),
            size=info.st_size,
            mtime=datetime.fromtimestamp(info.st_mtime, UTC),
            birthtime=datetime.fromtimestamp(birthtime, UTC),
        )

    async def mkdir(self, path: str, recursive: bool, mode: int | None) -> None:
        await asyncio.to_thread(self._mkdir, path, recursive, mode)

    def _mkdir(self, path: str, recursive: bool, mode: int | None) -> None:
        self._path(path).mkdir(
            mode if mode is not None else 0o777,
            parents=recursive,
            exist_ok=recursive,
        )

    async def readdir_with_types(self, path: str) -> Sequence[SessionFSReaddirWithTypesEntry]:
        return await asyncio.to_thread(self._readdir, path)

    def _readdir(self, path: str) -> list[SessionFSReaddirWithTypesEntry]:
        directory = self._path(path)
        entries: list[SessionFSReaddirWithTypesEntry] = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            info = self._stat(child.relative_to(self.root).as_posix())
            entries.append(
                SessionFSReaddirWithTypesEntry(
                    name=child.name,
                    type=(
                        DebugCollectLogsEntryKind.DIRECTORY
                        if info.is_directory
                        else DebugCollectLogsEntryKind.FILE
                    ),
                )
            )
        return entries

    async def rm(self, path: str, recursive: bool, force: bool) -> None:
        await asyncio.to_thread(self._rm, path, recursive, force)

    def _rm(self, path: str, recursive: bool, force: bool) -> None:
        if not path:
            raise _path_error(errno.EACCES)
        target = self._path(path)
        try:
            info = target.stat()
        except FileNotFoundError:
            if force:
                return
            raise
        if not stat.S_ISDIR(info.st_mode):
            target.unlink()
            return
        if not recursive:
            if next(target.iterdir(), None) is not None:
                raise _path_error(errno.ENOTEMPTY)
            target.rmdir()
            return
        for child in target.rglob("*"):
            self._path(child.relative_to(self.root).as_posix())
        shutil.rmtree(target)

    async def rename(self, src: str, dest: str) -> None:
        await asyncio.to_thread(self._rename, src, dest)

    def _rename(self, src: str, dest: str) -> None:
        if not src or not dest:
            raise _path_error(errno.EACCES)
        source, target = self._path(src), self._path(dest)
        source_info = source.stat()
        if source == target:
            return
        source_is_directory = stat.S_ISDIR(source_info.st_mode)
        if source_is_directory and target.is_relative_to(source):
            raise _path_error(errno.EINVAL)
        parent_info = target.parent.stat()
        if not stat.S_ISDIR(parent_info.st_mode):
            raise _path_error(errno.ENOTDIR)
        try:
            target_info = target.stat()
        except FileNotFoundError:
            target_info = None
        if target_info is not None:
            target_is_directory = stat.S_ISDIR(target_info.st_mode)
            if source_is_directory != target_is_directory:
                raise _path_error(errno.ENOTDIR if source_is_directory else errno.EISDIR)
            if target_is_directory:
                if next(target.iterdir(), None) is not None:
                    raise _path_error(errno.ENOTEMPTY)
                if os.name == "nt":
                    target.rmdir()
        os.replace(source, target)

    async def close(self) -> None:
        pass


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


class _BlobFileBackend:
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


class NativeSessionFs(SessionFsProvider):
    """SDK callbacks serialized only within this adapter, without session policy."""

    def __init__(
        self,
        backend: SessionFileBackend,
        *,
        conventions: SessionFsConventions = HOST_PATH_CONVENTIONS,
        workspace_path: str = "",
        deadline: float | None = None,
    ) -> None:
        self.backend = backend
        self.conventions = conventions
        self.workspace_path = workspace_path
        self.deadline = deadline
        self._lock = asyncio.Lock()
        self._closed = False

    def _path(self, path: str) -> str:
        return normalize_callback_path(path, self.conventions, self.workspace_path).lstrip("/")

    async def _call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        if self.deadline is not None and asyncio.get_running_loop().time() >= self.deadline:
            raise NativeSessionError(
                errno.ETIMEDOUT, "Native session filesystem deadline expired."
            )
        try:
            async with asyncio.timeout_at(self.deadline):
                async with self._lock:
                    if self._closed:
                        raise _path_error(errno.EBADF)
                    return await self._operation(operation)
        except TimeoutError:
            raise NativeSessionError(
                errno.ETIMEDOUT, "Native session filesystem deadline expired."
            ) from None

    @staticmethod
    async def _operation[T](operation: Callable[[], Awaitable[T]]) -> T:
        try:
            return await operation()
        except HttpResponseError as error:
            if _has_storage_code(error, StorageErrorCode.BLOB_NOT_FOUND):
                code = errno.ENOENT
            elif _has_storage_code(error, StorageErrorCode.BLOB_ALREADY_EXISTS):
                code = errno.EEXIST
            elif error.status_code in {401, 403}:
                code = errno.EACCES
            else:
                code = errno.EIO
            raise NativeSessionError(code, "Blob session filesystem operation failed.") from None
        except AzureError:
            raise NativeSessionError(
                errno.EIO, "Blob session filesystem operation failed."
            ) from None
        except UnicodeError:
            raise _path_error(errno.EILSEQ) from None
        except OSError as error:
            raise _path_error(error.errno or errno.EIO) from None

    async def read_file(self, path: str) -> str:
        relative = self._path(path)

        async def read() -> str:
            return (await self.backend.read_file(relative)).decode("utf-8")

        return await self._call(read)

    async def write_file(self, path: str, content: str, mode: int | None = None) -> None:
        relative = self._path(path)

        async def write() -> None:
            await self.backend.write_file(relative, content.encode("utf-8"), mode)

        await self._call(write)

    async def append_file(self, path: str, content: str, mode: int | None = None) -> None:
        relative = self._path(path)

        async def append() -> None:
            await self.backend.append_file(relative, content.encode("utf-8"), mode)

        await self._call(append)

    async def exists(self, path: str) -> bool:
        relative = self._path(path)
        try:
            await self._call(lambda: self.backend.stat(relative))
        except OSError as error:
            if error.errno in {errno.ENOENT, errno.ENOTDIR}:
                return False
            raise
        return True

    async def stat(self, path: str) -> SessionFsFileInfo:
        relative = self._path(path)
        return await self._call(lambda: self.backend.stat(relative))

    async def mkdir(self, path: str, recursive: bool, mode: int | None = None) -> None:
        relative = self._path(path)
        await self._call(lambda: self.backend.mkdir(relative, recursive, mode))

    async def readdir(self, path: str) -> list[str]:
        return [entry.name for entry in await self.readdir_with_types(path)]

    async def readdir_with_types(self, path: str) -> Sequence[SessionFSReaddirWithTypesEntry]:
        relative = self._path(path)
        return await self._call(lambda: self.backend.readdir_with_types(relative))

    async def rm(self, path: str, recursive: bool, force: bool) -> None:
        relative = self._path(path)
        await self._call(lambda: self.backend.rm(relative, recursive, force))

    async def rename(self, src: str, dest: str) -> None:
        source, target = self._path(src), self._path(dest)
        await self._call(lambda: self.backend.rename(source, target))

    async def close(self) -> None:
        """Close only this adapter's resources."""
        async with self._lock:
            if self._closed:
                return
            await self._operation(self.backend.close)
            self._closed = True


async def open_session_fs(
    route: StorageRoute,
    agent_slug: str,
    session_id: str,
    *,
    conventions: SessionFsConventions = HOST_PATH_CONVENTIONS,
    workspace_path: str = "",
    deadline: float | None = None,
) -> NativeSessionFs:
    """Open the configured backend; configured Blob failures never select local files."""
    prefix = session_prefix(route, agent_slug, session_id)
    async with AsyncExitStack() as cleanup:
        backend: SessionFileBackend
        if route.blob is None:
            backend = _LocalFileBackend(route.local_dir.joinpath(*prefix.split("/")))
        else:
            try:
                owned = await open_blob_service(route.blob)
                cleanup.push_async_callback(NativeSessionFs._operation, owned.close)
                backend = _BlobFileBackend(owned, route.blob.container_name, prefix)
            except (AzureError, ValueError):
                raise NativeSessionError(
                    errno.EINVAL, "Blob session storage configuration could not be opened."
                ) from None
        provider = NativeSessionFs(
            backend, conventions=conventions, workspace_path=workspace_path, deadline=deadline
        )
        cleanup.pop_all()
        cleanup.push_async_callback(provider.close)
        await provider._call(backend.initialize)
        await provider.mkdir(WORKSPACE_ROOT, recursive=True)
        cleanup.pop_all()
        return provider
