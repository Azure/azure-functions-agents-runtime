"""Immutable Blob-backed storage for Durable checkpoint references."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from azure.core.exceptions import (
    AzureError,
    ResourceExistsError,
    ResourceNotFoundError,
)

from ..harness._checkpoint import (
    MAX_AGENT_SLUG_LENGTH,
    MAX_CONTENT_TYPE_LENGTH,
    MAX_IDENTITY_VALUE_LENGTH,
    MAX_OWNER_SCOPE_DIGEST_LENGTH,
    MAX_VERSION_VALUE_LENGTH,
    ContentRef,
    ContinuationRef,
    ContinuationRole,
    IntegrityMetadata,
    StorageKind,
    is_valid_owner_scope_digest,
    validate_reference_expiry,
)
from ..harness._harness_binding import HarnessKind
from ..harness._session_storage import (
    BlobStorageSettings,
    OwnedBlobService,
    open_blob_service,
)

if TYPE_CHECKING:
    from azure.storage.blob import BlobProperties
    from azure.storage.blob.aio import BlobClient

# Dedicated namespace for runtime-owned Durable data and its storage format version.
_REFERENCE_ROOT = "durable/v1"

# Separate paths prevent content and harness continuation references from being confused.
_CONTENT_KIND = "content"
_CONTINUATION_KIND = "continuation"

class ReferenceStoreError(OSError):
    """A sanitized Durable reference-store failure."""

    status_code = 503


class ReferenceNotFoundError(ReferenceStoreError):
    """The referenced content or continuation no longer exists."""

    status_code = 404


class ReferenceIntegrityError(ReferenceStoreError):
    """The reference does not match its owner, metadata, or stored bytes."""

    status_code = 500


class ReferenceExpiredError(ReferenceStoreError):
    """The reference is past its authorized lifetime."""

    status_code = 410


@dataclass(frozen=True, init=False)
class ReferenceScope:
    """Identifies the one Durable turn authorized to use a stored reference."""

    backend_namespace: str
    agent_slug: str
    owner_scope_digest: str
    entry_id: str
    session_id: str
    session_incarnation_id: str
    run_id: str
    turn_generation: int

    @classmethod
    def create(
        cls,
        *,
        backend_namespace: str,
        agent_slug: str,
        owner_scope_digest: str,
        entry_id: str,
        session_id: str,
        session_incarnation_id: str,
        run_id: str,
        turn_generation: int,
    ) -> ReferenceScope:
        """Create a validated authorization boundary for one Durable turn."""
        values = {
            "backend namespace": (backend_namespace, MAX_IDENTITY_VALUE_LENGTH),
            "agent slug": (agent_slug, MAX_AGENT_SLUG_LENGTH),
            "owner scope digest": (
                owner_scope_digest,
                MAX_OWNER_SCOPE_DIGEST_LENGTH,
            ),
            "entry ID": (entry_id, MAX_IDENTITY_VALUE_LENGTH),
            "session ID": (session_id, MAX_IDENTITY_VALUE_LENGTH),
            "session incarnation ID": (
                session_incarnation_id,
                MAX_IDENTITY_VALUE_LENGTH,
            ),
            "run ID": (run_id, MAX_IDENTITY_VALUE_LENGTH),
        }
        normalized = {
            name: _bounded_value(value, maximum=maximum, name=name)
            for name, (value, maximum) in values.items()
        }
        owner_digest = normalized["owner scope digest"]
        if not is_valid_owner_scope_digest(owner_digest):
            raise ValueError(
                "Durable owner scope digest must be lower-case hexadecimal."
            )
        if isinstance(turn_generation, bool) or not isinstance(turn_generation, int):
            raise ValueError("Durable turn generation must be an integer.")
        if turn_generation < 0:
            raise ValueError("Durable turn generation must not be negative.")
        scope = object.__new__(cls)
        object.__setattr__(scope, "backend_namespace", normalized["backend namespace"])
        object.__setattr__(scope, "agent_slug", normalized["agent slug"])
        object.__setattr__(scope, "owner_scope_digest", owner_digest)
        object.__setattr__(scope, "entry_id", normalized["entry ID"])
        object.__setattr__(scope, "session_id", normalized["session ID"])
        object.__setattr__(
            scope,
            "session_incarnation_id",
            normalized["session incarnation ID"],
        )
        object.__setattr__(scope, "run_id", normalized["run ID"])
        object.__setattr__(scope, "turn_generation", turn_generation)
        return scope

    def turn_identity_key(self) -> tuple[str, str, str, str, str, str, int]:
        """Return the identity used to keep references within one active turn."""
        return (
            self.agent_slug,
            self.owner_scope_digest,
            self.entry_id,
            self.session_id,
            self.session_incarnation_id,
            self.run_id,
            self.turn_generation,
        )

    def digest(self) -> str:
        """Create the opaque storage identity used instead of customer identifiers."""
        payload = json.dumps(
            {
                "backend_namespace": self.backend_namespace,
                "agent_slug": self.agent_slug,
                "owner_scope_digest": self.owner_scope_digest,
                "entry_id": self.entry_id,
                "session_id": self.session_id,
                "session_incarnation_id": self.session_incarnation_id,
                "run_id": self.run_id,
                "turn_generation": self.turn_generation,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class DurableReferenceStore(Protocol):
    """Defines storage operations for content and harness continuation references."""

    async def write_content(
        self,
        scope: ReferenceScope,
        content: bytes,
        *,
        content_type: str,
        expires_at: datetime,
    ) -> ContentRef:
        """Store large content outside Durable state and return its verified pointer."""
        ...

    async def read_content(
        self,
        scope: ReferenceScope,
        reference: ContentRef,
    ) -> bytes:
        """Load referenced content after checking access, expiry, and integrity."""
        ...

    async def write_continuation(
        self,
        scope: ReferenceScope,
        state: bytes,
        *,
        harness_kind: HarnessKind,
        connector_version: str,
        connector_profile_id: str,
        state_format_version: str,
        continuation_role: ContinuationRole,
        expires_at: datetime,
    ) -> ContinuationRef:
        """Store harness resume state without placing SDK data in Durable state."""
        ...

    async def read_continuation(
        self,
        scope: ReferenceScope,
        reference: ContinuationRef,
    ) -> bytes:
        """Load authorized harness state so an interrupted agent turn can continue."""
        ...

    async def delete(
        self,
        scope: ReferenceScope,
        reference: ContentRef | ContinuationRef,
    ) -> None:
        """Remove referenced data when its Durable lifecycle no longer needs it."""
        ...

    async def close(self) -> None:
        """Release storage connections owned by this reference store."""
        ...


@dataclass(frozen=True)
class _StoredReference:
    """Carries one immutable Blob write into its typed reference envelope."""

    key: str
    created_at: datetime
    expires_at: datetime
    integrity: IntegrityMetadata


class BlobDurableReferenceStore:
    """Stores immutable Durable content and continuation data in Azure Blob Storage."""

    def __init__(
        self,
        owned: OwnedBlobService,
        *,
        container_name: str,
        backend_namespace: str,
    ) -> None:
        if not backend_namespace.strip():
            raise ValueError("Durable backend namespace must not be blank.")
        self._owned = owned
        self._container_name = container_name
        self._container = owned.service.get_container_client(container_name)
        self._backend_namespace = backend_namespace.strip()

    async def initialize(self) -> None:
        """Ensure the private Blob container is ready for Durable references."""
        with suppress(ResourceExistsError):
            await self._container.create_container()

    def _validate_scope(self, scope: ReferenceScope) -> None:
        """Prevent references from crossing Durable storage backends."""
        if scope.backend_namespace != self._backend_namespace:
            raise ReferenceIntegrityError("Reference scope does not match this backend.")

    def _prefix(self, scope: ReferenceScope, kind: str) -> str:
        """Build the opaque storage boundary for one turn and reference kind."""
        self._validate_scope(scope)
        return f"{_REFERENCE_ROOT}/{scope.digest()}/{kind}/"

    def _mint_key(self, scope: ReferenceScope, kind: str) -> str:
        """Create a unique immutable key inside the authorized storage boundary."""
        return self._prefix(scope, kind) + uuid.uuid4().hex

    def _blob(self, key: str) -> BlobClient:
        """Return the Blob client for one previously authorized opaque key."""
        return self._owned.service.get_blob_client(
            container=self._container_name,
            blob=key,
        )

    def _require_key(self, scope: ReferenceScope, kind: str, key: str) -> None:
        """Reject a key that falls outside the caller's authorized turn."""
        if not key.startswith(self._prefix(scope, kind)):
            raise ReferenceIntegrityError("Reference is outside the authorized scope.")

    async def _write(
        self,
        scope: ReferenceScope,
        kind: str,
        payload: bytes,
        *,
        expires_at: datetime,
    ) -> _StoredReference:
        """Persist immutable bytes and return the evidence needed to reference them."""
        created_at = datetime.now(UTC)
        validate_reference_expiry(
            created_at,
            expires_at,
            subject="Durable reference",
        )
        key = self._mint_key(scope, kind)
        digest = hashlib.sha256(payload).hexdigest()
        integrity = IntegrityMetadata(digest=digest, content_length=len(payload))
        metadata = _blob_metadata(
            scope,
            kind,
            integrity,
            created_at=created_at,
        )
        try:
            await self._blob(key).upload_blob(
                payload,
                overwrite=False,
                metadata=metadata,
            )
        except ResourceExistsError:
            raise ReferenceStoreError("Durable reference key collision.") from None
        except AzureError:
            raise ReferenceStoreError("Durable reference could not be persisted.") from None
        return _StoredReference(
            key=key,
            created_at=created_at,
            expires_at=expires_at,
            integrity=integrity,
        )

    async def _read(
        self,
        scope: ReferenceScope,
        kind: str,
        key: str,
        integrity: IntegrityMetadata,
        expires_at: datetime,
    ) -> bytes:
        """Load bytes only after authorization, lifetime, and integrity checks."""
        self._require_key(scope, kind, key)
        if expires_at <= datetime.now(UTC):
            raise ReferenceExpiredError("Durable reference has expired.")
        blob = self._blob(key)
        try:
            properties = await blob.get_blob_properties()
            self._validate_properties(scope, kind, properties, integrity)
            downloader = await blob.download_blob()
            payload = await downloader.readall()
        except ResourceNotFoundError:
            raise ReferenceNotFoundError("Durable reference was not found.") from None
        except AzureError:
            raise ReferenceStoreError("Durable reference could not be read.") from None
        if len(payload) != integrity.content_length:
            raise ReferenceIntegrityError("Durable reference length verification failed.")
        digest = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(digest, integrity.digest):
            raise ReferenceIntegrityError("Durable reference digest verification failed.")
        return payload

    @staticmethod
    def _validate_properties(
        scope: ReferenceScope,
        kind: str,
        properties: BlobProperties,
        integrity: IntegrityMetadata,
    ) -> None:
        """Verify Blob metadata still matches the trusted reference envelope."""
        metadata = properties.metadata or {}
        expected = _blob_metadata(scope, kind, integrity)
        if properties.size != integrity.content_length or any(
            metadata.get(key) != value for key, value in expected.items()
        ):
            raise ReferenceIntegrityError("Durable reference metadata verification failed.")

    async def write_content(
        self,
        scope: ReferenceScope,
        content: bytes,
        *,
        content_type: str,
        expires_at: datetime,
    ) -> ContentRef:
        """Store large content outside Durable state and return its verified pointer."""
        normalized_content_type = _bounded_value(
            content_type,
            maximum=MAX_CONTENT_TYPE_LENGTH,
            name="content type",
        )
        stored = await self._write(
            scope,
            _CONTENT_KIND,
            content,
            expires_at=expires_at,
        )
        return ContentRef(
            storage_kind=StorageKind.BLOB,
            opaque_content_key=stored.key,
            content_digest=stored.integrity.digest,
            content_length=stored.integrity.content_length,
            content_type=normalized_content_type,
            created_at=stored.created_at,
            expires_at=stored.expires_at,
            integrity_metadata=stored.integrity,
        )

    async def read_content(
        self,
        scope: ReferenceScope,
        reference: ContentRef,
    ) -> bytes:
        """Load referenced content after checking access, expiry, and integrity."""
        if reference.storage_kind is not StorageKind.BLOB:
            raise ReferenceIntegrityError("Content reference uses an unsupported store.")
        return await self._read(
            scope,
            _CONTENT_KIND,
            reference.opaque_content_key,
            reference.integrity_metadata,
            reference.expires_at,
        )

    async def write_continuation(
        self,
        scope: ReferenceScope,
        state: bytes,
        *,
        harness_kind: HarnessKind,
        connector_version: str,
        connector_profile_id: str,
        state_format_version: str,
        continuation_role: ContinuationRole,
        expires_at: datetime,
    ) -> ContinuationRef:
        """Store harness resume state without placing SDK data in Durable state."""
        validated_harness_kind = _require_enum(
            harness_kind,
            HarnessKind,
            name="harness kind",
        )
        validated_continuation_role = _require_enum(
            continuation_role,
            ContinuationRole,
            name="continuation role",
        )
        normalized_connector_version = _bounded_value(
            connector_version,
            maximum=MAX_VERSION_VALUE_LENGTH,
            name="connector version",
        )
        normalized_profile_id = _bounded_value(
            connector_profile_id,
            maximum=MAX_IDENTITY_VALUE_LENGTH,
            name="connector profile ID",
        )
        normalized_state_version = _bounded_value(
            state_format_version,
            maximum=MAX_VERSION_VALUE_LENGTH,
            name="state format version",
        )
        self._validate_scope(scope)
        stored = await self._write(
            scope,
            _CONTINUATION_KIND,
            state,
            expires_at=expires_at,
        )
        return ContinuationRef(
            harness_kind=validated_harness_kind,
            connector_version=normalized_connector_version,
            connector_profile_id=normalized_profile_id,
            backend_namespace=scope.backend_namespace,
            agent_slug=scope.agent_slug,
            owner_scope_digest=scope.owner_scope_digest,
            entry_id=scope.entry_id,
            session_id=scope.session_id,
            session_incarnation_id=scope.session_incarnation_id,
            run_id=scope.run_id,
            turn_generation=scope.turn_generation,
            storage_kind=StorageKind.BLOB,
            opaque_state_key=stored.key,
            state_format_version=normalized_state_version,
            continuation_role=validated_continuation_role,
            created_at=stored.created_at,
            expires_at=stored.expires_at,
            integrity_metadata=stored.integrity,
        )

    async def read_continuation(
        self,
        scope: ReferenceScope,
        reference: ContinuationRef,
    ) -> bytes:
        """Load authorized harness state so an interrupted agent turn can continue."""
        if reference.storage_kind is not StorageKind.BLOB:
            raise ReferenceIntegrityError("Continuation uses an unsupported store.")
        _validate_continuation_scope(scope, reference)
        return await self._read(
            scope,
            _CONTINUATION_KIND,
            reference.opaque_state_key,
            reference.integrity_metadata,
            reference.expires_at,
        )

    async def delete(
        self,
        scope: ReferenceScope,
        reference: ContentRef | ContinuationRef,
    ) -> None:
        """Remove referenced data when its Durable lifecycle no longer needs it."""
        if isinstance(reference, ContentRef):
            key = reference.opaque_content_key
            kind = _CONTENT_KIND
        else:
            _validate_continuation_scope(scope, reference)
            key = reference.opaque_state_key
            kind = _CONTINUATION_KIND
        self._require_key(scope, kind, key)
        try:
            await self._blob(key).delete_blob()
        except ResourceNotFoundError:
            return
        except AzureError:
            raise ReferenceStoreError("Durable reference could not be deleted.") from None

    async def close(self) -> None:
        """Release storage connections owned by this Blob reference store."""
        await self._owned.close()


def _blob_metadata(
    scope: ReferenceScope,
    kind: str,
    integrity: IntegrityMetadata,
    *,
    created_at: datetime | None = None,
) -> dict[str, str]:
    metadata = {
        "schema_version": "1",
        "reference_kind": kind,
        "scope_digest": scope.digest(),
        "sha256": integrity.digest,
        "content_length": str(integrity.content_length),
    }
    if created_at is not None:
        metadata["created_at"] = created_at.isoformat()
    return metadata


def _bounded_value(value: str, *, maximum: int, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"Durable {name} must not be blank.")
    if len(normalized) > maximum:
        raise ValueError(f"Durable {name} exceeds its length limit.")
    return normalized


def _require_enum[T: StrEnum](
    value: object,
    expected: type[T],
    *,
    name: str,
) -> T:
    if not isinstance(value, expected):
        raise ValueError(f"Durable {name} is invalid.")
    return value


def _validate_continuation_scope(
    scope: ReferenceScope,
    reference: ContinuationRef,
) -> None:
    if (
        scope.backend_namespace != reference.backend_namespace
        or scope.turn_identity_key() != reference.turn_identity_key()
    ):
        raise ReferenceIntegrityError("Continuation does not match the authorized scope.")


async def open_blob_reference_store(
    settings: BlobStorageSettings,
    *,
    backend_namespace: str,
) -> BlobDurableReferenceStore:
    try:
        async with AsyncExitStack() as cleanup:
            owned = await open_blob_service(settings)
            cleanup.push_async_callback(owned.close)
            store = BlobDurableReferenceStore(
                owned,
                container_name=settings.container_name,
                backend_namespace=backend_namespace,
            )
            await store.initialize()
            cleanup.pop_all()
            return store
    except ReferenceStoreError:
        raise
    except (AzureError, ValueError):
        raise ReferenceStoreError(
            "Durable reference storage could not be opened."
        ) from None
