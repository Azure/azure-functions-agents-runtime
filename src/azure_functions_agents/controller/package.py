"""Deterministic script-root packaging and digest-gated sandbox content delivery.

Captures the controller's mounted Azure Functions script root into a
byte-exact ``funcs_zip`` archive, delivers it plus a strict manifest seed
through the runtime-owned :class:`~azure_functions_agents.transport.ports.SandboxFileTransport`,
and reads back the harness-authored live session manifest for verification.
This module never authors the live manifest itself: the harness is the sole
writer of :data:`~azure_functions_agents.transport.manifest.SESSION_MANIFEST_PATH`,
and the seed this module writes is content metadata, never a readiness signal.

Script-root traversal never re-opens a path after validating it. On a
platform with ``openat``-style primitives (Linux, the production target),
every hop from an anchored root file descriptor is a ``dir_fd``-relative,
``O_NOFOLLOW`` open, so a filesystem race can only turn into a failed open,
never a silent escape, and the bytes archived for a file are read from that
same validated descriptor. Platforms without those primitives (Windows) fall
back to a resolved-path strategy strengthened with a captured file identity
re-checked immediately before and after each read.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat as stat_module
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING

from ..session_state.session_models import DurableSessionRecord
from ..transport.manifest import (
    SESSION_MANIFEST_PATH,
    ExpectedSandboxManifestBinding,
    ObservedSandboxManifestBinding,
    SandboxManifestMismatchError,
    parse_sandbox_manifest_binding,
    render_sandbox_manifest_binding,
    verify_sandbox_manifest,
)
from ..transport.ports import SandboxFileTransport
from ..transport.transport_models import (
    ProvisionedSandboxIdentity,
    SandboxFileNotFoundError,
    SandboxFileOperationError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# Content paths sit beside the harness-owned live manifest so both sides read
# one wire root; deriving them from that constant keeps the two from drifting.
_SESSION_DIR_PATH = SESSION_MANIFEST_PATH.rsplit("/", 1)[0]
CONTENT_DIR_PATH = f"{_SESSION_DIR_PATH}/content"
CONTENT_ARCHIVE_PATH = f"{CONTENT_DIR_PATH}/app.zip"
CONTENT_DIGEST_SIDECAR_PATH = f"{CONTENT_DIR_PATH}/app.sha256"
CONTENT_MANIFEST_SEED_PATH = f"{CONTENT_DIR_PATH}/manifest.seed.json"

FUNCS_ZIP_DIGEST_KIND = "funcs_zip"
MANIFEST_VERSION = 1

_FUNCS_ZIP_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

# Deterministic ZIP metadata: every version-sensitive field is pinned
# explicitly so the archive is byte-identical across interpreters and hosts
# rather than relying on any of zipfile's platform/version-dependent defaults.
# The UTF-8 filename flag is not pinned here: stdlib's own write path derives
# it deterministically from each name (clear for ASCII, set otherwise), and
# forcing it would be silently overridden by that same write path anyway.
_ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_UNIX_CREATOR_SYSTEM = 3
_ZIP_SPEC_VERSION = 20
_MS_DOS_DIRECTORY_ATTRIBUTE = 0x10
_DIRECTORY_UNIX_MODE = stat_module.S_IFDIR | 0o755
_EXECUTABLE_FILE_UNIX_MODE = stat_module.S_IFREG | 0o755
_STANDARD_FILE_UNIX_MODE = stat_module.S_IFREG | 0o644

# Standard (non-ZIP64) ZIP format limits; a package that would need ZIP64
# framing is rejected up front rather than silently switching digest regimes.
# The entry-size bound stays well under the raw 4 GiB-1 format limit: stdlib
# zipfile's own write path applies a conservative ~5% margin against the
# 2**31-1 signed-size boundary even for uncompressed (ZIP_STORED) entries, so
# a size preflighted against the raw format limit alone could still pass here
# and raise an untyped zipfile.LargeZipFile when the archive is written; the
# wrapping catch in the archive writer is the exact-boundary backstop.
_MAX_STANDARD_ZIP_ENTRIES = 0xFFFF
_MAX_STANDARD_ZIP_ENTRY_SIZE = 2_000_000_000

# Fixed-size portions of the standard ZIP records, used only to preflight a
# deterministic upper bound on the finished archive's size from metadata
# alone, before any file content is read.
_ZIP_LOCAL_HEADER_FIXED_SIZE = 30
_ZIP_CENTRAL_DIRECTORY_FIXED_SIZE = 46
_ZIP_END_OF_CENTRAL_DIRECTORY_SIZE = 22

# Operational ceiling on the finished archive, human-approved for v1.
# SandboxFileTransport.write_file() accepts only `bytes`, so the whole
# archive is materialized in controller memory at least once at delivery
# time regardless of how the capture itself streams file content; chunked
# delivery to avoid this is deferred past v1.
_MAX_ARCHIVE_OPERATIONAL_SIZE = 256 * 1024 * 1024

# Streaming chunk size used to copy one validated file's bytes into the
# archive without holding the whole file in memory at once.
_READ_CHUNK_SIZE = 1024 * 1024

# Bound on manual symlink-chain resolution, matching typical POSIX MAXSYMLINKS.
_MAX_SYMLINK_HOPS = 40

# These POSIX-only os.open() flags are declared conditionally in typeshed based
# on the platform mypy is checking, not on the platform actually running; a
# repo-wide type-checking pin would be needed to reference them as bare `os.*`
# attributes. Reading them through getattr keeps this module (and the mypy
# config) portable: the fallback 0 is inert because every call site sits
# behind `_posix_secure_traversal_available()`, which is False wherever these
# flags do not really exist.
_O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)


class ContentPackagingError(Exception):
    """Base class for controller-side content capture, packaging, and delivery errors."""


class ScriptRootUnavailableError(ContentPackagingError):
    """The configured script root is not usable for deterministic capture."""


class UnsafeScriptRootEntryError(ContentPackagingError):
    """A script-root entry cannot be safely archived (escaping link, special file, ...)."""


class ContentCaptureRaceError(ContentPackagingError):
    """The script root changed while its deterministic archive was being captured."""


class ContentArchiveTooLargeError(ContentPackagingError):
    """The deterministic archive would exceed the standard (non-ZIP64) ZIP format limits."""


class SessionSandboxIdentityRequiredError(ContentPackagingError):
    """The durable session record has no ``sandbox_id`` assigned yet."""


class ContentBindingMismatchError(ContentPackagingError):
    """A captured package or live sandbox identity disagrees with the authoritative binding."""


class ContentDeliveryVerificationError(ContentPackagingError):
    """A delivered content artifact's verified read-back did not match what was written."""


class LiveManifestNotReadyError(ContentPackagingError):
    """The harness has not yet published a live manifest at the fixed session path."""


@dataclass(frozen=True, slots=True)
class CapturedContentPackage:
    """An immutable deterministic script-root archive ready for sandbox delivery."""

    archive_bytes: bytes
    digest_kind: str
    digest: str

    @classmethod
    def create(
        cls, *, archive_bytes: bytes, digest_kind: str, digest: str
    ) -> CapturedContentPackage:
        if digest_kind != FUNCS_ZIP_DIGEST_KIND:
            raise ContentPackagingError(
                f"Unsupported content digest kind; expected {FUNCS_ZIP_DIGEST_KIND!r}."
            )
        if _FUNCS_ZIP_DIGEST_PATTERN.fullmatch(digest) is None:
            raise ContentPackagingError(
                "Content digest must match sha256:<64 lower-case hex characters>."
            )
        if len(archive_bytes) > _MAX_ARCHIVE_OPERATIONAL_SIZE:
            raise ContentArchiveTooLargeError(
                "Content archive exceeds the operational archive size bound."
            )
        if _render_funcs_zip_digest(archive_bytes) != digest:
            raise ContentPackagingError(
                "Content digest does not match the supplied archive bytes."
            )
        return cls(archive_bytes=archive_bytes, digest_kind=digest_kind, digest=digest)

    @property
    def size(self) -> int:
        """Return the exact byte length of the deterministic archive."""
        return len(self.archive_bytes)


@dataclass(frozen=True, slots=True)
class DeliveredContentPackage:
    """Immutable record of one completed content-delivery attempt."""

    package: CapturedContentPackage
    expected_binding: ExpectedSandboxManifestBinding

    @classmethod
    def create(
        cls,
        *,
        package: CapturedContentPackage,
        expected_binding: ExpectedSandboxManifestBinding,
    ) -> DeliveredContentPackage:
        return cls(package=package, expected_binding=expected_binding)


@dataclass(frozen=True, slots=True)
class _PosixSecureLocator:
    """Locates a regular file by root-anchored path components.

    Every hop from the root to this file is re-walked with ``O_NOFOLLOW``
    ``dir_fd``-relative opens at read time, so re-locating it can only fail
    closed, never silently follow a symlink swapped in after the scan.
    """

    components: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FallbackLocator:
    """Locates a regular file by its resolved path plus a captured identity.

    Used only on platforms without ``dir_fd``/``O_NOFOLLOW`` support. ``identity``
    is ``(st_dev, st_ino)`` captured at scan time, re-checked immediately before
    and after the read to narrow (not eliminate) the platform's race window.
    """

    resolved_path: Path
    identity: tuple[int, int]


type _ContentLocator = _PosixSecureLocator | _FallbackLocator


@dataclass(frozen=True, slots=True)
class _ScriptRootEntry:
    """One safely resolved script-root entry ready for deterministic archiving.

    ``device``/``inode`` identify the exact filesystem object captured, and
    ``ctime_ns`` records its last metadata-change time; together with
    ``size``/``mtime_ns`` these let every later re-stat detect not just a
    content rewrite but an in-place inode swap or a same-size mutation that
    also rewrote the timestamp, since even that still bumps ctime.
    """

    archive_name: str
    is_directory: bool
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    executable: bool
    locator: _ContentLocator | None


def capture_script_root(script_root: Path) -> CapturedContentPackage:
    """Deterministically archive a script root into an immutable content package.

    Captures every regular file and empty directory beneath ``script_root``,
    including hidden entries and vendored dependency trees, into a
    byte-exact ``ZIP_STORED`` archive. Fails closed on an unsafe entry, an
    empty root, a standard-ZIP size/count/aggregate overflow, or a mutation
    detected between validating an entry and reading its bytes -- including a
    final rescan that catches an entry added, removed, or retyped after the
    initial scan but before delivery.
    """

    resolved_root = _validate_script_root(script_root)
    if _posix_secure_traversal_available():
        return _capture_script_root_posix(resolved_root)
    return _capture_script_root_fallback(resolved_root)


def _capture_script_root_posix(root: Path) -> CapturedContentPackage:
    """Capture through one root file descriptor held open for the whole operation.

    The same anchored ``root_fd`` -- never a re-derived root pathname -- backs
    the initial scan, the archive write, and a closing rescan, so nothing in
    this operation ever reopens the root by path between validating it and
    reading from it.
    """

    root_fd = _open_root_directory(root)
    try:
        entries = _scan_posix_tree(root_fd)
        _require_non_empty_script_root(entries)
        _preflight_standard_zip_limits(entries)
        _preflight_aggregate_archive_size(entries)
        archive_bytes = _write_deterministic_archive_posix(root_fd, entries)
        _require_matching_entry_sets(entries, _scan_posix_tree(root_fd))
        return _finish_captured_package(archive_bytes)
    finally:
        os.close(root_fd)


def _capture_script_root_fallback(root: Path) -> CapturedContentPackage:
    entries = _scan_fallback(root)
    _require_non_empty_script_root(entries)
    _preflight_standard_zip_limits(entries)
    _preflight_aggregate_archive_size(entries)
    archive_bytes = _write_deterministic_archive_fallback(entries)
    _require_matching_entry_sets(entries, _scan_fallback(root))
    return _finish_captured_package(archive_bytes)


def _require_non_empty_script_root(entries: Sequence[_ScriptRootEntry]) -> None:
    if not entries:
        raise ScriptRootUnavailableError("Script root has no content to capture.")


def _require_matching_entry_sets(
    expected: Sequence[_ScriptRootEntry], rescanned: Sequence[_ScriptRootEntry]
) -> None:
    """Fail closed if a final rescan's entry shape disagrees with what was captured."""

    expected_shape = {(entry.archive_name, entry.is_directory) for entry in expected}
    rescanned_shape = {(entry.archive_name, entry.is_directory) for entry in rescanned}
    if expected_shape != rescanned_shape:
        raise ContentCaptureRaceError("Script root changed while it was being captured.")


def _finish_captured_package(archive_bytes: bytes) -> CapturedContentPackage:
    digest = _render_funcs_zip_digest(archive_bytes)
    return CapturedContentPackage.create(
        archive_bytes=archive_bytes, digest_kind=FUNCS_ZIP_DIGEST_KIND, digest=digest
    )


def build_expected_manifest_binding(
    session: DurableSessionRecord,
    *,
    sandbox_group_resource_id: str,
    state_store_fingerprint: str,
) -> ExpectedSandboxManifestBinding:
    """Build the authoritative manifest binding this session must publish.

    ``session`` and ``state_store_fingerprint`` are already-typed, opaque
    controller inputs; this function performs no derivation beyond stamping
    the fixed ``manifest_version`` and requiring a non-null ``sandbox_id``.
    """

    if session.sandbox_id is None:
        raise SessionSandboxIdentityRequiredError(
            "The durable session record has no sandbox_id assigned yet."
        )
    return ExpectedSandboxManifestBinding.create(
        manifest_version=MANIFEST_VERSION,
        protocol_version=session.protocol,
        session_id=session.session_id,
        owner_hash_version=session.owner_partition.owner_hash_version,
        owner_hash=session.owner_partition.owner_hash,
        app_hash=session.owner_partition.app_hash,
        sandbox_group_resource_id=sandbox_group_resource_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        state_store_fingerprint=state_store_fingerprint,
    )


async def deliver_content_package(
    transport: SandboxFileTransport,
    package: CapturedContentPackage,
    expected: ExpectedSandboxManifestBinding,
    live_identity: ProvisionedSandboxIdentity,
) -> DeliveredContentPackage:
    """Deliver a captured package plus its manifest seed, verifying every write.

    Requires the captured digest and live sandbox identity to already match
    ``expected`` before any sandbox write. The large content archive is
    verified by size only (never re-read in full) and a failed write always
    propagates as-is: unlike the small sidecar/seed, an ambiguous archive
    write outcome cannot be strengthened into a "landed anyway" success
    without a full read-back, which stays out of scope -- a coincidentally
    same-sized stale file from an earlier delivery must never be accepted.
    The sidecar and seed are verified byte-for-byte and, for the seed, by a
    strict re-parse, so a write that raises after possibly committing is
    classified by one bounded read-back through that stronger check before
    treating it as failed.
    """

    _require_matching_digest(package, expected)
    _require_matching_sandbox_identity(expected, live_identity)

    await _deliver_content_archive(transport, package)
    await _deliver_digest_sidecar(transport, package)
    await _deliver_manifest_seed(transport, expected, live_identity)

    return DeliveredContentPackage.create(package=package, expected_binding=expected)


async def read_live_manifest_binding(
    transport: SandboxFileTransport,
    expected: ExpectedSandboxManifestBinding,
    live_identity: ProvisionedSandboxIdentity,
) -> ObservedSandboxManifestBinding:
    """Read, strictly parse, and verify the harness-authored live session manifest.

    Returns the verified observation only once every routing-critical field
    matches. A missing manifest means the harness has not published one yet;
    any other file-operation failure (auth, throttling, transient service
    error) propagates unchanged so the caller can classify it, rather than
    being folded into the same not-ready outcome as a simple absence. A parse
    or binding mismatch on a manifest that *was* read is a redacted integrity
    error. This performs one direct read, not a readiness-polling loop.
    """

    try:
        manifest_bytes = await transport.read_file(SESSION_MANIFEST_PATH)
    except SandboxFileNotFoundError:
        raise LiveManifestNotReadyError(
            "The harness has not published a live session manifest yet."
        ) from None
    observed = parse_sandbox_manifest_binding(manifest_bytes)
    verify_sandbox_manifest(expected, observed, live_identity)
    return observed


def _require_matching_digest(
    package: CapturedContentPackage, expected: ExpectedSandboxManifestBinding
) -> None:
    if package.digest_kind != expected.digest_kind or package.digest != expected.digest:
        raise ContentBindingMismatchError(
            "Captured content digest does not match the authoritative session binding."
        )


def _require_matching_sandbox_identity(
    expected: ExpectedSandboxManifestBinding, live_identity: ProvisionedSandboxIdentity
) -> None:
    if (
        expected.sandbox_id != live_identity.sandbox_id
        or expected.sandbox_group_resource_id != live_identity.group_resource_id
    ):
        raise ContentBindingMismatchError(
            "Expected sandbox binding does not match the live sandbox identity."
        )


async def _deliver_content_archive(
    transport: SandboxFileTransport, package: CapturedContentPackage
) -> None:
    # No except here: an ambiguous write outcome must never be reclassified as
    # success by a same-sized (possibly stale) file already at this path.
    await transport.write_file(CONTENT_ARCHIVE_PATH, package.archive_bytes, create_dirs=True)
    if not await _content_archive_landed(transport, package.size):
        raise ContentDeliveryVerificationError(
            "Delivered content archive size does not match the captured package."
        )


async def _content_archive_landed(transport: SandboxFileTransport, expected_size: int) -> bool:
    try:
        observed = await transport.stat_file(CONTENT_ARCHIVE_PATH)
    except (SandboxFileNotFoundError, SandboxFileOperationError):
        return False
    return not observed.is_directory and observed.size == expected_size


async def _deliver_digest_sidecar(
    transport: SandboxFileTransport, package: CapturedContentPackage
) -> None:
    sidecar_bytes = _render_digest_sidecar(package.digest)
    try:
        await transport.write_file(CONTENT_DIGEST_SIDECAR_PATH, sidecar_bytes, create_dirs=True)
    except (SandboxFileNotFoundError, SandboxFileOperationError):
        await _reraise_unless_sidecar_landed(transport, sidecar_bytes)
        return
    await _require_sidecar_landed(transport, sidecar_bytes)


async def _reraise_unless_sidecar_landed(
    transport: SandboxFileTransport, sidecar_bytes: bytes
) -> None:
    if await _digest_sidecar_landed(transport, sidecar_bytes):
        return
    raise


async def _require_sidecar_landed(transport: SandboxFileTransport, sidecar_bytes: bytes) -> None:
    if await _digest_sidecar_landed(transport, sidecar_bytes):
        return
    raise ContentDeliveryVerificationError(
        "Delivered digest sidecar does not match what was written."
    )


async def _digest_sidecar_landed(transport: SandboxFileTransport, expected_bytes: bytes) -> bool:
    try:
        observed_bytes = await transport.read_file(CONTENT_DIGEST_SIDECAR_PATH)
    except (SandboxFileNotFoundError, SandboxFileOperationError):
        return False
    return observed_bytes == expected_bytes


async def _deliver_manifest_seed(
    transport: SandboxFileTransport,
    expected: ExpectedSandboxManifestBinding,
    live_identity: ProvisionedSandboxIdentity,
) -> None:
    seed_bytes = render_sandbox_manifest_binding(expected)
    try:
        await transport.write_file(CONTENT_MANIFEST_SEED_PATH, seed_bytes, create_dirs=True)
    except (SandboxFileNotFoundError, SandboxFileOperationError):
        await _reraise_unless_seed_landed(transport, expected, live_identity)
        return
    await _require_seed_landed(transport, expected, live_identity)


async def _reraise_unless_seed_landed(
    transport: SandboxFileTransport,
    expected: ExpectedSandboxManifestBinding,
    live_identity: ProvisionedSandboxIdentity,
) -> None:
    if await _manifest_seed_landed(transport, expected, live_identity):
        return
    raise


async def _require_seed_landed(
    transport: SandboxFileTransport,
    expected: ExpectedSandboxManifestBinding,
    live_identity: ProvisionedSandboxIdentity,
) -> None:
    if await _manifest_seed_landed(transport, expected, live_identity):
        return
    raise ContentDeliveryVerificationError(
        "Delivered manifest seed does not match the expected binding."
    )


async def _manifest_seed_landed(
    transport: SandboxFileTransport,
    expected: ExpectedSandboxManifestBinding,
    live_identity: ProvisionedSandboxIdentity,
) -> bool:
    try:
        seed_bytes = await transport.read_file(CONTENT_MANIFEST_SEED_PATH)
        observed = parse_sandbox_manifest_binding(seed_bytes)
        verify_sandbox_manifest(expected, observed, live_identity)
    except (SandboxFileNotFoundError, SandboxFileOperationError, SandboxManifestMismatchError):
        return False
    return True


def _render_digest_sidecar(digest: str) -> bytes:
    return f"{digest}\n".encode("ascii")


def _render_funcs_zip_digest(archive_bytes: bytes) -> str:
    return f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}"


def _resolve_script_root_path(script_root: Path) -> Path:
    try:
        return Path(script_root).resolve()
    except OSError:
        raise ScriptRootUnavailableError(
            "Script root is not usable for deterministic capture."
        ) from None


def _script_root_is_directory(resolved: Path) -> bool:
    try:
        return resolved.is_dir()
    except OSError:
        raise ScriptRootUnavailableError(
            "Script root is not usable for deterministic capture."
        ) from None


def _validate_script_root(script_root: Path) -> Path:
    resolved = _resolve_script_root_path(script_root)
    if not _script_root_is_directory(resolved):
        raise ScriptRootUnavailableError("Script root is not an existing directory.")
    return resolved


def _posix_secure_traversal_available() -> bool:
    """Whether this platform supports ``dir_fd``/``O_NOFOLLOW`` race-free traversal.

    A module-level function (rather than an inline check) so tests can force
    the portable fallback path on a platform that does support the secure one.
    """

    return (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.readlink in os.supports_dir_fd
        and os.listdir in os.supports_fd
    )


# ---------------------------------------------------------------------------
# POSIX secure traversal: dir_fd-anchored, O_NOFOLLOW at every hop
# ---------------------------------------------------------------------------


def _open_root_directory(root: Path) -> int:
    try:
        return os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError:
        raise ScriptRootUnavailableError(
            "Script root is not usable for deterministic capture."
        ) from None


def _scan_posix_secure(root: Path) -> tuple[_ScriptRootEntry, ...]:
    """Convenience wrapper: open, scan, and close one root fd for standalone use.

    The production capture flow does not use this; it keeps one root fd open
    across scan, write, and rescan via :func:`_scan_posix_tree` directly.
    """

    root_fd = _open_root_directory(root)
    try:
        return _scan_posix_tree(root_fd)
    finally:
        os.close(root_fd)


def _scan_posix_tree(root_fd: int) -> tuple[_ScriptRootEntry, ...]:
    entries: list[_ScriptRootEntry] = []
    _scan_posix_directory(root_fd, root_fd, (), entries)
    return tuple(sorted(entries, key=lambda entry: entry.archive_name))


def _list_directory_names(dir_fd: int) -> list[str]:
    try:
        return sorted(os.listdir(dir_fd))
    except OSError:
        raise ContentCaptureRaceError("Script root changed while it was being captured.") from None


def _scan_posix_directory(
    root_fd: int, dir_fd: int, prefix: tuple[str, ...], entries: list[_ScriptRootEntry]
) -> None:
    names = _list_directory_names(dir_fd)
    if not names:
        _append_posix_empty_directory(dir_fd, prefix, entries)
        return
    for name in names:
        _scan_posix_child(root_fd, dir_fd, prefix, name, entries)


def _append_posix_empty_directory(
    dir_fd: int, prefix: tuple[str, ...], entries: list[_ScriptRootEntry]
) -> None:
    if not prefix:
        return
    entries.append(_posix_empty_directory_entry(dir_fd, prefix))


def _posix_empty_directory_entry(dir_fd: int, prefix: tuple[str, ...]) -> _ScriptRootEntry:
    info = os.fstat(dir_fd)
    return _ScriptRootEntry(
        archive_name=f"{'/'.join(prefix)}/",
        is_directory=True,
        size=0,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
        device=info.st_dev,
        inode=info.st_ino,
        executable=False,
        locator=None,
    )


def _lstat_child(dir_fd: int, name: str) -> os.stat_result:
    """lstat a name freshly listed from ``dir_fd``; its disappearance here is a capture race."""

    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        raise ContentCaptureRaceError("Script root changed while it was being captured.") from None


def _open_nofollow_child(dir_fd: int, name: str) -> int:
    try:
        return os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        raise ContentCaptureRaceError("Script root changed while it was being captured.") from None


def _scan_posix_child(
    root_fd: int,
    dir_fd: int,
    prefix: tuple[str, ...],
    name: str,
    entries: list[_ScriptRootEntry],
) -> None:
    archive_components = (*prefix, name)
    child_lstat = _lstat_child(dir_fd, name)
    if stat_module.S_ISLNK(child_lstat.st_mode):
        entries.append(_resolve_posix_symlink(root_fd, prefix, name, archive_components))
        return
    fd = _open_nofollow_child(dir_fd, name)
    try:
        info = os.fstat(fd)
        _append_directory_or_file_entry(root_fd, fd, info, archive_components, entries)
    finally:
        os.close(fd)


def _append_directory_or_file_entry(
    root_fd: int,
    fd: int,
    info: os.stat_result,
    archive_components: tuple[str, ...],
    entries: list[_ScriptRootEntry],
) -> None:
    if stat_module.S_ISDIR(info.st_mode):
        _scan_posix_directory(root_fd, fd, archive_components, entries)
        return
    if stat_module.S_ISREG(info.st_mode):
        entries.append(_posix_regular_file_entry(archive_components, info, archive_components))
        return
    raise UnsafeScriptRootEntryError(
        "Script root entry is neither a regular file, directory, nor safe symlink."
    )


def _posix_regular_file_entry(
    archive_components: tuple[str, ...],
    info: os.stat_result,
    locator_components: tuple[str, ...],
) -> _ScriptRootEntry:
    return _ScriptRootEntry(
        archive_name="/".join(archive_components),
        is_directory=False,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
        device=info.st_dev,
        inode=info.st_ino,
        executable=(info.st_mode & 0o111) != 0,
        locator=_PosixSecureLocator(components=locator_components),
    )


@dataclass(frozen=True, slots=True)
class _SymlinkHopOutcome:
    """One step of manual symlink-chain resolution.

    ``entry_stat`` is ``None`` while ``components`` still names another
    symlink to follow, and holds the target's stat once resolution reaches a
    regular file.
    """

    components: tuple[str, ...]
    entry_stat: os.stat_result | None


def _resolve_posix_symlink(
    root_fd: int, dir_components: tuple[str, ...], name: str, archive_components: tuple[str, ...]
) -> _ScriptRootEntry:
    """Manually resolve a symlink chain to a contained regular file.

    Every hop is anchored at ``root_fd`` through ``dir_fd``-relative
    ``O_NOFOLLOW`` opens, so an attacker cannot race a benign target into an
    escaping one partway through resolution; a chain longer than
    ``_MAX_SYMLINK_HOPS`` (including a direct cycle) fails closed.
    """

    components = (*dir_components, name)
    for _ in range(_MAX_SYMLINK_HOPS):
        outcome = _follow_one_symlink_hop(root_fd, components)
        if outcome.entry_stat is not None:
            return _posix_regular_file_entry(
                archive_components, outcome.entry_stat, outcome.components
            )
        components = outcome.components
    raise UnsafeScriptRootEntryError("Script root symlink exceeds the maximum resolution depth.")


def _open_directory_chain(root_fd: int, components: tuple[str, ...]) -> int:
    """Open a directory fd for a component chain, walking from the root anchor.

    Every hop uses ``O_NOFOLLOW`` so a symlink anywhere in an intermediate
    position fails the open rather than being silently followed. Returns an
    fd the caller owns and must close.
    """

    current_fd = root_fd
    opened_fds: list[int] = []
    try:
        for component in components:
            next_fd = os.open(
                component, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=current_fd
            )
            opened_fds.append(next_fd)
            current_fd = next_fd
        return os.dup(current_fd)
    finally:
        for opened_fd in opened_fds:
            os.close(opened_fd)


def _open_directory_chain_checked(root_fd: int, components: tuple[str, ...]) -> int:
    try:
        return _open_directory_chain(root_fd, components)
    except OSError:
        raise ContentCaptureRaceError("Script root changed while it was being captured.") from None


def _follow_one_symlink_hop(root_fd: int, components: tuple[str, ...]) -> _SymlinkHopOutcome:
    parent_components, name = components[:-1], components[-1]
    parent_fd = _open_directory_chain_checked(root_fd, parent_components)
    try:
        return _resolve_symlink_candidate(parent_fd, parent_components, name)
    finally:
        os.close(parent_fd)


def _lstat_symlink_target(parent_fd: int, name: str) -> os.stat_result:
    """lstat a resolved symlink target; its absence here means a broken symlink, not a race."""

    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        raise UnsafeScriptRootEntryError("Script root symlink is broken.") from None


def _resolve_symlink_candidate(
    parent_fd: int, parent_components: tuple[str, ...], name: str
) -> _SymlinkHopOutcome:
    candidate_lstat = _lstat_symlink_target(parent_fd, name)
    if stat_module.S_ISLNK(candidate_lstat.st_mode):
        return _read_next_symlink_hop(parent_fd, parent_components, name)
    return _open_and_classify_symlink_target(parent_fd, parent_components, name)


def _open_and_classify_symlink_target(
    parent_fd: int, parent_components: tuple[str, ...], name: str
) -> _SymlinkHopOutcome:
    fd = _open_nofollow_child(parent_fd, name)
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat_module.S_ISREG(info.st_mode):
        raise UnsafeScriptRootEntryError(
            "Script root symlink does not target a safely archivable regular file."
        )
    return _SymlinkHopOutcome(components=(*parent_components, name), entry_stat=info)


def _read_symlink_target(parent_fd: int, name: str) -> str:
    try:
        return os.readlink(name, dir_fd=parent_fd)
    except OSError:
        raise UnsafeScriptRootEntryError("Script root symlink could not be safely read.") from None


def _read_next_symlink_hop(
    parent_fd: int, parent_components: tuple[str, ...], name: str
) -> _SymlinkHopOutcome:
    target = _read_symlink_target(parent_fd, name)
    resolved_components = _resolve_symlink_target(parent_components, target)
    if not resolved_components:
        raise UnsafeScriptRootEntryError("Script root symlink resolves outside the script root.")
    return _SymlinkHopOutcome(components=resolved_components, entry_stat=None)


def _resolve_symlink_target(
    containing_dir_components: tuple[str, ...], target: str
) -> tuple[str, ...] | None:
    """Resolve a relative symlink target string to root-anchored components.

    An absolute target is always rejected: it names a real host filesystem
    path, never a location relative to the script root, regardless of where
    it would happen to resolve. A ``..`` that would ascend above the root is
    rejected the same way.
    """

    if target.startswith("/"):
        return None
    components: list[str] = list(containing_dir_components)
    for segment in target.split("/"):
        next_components = _apply_symlink_target_segment(components, segment)
        if next_components is None:
            return None
        components = next_components
    if not components:
        return None
    return tuple(components)


def _apply_symlink_target_segment(components: list[str], segment: str) -> list[str] | None:
    if segment in ("", "."):
        return components
    if segment != "..":
        return [*components, segment]
    if not components:
        return None
    return components[:-1]


def _require_posix_locator(entry: _ScriptRootEntry) -> _PosixSecureLocator:
    if isinstance(entry.locator, _PosixSecureLocator):
        return entry.locator
    raise ContentPackagingError("Script root entry is missing its secure content locator.")


def _open_posix_regular_file_for_read(root_fd: int, components: tuple[str, ...]) -> int:
    """Re-locate a regular file securely from the root anchor for reading.

    Every hop is a fresh ``O_NOFOLLOW`` open anchored at ``root_fd``, so any
    change to the path since the scan -- a swapped symlink, a deleted or
    retyped entry -- fails this open rather than silently following it.
    """

    parent_components, name = components[:-1], components[-1]
    parent_fd = _open_directory_chain_checked(root_fd, parent_components)
    try:
        return _open_nofollow_child(parent_fd, name)
    finally:
        os.close(parent_fd)


def _require_matching_fd_stat(observed: os.stat_result, entry: _ScriptRootEntry) -> None:
    if (
        not stat_module.S_ISREG(observed.st_mode)
        or observed.st_dev != entry.device
        or observed.st_ino != entry.inode
        or observed.st_size != entry.size
        or observed.st_mtime_ns != entry.mtime_ns
        or observed.st_ctime_ns != entry.ctime_ns
    ):
        raise ContentCaptureRaceError("Script root changed while it was being captured.")


def _copy_file_into_archive_entry(source: IO[bytes], dest: IO[bytes]) -> None:
    while True:
        chunk = source.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        dest.write(chunk)


def _write_empty_archive_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    with archive.open(info, mode="w"):
        pass


def _copy_posix_fd_into_archive(
    fd: int, entry: _ScriptRootEntry, archive: zipfile.ZipFile, info: zipfile.ZipInfo
) -> None:
    try:
        with os.fdopen(fd, "rb") as source:
            _require_matching_fd_stat(os.fstat(source.fileno()), entry)
            with archive.open(info, mode="w") as dest:
                _copy_file_into_archive_entry(source, dest)
            _require_matching_fd_stat(os.fstat(source.fileno()), entry)
    except OSError:
        raise ContentCaptureRaceError("Script root changed while it was being captured.") from None


def _write_entry_posix(archive: zipfile.ZipFile, root_fd: int, entry: _ScriptRootEntry) -> None:
    info = _archive_member_info(entry)
    if entry.is_directory:
        _write_empty_archive_entry(archive, info)
        return
    locator = _require_posix_locator(entry)
    fd = _open_posix_regular_file_for_read(root_fd, locator.components)
    _copy_posix_fd_into_archive(fd, entry, archive, info)


def _write_deterministic_archive_posix(
    root_fd: int, entries: Sequence[_ScriptRootEntry]
) -> bytes:
    """Stream every entry into a deterministic archive using the caller's root fd.

    Takes an already-open ``root_fd`` and never reopens the root by path; the
    caller owns the fd's lifetime (opened once for scan, write, and rescan).
    """

    def write_entry(archive: zipfile.ZipFile, entry: _ScriptRootEntry) -> None:
        _write_entry_posix(archive, root_fd, entry)

    return _stream_entries_into_archive(entries, write_entry)


# ---------------------------------------------------------------------------
# Portable fallback traversal (platforms without dir_fd/O_NOFOLLOW support)
# ---------------------------------------------------------------------------


def _list_directory_children(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir(), key=lambda child: child.name)
    except OSError:
        raise ContentCaptureRaceError("Script root changed while it was being captured.") from None


def _scan_fallback_directory(root: Path, directory: Path, entries: list[_ScriptRootEntry]) -> None:
    children = _list_directory_children(directory)
    if not children:
        _append_fallback_empty_directory(root, directory, entries)
        return
    for child in children:
        _scan_fallback_child(root, child, entries)


def _scan_fallback(root: Path) -> tuple[_ScriptRootEntry, ...]:
    entries: list[_ScriptRootEntry] = []
    _scan_fallback_directory(root, root, entries)
    return tuple(sorted(entries, key=lambda entry: entry.archive_name))


def _append_fallback_empty_directory(
    root: Path, directory: Path, entries: list[_ScriptRootEntry]
) -> None:
    if directory == root:
        return
    entries.append(_fallback_empty_directory_entry(root, directory))


def _stat_fallback_path(path: Path) -> os.stat_result:
    """Stat a path just observed during the scan; its disappearance here is a capture race."""

    try:
        return path.stat()
    except OSError:
        raise ContentCaptureRaceError("Script root changed while it was being captured.") from None


def _fallback_empty_directory_entry(root: Path, directory: Path) -> _ScriptRootEntry:
    dir_stat = _stat_fallback_path(directory)
    return _ScriptRootEntry(
        archive_name=f"{directory.relative_to(root).as_posix()}/",
        is_directory=True,
        size=0,
        mtime_ns=dir_stat.st_mtime_ns,
        ctime_ns=dir_stat.st_ctime_ns,
        device=dir_stat.st_dev,
        inode=dir_stat.st_ino,
        executable=False,
        locator=None,
    )


def _fallback_stat_predicate(path: Path, predicate: Callable[[Path], bool]) -> bool:
    """Evaluate a stat-based type predicate, translating any raised ``OSError``.

    An unwrapped predicate call can otherwise leak a path through a
    permission or other stat failure (e.g. ``EACCES``, which
    ``Path.is_dir``/``is_file``/``is_symlink`` do not themselves swallow) --
    exactly the condition an adversarial or racing script root can trigger.
    """

    try:
        return predicate(path)
    except OSError:
        raise ContentCaptureRaceError("Script root changed while it was being captured.") from None


def _scan_fallback_child(root: Path, child: Path, entries: list[_ScriptRootEntry]) -> None:
    if _fallback_stat_predicate(child, Path.is_symlink):
        entries.append(_resolve_fallback_symlink(root, child))
        return
    if _fallback_stat_predicate(child, Path.is_dir):
        _scan_fallback_directory(root, child, entries)
        return
    if _fallback_stat_predicate(child, Path.is_file):
        entries.append(_fallback_file_entry(root, child, child))
        return
    raise UnsafeScriptRootEntryError(
        "Script root entry is neither a regular file, directory, nor safe symlink."
    )


def _read_fallback_symlink_target(link_path: Path) -> str:
    try:
        return os.readlink(link_path)
    except OSError:
        raise UnsafeScriptRootEntryError(
            "Script root symlink is broken or forms a loop."
        ) from None


def _resolve_fallback_symlink_path(link_path: Path) -> Path:
    try:
        return link_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise UnsafeScriptRootEntryError(
            "Script root symlink is broken or forms a loop."
        ) from None


def _resolve_fallback_symlink(root: Path, link_path: Path) -> _ScriptRootEntry:
    """Resolve one script-root symlink to a contained regular file.

    Only the immediate target is checked for being absolute; a deeper hop
    that is itself absolute is not inspected (unlike the POSIX secure path,
    which checks every hop). The containment check below still rejects any
    chain that resolves outside the root, so this narrows accuracy, not safety.
    """

    immediate_target = _read_fallback_symlink_target(link_path)
    if os.path.isabs(immediate_target):
        raise UnsafeScriptRootEntryError("Script root symlink resolves outside the script root.")
    resolved = _resolve_fallback_symlink_path(link_path)
    if not resolved.is_relative_to(root):
        raise UnsafeScriptRootEntryError("Script root symlink resolves outside the script root.")
    if _fallback_stat_predicate(resolved, Path.is_dir):
        raise UnsafeScriptRootEntryError(
            "Script root symlink targets a directory, which is unsupported."
        )
    if not _fallback_stat_predicate(resolved, Path.is_file):
        raise UnsafeScriptRootEntryError("Script root symlink does not target a regular file.")
    return _fallback_file_entry(root, link_path, resolved)


def _fallback_file_entry(root: Path, archive_source: Path, bytes_source: Path) -> _ScriptRootEntry:
    file_stat = _stat_fallback_path(bytes_source)
    return _ScriptRootEntry(
        archive_name=archive_source.relative_to(root).as_posix(),
        is_directory=False,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        ctime_ns=file_stat.st_ctime_ns,
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        executable=(file_stat.st_mode & 0o111) != 0,
        locator=_FallbackLocator(
            resolved_path=bytes_source, identity=(file_stat.st_dev, file_stat.st_ino)
        ),
    )


def _require_fallback_locator(entry: _ScriptRootEntry) -> _FallbackLocator:
    if isinstance(entry.locator, _FallbackLocator):
        return entry.locator
    raise ContentPackagingError("Script root entry is missing its secure content locator.")


def _stat_fallback_locator(locator: _FallbackLocator) -> os.stat_result:
    try:
        return locator.resolved_path.stat()
    except OSError:
        raise ContentCaptureRaceError("Script root changed while it was being captured.") from None


def _require_matching_fallback_stat(
    observed: os.stat_result, entry: _ScriptRootEntry, locator: _FallbackLocator
) -> None:
    if (
        (observed.st_dev, observed.st_ino) != locator.identity
        or observed.st_size != entry.size
        or observed.st_mtime_ns != entry.mtime_ns
        or observed.st_ctime_ns != entry.ctime_ns
    ):
        raise ContentCaptureRaceError("Script root changed while it was being captured.")


def _copy_fallback_locator_into_archive(
    locator: _FallbackLocator, archive: zipfile.ZipFile, info: zipfile.ZipInfo
) -> None:
    try:
        with locator.resolved_path.open("rb") as source, archive.open(info, mode="w") as dest:
            _copy_file_into_archive_entry(source, dest)
    except OSError:
        raise ContentCaptureRaceError("Script root changed while it was being captured.") from None


def _write_entry_fallback(archive: zipfile.ZipFile, entry: _ScriptRootEntry) -> None:
    info = _archive_member_info(entry)
    if entry.is_directory:
        _write_empty_archive_entry(archive, info)
        return
    locator = _require_fallback_locator(entry)
    _require_matching_fallback_stat(_stat_fallback_locator(locator), entry, locator)
    _copy_fallback_locator_into_archive(locator, archive, info)
    _require_matching_fallback_stat(_stat_fallback_locator(locator), entry, locator)


def _write_deterministic_archive_fallback(entries: Sequence[_ScriptRootEntry]) -> bytes:
    return _stream_entries_into_archive(entries, _write_entry_fallback)


# ---------------------------------------------------------------------------
# Shared preflight, streaming, and ZIP metadata helpers
# ---------------------------------------------------------------------------


def _preflight_standard_zip_limits(entries: Sequence[_ScriptRootEntry]) -> None:
    if len(entries) > _MAX_STANDARD_ZIP_ENTRIES:
        raise ContentArchiveTooLargeError(
            "Script root has more entries than the standard ZIP format allows."
        )
    for entry in entries:
        if entry.size > _MAX_STANDARD_ZIP_ENTRY_SIZE:
            raise ContentArchiveTooLargeError(
                "Script root has a file larger than the standard ZIP format allows."
            )


def _preflight_aggregate_archive_size(entries: Sequence[_ScriptRootEntry]) -> None:
    """Reject an archive whose deterministic total size is too large, from metadata alone.

    Computes the exact stored-ZIP overhead (local headers, central directory
    records, end-of-central-directory) plus entry sizes, without reading any
    file content, so an oversized tree is rejected before any read/write work.
    """

    total = _ZIP_END_OF_CENTRAL_DIRECTORY_SIZE
    for entry in entries:
        name_bytes = len(entry.archive_name.encode("utf-8"))
        total += _ZIP_LOCAL_HEADER_FIXED_SIZE + name_bytes + entry.size
        total += _ZIP_CENTRAL_DIRECTORY_FIXED_SIZE + name_bytes
        if total > _MAX_ARCHIVE_OPERATIONAL_SIZE:
            raise ContentArchiveTooLargeError(
                "Script root archive would exceed the operational archive size bound."
            )


def _populate_archive(
    temp_archive: IO[bytes],
    entries: Sequence[_ScriptRootEntry],
    write_entry: Callable[[zipfile.ZipFile, _ScriptRootEntry], None],
) -> None:
    try:
        with zipfile.ZipFile(
            temp_archive, mode="w", compression=zipfile.ZIP_STORED, allowZip64=False
        ) as archive:
            for entry in entries:
                write_entry(archive, entry)
    except zipfile.LargeZipFile:
        raise ContentArchiveTooLargeError(
            "Script root archive would exceed the standard ZIP format limits."
        ) from None


def _stream_entries_into_archive(
    entries: Sequence[_ScriptRootEntry],
    write_entry: Callable[[zipfile.ZipFile, _ScriptRootEntry], None],
) -> bytes:
    """Stream every entry into a temp-file-backed archive, then read it back once.

    Writing to a temporary file rather than an in-memory buffer keeps peak
    memory bounded by the read chunk size while each entry's bytes are being
    copied; the single final read-back is unavoidable given
    ``SandboxFileTransport.write_file()``'s ``bytes``-only contract.
    """

    with tempfile.TemporaryFile() as temp_archive:
        _populate_archive(temp_archive, entries, write_entry)
        temp_archive.seek(0)
        return temp_archive.read()


def _archive_member_info(entry: _ScriptRootEntry) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=entry.archive_name, date_time=_ARCHIVE_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = _UNIX_CREATOR_SYSTEM
    info.create_version = _ZIP_SPEC_VERSION
    info.extract_version = _ZIP_SPEC_VERSION
    info.reserved = 0
    info.volume = 0
    info.internal_attr = 0
    info.comment = b""
    info.extra = b""
    if entry.is_directory:
        info.external_attr = (_DIRECTORY_UNIX_MODE << 16) | _MS_DOS_DIRECTORY_ATTRIBUTE
    else:
        mode = _EXECUTABLE_FILE_UNIX_MODE if entry.executable else _STANDARD_FILE_UNIX_MODE
        info.external_attr = mode << 16
    return info
