"""Single-writer local native storage; no MAF transcripts or interrupted-turn recovery."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from ._harness import PROTOCOL_VERSION, RUNTIME_VERSION, SDK_VERSION, CopilotPreviewError


class NativeState:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._owner: BinaryIO | None = None

    def claim(self) -> None:
        if self._owner is not None:
            return
        owner: BinaryIO | None = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            owner = (self.root / "owner.lock").open("a+b")
            if owner.seek(0, os.SEEK_END) == 0:
                owner.write(b"0")
                owner.flush()
            owner.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if owner is not None:
                owner.close()
            raise CopilotPreviewError(
                "Copilot preview storage is unavailable or owned by another worker. "
                "Use one worker and a writable local session directory."
            ) from None
        self._owner = owner

    def close(self) -> None:
        if self._owner is not None:
            self._owner.close()
            self._owner = None

    @property
    def native_root(self) -> Path:
        return self.root / "native"

    @staticmethod
    def native_id(agent_slug: str, session_id: str) -> str:
        # Hash both identities; dots, Windows device names and casing cannot alias paths.
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"af-copilot:{agent_slug}:{session_id}"))

    def _marker_path(self, native_id: str) -> Path:
        return self.root / "turns" / f"{native_id}.json"

    def _files(self, native_id: str) -> dict[str, str]:
        directory = self.native_root / "session-state" / native_id
        try:
            if directory.is_symlink() or not (directory / "events.jsonl").is_file():
                raise CopilotPreviewError("Copilot native session state is missing.")
            result: dict[str, str] = {}
            for path in sorted(directory.rglob("*")):
                if path.is_symlink():
                    raise CopilotPreviewError("Copilot native session state contains a symbolic link.")
                if path.is_file():
                    result[path.relative_to(directory).as_posix()] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
            if not result:
                raise CopilotPreviewError("Copilot native session state is empty.")
            return result
        except OSError:
            raise CopilotPreviewError("Copilot native session state is unavailable.") from None

    def validate(self, native_id: str, *, new_session: bool) -> None:
        self.claim()
        marker = self._marker_path(native_id)
        native_directory = self.native_root / "session-state" / native_id
        try:
            if new_session:
                if marker.exists() or native_directory.exists():
                    raise CopilotPreviewError("Copilot preview session already exists; refusing to reset it.")
            else:
                if not marker.is_file():
                    raise CopilotPreviewError(
                        "Copilot preview session was not found. Omit the session id to start "
                        "a new conversation; existing MAF history is not imported."
                    )
                record = json.loads(marker.read_text(encoding="utf-8"))
                if not isinstance(record, dict) or record.get("identity") != self._identity(native_id):
                    raise CopilotPreviewError("Copilot preview session metadata is corrupt or incompatible.")
                if record.get("status") != "ready":
                    raise CopilotPreviewError(
                        "Copilot preview session has an unfinished turn. Interrupted-turn recovery "
                        "is unsupported; use a new session id, not this conversation."
                    )
                if record.get("files") != self._files(native_id):
                    raise CopilotPreviewError(
                        "Copilot native session state is missing or corrupt; refusing to reset it."
                    )
        except (OSError, ValueError):
            raise CopilotPreviewError("Copilot preview session metadata is unavailable or corrupt.") from None

    def begin(self, native_id: str, *, new_session: bool) -> None:
        self.validate(native_id, new_session=new_session)
        self._write(native_id, {"identity": self._identity(native_id), "status": "pending"})

    @staticmethod
    def _identity(native_id: str) -> dict[str, str | int]:
        return {
            "format": 1,
            "native_id": native_id,
            "sdk": SDK_VERSION,
            "runtime": RUNTIME_VERSION,
            "protocol": PROTOCOL_VERSION,
        }

    def complete(self, native_id: str) -> None:
        self._write(
            native_id,
            {
                "identity": self._identity(native_id),
                "status": "ready",
                "files": self._files(native_id),
            },
        )

    def _write(self, native_id: str, record: dict[str, Any]) -> None:
        path = self._marker_path(native_id)
        temporary = path.with_suffix(".pending")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(record, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            raise CopilotPreviewError(
                "Copilot preview could not commit local session state. "
                "No completed-turn continuity is available for this turn."
            ) from None
