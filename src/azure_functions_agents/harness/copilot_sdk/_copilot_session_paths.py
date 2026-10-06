"""Callback and physical-host path policies for Copilot session files."""

from __future__ import annotations

import errno
import ntpath
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path

from copilot.session import SessionFsConventions

from ._copilot_session_identity import _path_error

HOST_PATH_CONVENTIONS: SessionFsConventions = "windows" if os.name == "nt" else "posix"
WORKSPACE_ROOT = "/workspace"
SESSION_STATE_ROOT = "/session-state"


class SessionPathPolicy(ABC):
    """One convention's lexical paths and physical-host variations."""

    @abstractmethod
    def separators(self, path: str) -> str: ...

    @abstractmethod
    def comparison_key(self, path: str) -> str: ...

    @abstractmethod
    def validate_segments(self, parts: Sequence[str]) -> None: ...

    @abstractmethod
    def validate_local_parts(self, parts: Sequence[str]) -> None: ...

    @abstractmethod
    def write_flags(self, append: bool) -> int: ...

    @abstractmethod
    def replace_empty_directory(self, target: Path) -> None: ...

    def normalize(self, path: str, workspace_path: str = "") -> str:
        """Contain callbacks to the virtual roots or exact current workspace."""
        if not path or "\x00" in path:
            raise _path_error(errno.EINVAL)
        subject = self.separators(path)
        prefix = self.separators(workspace_path).rstrip("/") if workspace_path else ""
        compared, against = self.comparison_key(subject), self.comparison_key(prefix)
        if prefix and compared == against:
            subject = WORKSPACE_ROOT
        elif prefix and compared.startswith(against + "/"):
            subject = f"{WORKSPACE_ROOT}/{subject[len(prefix) + 1:]}"
        elif path.startswith(("\\\\", "//")) or re.match(r"[A-Za-z]:", path):
            raise _path_error(errno.EACCES)
        if not subject.startswith("/"):
            subject = f"{WORKSPACE_ROOT}/{subject}"
        parts = [part for part in subject.split("/") if part not in {"", "."}]
        if any(
            part == ".." or any(ord(character) < 32 for character in part)
            for part in parts
        ):
            raise _path_error(errno.EACCES)
        self.validate_segments(parts)
        normalized = "/" + "/".join(parts)
        if normalized != "/" and not any(
            normalized == root or normalized.startswith(root + "/")
            for root in (WORKSPACE_ROOT, SESSION_STATE_ROOT)
        ):
            raise _path_error(errno.EACCES)
        return normalized


class WindowsSessionPathPolicy(SessionPathPolicy):
    def separators(self, path: str) -> str:
        return path.replace("\\", "/")

    def comparison_key(self, path: str) -> str:
        return path.casefold()

    def validate_segments(self, parts: Sequence[str]) -> None:
        if any(ntpath.isreserved(part) for part in parts):
            raise _path_error(errno.EACCES)

    def validate_local_parts(self, parts: Sequence[str]) -> None:
        if any(ntpath.isreserved(part) for part in parts[1:]):
            raise _path_error(errno.EINVAL)

    def write_flags(self, append: bool) -> int:
        return os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC) | os.O_BINARY

    def replace_empty_directory(self, target: Path) -> None:
        target.rmdir()


class PosixSessionPathPolicy(SessionPathPolicy):
    def separators(self, path: str) -> str:
        return path

    def comparison_key(self, path: str) -> str:
        return path

    def validate_segments(self, parts: Sequence[str]) -> None:
        if any("\\" in part for part in parts):
            raise _path_error(errno.EINVAL)

    def validate_local_parts(self, parts: Sequence[str]) -> None:
        pass

    def write_flags(self, append: bool) -> int:
        return os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)

    def replace_empty_directory(self, target: Path) -> None:
        pass


_PATH_POLICIES: dict[SessionFsConventions, SessionPathPolicy] = {
    "windows": WindowsSessionPathPolicy(),
    "posix": PosixSessionPathPolicy(),
}


def select_session_path_policy(conventions: SessionFsConventions) -> SessionPathPolicy:
    return _PATH_POLICIES[conventions]


def normalize_callback_path(
    path: str, conventions: SessionFsConventions, workspace_path: str = ""
) -> str:
    return select_session_path_policy(conventions).normalize(path, workspace_path)
