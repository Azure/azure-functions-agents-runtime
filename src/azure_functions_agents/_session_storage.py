"""Harness-neutral Blob settings and owned client construction."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config.env import EnvVar, raw_env_value, runtime_env_value

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential
    from azure.storage.blob.aio import BlobServiceClient

DEFAULT_CONTAINER_NAME = "azure-functions-agents"


class SessionStorageError(OSError):
    """A sanitized persistence backend failure."""

    status_code = 503


@dataclass(frozen=True)
class BlobStorageSettings:
    container_name: str = DEFAULT_CONTAINER_NAME
    connection_string: str | None = field(default=None, repr=False)
    blob_service_url: str | None = field(default=None, repr=False)
    client_id: str = field(default="", repr=False)


def storage_client_id() -> str:
    """Preserve storage-specific identity precedence, including blank values."""
    return (
        raw_env_value(EnvVar.AZURE_WEB_JOBS_STORAGE_CLIENT_ID)
        or raw_env_value(EnvVar.AZURE_CLIENT_ID)
        or ""
    ).strip()


def blob_storage_from_environment(
    *, container_name: str | None = None
) -> BlobStorageSettings | None:
    """Resolve existing Blob settings without constructing a persistence provider."""
    connection = runtime_env_value(EnvVar.AZURE_WEB_JOBS_STORAGE)
    service_url = runtime_env_value(EnvVar.AZURE_WEB_JOBS_STORAGE_BLOB_SERVICE_URI)
    if not connection and not service_url:
        return None
    container = (
        container_name
        or runtime_env_value(EnvVar.SESSION_CONTAINER)
        or DEFAULT_CONTAINER_NAME
    )
    return BlobStorageSettings(
        container_name=container,
        connection_string=connection or None,
        blob_service_url=None if connection else service_url,
        client_id=storage_client_id(),
    )


@dataclass
class OwnedBlobService:
    service: BlobServiceClient
    credential: AsyncTokenCredential | None = field(default=None, repr=False)

    async def close(self) -> None:
        """Close both owned resources, including after a service-close failure."""
        try:
            await self.service.close()
        finally:
            if self.credential is not None:
                await self.credential.close()


async def open_blob_service(settings: BlobStorageSettings) -> OwnedBlobService:
    """Construct only the caller's Blob service and optional credential."""
    from azure.storage.blob.aio import BlobServiceClient

    if settings.connection_string:
        return OwnedBlobService(
            BlobServiceClient.from_connection_string(settings.connection_string)
        )
    if not settings.blob_service_url:
        raise ValueError("Blob storage requires a connection string or service URI.")
    from ._credential import build_async_credential_with_client_id

    async with AsyncExitStack() as cleanup:
        credential = build_async_credential_with_client_id(settings.client_id)
        cleanup.push_async_callback(credential.close)
        service = BlobServiceClient(
            account_url=settings.blob_service_url, credential=credential
        )
        cleanup.pop_all()
        return OwnedBlobService(service, credential)
