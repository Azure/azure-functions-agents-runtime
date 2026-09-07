"""Integrity-bound keyed documents for activity receipts and fences."""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .._credential import build_async_credential_with_client_id
from ..strict_json import canonical_json_bytes
from .durable_loop_activities import resolve_durable_content_blob_binding
from .durable_loop_protocol import ContentRefV1, DurableFaultProfile

_DOCUMENT_KEY = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,255}$")


class DurableReceiptStoreError(RuntimeError):
    """A keyed receipt could not be read or atomically updated."""


class ActivityReceiptStatus(StrEnum):
    """The externally persisted lifecycle of one side-effecting activity."""

    STARTED = "started"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    AMBIGUOUS = "ambiguous"


class ActivityReceiptV1(BaseModel):
    """Content-free keyed receipt whose sensitive details remain behind refs."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    operation_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    request_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    kind: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
    status: ActivityReceiptStatus
    attempt: Annotated[int, Field(ge=1, le=64)]
    updated_at: datetime
    result_ref: ContentRefV1 | None = None
    operation_ref: ContentRefV1 | None = None
    workspace_ref: ContentRefV1 | None = None
    lease_ref: ContentRefV1 | None = None
    error_code: Annotated[
        str,
        Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$"),
    ] | None = None


@dataclass(frozen=True, slots=True)
class KeyedDocument:
    """One immutable read snapshot and its compare-and-swap revision."""

    payload: bytes
    revision: str


@runtime_checkable
class DurableKeyedDocumentStore(Protocol):
    """Small create/CAS/delete boundary for external activity state."""

    async def get(self, key: str) -> KeyedDocument | None:
        """Read one keyed document."""

    async def create(self, key: str, payload: bytes) -> bool:
        """Create only when the key is absent."""

    async def replace(self, key: str, payload: bytes, *, revision: str) -> bool:
        """Replace only when the revision still matches."""

    async def delete(self, key: str, *, revision: str | None = None) -> bool:
        """Delete an existing key, optionally guarded by revision."""


class InMemoryDurableKeyedDocumentStore:
    """Deterministic keyed document store for crash-window tests."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._documents: dict[str, KeyedDocument] = {}
        self._revision = 0

    async def get(self, key: str) -> KeyedDocument | None:
        normalized = _validate_key(key)
        async with self._lock:
            document = self._documents.get(normalized)
            if document is None:
                return None
            return KeyedDocument(bytes(document.payload), document.revision)

    async def create(self, key: str, payload: bytes) -> bool:
        normalized = _validate_key(key)
        async with self._lock:
            if normalized in self._documents:
                return False
            self._revision += 1
            self._documents[normalized] = KeyedDocument(
                bytes(payload),
                str(self._revision),
            )
            return True

    async def replace(self, key: str, payload: bytes, *, revision: str) -> bool:
        normalized = _validate_key(key)
        async with self._lock:
            current = self._documents.get(normalized)
            if current is None or current.revision != revision:
                return False
            self._revision += 1
            self._documents[normalized] = KeyedDocument(
                bytes(payload),
                str(self._revision),
            )
            return True

    async def delete(self, key: str, *, revision: str | None = None) -> bool:
        normalized = _validate_key(key)
        async with self._lock:
            current = self._documents.get(normalized)
            if current is None or (
                revision is not None and current.revision != revision
            ):
                return False
            self._documents.pop(normalized)
            return True


class BlobDurableKeyedDocumentStore:
    """Blob-backed create/CAS store in the dedicated durable content container."""

    def __init__(
        self,
        service_client: Any,
        *,
        container_name: str,
    ) -> None:
        self._service_client = service_client
        self._container_name = container_name
        self._ensure_lock = asyncio.Lock()
        self._ensured = False

    @classmethod
    def from_environment(cls) -> BlobDurableKeyedDocumentStore:
        """Build from the same validated dedicated Blob binding as content."""
        from azure.storage.blob.aio import BlobServiceClient

        binding = resolve_durable_content_blob_binding()
        return cls(
            BlobServiceClient(
                account_url=binding.service_url,
                credential=build_async_credential_with_client_id(binding.client_id),
            ),
            container_name=binding.container_name,
        )

    async def get(self, key: str) -> KeyedDocument | None:
        client = await self._client(key)
        from azure.core.exceptions import ResourceNotFoundError

        try:
            downloader = await client.download_blob()
            payload = await downloader.readall()
        except ResourceNotFoundError:
            return None
        properties = downloader.properties
        etag = getattr(properties, "etag", None)
        if not isinstance(etag, str) or not etag:
            raise DurableReceiptStoreError("receipt Blob returned no ETag")
        return KeyedDocument(bytes(payload), etag)

    async def create(self, key: str, payload: bytes) -> bool:
        client = await self._client(key)
        from azure.core.exceptions import ResourceExistsError

        try:
            await client.upload_blob(payload, overwrite=False)
        except ResourceExistsError:
            return False
        return True

    async def replace(self, key: str, payload: bytes, *, revision: str) -> bool:
        client = await self._client(key)
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

        try:
            await client.upload_blob(
                payload,
                overwrite=True,
                etag=revision,
                match_condition=MatchConditions.IfNotModified,
            )
        except (ResourceModifiedError, ResourceNotFoundError):
            return False
        return True

    async def delete(self, key: str, *, revision: str | None = None) -> bool:
        client = await self._client(key)
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

        kwargs: dict[str, object] = {}
        if revision is not None:
            kwargs = {
                "etag": revision,
                "match_condition": MatchConditions.IfNotModified,
            }
        try:
            await client.delete_blob(**kwargs)
        except (ResourceModifiedError, ResourceNotFoundError):
            return False
        return True

    async def _client(self, key: str) -> Any:
        await self._ensure_container()
        return self._service_client.get_blob_client(
            container=self._container_name,
            blob=f"runtime-state/{_validate_key(key)}",
        )

    async def _ensure_container(self) -> None:
        if self._ensured:
            return
        async with self._ensure_lock:
            if self._ensured:
                return
            from azure.core.exceptions import ResourceExistsError

            container = self._service_client.get_container_client(
                self._container_name
            )
            with contextlib.suppress(ResourceExistsError):
                await container.create_container()
            self._ensured = True


def _validate_key(key: str) -> str:
    if _DOCUMENT_KEY.fullmatch(key) is None or ".." in key.split("/"):
        raise ValueError("durable receipt key is invalid")
    return key


async def read_activity_receipt(
    store: DurableKeyedDocumentStore,
    key: str,
) -> tuple[ActivityReceiptV1, str] | None:
    """Read and validate one activity receipt with its CAS revision."""
    document = await store.get(key)
    if document is None:
        return None
    return ActivityReceiptV1.model_validate_json(document.payload), document.revision


async def create_activity_receipt(
    store: DurableKeyedDocumentStore,
    key: str,
    receipt: ActivityReceiptV1,
) -> bool:
    """Create one receipt without overwriting another worker's claim."""
    return await store.create(key, canonical_json_bytes(receipt))


async def replace_activity_receipt(
    store: DurableKeyedDocumentStore,
    key: str,
    receipt: ActivityReceiptV1,
    *,
    revision: str,
) -> bool:
    """Advance one receipt by compare-and-swap."""
    return await store.replace(
        key,
        canonical_json_bytes(receipt),
        revision=revision,
    )


class DurableOneShotFaults:
    """Persist deterministic one-shot live-qualification fault consumption."""

    def __init__(
        self,
        store: DurableKeyedDocumentStore,
        *,
        enabled: bool,
    ) -> None:
        self._store = store
        self._enabled = enabled

    async def consume(
        self,
        profile: DurableFaultProfile,
        *,
        run_id: str,
        point: str,
    ) -> bool:
        """Return true exactly once for the selected fixed profile and point."""
        if not self._enabled or profile is DurableFaultProfile.NONE:
            return False
        expected_point = {
            DurableFaultProfile.MODEL_APIM_429_ONCE: "model_apim_429",
            DurableFaultProfile.MODEL_TIMEOUT_ONCE: "model_timeout",
            DurableFaultProfile.TOOL_ACTIVITY_ACK_LOSS_ONCE: "tool_activity_ack_loss",
            DurableFaultProfile.SANDBOX_LOSS_AFTER_CHECKPOINT: (
                "sandbox_loss_after_checkpoint"
            ),
            DurableFaultProfile.CLEANUP_FAILURE_ONCE: "cleanup_failure",
            DurableFaultProfile.COMMIT_ACK_LOSS_ONCE: "commit_ack_loss",
        }[profile]
        if point != expected_point:
            return False
        key = (
            "faults/"
            + _safe_segment(run_id)
            + "/"
            + profile.value.replace("_", "-")
        )
        return await self._store.create(key, b'{"schema_version":"1"}')


def _safe_segment(value: str) -> str:
    rendered = re.sub(r"[^a-z0-9_-]", "-", value.casefold()).strip("-")
    if not rendered:
        raise ValueError("durable receipt segment is invalid")
    return rendered[:96]
