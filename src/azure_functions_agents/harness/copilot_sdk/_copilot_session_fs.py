"""The SDK SessionFs provider boundary for opaque local or Blob files."""

from __future__ import annotations

import asyncio
import errno
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from typing import Protocol

from azure.core.exceptions import AzureError, HttpResponseError
from azure.storage.blob import StorageErrorCode
from copilot.generated.rpc import SessionFSReaddirWithTypesEntry
from copilot.session import SessionFsConventions
from copilot.session_fs_provider import SessionFsFileInfo, SessionFsProvider

from ._copilot_session_blob import _has_storage_code, open_blob_backend
from ._copilot_session_identity import (
    CopilotSessionError,
    StorageMode,
    StorageRoute,
    _path_error,
    session_prefix,
)
from ._copilot_session_local import open_local_backend
from ._copilot_session_paths import (
    HOST_PATH_CONVENTIONS,
    WORKSPACE_ROOT,
    select_session_path_policy,
)


class SessionFileBackend(Protocol):
    """The byte operations required by one session-scoped SDK provider."""

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


type BackendFactory = Callable[[StorageRoute, str], Awaitable[SessionFileBackend]]

_BACKEND_FACTORIES: dict[StorageMode, BackendFactory] = {
    StorageMode.LOCAL: open_local_backend,
    StorageMode.BLOB: open_blob_backend,
}


class CopilotSessionFs(SessionFsProvider):
    """SDK callbacks serialized within this provider, without session policy."""

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
        self._policy = select_session_path_policy(conventions)
        self._lock = asyncio.Lock()
        self._closed = False

    def _path(self, path: str) -> str:
        return self._policy.normalize(path, self.workspace_path).lstrip("/")

    async def _call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        if self.deadline is not None and asyncio.get_running_loop().time() >= self.deadline:
            raise CopilotSessionError(
                errno.ETIMEDOUT, "Native session filesystem deadline expired."
            )
        try:
            async with asyncio.timeout_at(self.deadline):
                async with self._lock:
                    if self._closed:
                        raise _path_error(errno.EBADF)
                    return await self._operation(operation)
        except TimeoutError:
            raise CopilotSessionError(
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
            raise CopilotSessionError(code, "Blob session filesystem operation failed.") from None
        except AzureError:
            raise CopilotSessionError(
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
        """Close only this provider's resources."""
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
) -> CopilotSessionFs:
    """Open the frozen route once; configured failures never fall back to local."""
    prefix = session_prefix(route, agent_slug, session_id)
    async with AsyncExitStack() as cleanup:
        backend = await _BACKEND_FACTORIES[route.mode](route, prefix)
        provider = CopilotSessionFs(
            backend, conventions=conventions, workspace_path=workspace_path, deadline=deadline
        )
        cleanup.push_async_callback(provider.close)
        await provider._call(backend.initialize)
        await provider.mkdir(WORKSPACE_ROOT, recursive=True)
        cleanup.pop_all()
        return provider
