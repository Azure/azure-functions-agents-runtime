from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from azure_functions_agents.controller.package import (
    ContentArchiveTooLargeError,
    ContentCaptureRaceError,
    ScriptRootUnavailableError,
    UnsafeScriptRootEntryError,
)
from azure_functions_agents.experimental import durable_loop_packaging as packaging


@pytest.mark.asyncio
async def test_windows_bundle_is_deterministic_and_contains_exact_tool_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(packaging, "_WINDOWS", True)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "hello.py").write_bytes(b"def hello():\n    return 'hello'\n")
    (tmp_path / "empty").mkdir()
    first = await packaging.get_durable_tool_package(tmp_path)
    second = await packaging.get_durable_tool_package(tmp_path)
    assert first == second
    assert first.digest == "sha256:" + hashlib.sha256(first.archive_bytes).hexdigest()
    assert first.digest_kind == "funcs_zip"
    with zipfile.ZipFile(io.BytesIO(first.archive_bytes)) as archive:
        assert archive.namelist() == ["empty/", "tools/hello.py"]
        assert archive.read("tools/hello.py") == b"def hello():\n    return 'hello'\n"
        assert archive.getinfo("tools/hello.py").date_time == (1980, 1, 1, 0, 0, 0)


@pytest.mark.asyncio
async def test_non_windows_uses_existing_secure_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(packaging, "_WINDOWS", False)
    capture = AsyncMock()
    monkeypatch.setattr(packaging, "get_content_package", capture)
    assert await packaging.get_durable_tool_package(tmp_path) is capture.return_value
    capture.assert_awaited_once_with(tmp_path)


@pytest.mark.asyncio
async def test_windows_empty_bundle_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(packaging, "_WINDOWS", True)
    with pytest.raises(ScriptRootUnavailableError):
        await packaging.get_durable_tool_package(tmp_path)


@pytest.mark.asyncio
async def test_windows_link_or_junction_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(packaging, "_WINDOWS", True)
    (tmp_path / "tools").mkdir()
    monkeypatch.setattr(Path, "is_junction", lambda path: path.name == "tools")
    with pytest.raises(UnsafeScriptRootEntryError):
        await packaging.get_durable_tool_package(tmp_path)


@pytest.mark.asyncio
async def test_windows_bundle_mutation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(packaging, "_WINDOWS", True)
    (tmp_path / "tool.py").write_bytes(b"original")
    original_scan = packaging._bundle_entries
    scans = 0

    def scan(root: Path):
        nonlocal scans
        scans += 1
        if scans == 2:
            (root / "tool.py").write_bytes(b"modified content")
        return original_scan(root)

    monkeypatch.setattr(packaging, "_bundle_entries", scan)
    with pytest.raises(ContentCaptureRaceError):
        await packaging.get_durable_tool_package(tmp_path)


@pytest.mark.asyncio
async def test_windows_bundle_size_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(packaging, "_WINDOWS", True)
    monkeypatch.setattr(packaging, "_MAX_BUNDLE_SIZE", 128)
    (tmp_path / "large.py").write_bytes(b"x" * 129)
    with pytest.raises(ContentArchiveTooLargeError):
        await packaging.get_durable_tool_package(tmp_path)
