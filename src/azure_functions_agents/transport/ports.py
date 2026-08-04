"""Narrow async ports for ACA Sandbox file and process transport."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .transport_models import SandboxExecResult, SandboxFileEntry, SandboxFileStat


@runtime_checkable
class SandboxFileTransport(Protocol):
    """The exact six-operation data-plane file boundary.

    Every operation raises :class:`~.transport_models.SandboxFileNotFoundError`
    for a missing path and :class:`~.transport_models.SandboxFileOperationError`
    for any other operational failure; callers must not catch a bare ``OSError``
    or ``Exception`` expecting to observe provider-specific exception types.
    """

    async def list_files(self, path: str) -> tuple[SandboxFileEntry, ...]:
        """List entries at ``path``."""

    async def stat_file(self, path: str) -> SandboxFileStat:
        """Read metadata for one file or directory."""

    async def read_file(self, path: str) -> bytes:
        """Read one file as bytes."""

    async def write_file(self, path: str, content: bytes, *, create_dirs: bool = False) -> None:
        """Write one file directly through the file data plane."""

    async def delete_file(self, path: str) -> None:
        """Delete one file or empty directory directly through the file data plane."""

    async def mkdir(self, path: str) -> None:
        """Create one directory directly through the file data plane."""


@runtime_checkable
class SandboxProcessTransport(Protocol):
    """A process-only boundary kept separate from direct file transport."""

    async def exec(
        self, command: str, *, timeout_seconds: float | None = None
    ) -> SandboxExecResult:
        """Run a controlled harness process command."""
