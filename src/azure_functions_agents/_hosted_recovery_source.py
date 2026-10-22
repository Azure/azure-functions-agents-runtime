"""Bounded, read-only access to opted-in, reviewed project Python source."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PureWindowsPath

_MAX_FILES = 200
_MAX_FILE_BYTES = 32 * 1024
_MAX_ENTRIES = 10_000
_MAX_DEPTH = 32
_MAX_PATH_BYTES = 1024
_MAX_SEARCH_RESULTS = 50
_MAX_SEARCH_BYTES = 8 * 1024
_MAX_QUERY_LENGTH = 256
_IGNORED_NAMES = frozenset(
    {
        "__pycache__", "__blobstorage__", "__queuestorage__", "artifacts", "bin",
        "build", "dist", "env", "envs", "venv", "venvs", "node_modules", "obj",
        "site", "site-packages", "htmlcov", "coverage", "credentials", "secrets",
    }
)
_SENSITIVE_STEMS = frozenset({"env", "credential", "credentials", "secret", "secrets"})


def _allowed_name(name: str) -> bool:
    folded = name.casefold()
    return (
        not folded.startswith(".")
        and folded not in _IGNORED_NAMES
        and Path(folded).stem not in _SENSITIVE_STEMS
        and not folded.startswith("local.settings")
        and not name.endswith((" ", "."))
        and ":" not in name
    )


def _plain_stat(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or (
        info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        if os.name == "nt" else False
    ):
        raise ValueError("Linked source paths are not allowed.")
    return info


def _source_stat(path: Path) -> os.stat_result:
    info = _plain_stat(path)
    if os.name == "nt" and info.st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN:
        raise ValueError("Hidden source paths are not allowed.")
    if stat.S_ISDIR(info.st_mode) and any(
        (path / marker).exists() for marker in ("pyvenv.cfg", "conda-meta")
    ):
        raise ValueError("Environment source paths are not allowed.")
    return info


class RecoverySource:
    """Inspect app-root Python source; content is not automatically secret-redacted."""

    def __init__(self, root: Path) -> None:
        self._root = root.absolute()

    def _check_root(self) -> None:
        for directory in (*reversed(self._root.parents), self._root):
            if not stat.S_ISDIR(_plain_stat(directory).st_mode):
                raise ValueError("Source root is not a directory.")

    def _source_path(self, path: str) -> tuple[Path, os.stat_result]:
        windows = PureWindowsPath(path)
        parts = path.replace("\\", "/").split("/")
        if (
            not path
            or len(path.encode("utf-8")) > _MAX_PATH_BYTES
            or "\x00" in path
            or windows.drive
            or windows.root
            or len(parts) > _MAX_DEPTH + 1
            or any(part in {"", ".", ".."} or not _allowed_name(part) for part in parts)
            or Path(parts[-1]).suffix.casefold() != ".py"
        ):
            raise ValueError("Only project-relative Python source paths are allowed.")
        self._check_root()
        candidate = self._root
        for part in parts[:-1]:
            candidate /= part
            if not stat.S_ISDIR(_source_stat(candidate).st_mode):
                raise ValueError("Source parent is not a directory.")
        candidate /= parts[-1]
        info = _source_stat(candidate)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Only regular Python source files are allowed.")
        if info.st_size > _MAX_FILE_BYTES:
            raise ValueError("Source file exceeds the size limit.")
        return candidate, info

    def list_files(self) -> list[str]:
        """Return sorted relative source names within fixed discovery budgets."""
        try:
            self._check_root()
            return self._discover()
        except OSError:
            raise OSError("Project source is unavailable.") from None

    def _discover(self) -> list[str]:
        found: list[str] = []
        visited = 0
        for directory, directories, files in os.walk(self._root, followlinks=False):
            parent = Path(directory)
            depth = len(parent.relative_to(self._root).parts)
            names = sorted(directories + files)
            directories.clear()
            for name in names:
                visited += 1
                if visited > _MAX_ENTRIES:
                    return sorted(found)
                if not _allowed_name(name):
                    continue
                candidate = parent / name
                try:
                    info = _source_stat(candidate)
                    if stat.S_ISDIR(info.st_mode):
                        if depth < _MAX_DEPTH:
                            directories.append(name)
                        continue
                    relative = candidate.relative_to(self._root).as_posix()
                    self._source_path(relative)
                except (OSError, ValueError):
                    continue
                found.append(relative)
                if len(found) == _MAX_FILES:
                    return sorted(found)
        return sorted(found)

    def read_file(self, path: str) -> str:
        """Read one regular UTF-8 Python file without creating or modifying files."""
        try:
            candidate, before = self._source_path(path)
            with candidate.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("Source changed during access.")
                self._source_path(path)
                data = stream.read(_MAX_FILE_BYTES + 1)
            if len(data) > _MAX_FILE_BYTES:
                raise ValueError("Source file exceeds the size limit.")
            return data.decode("utf-8-sig")
        except FileNotFoundError:
            raise FileNotFoundError("Source file was not found.") from None
        except OSError:
            raise OSError("Source file is unavailable.") from None
        except UnicodeError:
            raise ValueError("Source file must be valid UTF-8 text.") from None

    def search_source(self, query: str) -> list[str]:
        """Return bounded path:line:text matches for a case-insensitive literal."""
        if not query.strip() or len(query) > _MAX_QUERY_LENGTH:
            raise ValueError("Search requires a nonblank query of at most 256 characters.")
        matches: list[str] = []
        remaining = _MAX_SEARCH_BYTES
        needle = query.casefold()
        for path in self.list_files():
            try:
                source = self.read_file(path)
            except (OSError, ValueError):
                continue
            for number, line in enumerate(source.splitlines(), start=1):
                if needle not in line.casefold():
                    continue
                result = f"{path}:{number}:{line}".encode()[:min(remaining, 1024)]
                matches.append(result.decode("utf-8", errors="ignore"))
                remaining -= len(result)
                if len(matches) == _MAX_SEARCH_RESULTS or remaining == 0:
                    return matches
        return matches