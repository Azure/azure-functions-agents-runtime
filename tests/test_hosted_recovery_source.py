"""Read-only recovery source boundaries."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from azure_functions_agents import _hosted_recovery_source as source_module
from azure_functions_agents._hosted_recovery_source import RecoverySource


def _write(root: Path, name: str, text: str = "value = 1\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_lazy_sorted_discovery_read_search_and_no_writes(tmp_path, monkeypatch):
    root = tmp_path / "app"
    reader = RecoverySource(root)
    _write(root, "z.py")
    _write(root, "pkg/a.py", "# café\nVALUE = 42\n")
    _write(root, "tests/test_app.py")
    _write(root, "Upper.PY")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    monkeypatch.chdir(tmp_path)

    assert reader.list_files() == ["Upper.PY", "pkg/a.py", "tests/test_app.py", "z.py"]
    assert reader.read_file("pkg\\a.py") == "# café\nVALUE = 42\n"
    assert reader.search_source("value = 42") == ["pkg/a.py:2:VALUE = 42"]
    assert reader.search_source("absent") == []
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("path", [
    "../outside.py", "pkg/../../outside.py", "pkg\\..\\outside.py", "/absolute.py",
    "C:\\absolute.py", "c:relative.py", "\\\\server\\share\\file.py", "\\root.py",
    ".env", "local.settings.json", "LOCAL.SETTINGS.PY", "credentials.py", "ENV.py",
    "secrets.py", "data.json", ".hidden/a.py", "VENV/a.py", "pkg/file.py:stream",
    "pkg./file.py", "pkg /file.py", "pkg//file.py", "./file.py", "file.py\x00", "",
])
def test_rejects_disallowed_paths(tmp_path, path):
    with pytest.raises(ValueError) as error:
        RecoverySource(tmp_path).read_file(path)
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize("directory", [
    ".git", ".venv", "venv", "VENV", "env", "envs", "build", "BUILD", "dist",
    "site", "site-packages", "node_modules", "__pycache__", "artifacts", "htmlcov",
    "__blobstorage__", "__queuestorage__", "credentials",
])
def test_prunes_excluded_directories(tmp_path, directory, monkeypatch):
    _write(tmp_path, f"{directory}/private.py", "# hidden\n")
    _write(tmp_path, "app.py")
    original_walk = source_module.os.walk
    visited = []

    def walk(*args, **kwargs):
        for entry in original_walk(*args, **kwargs):
            visited.append(Path(entry[0]))
            yield entry

    monkeypatch.setattr(source_module.os, "walk", walk)
    reader = RecoverySource(tmp_path)
    assert reader.list_files() == ["app.py"]
    assert visited == [tmp_path]
    assert reader.search_source("hidden") == []


def test_hidden_sensitive_non_source_and_oversized_files_are_not_listed(tmp_path):
    for name in (".hidden.py", ".env", "local.settings.json", "credentials.py", "env.py", "readme.md"):
        _write(tmp_path, name)
    _write(tmp_path, "huge.py", "x" * (32 * 1024 + 1))
    _write(tmp_path, "exact.py", "x" * (32 * 1024))
    reader = RecoverySource(tmp_path)
    assert reader.list_files() == ["exact.py"]
    assert len(reader.read_file("exact.py")) == 32 * 1024
    with pytest.raises(ValueError, match="size limit"):
        reader.read_file("huge.py")


@pytest.mark.parametrize("marker", ["pyvenv.cfg", "conda-meta/history"])
def test_custom_named_environments_are_pruned_and_unreadable(tmp_path, marker):
    _write(tmp_path, f"custom_environment/{marker}", "")
    _write(tmp_path, "custom_environment/private.py")
    reader = RecoverySource(tmp_path)
    assert reader.list_files() == []
    with pytest.raises(ValueError, match="Environment"):
        reader.read_file("custom_environment/private.py")


@pytest.mark.skipif(os.name != "nt", reason="Windows hidden attributes")
def test_windows_hidden_metadata_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Path, "lstat",
        lambda self: SimpleNamespace(
            st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_HIDDEN
        ),
    )
    with pytest.raises(ValueError, match="Hidden"):
        source_module._source_stat(tmp_path / "private.py")


def test_listing_and_search_have_fixed_budgets(tmp_path):
    for number in range(205):
        _write(tmp_path, f"{number:03}.py", "needle\n" * 100)
    reader = RecoverySource(tmp_path)
    assert reader.list_files() == [f"{number:03}.py" for number in range(200)]
    matches = reader.search_source("needle")
    assert len(matches) == 50
    assert matches[0] == "000.py:1:needle"
    _write(tmp_path, "000.py", ("needle" + "é" * 2000 + "\n") * 8)
    matches = reader.search_source("needle")
    assert sum(len(match.encode("utf-8")) for match in matches) <= 8 * 1024
    assert all(len(match.encode("utf-8")) <= 1024 for match in matches)


@pytest.mark.parametrize("query", ["", " \t\n", "x" * 257])
def test_invalid_search_queries(tmp_path, query):
    with pytest.raises(ValueError, match="Search requires"):
        RecoverySource(tmp_path).search_source(query)


def test_entry_and_depth_budgets(tmp_path, monkeypatch):
    monkeypatch.setattr(source_module, "_MAX_ENTRIES", 3)
    for index in range(5):
        _write(tmp_path, f"{index}.txt")
    _write(tmp_path, "last.py")
    assert RecoverySource(tmp_path).list_files() == []
    monkeypatch.setattr(source_module, "_MAX_ENTRIES", 100)
    monkeypatch.setattr(source_module, "_MAX_DEPTH", 1)
    _write(tmp_path, "pkg/visible.py")
    _write(tmp_path, "pkg/deeper/hidden.py")
    assert RecoverySource(tmp_path).list_files() == ["last.py", "pkg/visible.py"]
    with pytest.raises(ValueError):
        RecoverySource(tmp_path).read_file("pkg/deeper/hidden.py")


def test_missing_invalid_encoding_and_safe_errors(tmp_path):
    reader = RecoverySource(tmp_path)
    with pytest.raises(FileNotFoundError) as error:
        reader.read_file("missing.py")
    assert str(tmp_path) not in str(error.value)
    (tmp_path / "binary.py").write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="UTF-8"):
        reader.read_file("binary.py")
    assert reader.search_source("anything") == []
    with pytest.raises(OSError) as error:
        RecoverySource(tmp_path / "missing").list_files()
    assert str(tmp_path) not in str(error.value)


def test_listing_does_not_open_source_and_discovery_is_not_cached(tmp_path, monkeypatch):
    reader = RecoverySource(tmp_path)
    _write(tmp_path, "first.py")
    with monkeypatch.context() as patch:
        def forbidden_open(*args, **kwargs):
            pytest.fail("Listing must not read file contents.")

        patch.setattr(Path, "open", forbidden_open)
        assert reader.list_files() == ["first.py"]
    _write(tmp_path, "second.py")
    assert reader.list_files() == ["first.py", "second.py"]


def test_source_errors_do_not_disclose_paths(tmp_path, monkeypatch):
    _write(tmp_path, "app.py")

    def denied(*args, **kwargs):
        raise PermissionError(f"private location: {tmp_path}")

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(OSError, match="Source file is unavailable") as error:
        RecoverySource(tmp_path).read_file("app.py")
    assert str(tmp_path) not in str(error.value)
    assert RecoverySource(tmp_path).search_source("anything") == []


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFDIR])
def test_link_metadata_is_rejected_without_symlink_privileges(tmp_path, monkeypatch, mode):
    attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT if mode == stat.S_IFDIR else 0
    if attributes and os.name != "nt":
        pytest.skip("Reparse attributes are Windows-specific.")
    monkeypatch.setattr(
        Path, "lstat", lambda self: SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
    )
    with pytest.raises(ValueError, match="Linked"):
        source_module._plain_stat(tmp_path / "linked.py")


def test_symlink_files_directories_and_root_are_rejected(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    outside = _write(tmp_path, "outside/secret.py", "private = True\n")
    try:
        (root / "linked.py").symlink_to(outside)
        (root / "linked").symlink_to(outside.parent, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or privileges.")
    reader = RecoverySource(root)
    assert reader.list_files() == []
    for name in ("linked.py", "linked/secret.py"):
        with pytest.raises(ValueError, match="Linked"):
            reader.read_file(name)
    with pytest.raises(ValueError, match="Linked"):
        RecoverySource(root / "linked").list_files()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_windows_junction_is_pruned_and_rejected(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    outside = _write(tmp_path, "outside/private.py").parent
    junction = root / "linked"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, check=False,
    )
    assert result.returncode == 0
    try:
        reader = RecoverySource(root)
        assert reader.list_files() == []
        with pytest.raises(ValueError, match="Linked"):
            reader.read_file("linked/private.py")
        with pytest.raises(ValueError, match="Linked"):
            RecoverySource(junction).list_files()
    finally:
        junction.rmdir()