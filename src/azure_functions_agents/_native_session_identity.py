"""SDK-independent routing and metadata-only history compatibility checks."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from azure.core.exceptions import ResourceNotFoundError

from ._history_identity import validate_agent_slug
from ._session_id import SESSION_ID_PATTERN
from .config.paths import resolve_config_dir

if TYPE_CHECKING:
    from azure.storage.blob.aio import BlobServiceClient

STORAGE_ENV = "AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE"
CONNECTION_ENV = "AzureWebJobsStorage"
BLOB_URI_ENV = "AzureWebJobsStorage__blobServiceUri"
CLIENT_ID_ENV = "AzureWebJobsStorage__clientId"
NAMESPACE = "copilot-native/v1"


class _AsyncCloseable(Protocol):
    async def close(self) -> None: ...


@dataclass
class _OwnedBlobService:
    service: BlobServiceClient
    credential: _AsyncCloseable | None = None

    async def close(self) -> None:
        errors: list[BaseException] = []
        for resource in (self.service, self.credential):
            if resource is None:
                continue
            try:
                await resource.close()
            except BaseException as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Blob resources could not be closed.", errors)


async def _open_owned_blob_service(route: StorageRoute) -> _OwnedBlobService:
    from azure.storage.blob.aio import BlobServiceClient

    credential: _AsyncCloseable | None = None
    try:
        if route.connection_string:
            service = BlobServiceClient.from_connection_string(route.connection_string)
        else:
            from ._credential import build_async_credential_with_client_id

            assert route.blob_uri is not None
            credential = build_async_credential_with_client_id(route.client_id or "")
            service = BlobServiceClient(account_url=route.blob_uri, credential=credential)
        return _OwnedBlobService(service, credential)
    except BaseException as error:
        if credential is not None:
            try:
                await credential.close()
            except BaseException as close_error:
                raise BaseExceptionGroup(
                    "Blob client creation and credential cleanup failed.",
                    [error, close_error],
                ) from None
        raise


class NativeSessionError(RuntimeError):
    """A content-free native persistence diagnostic."""

    status_code = 500


class SessionConflictError(NativeSessionError):
    status_code = 409


class IncompatibleSessionError(SessionConflictError):
    """A format, identity, or lifecycle state cannot be continued."""


class CorruptSessionError(NativeSessionError):
    """A persisted native envelope failed validation."""


class PersistenceUnavailableError(NativeSessionError):
    status_code = 503


class LeaseLostError(PersistenceUnavailableError):
    """Ownership is lost; this owner cannot publish completion."""


class StorageMode(StrEnum):
    LOCAL = "local"
    BLOB = "blob"


@dataclass(frozen=True)
class StorageRoute:
    mode: StorageMode
    app_key: str
    local_dir: Path
    container: str
    identity_key: str
    connection_string: str | None = field(default=None, repr=False)
    blob_uri: str | None = field(default=None, repr=False)
    client_id: str | None = field(default=None, repr=False)


def opaque_key(kind: Literal["a", "g", "s"], identity: str) -> str:
    digest = hashlib.sha256(f"{NAMESPACE}:{kind}\0{identity}".encode()).digest()
    return f"{kind}_{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"


def validate_identity(agent_slug: str, session_id: str) -> None:
    validate_agent_slug(agent_slug)
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("Invalid native session identity.")
    if session_id in {".", ".."}:
        raise ValueError("Invalid native session identity.")


def resolve_route(app_root: Path, *, for_guard: bool = False) -> StorageRoute:
    deployed = bool(os.environ.get("WEBSITE_INSTANCE_ID"))
    raw = None if for_guard else os.environ.get(STORAGE_ENV)
    if raw is None:
        mode = StorageMode.BLOB if deployed else StorageMode.LOCAL
    else:
        try:
            mode = StorageMode(raw.strip().lower())
        except ValueError:
            raise ValueError(f"{STORAGE_ENV} must be local or blob (or unset).") from None
    if deployed and mode is StorageMode.LOCAL:
        raise ValueError("Copilot native local storage is not supported on deployed instances.")
    if deployed:
        site = os.environ.get("WEBSITE_SITE_NAME", "").strip()
        if not site:
            raise ValueError("Copilot native storage requires WEBSITE_SITE_NAME on deployed instances.")
        app_identity = f"{site}\0{os.environ.get('WEBSITE_SLOT_NAME') or 'production'}"
    else:
        app_identity = str(app_root.resolve())
    conn = os.environ.get(CONNECTION_ENV, "").strip() or None
    uri = os.environ.get(BLOB_URI_ENV, "").strip() or None
    if mode is StorageMode.BLOB and not (conn or uri) and not for_guard:
        raise ValueError("Copilot Blob storage requires AzureWebJobsStorage or its blobServiceUri.")
    client_id = (
        os.environ.get(CLIENT_ID_ENV)
        or os.environ.get("AZURE_CLIENT_ID")
        or ""
    ).strip() or None
    container = (
        os.environ.get("AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER", "").strip()
        or "azure-functions-agents"
    )
    app_key = opaque_key("a", app_identity)
    settings = (mode.value, app_key, container, conn or "", uri if not conn else "",
                client_id if not conn else "")
    identity_key = hashlib.sha256(repr(settings).encode()).hexdigest()
    return StorageRoute(
        mode, app_key, Path(resolve_config_dir()).resolve() / "agent-sessions", container,
        identity_key, conn, uri, client_id,
    )


def state_name(route: StorageRoute, agent_slug: str, session_id: str) -> str:
    validate_identity(agent_slug, session_id)
    return (
        f"{NAMESPACE}/{route.app_key}/{opaque_key('g', agent_slug)}/"
        f"{opaque_key('s', session_id)}/state.json"
    )


async def _blob_metadata_exists(route: StorageRoute, name: str) -> bool:
    try:
        owned = await _open_owned_blob_service(route)
        try:
            await owned.service.get_blob_client(
                container=route.container, blob=name
            ).get_blob_properties()
            return True
        finally:
            await owned.close()
    except ResourceNotFoundError:
        return False
    except Exception:
        raise PersistenceUnavailableError("Session metadata could not be checked.") from None


async def metadata_exists(route: StorageRoute, name: str) -> bool:
    try:
        if route.mode is StorageMode.LOCAL:
            return (route.local_dir / name).stat().st_size >= 0
        return await _blob_metadata_exists(route, name)
    except FileNotFoundError:
        return False
    except OSError:
        raise PersistenceUnavailableError("Session metadata could not be checked.") from None


async def guard_opposite_history(route: StorageRoute, slug: str, session_id: str,
                                 *, native: bool) -> None:
    """Probe only metadata; never read or change opposite-harness content."""
    validate_identity(slug, session_id)
    maf_name = f"agent-sessions/{slug}/{session_id}.jsonl"
    maf_local = route.local_dir / slug / f"{session_id}.jsonl"
    try:
        has_maf = maf_local.is_file()
    except OSError:
        raise PersistenceUnavailableError("MAF session metadata could not be checked.") from None
    if route.connection_string or route.blob_uri:
        has_maf |= await metadata_exists(replace(route, mode=StorageMode.BLOB), maf_name)
    native_routes = [replace(route, mode=StorageMode.LOCAL)]
    if route.connection_string or route.blob_uri:
        native_routes.append(replace(route, mode=StorageMode.BLOB))
    has_native = False
    for location in native_routes:
        has_native |= await metadata_exists(location, state_name(location, slug, session_id))
    if native and has_maf and not has_native:
        raise IncompatibleSessionError("This session has MAF history, not native state; use a new ID.")
    if not native and has_native and not has_maf:
        raise IncompatibleSessionError("This session has native state, not MAF history; use a new ID.")
