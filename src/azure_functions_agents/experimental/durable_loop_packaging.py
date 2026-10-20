"""Tool-bundle capture for the durable-loop Windows development spike."""

from __future__ import annotations

import asyncio
import hashlib
import io
import stat
import sys
import zipfile
from pathlib import Path

from ..controller.package import (
    FUNCS_ZIP_DIGEST_KIND,
    CapturedContentPackage,
    ContentArchiveTooLargeError,
    ContentCaptureRaceError,
    ScriptRootUnavailableError,
    UnsafeScriptRootEntryError,
    get_content_package,
)

_WINDOWS = sys.platform == "win32"
_MAX_BUNDLE_SIZE = 256 * 1024 * 1024
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
type _BundleEntry = tuple[str, int, int, int, int, int, int]


async def get_durable_tool_package(root: Path) -> CapturedContentPackage:
    """Capture a trusted local Windows bundle, or use secure Linux capture."""
    if not _WINDOWS:
        return await get_content_package(root)
    return await asyncio.to_thread(_capture_windows_bundle, root)


def _bundle_entries(root: Path) -> tuple[_BundleEntry, ...]:
    entries: list[_BundleEntry] = []

    def visit(path: Path) -> None:
        if path.is_symlink() or path.is_junction():
            raise UnsafeScriptRootEntryError(
                "Windows tool bundles cannot contain links or junctions."
            )
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            children = sorted(path.iterdir())
            if not children and path != root:
                entries.append(
                    (
                        name + "/",
                        0,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                        info.st_dev,
                        info.st_ino,
                        info.st_mode,
                    )
                )
            for child in children:
                visit(child)
        elif stat.S_ISREG(info.st_mode):
            entries.append(
                (
                    name,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                )
            )
        else:
            raise UnsafeScriptRootEntryError(
                "Windows tool bundles require regular files and directories."
            )

    visit(root)
    return tuple(sorted(entries))


def _capture_windows_bundle(root: Path) -> CapturedContentPackage:
    if root.is_symlink() or root.is_junction():
        raise UnsafeScriptRootEntryError("Windows tool bundle root cannot be a link or junction.")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ScriptRootUnavailableError("Tool bundle root must be a directory.")
    entries = _bundle_entries(root)
    if not entries:
        raise ScriptRootUnavailableError("Tool bundle root cannot be empty.")
    # Stored ZIP headers plus worst-case UTF-8 name metadata, before reading files.
    estimated_size = 22 + sum(
        size + 76 + 4 * len(name.encode("utf-8")) for name, size, *_ in entries
    )
    if len(entries) > 0xFFFF or estimated_size > _MAX_BUNDLE_SIZE:
        raise ContentArchiveTooLargeError("Windows tool bundle exceeds archive size limits.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name, size, *_ in entries:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = (
                ((stat.S_IFDIR | 0o755) << 16) | 0x10
                if name.endswith("/")
                else (stat.S_IFREG | 0o644) << 16
            )
            payload = b""
            if not name.endswith("/"):
                with (root / name).open("rb") as source:
                    payload = source.read(size + 1)
                if len(payload) != size:
                    raise ContentCaptureRaceError("Windows tool bundle changed during capture.")
            archive.writestr(info, payload)
    if _bundle_entries(root) != entries:
        raise ContentCaptureRaceError("Windows tool bundle changed during capture.")
    payload = buffer.getvalue()
    return CapturedContentPackage.create(
        archive_bytes=payload,
        digest_kind=FUNCS_ZIP_DIGEST_KIND,
        digest="sha256:" + hashlib.sha256(payload).hexdigest(),
    )
