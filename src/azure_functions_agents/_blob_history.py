"""Azure Blob Storage-backed :class:`HistoryProvider` for MAF agent sessions.

The Microsoft Agent Framework ships :class:`FileHistoryProvider` for local
disk storage and :class:`InMemoryHistoryProvider` for tests, but does not yet
provide a blob-backed implementation. This module fills that gap so the
runtime can persist multi-turn history to the same storage account that
Azure Functions already requires (``AzureWebJobsStorage``) — no extra
resources, no file-share mounts, no storage account keys, and true
multi-instance support.

Wire format
-----------

One blob per session, named ``{blob_prefix}{session_id}.jsonl`` inside a
single container (default: ``azure-functions-agents``). Blobs are
**Append Blobs**: every call to :meth:`save_messages` appends the JSON Lines
serialization of just the new messages from the current turn — this matches
the contract that MAF's :meth:`HistoryProvider.after_run` only ever passes
the per-turn delta (input + response messages), never the full history.

Concurrency
-----------

``BlobClient.append_block`` is atomic on the server side, so two Function
instances appending to the same session blob simultaneously cannot interleave
within a single block. The documented runtime contract is still
"one active turn per session id" — cross-instance turn ordering is the
caller's responsibility.

Configuration
-------------

The provider accepts either an Azure Storage connection string or an
identity-based ``(blob_service_url, credential)`` pair. Helpers in this
module resolve those from the standard Azure Functions
``AzureWebJobsStorage`` settings:

* ``AzureWebJobsStorage`` — connection string (local dev, Azurite).
* ``AzureWebJobsStorage__blobServiceUri`` (+ optional
  ``AzureWebJobsStorage__clientId``) — identity-based; uses
  :class:`DefaultAzureCredential`, honoring the user-assigned client id when
  present (matches the Bicep samples in this repo).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from agent_framework import HistoryProvider, Message
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from ._logger import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONTAINER_NAME = "azure-functions-agents"
DEFAULT_BLOB_PREFIX = "agent-sessions/"
DEFAULT_SOURCE_ID = "blob_history"

_CONN_STRING_ENV = "AzureWebJobsStorage"
_BLOB_SERVICE_URI_ENV = "AzureWebJobsStorage__blobServiceUri"
_CLIENT_ID_ENV = "AzureWebJobsStorage__clientId"
_CONTAINER_ENV = "AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER"


# ---------------------------------------------------------------------------
# Process-wide caches
# ---------------------------------------------------------------------------

# The BlobServiceClient is keyed by a non-secret identifier (account URL for
# identity-based, or a stable hash sentinel for connection strings) so that
# secrets never live inside dict keys that could leak into reprs / logs.
_SERVICE_CLIENTS: dict[str, Any] = {}
_SERVICE_CLIENTS_LOCK = asyncio.Lock()

# Container existence check is process-wide: we only need to create it once
# per (cache_key, container_name).
_ENSURED_CONTAINERS: set[tuple[str, str]] = set()
_ENSURED_CONTAINERS_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class BlobHistoryProvider(HistoryProvider):
    """Append-blob-backed :class:`HistoryProvider`.

    Each session is stored as a single Append Blob named
    ``{blob_prefix}{session_id}.jsonl``. Messages are written as JSON Lines —
    one ``Message.to_dict()`` payload per line.
    """

    DEFAULT_SOURCE_ID: ClassVar[str] = DEFAULT_SOURCE_ID

    def __init__(
        self,
        *,
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

    # ------------------------------------------------------------------
    # MAF HistoryProvider surface
    # ------------------------------------------------------------------

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

        # Blob does not exist yet — create it and retry. Suppress the
        # "already exists" race that happens when another instance wins
        # the create.
        with contextlib.suppress(ResourceExistsError):
            await blob_client.create_append_blob()
        await blob_client.append_block(data)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _blob_name(self, session_id: str | None) -> str:
        stem = session_id or "default"
        return f"{self._blob_prefix}{stem}.jsonl"

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


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


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
    """Build a non-secret cache key for the :class:`BlobServiceClient` cache.

    We intentionally avoid storing the raw connection string as a dict key so
    that secrets do not surface in error reprs or debug dumps. A short hash
    is sufficient because we only need stable equality for the cache.
    """
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
        from azure.identity.aio import DefaultAzureCredential

        # Precedence: storage-specific identity (AzureWebJobsStorage__clientId) wins,
        # then app-wide AZURE_CLIENT_ID, then bare DefaultAzureCredential().
        client_id = (
            os.environ.get(_CLIENT_ID_ENV) or os.environ.get("AZURE_CLIENT_ID") or ""
        ).strip()
        credential = (
            DefaultAzureCredential(managed_identity_client_id=client_id)
            if client_id
            else DefaultAzureCredential()
        )
    return BlobServiceClient(account_url=blob_service_url, credential=credential)


# ---------------------------------------------------------------------------
# Factory used by the runner
# ---------------------------------------------------------------------------


def build_blob_provider_from_environment(
    *,
    container_name: str | None = None,
) -> BlobHistoryProvider | None:
    """Construct a :class:`BlobHistoryProvider` from ``AzureWebJobsStorage`` env vars.

    Returns ``None`` if neither a connection string nor a blob service URI is
    configured. Honors both the connection-string form (used locally with
    Azurite) and the identity-based form
    (``AzureWebJobsStorage__blobServiceUri``) that Azure Functions deploys
    use with managed identity.
    """
    conn = (os.environ.get(_CONN_STRING_ENV) or "").strip()
    uri = (os.environ.get(_BLOB_SERVICE_URI_ENV) or "").strip()
    if not conn and not uri:
        return None
    container = container_name or (os.environ.get(_CONTAINER_ENV) or "").strip() or None
    kwargs: dict[str, Any] = {}
    if container:
        kwargs["container_name"] = container
    if conn:
        logger.info(
            "BlobHistoryProvider: using AzureWebJobsStorage connection string (container=%s).",
            container or DEFAULT_CONTAINER_NAME,
        )
        return BlobHistoryProvider(connection_string=conn, **kwargs)
    logger.info(
        "BlobHistoryProvider: using AzureWebJobsStorage__blobServiceUri=%s (container=%s).",
        uri,
        container or DEFAULT_CONTAINER_NAME,
    )
    return BlobHistoryProvider(blob_service_url=uri, **kwargs)


def reset_caches_for_testing() -> None:
    """Drop the module-level caches. Test-only helper."""
    _SERVICE_CLIENTS.clear()
    _ENSURED_CONTAINERS.clear()
