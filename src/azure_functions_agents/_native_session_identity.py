"""SDK-independent native session routing and readable storage paths."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ._agent_identity import agent_id, resolve_app_correlation_key
from ._history_identity import validate_agent_slug
from ._session_id import SESSION_ID_PATTERN
from ._session_storage import (
    BlobStorageSettings,
    SessionStorageError,
    blob_storage_from_environment,
)
from .config.paths import resolve_config_dir

NAMESPACE = "copilot-native"


class NativeSessionError(SessionStorageError):
    """A sanitized native filesystem or backend configuration failure."""


class StorageMode(StrEnum):
    LOCAL = "local"
    BLOB = "blob"


@dataclass(frozen=True)
class StorageRoute:
    local_dir: Path
    correlation_key: str
    blob: BlobStorageSettings | None = None

    @property
    def mode(self) -> StorageMode:
        return StorageMode.BLOB if self.blob is not None else StorageMode.LOCAL


def resolve_route(app_root: Path) -> StorageRoute:
    """Freeze existing storage configuration and the shared app correlation key."""
    del app_root
    return StorageRoute(
        local_dir=Path(resolve_config_dir()).absolute(),
        correlation_key=resolve_app_correlation_key(),
        blob=blob_storage_from_environment(),
    )


def validate_identity(agent_slug: str, session_id: str) -> None:
    validate_agent_slug(agent_slug)
    if (
        not isinstance(session_id, str)
        or not SESSION_ID_PATTERN.fullmatch(session_id)
        or session_id in {".", ".."}
    ):
        raise ValueError("Invalid native session identity.")


def session_prefix(route: StorageRoute, agent_slug: str, session_id: str) -> str:
    """Return the native root using the shared agent ID exactly once."""
    validate_identity(agent_slug, session_id)
    identity = agent_id(agent_slug, correlation_key=route.correlation_key)
    if any(
        part in {"", ".", ".."}
        or "\\" in part
        or ":" in part
        or any(ord(character) < 32 for character in part)
        for part in identity.split("/")
    ):
        raise ValueError("Invalid native agent identity path.")
    return f"{NAMESPACE}/{identity}/{session_id}"
