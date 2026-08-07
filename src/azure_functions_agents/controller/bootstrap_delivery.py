"""Deliver the sandbox bootstrap only after controller content verification succeeds."""

from __future__ import annotations

import hashlib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ..harness import bootstrap
from ..journal_paths import BOOT_READY_PATH, BOOTSTRAP_DIGEST_PATH, BOOTSTRAP_PATH
from ..transport.manifest import ExpectedSandboxManifestBinding
from ..transport.ports import SandboxFileTransport
from ..transport.transport_models import (
    ProvisionedSandboxIdentity,
    SandboxFileNotFoundError,
    SandboxFileOperationError,
)
from .package import (
    CapturedContentPackage,
    ContentDeliveryVerificationError,
    DeliveredContentPackage,
    deliver_content_package,
)

_BOOT_READY_CONTENT = b"ready\n"


@dataclass(frozen=True, slots=True)
class BootstrapArtifact:
    """The exact bootstrap source and digest delivered to one sandbox."""

    source: bytes
    digest: str

    @classmethod
    def create(cls, source: bytes) -> BootstrapArtifact:
        if not source:
            raise ContentDeliveryVerificationError("Sandbox bootstrap source is empty.")
        return cls(source=source, digest=f"sha256:{hashlib.sha256(source).hexdigest()}")

    @property
    def digest_sidecar(self) -> bytes:
        """Return the canonical bootstrap-digest sidecar bytes."""

        return f"{self.digest}\n".encode("ascii")


@dataclass(frozen=True, slots=True)
class DeliveredSandboxBootstrap:
    """Records content and bootstrap artifacts that are safe for supervisor startup."""

    content: DeliveredContentPackage
    bootstrap: BootstrapArtifact


def load_bootstrap_artifact() -> BootstrapArtifact:
    """Read the runtime's stdlib-only bootstrap source for sandbox delivery."""

    source_path = Path(bootstrap.__file__ or "")
    try:
        return BootstrapArtifact.create(source_path.read_bytes())
    except OSError:
        raise ContentDeliveryVerificationError(
            "Sandbox bootstrap source could not be read for delivery."
        ) from None


async def deliver_sandbox_bootstrap(
    transport: SandboxFileTransport,
    artifact: BootstrapArtifact | None = None,
) -> BootstrapArtifact:
    """Write bootstrap files and the final supervisor sentinel with exact read-back."""

    resolved_artifact = artifact if artifact is not None else load_bootstrap_artifact()
    await _write_verified(transport, BOOTSTRAP_PATH, resolved_artifact.source)
    await _write_verified(transport, BOOTSTRAP_DIGEST_PATH, resolved_artifact.digest_sidecar)
    await _write_verified(transport, BOOT_READY_PATH, _BOOT_READY_CONTENT)
    return resolved_artifact


async def deliver_content_and_bootstrap(
    transport: SandboxFileTransport,
    package: CapturedContentPackage,
    expected: ExpectedSandboxManifestBinding,
    live_identity: ProvisionedSandboxIdentity,
    *,
    artifact: BootstrapArtifact | None = None,
) -> DeliveredSandboxBootstrap:
    """Deliver verified content followed by the supervisor-unblocking bootstrap files."""

    with suppress(SandboxFileNotFoundError):
        await transport.delete_file(BOOT_READY_PATH)
    content = await deliver_content_package(transport, package, expected, live_identity)
    delivered_bootstrap = await deliver_sandbox_bootstrap(transport, artifact)
    return DeliveredSandboxBootstrap(content=content, bootstrap=delivered_bootstrap)


async def _write_verified(
    transport: SandboxFileTransport,
    path: str,
    content: bytes,
) -> None:
    try:
        await transport.write_file(path, content, create_dirs=True)
    except (SandboxFileNotFoundError, SandboxFileOperationError):
        if await _matches_written_content(transport, path, content):
            return
        raise
    if not await _matches_written_content(transport, path, content):
        raise ContentDeliveryVerificationError(
            "Delivered sandbox bootstrap artifact does not match what was written."
        )


async def _matches_written_content(
    transport: SandboxFileTransport,
    path: str,
    expected: bytes,
) -> bool:
    try:
        return await transport.read_file(path) == expected
    except SandboxFileNotFoundError:
        return False
