"""In-memory test double for the runtime-owned Sandbox transport Protocols."""

from __future__ import annotations

from dataclasses import dataclass

from azure_functions_agents.transport.transport_models import (
    SandboxExecResult,
    SandboxFileEntry,
    SandboxFileNotFoundError,
    SandboxFileOperationError,
    SandboxFileStat,
)


@dataclass(frozen=True, slots=True)
class RecordedTransportCall:
    """One direct transport operation observed by the fake."""

    operation: str
    path: str | None = None
    command: str | None = None


class FakeSandboxTransport:
    """Small in-memory file/process double that records direct operations."""

    def __init__(self) -> None:
        self.calls: list[RecordedTransportCall] = []
        self._files: dict[str, bytes] = {}
        self._directories: set[str] = {"/"}
        self.next_exec_result = SandboxExecResult(exit_code=0, stdout="", stderr="")
        self.read_errors: list[Exception] = []

    def seed_file(self, path: str, content: bytes) -> None:
        """Populate a file without recording a transport operation."""

        normalized = _normalize(path)
        self._directories.add(_parent(normalized))
        self._files[normalized] = content

    async def list_files(self, path: str) -> tuple[SandboxFileEntry, ...]:
        normalized = _normalize(path)
        self.calls.append(RecordedTransportCall("list_files", path=normalized))
        if normalized not in self._directories:
            raise SandboxFileNotFoundError(normalized)

        prefix = "/" if normalized == "/" else f"{normalized}/"
        entries: list[SandboxFileEntry] = []
        for candidate in sorted((*self._directories, *self._files)):
            if candidate == normalized or not candidate.startswith(prefix):
                continue
            remainder = candidate[len(prefix) :]
            if "/" in remainder:
                continue
            is_directory = candidate in self._directories
            entries.append(
                SandboxFileEntry(
                    name=remainder,
                    path=candidate,
                    size=None if is_directory else len(self._files[candidate]),
                    is_directory=is_directory,
                )
            )
        return tuple(entries)

    async def stat_file(self, path: str) -> SandboxFileStat:
        normalized = _normalize(path)
        self.calls.append(RecordedTransportCall("stat_file", path=normalized))
        if normalized in self._directories:
            return SandboxFileStat(path=normalized, size=None, is_directory=True)
        if normalized in self._files:
            return SandboxFileStat(
                path=normalized,
                size=len(self._files[normalized]),
                is_directory=False,
            )
        raise SandboxFileNotFoundError(normalized)

    async def read_file(self, path: str) -> bytes:
        normalized = _normalize(path)
        self.calls.append(RecordedTransportCall("read_file", path=normalized))
        if self.read_errors:
            raise self.read_errors.pop(0)
        try:
            return self._files[normalized]
        except KeyError:
            raise SandboxFileNotFoundError(normalized) from None

    async def write_file(self, path: str, content: bytes, *, create_dirs: bool = False) -> None:
        normalized = _normalize(path)
        self.calls.append(RecordedTransportCall("write_file", path=normalized))
        parent = _parent(normalized)
        if parent not in self._directories:
            if not create_dirs:
                raise SandboxFileNotFoundError(parent)
            _add_parent_directories(self._directories, parent)
        self._files[normalized] = content

    async def delete_file(self, path: str) -> None:
        normalized = _normalize(path)
        self.calls.append(RecordedTransportCall("delete_file", path=normalized))
        if normalized in self._files:
            del self._files[normalized]
            return
        if normalized in self._directories:
            prefix = f"{normalized}/"
            if any(candidate.startswith(prefix) for candidate in (*self._directories, *self._files)):
                raise SandboxFileOperationError("directory is not empty")
            self._directories.remove(normalized)
            return
        raise SandboxFileNotFoundError(normalized)

    async def mkdir(self, path: str) -> None:
        normalized = _normalize(path)
        self.calls.append(RecordedTransportCall("mkdir", path=normalized))
        parent = _parent(normalized)
        if parent not in self._directories:
            raise SandboxFileNotFoundError(parent)
        self._directories.add(normalized)

    async def exec(
        self, command: str, *, timeout_seconds: float | None = None
    ) -> SandboxExecResult:
        del timeout_seconds
        self.calls.append(RecordedTransportCall("exec", command=command))
        return self.next_exec_result


def _normalize(path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("fake transport paths must be absolute")
    if path == "/":
        return path
    return path.rstrip("/")


def _parent(path: str) -> str:
    if path == "/":
        return "/"
    parent, _, _ = path.rpartition("/")
    return parent or "/"


def _add_parent_directories(directories: set[str], path: str) -> None:
    current = "/"
    for segment in path.strip("/").split("/"):
        if not segment:
            continue
        current = f"{current.rstrip('/')}/{segment}"
        directories.add(current)
