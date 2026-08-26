"""Narrow async ports for ACA Sandbox file and process transport."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .manifest import ExpectedSandboxManifestBinding
from .transport_models import (
    PersistedSandboxBinding,
    ProvisionedSandboxIdentity,
    SandboxCreateRequest,
    SandboxExecResult,
    SandboxFileEntry,
    SandboxFileStat,
    SandboxGroupBinding,
    SandboxGroupIdentity,
    SandboxLifecyclePolicy,
    SandboxSnapshot,
    SandboxSummary,
)


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


@runtime_checkable
class SandboxSessionHandle(SandboxFileTransport, SandboxProcessTransport, Protocol):
    """A live session sandbox with file, process, and lifecycle operations."""

    @property
    def identity(self) -> ProvisionedSandboxIdentity:
        """Return the provider-neutral identity for this live sandbox."""

    async def stop(self) -> None:
        """Stop this individual sandbox without changing its group."""

    async def resume(self) -> None:
        """Resume this individual sandbox without trusting advisory state."""

    async def delete(self) -> None:
        """Delete this individual sandbox."""

    async def get_lifecycle_policy(self) -> SandboxLifecyclePolicy:
        """Read the complete per-sandbox lifecycle policy."""

    async def set_lifecycle_policy(self, policy: SandboxLifecyclePolicy) -> None:
        """Set the complete per-sandbox lifecycle policy."""

    async def close(self) -> None:
        """Release controller-side resources for this handle."""


@runtime_checkable
class SandboxSessionProvider(Protocol):
    """A provider-neutral owner of one customer-configured Sandbox Group."""

    @property
    def group(self) -> SandboxGroupIdentity:
        """Return the resolved, immutable Sandbox Group identity."""

    async def create(
        self,
        request: SandboxCreateRequest,
        *,
        persisted_group: SandboxGroupBinding,
    ) -> SandboxSessionHandle:
        """Create one session sandbox in the bound group."""

    async def attach(
        self,
        persisted: PersistedSandboxBinding,
        expected: ExpectedSandboxManifestBinding,
        *,
        readiness_timeout_seconds: float,
    ) -> SandboxSessionHandle:
        """Attach to a persisted sandbox and prove its manifest binding."""

    async def resume(
        self,
        persisted: PersistedSandboxBinding,
        expected: ExpectedSandboxManifestBinding,
        *,
        readiness_timeout_seconds: float,
    ) -> SandboxSessionHandle:
        """Resume a persisted sandbox and prove its manifest binding."""

    async def list_sandboxes(
        self, *, labels: dict[str, str], max_items: int | None = None
    ) -> tuple[SandboxSummary, ...]:
        """List app-owned sandboxes using an exact label selector."""

    async def delete_sandbox(self, sandbox_id: str) -> None:
        """Delete one group-owned sandbox by its provider identifier."""

    async def list_snapshots(self, *, max_items: int | None = None) -> tuple[SandboxSnapshot, ...]:
        """List snapshots visible to the bound Sandbox Group."""

    async def delete_snapshot(self, snapshot_id: str) -> None:
        """Delete one unreferenced snapshot from the bound Sandbox Group."""

    async def close(self) -> None:
        """Release controller-side provider resources."""
