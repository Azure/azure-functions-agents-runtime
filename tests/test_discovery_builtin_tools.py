from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from azure_functions_agents.discovery import builtin_tools


@pytest.fixture
def allowed_read_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    monkeypatch.setattr(builtin_tools, "_ALLOWED_READ_DIRS", [str(allowed_dir)])
    return allowed_dir


def _create_symlink_or_skip(link_path: Path, target_path: Path) -> None:
    try:
        os.symlink(target_path, link_path)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")


def test_view_allows_file_within_allowed_dir(allowed_read_dir: Path) -> None:
    file_path = allowed_read_dir / "notes.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")

    result = json.loads(asyncio.run(builtin_tools.view(path=str(file_path))))

    assert result["content"] == "alpha\nbeta\n"
    assert result["total_lines"] == 2


def test_view_rejects_file_outside_allowed_dir(tmp_path: Path, allowed_read_dir: Path) -> None:
    outside_path = tmp_path / "outside.txt"
    outside_path.write_text("secret\n", encoding="utf-8")

    result = json.loads(asyncio.run(builtin_tools.view(path=str(outside_path))))

    assert result == {"error": "Access denied: path is not in an allowed directory"}


def test_view_rejects_symlink_to_outside_allowed_dir(
    tmp_path: Path, allowed_read_dir: Path
) -> None:
    outside_path = tmp_path / "outside.txt"
    outside_path.write_text("secret\n", encoding="utf-8")
    symlink_path = allowed_read_dir / "outside-link.txt"
    _create_symlink_or_skip(symlink_path, outside_path)

    result = json.loads(asyncio.run(builtin_tools.view(path=str(symlink_path))))

    assert result == {"error": "Access denied: path is not in an allowed directory"}


def test_jq_allows_symlink_to_file_within_allowed_dir(allowed_read_dir: Path) -> None:
    target_path = allowed_read_dir / "data.json"
    target_path.write_text(json.dumps({"items": [{"name": "alpha"}]}), encoding="utf-8")
    symlink_path = allowed_read_dir / "data-link.json"
    _create_symlink_or_skip(symlink_path, target_path)

    result = json.loads(
        asyncio.run(builtin_tools.jq(path=str(symlink_path), query=".items.[0].name"))
    )

    assert result == {"result": "alpha"}
