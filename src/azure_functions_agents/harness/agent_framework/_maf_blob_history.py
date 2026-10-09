"""Append-blob JSONL history for Microsoft Agent Framework sessions."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, ClassVar

from agent_framework import HistoryProvider, Message
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError

from ..._logger import logger
from .._harness_lifecycle import _register_shutdown, _unregister_shutdown
from .._history_identity import validate_agent_slug
from .._session_storage import (
    DEFAULT_CONTAINER_NAME,
    SessionStorageError,
    blob_storage_from_environment,
    storage_client_id,
)

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential

DEFAULT_BLOB_PREFIX = "agent-sessions/"
DEFAULT_SOURCE_ID = "blob_history"

# Cache keys never contain the raw connection string.
_SERVICE_CLIENTS: dict[str, Any] = {}
_SERVICE_CLIENTS_LOCK = asyncio.Lock()
_OWNED_CREDENTIALS: dict[int, AsyncTokenCredential] = {}
_ENSURED_CONTAINERS: set[tuple[str, str]] = set()
_ENSURED_CONTAINERS_LOCK = asyncio.Lock()


class BlobHistoryProvider(HistoryProvider):
    """Store each agent/session's per-turn message deltas in one Append Blob."""

    DEFAULT_SOURCE_ID: ClassVar[str] = DEFAULT_SOURCE_ID

    def __init__(
        self,
        *,
        agent_slug: str,
        connection_string: str | None = None,
        blob_service_url: str | None = None,
        credential: Any | None = None,
        container_name: str = DEFAULT_CONTAINER_NAME,
        blob_prefix: str = DEFAULT_BLOB_PREFIX,
        source_id: str = DEFAULT_SOURCE_ID,
        load_messages: bool = True,
        store_inputs: bool = True,
        store_context_messages: bool = False,
        store_context_from: set[str] | None = None,
        store_outputs: bool = True,
        skip_excluded: bool = False,
    ) -> None:
        super().__init__(
            source_id=source_id,
            load_messages=load_messages,
            store_inputs=store_inputs,
            store_context_messages=store_context_messages,
            store_context_from=store_context_from,
            store_outputs=store_outputs,
        )
        if not connection_string and not blob_service_url:
            raise ValueError(
                "BlobHistoryProvider requires either 'connection_string' or 'blob_service_url'."
            )
        self._agent_slug = validate_agent_slug(agent_slug)
        self.skip_excluded = skip_excluded
        self._connection_string = connection_string
        self._blob_service_url = blob_service_url
        self._credential = credential
        self._container_name = container_name
        self._blob_prefix = _normalize_prefix(blob_prefix)
        self._cache_key = _service_client_cache_key(
            connection_string=connection_string,
            blob_service_url=blob_service_url,
        )

    async def get_messages(
        self,
        session_id: str | None,
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Message]:
        del state, kwargs
        blob_client = await self._get_blob_client(session_id)
        try:
            downloader = await blob_client.download_blob(encoding="utf-8")
            content = await downloader.readall()
        except ResourceNotFoundError:
            return []

        text = content if isinstance(content, str) else content.decode("utf-8")
        messages: list[Message] = []
        for line_number, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError as exc:
                raise ValueError(
                    f"Failed to deserialize history line {line_number} from blob "
                    f"'{self._container_name}/{self._blob_name(session_id)}'."
                ) from exc
            if not isinstance(payload, Mapping):
                raise ValueError(
                    f"History line {line_number} in blob "
                    f"'{self._container_name}/{self._blob_name(session_id)}' "
                    "did not deserialize to a mapping."
                )
            messages.append(Message.from_dict(dict(payload)))

        if self.skip_excluded:
            messages = [
                m for m in messages if not m.additional_properties.get("_excluded", False)
            ]
        return messages

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del state, kwargs
        if not messages:
            return

        payload = "".join(f"{_serialize_message(message)}\n" for message in messages)
        data = payload.encode("utf-8")
        blob_client = await self._get_blob_client(session_id)
        try:
            await blob_client.append_block(data)
            return
        except ResourceNotFoundError:
            pass

        with contextlib.suppress(ResourceExistsError):
            await blob_client.create_append_blob()
        await blob_client.append_block(data)

    def _blob_name(self, session_id: str | None) -> str:
        stem = session_id or "default"
        return f"{self._blob_prefix}{self._agent_slug}/{stem}.jsonl"

    async def _get_blob_client(self, session_id: str | None) -> Any:
        service_client = await self._get_service_client()
        await self._ensure_container(service_client)
        return service_client.get_blob_client(
            container=self._container_name,
            blob=self._blob_name(session_id),
        )

    async def _get_service_client(self) -> Any:
        cached = _SERVICE_CLIENTS.get(self._cache_key)
        if cached is not None:
            return cached
        async with _SERVICE_CLIENTS_LOCK:
            cached = _SERVICE_CLIENTS.get(self._cache_key)
            if cached is not None:
                return cached
            client = _build_service_client(
                connection_string=self._connection_string,
                blob_service_url=self._blob_service_url,
                credential=self._credential,
            )
            _SERVICE_CLIENTS[self._cache_key] = client
            _register_shutdown(shutdown)
            return client

    async def _ensure_container(self, service_client: Any) -> None:
        key = (self._cache_key, self._container_name)
        if key in _ENSURED_CONTAINERS:
            return
        async with _ENSURED_CONTAINERS_LOCK:
            if key in _ENSURED_CONTAINERS:
                return
            container_client = service_client.get_container_client(self._container_name)
            with contextlib.suppress(ResourceExistsError):
                await container_client.create_container()
            _ENSURED_CONTAINERS.add(key)


def _serialize_message(message: Message) -> str:
    payload = message.to_dict()
    serialized = json.dumps(payload)
    if "\n" in serialized or "\r" in serialized:
        raise ValueError("Serialized message must not contain newline characters for JSONL.")
    return serialized


def _normalize_prefix(prefix: str) -> str:
    """Strip leading slashes and ensure exactly one trailing slash if non-empty."""
    cleaned = (prefix or "").lstrip("/")
    if cleaned and not cleaned.endswith("/"):
        cleaned = f"{cleaned}/"
    return cleaned


def _service_client_cache_key(
    *,
    connection_string: str | None,
    blob_service_url: str | None,
) -> str:
    """Preserve the non-secret account URL or hashed connection cache key."""
    if blob_service_url:
        return f"url::{blob_service_url}"
    assert connection_string is not None
    digest = hashlib.sha256(connection_string.encode("utf-8")).hexdigest()[:32]
    return f"conn::{digest}"


def _build_service_client(
    *,
    connection_string: str | None,
    blob_service_url: str | None,
    credential: Any | None,
) -> Any:
    from azure.storage.blob.aio import BlobServiceClient

    if connection_string:
        return BlobServiceClient.from_connection_string(connection_string)
    assert blob_service_url is not None
    if credential is None:
        from ..._credential import build_async_credential_with_client_id

        owned_credential = build_async_credential_with_client_id(storage_client_id())
        _OWNED_CREDENTIALS[id(owned_credential)] = owned_credential
        _register_shutdown(shutdown)
        credential = owned_credential
    return BlobServiceClient(account_url=blob_service_url, credential=credential)


async def _close_service(cache_key: str) -> None:
    try:
        await asyncio.wait_for(_SERVICE_CLIENTS[cache_key].close(), timeout=5)
    except (AzureError, OSError):
        logger.error("MAF history storage client cleanup failed.")
        raise SessionStorageError(errno.EIO, "MAF history storage cleanup failed.") from None
    _SERVICE_CLIENTS.pop(cache_key, None)


async def _close_credential(credential: AsyncTokenCredential) -> None:
    try:
        await asyncio.wait_for(credential.close(), timeout=5)
    except (AzureError, OSError):
        logger.error("MAF history storage credential cleanup failed.")
        raise SessionStorageError(errno.EIO, "MAF history storage cleanup failed.") from None
    _OWNED_CREDENTIALS.pop(id(credential), None)


async def shutdown() -> None:
    """Close only this provider's cached clients and owned credentials."""
    async with AsyncExitStack() as cleanup:
        cleanup.callback(_ENSURED_CONTAINERS.clear)
        for credential in tuple(_OWNED_CREDENTIALS.values()):
            cleanup.push_async_callback(_close_credential, credential)
        for cache_key in tuple(_SERVICE_CLIENTS):
            cleanup.push_async_callback(_close_service, cache_key)
    _unregister_shutdown(shutdown)


def build_blob_provider_from_environment(
    *,
    agent_slug: str,
    container_name: str | None = None,
) -> BlobHistoryProvider | None:
    """Construct history from connection-string-first Azure Functions Blob settings."""
    settings = blob_storage_from_environment(container_name=container_name)
    if settings is None:
        return None
    if settings.connection_string:
        logger.info(
            "BlobHistoryProvider: using AzureWebJobsStorage connection string (container=%s).",
            settings.container_name,
        )
        return BlobHistoryProvider(
            agent_slug=agent_slug,
            connection_string=settings.connection_string,
            container_name=settings.container_name,
        )
    logger.info(
        "BlobHistoryProvider: using AzureWebJobsStorage__blobServiceUri=%s (container=%s).",
        settings.blob_service_url,
        settings.container_name,
    )
    return BlobHistoryProvider(
        agent_slug=agent_slug,
        blob_service_url=settings.blob_service_url,
        container_name=settings.container_name,
    )


def reset_caches_for_testing() -> None:
    """Drop the module-level caches. Test-only helper."""
    _SERVICE_CLIENTS.clear()
    _OWNED_CREDENTIALS.clear()
    _ENSURED_CONTAINERS.clear()
    _unregister_shutdown(shutdown)
