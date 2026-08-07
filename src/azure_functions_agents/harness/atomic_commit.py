"""Crash-safe whole-turn checkpoint commits for a sandbox-local harness."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..journal_paths import checkpoint_name, validate_checkpoint_name


class AtomicCommitError(RuntimeError):
    """A checkpoint could not be safely committed or recovered."""


class AtomicCommitCorruptError(AtomicCommitError):
    """The durable current pointer was malformed or referred outside checkpoints."""


type FaultHook = Callable[[str], None]
type FsyncHook = Callable[[int], None]


@dataclass(frozen=True, slots=True)
class CommittedCheckpoint:
    """The immutable checkpoint selected by the durable current pointer."""

    name: str
    path: Path


class AtomicCommitStore:
    """Commit a conversation state and working-file set as one recoverable turn."""

    def __init__(
        self,
        root: Path,
        *,
        fault_hook: FaultHook | None = None,
        fsync_hook: FsyncHook = os.fsync,
    ) -> None:
        self._root = root
        self._fault_hook = fault_hook
        self._fsync = fsync_hook

    @property
    def root(self) -> Path:
        """Return the checkpoint root without creating files."""
        return self._root

    def commit(self, *, conversation: bytes, working_files: Mapping[str, bytes]) -> CommittedCheckpoint:
        """Durably publish a complete turn only after its current pointer is fsynced."""
        if not isinstance(conversation, bytes):
            raise TypeError("conversation must be bytes")
        self._ensure_layout()
        token = uuid.uuid4().hex
        staging = self._staging_root / token
        name = checkpoint_name(token)
        checkpoint = self._checkpoints_root / name
        staging.mkdir()
        self._write_durable(staging / "conversation.json", conversation)
        for relative_path, content in working_files.items():
            self._write_working_file(staging, relative_path, content)
        self._sync_directory(staging)
        self._fault("after_staging_fsync")

        os.replace(staging, checkpoint)
        self._fault("after_checkpoint_rename")
        self._sync_directory(self._checkpoints_root)
        self._fault("after_checkpoints_fsync")

        pointer_temp = self._root / f".current-{token}.tmp"
        self._write_durable(pointer_temp, f"{name}\n".encode("ascii"))
        self._fault("after_pointer_write")
        os.replace(pointer_temp, self._current_pointer)
        self._fault("after_pointer_replace")
        self._sync_directory(self._root)
        self._fault("after_pointer_fsync")
        return CommittedCheckpoint(name=name, path=checkpoint)

    def recover(self) -> CommittedCheckpoint | None:
        """Discard incomplete/unpointed trees and return only the pointer-selected turn."""
        self._ensure_layout()
        selected = self._read_current_pointer()
        self._discard_children(self._staging_root)
        for checkpoint in tuple(self._checkpoints_root.iterdir()):
            if checkpoint.is_symlink():
                checkpoint.unlink()
                continue
            if not checkpoint.is_dir():
                raise AtomicCommitCorruptError("checkpoint root contains a non-directory entry")
            if selected is None or checkpoint.name != selected.name:
                shutil.rmtree(checkpoint)
        return selected

    @property
    def _staging_root(self) -> Path:
        return self._root / "staging"

    @property
    def _checkpoints_root(self) -> Path:
        return self._root / "checkpoints"

    @property
    def _current_pointer(self) -> Path:
        return self._root / "current"

    def _ensure_layout(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._staging_root.mkdir(exist_ok=True)
        self._checkpoints_root.mkdir(exist_ok=True)

    def _write_working_file(self, staging: Path, relative_path: str, content: bytes) -> None:
        if not isinstance(content, bytes):
            raise TypeError("working file content must be bytes")
        target = _safe_child(staging, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_durable(target, content)

    def _write_durable(self, path: Path, content: bytes) -> None:
        with path.open("wb") as output:
            output.write(content)
            output.flush()
            self._fsync(output.fileno())
        self._fault("after_file_write")

    def _read_current_pointer(self) -> CommittedCheckpoint | None:
        try:
            raw = self._current_pointer.read_bytes()
        except FileNotFoundError:
            return None
        try:
            name = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise AtomicCommitCorruptError("current pointer was not ASCII.") from exc
        try:
            validate_checkpoint_name(name)
        except ValueError as exc:
            raise AtomicCommitCorruptError("current pointer target was unsafe.") from exc
        target = self._checkpoints_root / name
        if target.parent != self._checkpoints_root or target.is_symlink() or not target.is_dir():
            raise AtomicCommitCorruptError("current pointer did not identify a checkpoint.")
        return CommittedCheckpoint(name=name, path=target)

    def _discard_children(self, root: Path) -> None:
        for child in tuple(root.iterdir()):
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)

    def _sync_directory(self, path: Path) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            self._fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fault(self, stage: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(stage)


def _safe_child(root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise AtomicCommitError("working file path must be non-empty")
    if "\\" in relative_path:
        raise AtomicCommitError("working file path must use relative POSIX separators")
    candidate = PurePosixPath(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        raise AtomicCommitError("working file path must stay within the checkpoint")
    return root.joinpath(*candidate.parts)
