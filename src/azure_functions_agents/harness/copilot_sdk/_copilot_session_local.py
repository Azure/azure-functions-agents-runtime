"""Ordinary local byte operations for contained Copilot session trees."""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import stat
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from copilot.generated.rpc import DebugCollectLogsEntryKind, SessionFSReaddirWithTypesEntry
from copilot.session_fs_provider import SessionFsFileInfo

from ._copilot_session_identity import StorageRoute, _path_error
from ._copilot_session_paths import HOST_PATH_CONVENTIONS, select_session_path_policy

if TYPE_CHECKING:
    from ._copilot_session_fs import SessionFileBackend


class LocalSessionFileBackend:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self._host_policy = select_session_path_policy(HOST_PATH_CONVENTIONS)

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
        self._host_policy.validate_local_parts(target.parts)
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
        flags = self._host_policy.write_flags(append)
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
                self._host_policy.replace_empty_directory(target)
        os.replace(source, target)

    async def close(self) -> None:
        pass


async def open_local_backend(route: StorageRoute, prefix: str) -> SessionFileBackend:
    return LocalSessionFileBackend(route.local_dir.joinpath(*prefix.split("/")))
