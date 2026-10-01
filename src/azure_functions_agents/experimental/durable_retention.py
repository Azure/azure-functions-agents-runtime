"""Indexed retention, expiry, and exact-resource cleanup for durable runs."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Protocol, TypeGuard, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..strict_json import canonical_json_bytes
from .durable_loop_activities import (
    DurableLoopContentError,
    canonical_content_object_id,
)
from .durable_loop_protocol import (
    DURABLE_LOOP_ORCHESTRATOR_V4_NAME,
    MAX_RUN_ARTIFACTS_PER_PAGE,
    ContentRefV1,
    DurableLoopRunStatus,
    DurableObjectReferenceState,
    DurableObjectReferenceV1,
    DurableObjectRunReferenceV1,
    DurablePublicStatusV1,
    DurableResourceExpiryV1,
    DurableRetentionPolicyV1,
    DurableRunArtifactPageV1,
    DurableRunArtifactV1,
    DurableRunIdentityV1,
    DurableRunIdentityV2,
    DurableRunResourceHeaderV1,
    DurableRunTombstoneV1,
    DurableSessionResourceV1,
    DurableSessionTombstoneV1,
    canonical_hash,
)
from .durable_loop_receipts import (
    DurableKeyedDocumentStore,
    KeyedDocument,
)

_CONTENT_KIND = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_STATUSES = frozenset(
    {
        DurableLoopRunStatus.COMPLETED,
        DurableLoopRunStatus.FAILED,
        DurableLoopRunStatus.CANCELLED,
    }
)
_SESSION_CLEANUP_FENCE = "0" * 64
_DEFAULT_OBJECT_SHARDS = 32
_DEFAULT_PENDING_SAFETY_SECONDS = 60 * 60
_MAX_CAS_ATTEMPTS = 64
_CLEANUP_BUCKET_SECONDS = 60 * 60
_CLEANUP_CURSOR_KEY = "retention/cleanup-cursor/v1"
_CLEANUP_INITIAL_LOOKBACK_HOURS = 24


class DurableRetentionError(RuntimeError):
    """A retained resource could not be safely read or updated."""


class DurableRetentionConflictError(DurableRetentionError):
    """A stable retained identity was reused with conflicting content."""


class DurableRetentionBusyError(DurableRetentionError):
    """A concurrent retention fence requires the operation to retry."""


class DurableRetentionExpiredError(DurableRetentionError):
    """A retained public resource has expired."""


class DurableRetentionLegacyExcludedError(DurableRetentionError):
    """An unindexed legacy run is intentionally excluded from cleanup."""


class DurableSessionExpiredError(DurableRetentionError):
    """An owner-bound session ID is retained only as an expiry tombstone."""


class DurableSessionBusyError(DurableRetentionError):
    """A session has another active run or cleanup fence."""


class DurableAdmissionReceiptState(StrEnum):
    """The long-lived keyed disposition of one run admission."""

    ACTIVE = "active"
    TERMINAL = "terminal"


class DurableRunVisibility(StrEnum):
    """The indexed visibility of a retained run for its authorized owner."""

    ACTIVE = "active"
    TERMINAL = "terminal"
    EXPIRED = "expired"
    NOT_FOUND = "not_found"


class DurableCleanupRecordKind(StrEnum):
    """An exact retained target discoverable from one time bucket."""

    OBJECT_REFERENCE = "object_reference"
    ARTIFACT_PAGE_CANDIDATE = "artifact_page_candidate"
    RUN = "run"
    SESSION = "session"


class DurableAdmissionReceiptV1(BaseModel):
    """Content-free keyed replay receipt kept outside the bounded session entity."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    request_id_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    request_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    session_id: Annotated[str, Field(min_length=1, max_length=128)]
    owner_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    access_namespace_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    state: DurableAdmissionReceiptState
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    terminal_status: DurableLoopRunStatus | None = None
    record_version: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_receipt(self) -> DurableAdmissionReceiptV1:
        created = _as_utc(self.created_at, "created_at")
        updated = _as_utc(self.updated_at, "updated_at")
        expires = _as_utc(self.expires_at, "expires_at")
        if not created <= updated < expires:
            raise ValueError("admission receipt dates must be monotonic")
        terminal = self.state is DurableAdmissionReceiptState.TERMINAL
        if terminal != (self.terminal_status is not None):
            raise ValueError("terminal admission state must match terminal status")
        if self.terminal_status is not None and self.terminal_status not in _TERMINAL_STATUSES:
            raise ValueError("admission receipt terminal status is invalid")
        return self


class DurableCleanupRecordV1(BaseModel):
    """One exact cleanup target placed in a deterministic time bucket."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    record_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    kind: DurableCleanupRecordKind
    not_before: datetime
    run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    run_id_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    session_id_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    retention_class: Annotated[
        str,
        Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$"),
    ] | None = None
    content_ref: ContentRefV1 | None = None
    document_key: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    page_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    tombstone_seconds: Annotated[int, Field(ge=60 * 60, le=730 * 24 * 60 * 60)] | None = (
        None
    )

    @model_validator(mode="after")
    def validate_record(self) -> DurableCleanupRecordV1:
        _as_utc(self.not_before, "not_before")
        if self.record_id != _cleanup_record_hash(
            self.model_dump(mode="json", exclude={"record_id"})
        ):
            raise ValueError("cleanup record hash mismatch")
        if self.kind is DurableCleanupRecordKind.OBJECT_REFERENCE:
            if (
                self.run_id_hash is None
                or self.retention_class is None
                or self.content_ref is None
            ):
                raise ValueError("object cleanup record is incomplete")
        elif self.kind is DurableCleanupRecordKind.ARTIFACT_PAGE_CANDIDATE:
            if (
                self.run_id is None
                or self.document_key is None
                or self.page_hash is None
            ):
                raise ValueError("artifact-page cleanup record is incomplete")
        elif self.kind is DurableCleanupRecordKind.RUN:
            if self.run_id is None:
                raise ValueError("run cleanup record is incomplete")
        elif (
            self.session_id_hash is None
            or self.tombstone_seconds is None
        ):
            raise ValueError("session cleanup record is incomplete")
        return self

    @classmethod
    def create(
        cls,
        *,
        kind: DurableCleanupRecordKind,
        not_before: datetime,
        run_id: str | None = None,
        run_id_hash: str | None = None,
        session_id_hash: str | None = None,
        retention_class: str | None = None,
        content_ref: ContentRefV1 | None = None,
        document_key: str | None = None,
        page_hash: str | None = None,
        tombstone_seconds: int | None = None,
    ) -> DurableCleanupRecordV1:
        """Create one cleanup record with an integrity-bound identifier."""
        normalized_not_before = _as_utc(not_before, "not_before")
        unsigned = cls.model_construct(
            record_id="0" * 64,
            kind=kind,
            not_before=normalized_not_before,
            run_id=run_id,
            run_id_hash=run_id_hash,
            session_id_hash=session_id_hash,
            retention_class=retention_class,
            content_ref=content_ref,
            document_key=document_key,
            page_hash=page_hash,
            tombstone_seconds=tombstone_seconds,
        )
        values = unsigned.model_dump(mode="json", exclude={"record_id"})
        return cls(
            record_id=_cleanup_record_hash(values),
            kind=kind,
            not_before=normalized_not_before,
            run_id=run_id,
            run_id_hash=run_id_hash,
            session_id_hash=session_id_hash,
            retention_class=retention_class,
            content_ref=content_ref,
            document_key=document_key,
            page_hash=page_hash,
            tombstone_seconds=tombstone_seconds,
        )


class DurableCleanupBucketPageV1(BaseModel):
    """One bounded CAS page of exact cleanup records."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    bucket: Annotated[str, Field(pattern=r"^[0-9]{10}$")]
    page_index: Annotated[int, Field(ge=0)]
    records: Annotated[
        tuple[DurableCleanupRecordV1, ...],
        Field(min_length=1, max_length=MAX_RUN_ARTIFACTS_PER_PAGE),
    ]
    previous_page_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    page_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def validate_page(self) -> DurableCleanupBucketPageV1:
        if self.page_hash != _cleanup_page_hash(
            bucket=self.bucket,
            page_index=self.page_index,
            records=self.records,
            previous_page_hash=self.previous_page_hash,
        ):
            raise ValueError("cleanup bucket page hash mismatch")
        record_ids = [record.record_id for record in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("cleanup bucket page records must be unique")
        return self

    @classmethod
    def create(
        cls,
        *,
        bucket: str,
        page_index: int,
        records: tuple[DurableCleanupRecordV1, ...],
        previous_page_hash: str | None,
    ) -> DurableCleanupBucketPageV1:
        """Create one hash-linked cleanup bucket page."""
        return cls(
            bucket=bucket,
            page_index=page_index,
            records=records,
            previous_page_hash=previous_page_hash,
            page_hash=_cleanup_page_hash(
                bucket=bucket,
                page_index=page_index,
                records=records,
                previous_page_hash=previous_page_hash,
            ),
        )


class DurableCleanupCursorV1(BaseModel):
    """Persistent exact-bucket cursor for bounded cleanup catch-up."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    next_bucket: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_cursor(self) -> DurableCleanupCursorV1:
        next_bucket = _as_utc(self.next_bucket, "next_bucket")
        updated_at = _as_utc(self.updated_at, "updated_at")
        if next_bucket != _cleanup_bucket_start(next_bucket):
            raise ValueError("cleanup cursor must point to an hourly bucket")
        if next_bucket > _cleanup_bucket_start(updated_at) + timedelta(hours=1):
            raise ValueError("cleanup cursor is ahead of its update window")
        return self


@runtime_checkable
class DurableRetentionContentStore(Protocol):
    """Immutable content operations required by indexed retention."""

    async def put_bytes(
        self,
        *,
        kind: str,
        payload: bytes,
        media_type: str,
        retention_class: str,
    ) -> ContentRefV1:
        """Commit one exact immutable object."""

    async def get_bytes(self, reference: ContentRefV1) -> bytes:
        """Read and verify one exact immutable object."""

    async def delete_bytes(self, reference: ContentRefV1) -> bool:
        """Delete one exact immutable object."""


@dataclass(frozen=True, slots=True)
class DurableRetentionAdmission:
    """A keyed admission receipt plus its replay disposition."""

    receipt: DurableAdmissionReceiptV1
    replayed: bool


@dataclass(frozen=True, slots=True)
class DurableRunAccessRecord:
    """Owner/access binding resolved before any public run resource is read."""

    visibility: DurableRunVisibility
    owner_hash: str
    access_namespace_hash: str
    header: DurableRunResourceHeaderV1 | None = None
    tombstone: DurableRunTombstoneV1 | None = None


@dataclass(frozen=True, slots=True)
class DurableRetentionCleanupResult:
    """Bounded counts from one exact cleanup pass."""

    examined: int = 0
    released_references: int = 0
    deleted_objects: int = 0
    deleted_documents: int = 0
    legacy_excluded: bool = False


def is_retention_indexed_identity(
    identity: DurableRunIdentityV1,
) -> TypeGuard[DurableRunIdentityV2]:
    """Return whether admission explicitly opted into indexed retention."""
    return (
        isinstance(identity, DurableRunIdentityV2)
        and identity.orchestration_version == DURABLE_LOOP_ORCHESTRATOR_V4_NAME
    )


def retained_identifier_hash(kind: str, value: str) -> str:
    """Hash a public identifier for keyed retention records."""
    return canonical_hash({"kind": kind, "value": value})


def compute_resource_expiry(
    terminal_at: datetime,
    policy: DurableRetentionPolicyV1,
) -> DurableResourceExpiryV1:
    """Compute immutable absolute deadlines from a frozen retention policy."""
    terminal = _as_utc(terminal_at, "terminal_at")
    public_expiry = terminal + timedelta(seconds=policy.event_result_seconds)
    receipt_expiry = terminal + timedelta(seconds=policy.receipt_seconds)
    tombstone_expiry = terminal + timedelta(seconds=policy.tombstone_seconds)
    return DurableResourceExpiryV1(
        events_expires_at=public_expiry,
        result_expires_at=public_expiry,
        human_content_expires_at=terminal
        + timedelta(seconds=policy.human_content_seconds),
        receipts_expire_at=receipt_expiry,
        skills_expire_at=public_expiry
        + timedelta(seconds=policy.skill_grace_seconds),
        tombstone_expires_at=tombstone_expiry,
        idempotency_expires_at=tombstone_expiry,
    )


def durable_history_purge_allowed(
    header: DurableRunResourceHeaderV1,
    *,
    now: datetime,
) -> bool:
    """Return whether terminal history is past every retained recovery deadline."""
    if header.status not in _TERMINAL_STATUSES or header.expiry is None:
        return False
    current = _as_utc(now, "now")
    return current >= max(
        _as_utc(header.expiry.receipts_expire_at, "receipts_expires_at"),
        _as_utc(header.expiry.skills_expire_at, "skills_expires_at"),
        _as_utc(header.expiry.tombstone_expires_at, "tombstone_expires_at"),
    )


class DurableRetentionManager:
    """CAS coordinator for retained runs, objects, sessions, and cleanup indices."""

    def __init__(
        self,
        documents: DurableKeyedDocumentStore,
        content: DurableRetentionContentStore,
        *,
        object_shards: int = _DEFAULT_OBJECT_SHARDS,
        pending_safety_seconds: int = _DEFAULT_PENDING_SAFETY_SECONDS,
        maximum_cas_attempts: int = _MAX_CAS_ATTEMPTS,
    ) -> None:
        if object_shards < 2 or object_shards > 256:
            raise ValueError("object shard count must be between 2 and 256")
        if pending_safety_seconds < 60 or pending_safety_seconds > 24 * 60 * 60:
            raise ValueError("pending safety window must be between 60 seconds and one day")
        if maximum_cas_attempts < 1 or maximum_cas_attempts > 256:
            raise ValueError("CAS attempt limit must be between 1 and 256")
        self._documents = documents
        self._content = content
        self._object_shards = object_shards
        self._pending_safety = timedelta(seconds=pending_safety_seconds)
        self._maximum_cas_attempts = maximum_cas_attempts

    async def admit_run_resource(
        self,
        identity: DurableRunIdentityV1,
    ) -> tuple[DurableRunResourceHeaderV1, DurableRetentionAdmission]:
        """Create or replay the keyed receipt and indexed V4 run root."""
        if not is_retention_indexed_identity(identity):
            raise DurableRetentionLegacyExcludedError(
                "legacy durable identity is not indexed for automatic cleanup"
            )
        admission = await self.reserve_admission_receipt(identity)
        header = await self.create_run_resource(identity)
        return header, admission

    async def create_run_resource(
        self,
        identity: DurableRunIdentityV2,
    ) -> DurableRunResourceHeaderV1:
        """Create one immutable-identity run root without overwriting a replay."""
        if not is_retention_indexed_identity(identity):
            raise DurableRetentionLegacyExcludedError(
                "legacy durable identity is not indexed for automatic cleanup"
            )
        header = DurableRunResourceHeaderV1(
            run_id=identity.run_id,
            session_id=identity.session_id,
            owner_hash=identity.owner_hash,
            access_namespace_hash=identity.access_namespace_hash,
            request_id_hash=identity.request_id_hash,
            status=DurableLoopRunStatus.PENDING,
            retention_policy=identity.retention_policy,
            created_at=identity.created_at,
            updated_at=identity.created_at,
            active_deadline=identity.active_deadline,
            record_version=1,
        )
        key = _run_header_key(identity.run_id)
        payload = canonical_json_bytes(header)
        if await self._documents.create(key, payload):
            return header
        existing = await self._documents.get(key)
        if existing is None:
            raise DurableRetentionError("run resource header disappeared after create race")
        observed = DurableRunResourceHeaderV1.model_validate_json(existing.payload)
        if observed != header:
            raise DurableRetentionConflictError("run resource header conflicts")
        return observed

    async def get_run_resource(
        self,
        run_id: str,
    ) -> tuple[DurableRunResourceHeaderV1, str] | None:
        """Read one indexed run root and its CAS revision."""
        document = await self._documents.get(_run_header_key(run_id))
        if document is None:
            return None
        return (
            DurableRunResourceHeaderV1.model_validate_json(document.payload),
            document.revision,
        )

    async def reserve_admission_receipt(
        self,
        identity: DurableRunIdentityV2,
    ) -> DurableRetentionAdmission:
        """Reserve a long-lived admission key before touching the bounded entity."""
        if not is_retention_indexed_identity(identity):
            raise DurableRetentionLegacyExcludedError(
                "legacy durable identity has no long-lived admission receipt"
            )
        receipt = DurableAdmissionReceiptV1(
            request_id_hash=identity.request_id_hash,
            request_hash=identity.request_hash,
            run_id=identity.run_id,
            session_id=identity.session_id,
            owner_hash=identity.owner_hash,
            access_namespace_hash=identity.access_namespace_hash,
            state=DurableAdmissionReceiptState.ACTIVE,
            created_at=identity.created_at,
            updated_at=identity.created_at,
            expires_at=identity.active_deadline,
            record_version=1,
        )
        key = _admission_receipt_key(
            identity.owner_hash,
            identity.request_id_hash,
        )
        if await self._documents.create(key, canonical_json_bytes(receipt)):
            return DurableRetentionAdmission(receipt=receipt, replayed=False)
        document = await self._documents.get(key)
        if document is None:
            raise DurableRetentionError("admission receipt disappeared after create race")
        observed = DurableAdmissionReceiptV1.model_validate_json(document.payload)
        if (
            observed.request_hash != receipt.request_hash
            or observed.run_id != receipt.run_id
            or observed.session_id != receipt.session_id
            or observed.access_namespace_hash != receipt.access_namespace_hash
        ):
            raise DurableRetentionConflictError(
                "admission key was reused with different request content"
            )
        return DurableRetentionAdmission(receipt=observed, replayed=True)

    async def read_admission_receipt(
        self,
        *,
        owner_hash: str,
        request_id_hash: str,
    ) -> tuple[DurableAdmissionReceiptV1, str] | None:
        """Read one keyed long-lived admission receipt."""
        document = await self._documents.get(
            _admission_receipt_key(owner_hash, request_id_hash)
        )
        if document is None:
            return None
        return (
            DurableAdmissionReceiptV1.model_validate_json(document.payload),
            document.revision,
        )

    async def tracked_put(
        self,
        *,
        run_id: str,
        kind: str,
        payload: bytes,
        media_type: str,
        retention_class: str,
        expires_at: datetime,
        now: datetime,
    ) -> ContentRefV1:
        """Acquire, upload, index, promote, and post-verify one run artifact."""
        _validate_content_kind(kind)
        current = _as_utc(now, "now")
        deadline = _as_utc(expires_at, "expires_at")
        if deadline <= current:
            raise ValueError("tracked content expiry must be in the future")
        run = await self.get_run_resource(run_id)
        if run is None:
            raise DurableRetentionLegacyExcludedError(
                "run has no indexed retention resource"
            )
        run_id_hash = retained_identifier_hash("run", run_id)
        digest = hashlib.sha256(payload).hexdigest()
        object_id = canonical_content_object_id(kind, digest)
        pending_until = current + self._pending_safety
        pending_ref = ContentRefV1(
            object_id=object_id,
            sha256=digest,
            byte_length=len(payload),
            media_type=media_type,
            encryption_version="retention-pending-v1",
            retention_class=retention_class,
        )
        await self.schedule_cleanup(
            DurableCleanupRecordV1.create(
                kind=DurableCleanupRecordKind.OBJECT_REFERENCE,
                not_before=pending_until,
                run_id_hash=run_id_hash,
                retention_class=retention_class,
                content_ref=pending_ref,
            )
        )
        for _ in range(self._maximum_cas_attempts):
            await self.acquire_pending_reference(
                object_kind=kind,
                object_digest=digest,
                run_id_hash=run_id_hash,
                retention_class=retention_class,
                pending_until=pending_until,
                now=current,
            )
            reference = await self._content.put_bytes(
                kind=kind,
                payload=payload,
                media_type=media_type,
                retention_class=retention_class,
            )
            _assert_committed_reference(
                reference,
                object_id=object_id,
                digest=digest,
                byte_length=len(payload),
                media_type=media_type,
                retention_class=retention_class,
            )
            try:
                await self._content.get_bytes(reference)
            except DurableLoopContentError:
                await asyncio.sleep(0)
                continue
            artifact = DurableRunArtifactV1(
                kind=kind,
                retention_class=retention_class,
                content_ref=reference,
                expires_at=deadline,
            )
            await self.append_run_artifact(run_id, artifact, now=current)
            await self.promote_reference_live(
                object_kind=kind,
                object_digest=digest,
                run_id_hash=run_id_hash,
                retention_class=retention_class,
                deadline=deadline,
                now=current,
            )
            if await self.verify_live_reference(
                object_kind=kind,
                object_digest=digest,
                run_id_hash=run_id_hash,
                retention_class=retention_class,
                now=current,
            ):
                await self.schedule_cleanup(
                    DurableCleanupRecordV1.create(
                        kind=DurableCleanupRecordKind.OBJECT_REFERENCE,
                        not_before=deadline,
                        run_id_hash=run_id_hash,
                        retention_class=retention_class,
                        content_ref=reference,
                    )
                )
                return reference
            await asyncio.sleep(0)
        raise DurableRetentionBusyError(
            "tracked content could not clear a concurrent deletion fence"
        )

    async def append_keyed_artifact(
        self,
        *,
        run_id: str,
        kind: str,
        retention_class: str,
        document_key: str,
        expires_at: datetime,
        now: datetime,
    ) -> DurableRunResourceHeaderV1:
        """Index one exact keyed document for later owner-aware cleanup."""
        artifact = DurableRunArtifactV1(
            kind=kind,
            retention_class=retention_class,
            keyed_document_name=document_key,
            expires_at=_as_utc(expires_at, "expires_at"),
        )
        return await self.append_run_artifact(run_id, artifact, now=now)

    async def append_run_artifact(
        self,
        run_id: str,
        artifact: DurableRunArtifactV1,
        *,
        now: datetime,
    ) -> DurableRunResourceHeaderV1:
        """Append one immutable bounded page and CAS its run head."""
        current = _as_utc(now, "now")
        for _ in range(self._maximum_cas_attempts):
            loaded = await self.get_run_resource(run_id)
            if loaded is None:
                raise DurableRetentionLegacyExcludedError(
                    "run has no indexed retention resource"
                )
            header, revision = loaded
            if await self._artifact_is_indexed(header, artifact):
                return header
            page = _create_artifact_page(header, artifact)
            page_key = _artifact_page_key(run_id, page.page_hash)
            await self.schedule_cleanup(
                DurableCleanupRecordV1.create(
                    kind=DurableCleanupRecordKind.ARTIFACT_PAGE_CANDIDATE,
                    not_before=current + self._pending_safety,
                    run_id=run_id,
                    document_key=page_key,
                    page_hash=page.page_hash,
                )
            )
            payload = canonical_json_bytes(page)
            if not await self._documents.create(page_key, payload):
                existing = await self._documents.get(page_key)
                if existing is None or existing.payload != payload:
                    raise DurableRetentionConflictError(
                        "artifact page hash conflicts"
                    )
            replacement = header.model_copy(
                update={
                    "artifact_page_count": header.artifact_page_count + 1,
                    "artifact_page_head_hash": page.page_hash,
                    "record_version": header.record_version + 1,
                    "updated_at": max(
                        _as_utc(header.updated_at, "updated_at"),
                        current,
                    ),
                }
            )
            if await self._documents.replace(
                _run_header_key(run_id),
                canonical_json_bytes(replacement),
                revision=revision,
            ):
                return replacement
        raise DurableRetentionBusyError("run artifact index CAS retry limit exceeded")

    async def terminalize_run(
        self,
        *,
        run_id: str,
        status: DurableLoopRunStatus,
        terminal_at: datetime,
        projection: DurablePublicStatusV1,
    ) -> DurableRunResourceHeaderV1:
        """Persist the immutable terminal projection, expiry, receipt, and tombstone."""
        if status not in _TERMINAL_STATUSES:
            raise ValueError("terminal run status is required")
        terminal = _as_utc(terminal_at, "terminal_at")
        loaded = await self.get_run_resource(run_id)
        if loaded is None:
            raise DurableRetentionLegacyExcludedError(
                "run has no indexed retention resource"
            )
        initial, _ = loaded
        expiry = compute_resource_expiry(terminal, initial.retention_policy)
        if projection.run_id != run_id or projection.status is not status:
            raise DurableRetentionConflictError(
                "terminal projection does not match the run disposition"
            )
        if (
            projection.resource_expiry is not None
            and projection.resource_expiry != expiry
        ):
            raise DurableRetentionConflictError(
                "terminal projection has conflicting expiry"
            )
        retained_projection = projection.model_copy(
            update={"resource_expiry": expiry}
        )
        projection_ref = await self.tracked_put(
            run_id=run_id,
            kind="terminal-projection",
            payload=canonical_json_bytes(retained_projection),
            media_type="application/json",
            retention_class="result",
            expires_at=expiry.result_expires_at,
            now=terminal,
        )
        for _ in range(self._maximum_cas_attempts):
            loaded = await self.get_run_resource(run_id)
            if loaded is None:
                raise DurableRetentionError(
                    "run resource disappeared during terminalization"
                )
            header, revision = loaded
            if header.status in _TERMINAL_STATUSES:
                if (
                    header.status is status
                    and header.terminal_at == terminal
                    and header.terminal_projection_ref == projection_ref
                    and header.expiry == expiry
                ):
                    await self._finish_terminalization(header)
                    return header
                raise DurableRetentionConflictError(
                    "run has a conflicting terminal projection"
                )
            replacement = header.model_copy(
                update={
                    "expiry": expiry,
                    "record_version": header.record_version + 1,
                    "status": status,
                    "terminal_at": terminal,
                    "terminal_projection_ref": projection_ref,
                    "updated_at": max(
                        _as_utc(header.updated_at, "updated_at"),
                        terminal,
                    ),
                }
            )
            if await self._documents.replace(
                _run_header_key(run_id),
                canonical_json_bytes(replacement),
                revision=revision,
            ):
                await self._finish_terminalization(replacement)
                return replacement
        raise DurableRetentionBusyError("run terminalization CAS retry limit exceeded")

    async def read_terminal_projection(
        self,
        run_id: str,
        *,
        now: datetime,
    ) -> DurablePublicStatusV1:
        """Read the retained terminal projection until its explicit result expiry."""
        loaded = await self.get_run_resource(run_id)
        if loaded is None:
            raise DurableRetentionError("run resource was not found")
        header, _ = loaded
        if (
            header.status not in _TERMINAL_STATUSES
            or header.expiry is None
            or header.terminal_projection_ref is None
        ):
            raise DurableRetentionError("run has no terminal projection")
        if _as_utc(now, "now") >= _as_utc(
            header.expiry.result_expires_at,
            "result_expires_at",
        ):
            raise DurableRetentionExpiredError("result_expired")
        payload = await self._content.get_bytes(header.terminal_projection_ref)
        return DurablePublicStatusV1.model_validate_json(payload)

    async def run_visibility(
        self,
        run_id: str,
        *,
        now: datetime,
    ) -> DurableRunVisibility:
        """Resolve indexed live, terminal, expired, or absent visibility."""
        current = _as_utc(now, "now")
        loaded = await self.get_run_resource(run_id)
        if loaded is not None:
            header, _ = loaded
            if header.status not in _TERMINAL_STATUSES:
                return DurableRunVisibility.ACTIVE
            if header.expiry is None:
                raise DurableRetentionError("terminal run has no explicit expiry")
            if current < _as_utc(
                header.expiry.result_expires_at,
                "result_expires_at",
            ):
                return DurableRunVisibility.TERMINAL
        tombstone = await self._read_run_tombstone(run_id)
        if tombstone is None:
            return DurableRunVisibility.NOT_FOUND
        if current < _as_utc(
            tombstone[0].tombstone_expires_at,
            "tombstone_expires_at",
        ):
            return DurableRunVisibility.EXPIRED
        return DurableRunVisibility.NOT_FOUND

    async def get_run_access_record(
        self,
        run_id: str,
        *,
        now: datetime,
    ) -> DurableRunAccessRecord | None:
        """Resolve the retained owner/access binding without reading run content."""
        current = _as_utc(now, "now")
        loaded = await self.get_run_resource(run_id)
        if loaded is not None:
            header, _ = loaded
            visibility = DurableRunVisibility.ACTIVE
            if header.status in _TERMINAL_STATUSES:
                if header.expiry is None:
                    raise DurableRetentionError("terminal run has no explicit expiry")
                if current >= _as_utc(
                    header.expiry.tombstone_expires_at,
                    "tombstone_expires_at",
                ):
                    return None
                visibility = (
                    DurableRunVisibility.TERMINAL
                    if current
                    < max(
                        _as_utc(
                            header.expiry.events_expires_at,
                            "events_expires_at",
                        ),
                        _as_utc(
                            header.expiry.result_expires_at,
                            "result_expires_at",
                        ),
                    )
                    else DurableRunVisibility.EXPIRED
                )
            return DurableRunAccessRecord(
                visibility=visibility,
                owner_hash=header.owner_hash,
                access_namespace_hash=header.access_namespace_hash,
                header=header,
            )
        retained = await self._read_run_tombstone(run_id)
        if retained is None:
            return None
        tombstone, _ = retained
        if current >= _as_utc(
            tombstone.tombstone_expires_at,
            "tombstone_expires_at",
        ):
            return None
        return DurableRunAccessRecord(
            visibility=DurableRunVisibility.EXPIRED,
            owner_hash=tombstone.owner_hash,
            access_namespace_hash=tombstone.access_namespace_hash,
            tombstone=tombstone,
        )

    async def tracked_put_for_resource(
        self,
        *,
        resource_hash: str,
        kind: str,
        payload: bytes,
        media_type: str,
        retention_class: str,
        expires_at: datetime,
        now: datetime,
    ) -> ContentRefV1:
        """Track exact content for a non-run resource such as staged admission."""
        if _SHA256.fullmatch(resource_hash) is None:
            raise ValueError("resource hash must be a SHA-256 digest")
        _validate_content_kind(kind)
        current = _as_utc(now, "now")
        deadline = _as_utc(expires_at, "expires_at")
        if deadline <= current:
            raise ValueError("tracked resource expiry must be in the future")
        return await self._tracked_put_for_resource(
            resource_hash=resource_hash,
            kind=kind,
            payload=payload,
            media_type=media_type,
            retention_class=retention_class,
            expires_at=deadline,
            now=current,
        )

    async def provisional_run_artifact_expiry(
        self,
        run_id: str,
        *,
        retention_class: str,
    ) -> datetime:
        """Return a bounded pre-terminal deadline from the frozen run policy."""
        loaded = await self.get_run_resource(run_id)
        if loaded is None:
            raise DurableRetentionLegacyExcludedError(
                "run has no indexed retention resource"
            )
        header, _ = loaded
        policy = header.retention_policy
        extra_seconds = {
            "event": policy.event_result_seconds,
            "human": policy.human_content_seconds,
            "receipt": policy.receipt_seconds,
            "result": policy.event_result_seconds,
            "run": policy.receipt_seconds,
            "skill": policy.event_result_seconds + policy.skill_grace_seconds,
            "trigger_pending": policy.trigger_admission_deadline_seconds,
        }.get(retention_class, policy.receipt_seconds)
        return _as_utc(header.active_deadline, "active_deadline") + timedelta(
            seconds=extra_seconds
        )

    async def purgeable_run_ids(
        self,
        bucket_time: datetime,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        """Return exact indexed run IDs whose Durable history may be purged."""
        current = _as_utc(now, "now")
        run_ids: list[str] = []
        for record in await self.read_cleanup_bucket(bucket_time):
            if record.kind is not DurableCleanupRecordKind.RUN or record.run_id is None:
                continue
            loaded = await self.get_run_resource(record.run_id)
            if loaded is None or not durable_history_purge_allowed(
                loaded[0],
                now=current,
            ):
                continue
            if record.run_id not in run_ids:
                run_ids.append(record.run_id)
        return tuple(run_ids)

    async def acquire_pending_reference(
        self,
        *,
        object_kind: str,
        object_digest: str,
        run_id_hash: str,
        retention_class: str,
        pending_until: datetime,
        now: datetime,
    ) -> None:
        """CAS one pending shard entry while respecting an object deletion fence."""
        _validate_object_identity(object_kind, object_digest)
        current = _as_utc(now, "now")
        deadline = _as_utc(pending_until, "pending_until")
        if deadline <= current:
            raise ValueError("pending reference deadline must be in the future")
        for _ in range(self._maximum_cas_attempts):
            root, root_revision = await self._load_or_create_object_root(
                object_kind,
                object_digest,
                now=current,
            )
            if root.state is DurableObjectReferenceState.DELETING:
                await asyncio.sleep(0)
                continue
            await self._upsert_object_run_reference(
                object_kind=object_kind,
                object_digest=object_digest,
                run_id_hash=run_id_hash,
                retention_class=retention_class,
                deadline=deadline,
                pending=True,
                now=current,
            )
            state = await self._aggregate_object_state(
                object_kind,
                object_digest,
            )
            refreshed = root.model_copy(
                update={
                    "record_version": root.record_version + 1,
                    "state": state,
                    "updated_at": current,
                }
            )
            if await self._documents.replace(
                _object_root_key(object_kind, object_digest),
                canonical_json_bytes(refreshed),
                revision=root_revision,
            ):
                return
        raise DurableRetentionBusyError(
            "pending object reference could not clear a deletion fence"
        )

    async def promote_reference_live(
        self,
        *,
        object_kind: str,
        object_digest: str,
        run_id_hash: str,
        retention_class: str,
        deadline: datetime,
        now: datetime,
    ) -> None:
        """Promote one pending shard entry and CAS-touch its object root."""
        current = _as_utc(now, "now")
        live_deadline = _as_utc(deadline, "deadline")
        for _ in range(self._maximum_cas_attempts):
            root_document = await self._documents.get(
                _object_root_key(object_kind, object_digest)
            )
            if root_document is None:
                raise DurableRetentionError("object reference root was not found")
            root = _parse_object_reference(
                root_document,
                object_kind=object_kind,
                object_digest=object_digest,
            )
            if root.state is DurableObjectReferenceState.DELETING:
                await asyncio.sleep(0)
                continue
            await self._upsert_object_run_reference(
                object_kind=object_kind,
                object_digest=object_digest,
                run_id_hash=run_id_hash,
                retention_class=retention_class,
                deadline=live_deadline,
                pending=False,
                now=current,
            )
            refreshed = root.model_copy(
                update={
                    "record_version": root.record_version + 1,
                    "state": DurableObjectReferenceState.LIVE,
                    "updated_at": current,
                }
            )
            if await self._documents.replace(
                _object_root_key(object_kind, object_digest),
                canonical_json_bytes(refreshed),
                revision=root_document.revision,
            ):
                return
        raise DurableRetentionBusyError(
            "live object reference could not clear a deletion fence"
        )

    async def verify_live_reference(
        self,
        *,
        object_kind: str,
        object_digest: str,
        run_id_hash: str,
        retention_class: str,
        now: datetime,
    ) -> bool:
        """Post-verify that no deletion fence can race publication."""
        root_document = await self._documents.get(
            _object_root_key(object_kind, object_digest)
        )
        if root_document is None:
            return False
        root = _parse_object_reference(
            root_document,
            object_kind=object_kind,
            object_digest=object_digest,
        )
        if root.state is DurableObjectReferenceState.DELETING:
            return False
        shard = await self._read_object_shard(
            object_kind,
            object_digest,
            run_id_hash,
        )
        if shard is None:
            return False
        current = _as_utc(now, "now")
        return any(
            reference.run_id_hash == run_id_hash
            and reference.retention_class == retention_class
            and not reference.pending
            and _as_utc(reference.deadline, "deadline") > current
            for reference in shard[0].references
        )

    async def cleanup_object_reference(
        self,
        reference: ContentRefV1,
        *,
        run_id_hash: str,
        retention_class: str,
        now: datetime,
    ) -> tuple[bool, bool]:
        """Release one due shard entry and delete only an unreferenced exact object."""
        object_kind, object_digest = _canonical_reference_identity(reference)
        current = _as_utc(now, "now")
        released = await self._remove_due_object_run_reference(
            object_kind=object_kind,
            object_digest=object_digest,
            run_id_hash=run_id_hash,
            retention_class=retention_class,
            now=current,
        )
        deleted = await self._try_delete_unreferenced_object(
            reference,
            object_kind=object_kind,
            object_digest=object_digest,
            now=current,
        )
        return released, deleted

    async def put_session_context(
        self,
        *,
        session_id_hash: str,
        owner_hash: str,
        access_namespace_hash: str,
        active_run_id_hash: str,
        committed_generation: int,
        payload: bytes,
        media_type: str,
        committed_at: datetime,
        retention_seconds: int,
        tombstone_seconds: int,
    ) -> DurableSessionResourceV1:
        """Commit and renew one session context, clearing its active-run fence."""
        committed = _as_utc(committed_at, "committed_at")
        expires = committed + timedelta(seconds=retention_seconds)
        reference = await self._tracked_put_for_resource(
            resource_hash=session_id_hash,
            kind="session-context",
            payload=payload,
            media_type=media_type,
            retention_class="session",
            expires_at=expires,
            now=committed,
        )
        key = _session_resource_key(session_id_hash)
        for _ in range(self._maximum_cas_attempts):
            document = await self._documents.get(key)
            if document is None:
                resource = DurableSessionResourceV1(
                    session_id_hash=session_id_hash,
                    owner_hash=owner_hash,
                    access_namespace_hash=access_namespace_hash,
                    committed_generation=committed_generation,
                    context_ref=reference,
                    renewed_at=committed,
                    expires_at=expires,
                    record_version=1,
                )
                if await self._documents.create(
                    key,
                    canonical_json_bytes(resource),
                ):
                    await self._schedule_session_cleanup(
                        resource,
                        tombstone_seconds=tombstone_seconds,
                    )
                    return resource
                continue
            current = DurableSessionResourceV1.model_validate_json(document.payload)
            _assert_session_owner(
                current,
                owner_hash=owner_hash,
                access_namespace_hash=access_namespace_hash,
            )
            if (
                current.active_run_id_hash is None
                and current.committed_generation == committed_generation
                and current.context_ref == reference
            ):
                return current
            if current.active_run_id_hash != active_run_id_hash:
                raise DurableSessionBusyError(
                    "session commit does not own the active-run fence"
                )
            if committed_generation <= current.committed_generation:
                raise DurableRetentionConflictError(
                    "session generation did not advance"
                )
            replacement = current.model_copy(
                update={
                    "active_run_id_hash": None,
                    "committed_generation": committed_generation,
                    "context_ref": reference,
                    "expires_at": expires,
                    "record_version": current.record_version + 1,
                    "renewed_at": committed,
                }
            )
            if await self._documents.replace(
                key,
                canonical_json_bytes(replacement),
                revision=document.revision,
            ):
                await self.cleanup_object_reference(
                    current.context_ref,
                    run_id_hash=session_id_hash,
                    retention_class="session",
                    now=committed,
                )
                await self._schedule_session_cleanup(
                    replacement,
                    tombstone_seconds=tombstone_seconds,
                )
                return replacement
        raise DurableRetentionBusyError("session commit CAS retry limit exceeded")

    async def acquire_session_admission(
        self,
        *,
        session_id_hash: str,
        owner_hash: str,
        access_namespace_hash: str,
        run_id_hash: str,
        now: datetime,
    ) -> DurableSessionResourceV1 | None:
        """Fence an existing retained session against cleanup and another run."""
        current_time = _as_utc(now, "now")
        tombstone = await self._read_session_tombstone(session_id_hash)
        if tombstone is not None:
            retained, revision = tombstone
            if current_time < _as_utc(
                retained.tombstone_expires_at,
                "tombstone_expires_at",
            ):
                _assert_session_tombstone_owner(
                    retained,
                    owner_hash=owner_hash,
                    access_namespace_hash=access_namespace_hash,
                )
                raise DurableSessionExpiredError("session_expired")
            await self._documents.delete(
                _session_tombstone_key(session_id_hash),
                revision=revision,
            )
        key = _session_resource_key(session_id_hash)
        for _ in range(self._maximum_cas_attempts):
            document = await self._documents.get(key)
            if document is None:
                return None
            resource = DurableSessionResourceV1.model_validate_json(document.payload)
            _assert_session_owner(
                resource,
                owner_hash=owner_hash,
                access_namespace_hash=access_namespace_hash,
            )
            if resource.active_run_id_hash == run_id_hash:
                return resource
            if resource.active_run_id_hash is not None:
                raise DurableSessionBusyError("session has an active run")
            if current_time >= _as_utc(resource.expires_at, "expires_at"):
                raise DurableSessionExpiredError("session_expired")
            replacement = resource.model_copy(
                update={
                    "active_run_id_hash": run_id_hash,
                    "record_version": resource.record_version + 1,
                }
            )
            if await self._documents.replace(
                key,
                canonical_json_bytes(replacement),
                revision=document.revision,
            ):
                return replacement
        raise DurableRetentionBusyError("session admission CAS retry limit exceeded")

    async def release_session_admission(
        self,
        *,
        session_id_hash: str,
        run_id_hash: str,
    ) -> DurableSessionResourceV1 | None:
        """Release an active session fence without renewing committed context."""
        key = _session_resource_key(session_id_hash)
        for _ in range(self._maximum_cas_attempts):
            document = await self._documents.get(key)
            if document is None:
                return None
            resource = DurableSessionResourceV1.model_validate_json(document.payload)
            if resource.active_run_id_hash is None:
                return resource
            if resource.active_run_id_hash != run_id_hash:
                raise DurableSessionBusyError("session fence is owned by another run")
            replacement = resource.model_copy(
                update={
                    "active_run_id_hash": None,
                    "record_version": resource.record_version + 1,
                }
            )
            if await self._documents.replace(
                key,
                canonical_json_bytes(replacement),
                revision=document.revision,
            ):
                return replacement
        raise DurableRetentionBusyError("session release CAS retry limit exceeded")

    async def expire_session(
        self,
        session_id_hash: str,
        *,
        now: datetime,
        tombstone_seconds: int,
    ) -> bool:
        """Fence, tombstone, and remove one expired inactive session exactly."""
        current_time = _as_utc(now, "now")
        key = _session_resource_key(session_id_hash)
        for _ in range(self._maximum_cas_attempts):
            document = await self._documents.get(key)
            if document is None:
                retained_tombstone = await self._read_session_tombstone(
                    session_id_hash
                )
                if retained_tombstone is None:
                    return False
                tombstone, revision = retained_tombstone
                if current_time >= _as_utc(
                    tombstone.tombstone_expires_at,
                    "tombstone_expires_at",
                ):
                    await self._documents.delete(
                        _session_tombstone_key(session_id_hash),
                        revision=revision,
                    )
                return True
            resource = DurableSessionResourceV1.model_validate_json(document.payload)
            if current_time < _as_utc(resource.expires_at, "expires_at"):
                return False
            if resource.active_run_id_hash not in {None, _SESSION_CLEANUP_FENCE}:
                return False
            fenced = resource
            fenced_revision = document.revision
            if resource.active_run_id_hash is None:
                fenced = resource.model_copy(
                    update={
                        "active_run_id_hash": _SESSION_CLEANUP_FENCE,
                        "record_version": resource.record_version + 1,
                    }
                )
                if not await self._documents.replace(
                    key,
                    canonical_json_bytes(fenced),
                    revision=document.revision,
                ):
                    continue
                refreshed = await self._documents.get(key)
                if refreshed is None:
                    continue
                fenced_revision = refreshed.revision
            tombstone = DurableSessionTombstoneV1(
                session_id_hash=session_id_hash,
                owner_hash=fenced.owner_hash,
                access_namespace_hash=fenced.access_namespace_hash,
                expired_at=fenced.expires_at,
                tombstone_expires_at=_as_utc(
                    fenced.expires_at,
                    "expires_at",
                )
                + timedelta(seconds=tombstone_seconds),
            )
            await self._create_or_verify(
                _session_tombstone_key(session_id_hash),
                tombstone,
                DurableSessionTombstoneV1,
            )
            await self.schedule_cleanup(
                DurableCleanupRecordV1.create(
                    kind=DurableCleanupRecordKind.SESSION,
                    not_before=tombstone.tombstone_expires_at,
                    session_id_hash=session_id_hash,
                    tombstone_seconds=tombstone_seconds,
                )
            )
            if not await self._documents.delete(key, revision=fenced_revision):
                continue
            await self.cleanup_object_reference(
                fenced.context_ref,
                run_id_hash=session_id_hash,
                retention_class="session",
                now=current_time,
            )
            return True
        raise DurableRetentionBusyError("session expiry CAS retry limit exceeded")

    async def schedule_cleanup(self, record: DurableCleanupRecordV1) -> None:
        """Append one exact record to its deterministic hourly cleanup bucket."""
        bucket = _cleanup_bucket(record.not_before)
        head_key = _cleanup_head_key(bucket)
        for _ in range(self._maximum_cas_attempts):
            document = await self._documents.get(head_key)
            if document is None:
                page = DurableCleanupBucketPageV1.create(
                    bucket=bucket,
                    page_index=0,
                    records=(record,),
                    previous_page_hash=None,
                )
                if await self._documents.create(
                    head_key,
                    canonical_json_bytes(page),
                ):
                    return
                continue
            head = DurableCleanupBucketPageV1.model_validate_json(document.payload)
            if any(item.record_id == record.record_id for item in head.records):
                return
            if len(head.records) < MAX_RUN_ARTIFACTS_PER_PAGE:
                replacement = DurableCleanupBucketPageV1.create(
                    bucket=bucket,
                    page_index=head.page_index,
                    records=(*head.records, record),
                    previous_page_hash=head.previous_page_hash,
                )
            else:
                await self._create_or_verify(
                    _cleanup_page_key(bucket, head.page_hash),
                    head,
                    DurableCleanupBucketPageV1,
                )
                replacement = DurableCleanupBucketPageV1.create(
                    bucket=bucket,
                    page_index=head.page_index + 1,
                    records=(record,),
                    previous_page_hash=head.page_hash,
                )
            if await self._documents.replace(
                head_key,
                canonical_json_bytes(replacement),
                revision=document.revision,
            ):
                return
        raise DurableRetentionBusyError("cleanup bucket CAS retry limit exceeded")

    async def read_cleanup_bucket(
        self,
        bucket_time: datetime,
    ) -> tuple[DurableCleanupRecordV1, ...]:
        """Read one bounded hash-linked time bucket without scanning prefixes."""
        bucket = _cleanup_bucket(bucket_time)
        document = await self._documents.get(_cleanup_head_key(bucket))
        if document is None:
            return ()
        page = DurableCleanupBucketPageV1.model_validate_json(document.payload)
        records: list[DurableCleanupRecordV1] = list(page.records)
        previous = page.previous_page_hash
        expected_index = page.page_index - 1
        while previous is not None:
            prior = await self._documents.get(_cleanup_page_key(bucket, previous))
            if prior is None:
                raise DurableRetentionError("cleanup bucket chain is incomplete")
            page = DurableCleanupBucketPageV1.model_validate_json(prior.payload)
            if page.page_index != expected_index or page.page_hash != previous:
                raise DurableRetentionError("cleanup bucket chain is invalid")
            records.extend(page.records)
            previous = page.previous_page_hash
            expected_index -= 1
        return tuple(records)

    async def due_cleanup_buckets(
        self,
        *,
        now: datetime,
        limit: int = 25,
    ) -> tuple[datetime, ...]:
        """Return a bounded contiguous batch of exact buckets from the durable cursor."""
        if limit < 1 or limit > MAX_RUN_ARTIFACTS_PER_PAGE:
            raise ValueError("cleanup bucket limit is invalid")
        current = _as_utc(now, "now")
        current_bucket = _cleanup_bucket_start(current)
        for _ in range(self._maximum_cas_attempts):
            document = await self._documents.get(_CLEANUP_CURSOR_KEY)
            if document is None:
                cursor = DurableCleanupCursorV1(
                    next_bucket=current_bucket
                    - timedelta(hours=_CLEANUP_INITIAL_LOOKBACK_HOURS),
                    updated_at=current,
                )
                if await self._documents.create(
                    _CLEANUP_CURSOR_KEY,
                    canonical_json_bytes(cursor),
                ):
                    document = await self._documents.get(_CLEANUP_CURSOR_KEY)
                if document is None:
                    continue
            cursor = DurableCleanupCursorV1.model_validate_json(document.payload)
            start = _as_utc(cursor.next_bucket, "next_bucket")
            if start > current_bucket:
                return ()
            count = min(
                limit,
                int((current_bucket - start).total_seconds())
                // _CLEANUP_BUCKET_SECONDS
                + 1,
            )
            return tuple(start + timedelta(hours=index) for index in range(count))
        raise DurableRetentionBusyError("cleanup cursor CAS retry limit exceeded")

    async def advance_cleanup_cursor(
        self,
        bucket_time: datetime,
        *,
        now: datetime,
    ) -> None:
        """Advance the durable cursor after one exact bucket completes."""
        bucket = _cleanup_bucket_start(bucket_time)
        current = _as_utc(now, "now")
        for _ in range(self._maximum_cas_attempts):
            document = await self._documents.get(_CLEANUP_CURSOR_KEY)
            if document is None:
                raise DurableRetentionError("cleanup cursor is unavailable")
            cursor = DurableCleanupCursorV1.model_validate_json(document.payload)
            expected = _as_utc(cursor.next_bucket, "next_bucket")
            if expected > bucket:
                return
            if expected < bucket:
                raise DurableRetentionConflictError(
                    "cleanup cursor cannot skip an unprocessed bucket"
                )
            replacement = DurableCleanupCursorV1(
                next_bucket=bucket + timedelta(hours=1),
                updated_at=current,
            )
            if await self._documents.replace(
                _CLEANUP_CURSOR_KEY,
                canonical_json_bytes(replacement),
                revision=document.revision,
            ):
                return
        raise DurableRetentionBusyError("cleanup cursor CAS retry limit exceeded")

    async def cleanup_bucket(
        self,
        bucket_time: datetime,
        *,
        now: datetime,
        limit: int = MAX_RUN_ARTIFACTS_PER_PAGE,
    ) -> DurableRetentionCleanupResult:
        """Process a bounded number of due exact records from one known bucket."""
        if limit < 1 or limit > MAX_RUN_ARTIFACTS_PER_PAGE:
            raise ValueError("cleanup limit is invalid")
        current = _as_utc(now, "now")
        records = await self.read_cleanup_bucket(bucket_time)
        result = DurableRetentionCleanupResult()
        for record in records[:limit]:
            if current < _as_utc(record.not_before, "not_before"):
                continue
            outcome = await self._process_cleanup_record(record, now=current)
            result = _merge_cleanup_results(
                result,
                outcome,
                examined_increment=1,
            )
        return result

    async def cleanup_run(
        self,
        run_id: str,
        *,
        now: datetime,
    ) -> DurableRetentionCleanupResult:
        """Clean only artifacts named by one indexed run resource."""
        current = _as_utc(now, "now")
        loaded = await self.get_run_resource(run_id)
        if loaded is None:
            if await self._read_run_tombstone(run_id) is not None:
                return DurableRetentionCleanupResult()
            return DurableRetentionCleanupResult(legacy_excluded=True)
        header, _ = loaded
        pages = await self._artifact_pages(header)
        released = 0
        deleted_objects = 0
        deleted_documents = 0
        run_id_hash = retained_identifier_hash("run", run_id)
        for page, _ in pages:
            for artifact in page.artifacts:
                if current < _as_utc(artifact.expires_at, "expires_at"):
                    continue
                if artifact.content_ref is not None:
                    did_release, did_delete = await self.cleanup_object_reference(
                        artifact.content_ref,
                        run_id_hash=run_id_hash,
                        retention_class=artifact.retention_class,
                        now=current,
                    )
                    released += int(did_release)
                    deleted_objects += int(did_delete)
                elif artifact.keyed_document_name is not None:
                    deleted_documents += int(
                        await self._documents.delete(
                            artifact.keyed_document_name
                        )
                    )
        deleted_documents += await self._cleanup_run_control_documents(
            header,
            pages=pages,
            now=current,
        )
        return DurableRetentionCleanupResult(
            examined=sum(len(page.artifacts) for page, _ in pages),
            released_references=released,
            deleted_objects=deleted_objects,
            deleted_documents=deleted_documents,
        )

    async def _cleanup_run_control_documents(
        self,
        header: DurableRunResourceHeaderV1,
        *,
        pages: tuple[tuple[DurableRunArtifactPageV1, str], ...],
        now: datetime,
    ) -> int:
        deleted = 0
        if header.expiry is None:
            return deleted
        if now >= _as_utc(
            header.expiry.tombstone_expires_at,
            "tombstone_expires_at",
        ):
            tombstone = await self._read_run_tombstone(header.run_id)
            if tombstone is not None:
                deleted += int(
                    await self._documents.delete(
                        _run_tombstone_key(header.run_id),
                        revision=tombstone[1],
                    )
                )
            admission = await self.read_admission_receipt(
                owner_hash=header.owner_hash,
                request_id_hash=header.request_id_hash,
            )
            if admission is not None:
                deleted += int(
                    await self._documents.delete(
                        _admission_receipt_key(
                            header.owner_hash,
                            header.request_id_hash,
                        ),
                        revision=admission[1],
                    )
                )
        all_artifacts_expired = all(
            now >= _as_utc(artifact.expires_at, "expires_at")
            for page, _ in pages
            for artifact in page.artifacts
        )
        if not all_artifacts_expired or not durable_history_purge_allowed(
            header,
            now=now,
        ):
            return deleted
        for page, revision in pages:
            deleted += int(
                await self._documents.delete(
                    _artifact_page_key(header.run_id, page.page_hash),
                    revision=revision,
                )
            )
        refreshed = await self.get_run_resource(header.run_id)
        if refreshed is not None:
            _, revision = refreshed
            deleted += int(
                await self._documents.delete(
                    _run_header_key(header.run_id),
                    revision=revision,
                )
            )
        return deleted

    async def _process_cleanup_record(
        self,
        record: DurableCleanupRecordV1,
        *,
        now: datetime,
    ) -> DurableRetentionCleanupResult:
        if record.kind is DurableCleanupRecordKind.OBJECT_REFERENCE:
            assert record.content_ref is not None
            assert record.run_id_hash is not None
            assert record.retention_class is not None
            released, deleted = await self.cleanup_object_reference(
                record.content_ref,
                run_id_hash=record.run_id_hash,
                retention_class=record.retention_class,
                now=now,
            )
            return DurableRetentionCleanupResult(
                released_references=int(released),
                deleted_objects=int(deleted),
            )
        if record.kind is DurableCleanupRecordKind.ARTIFACT_PAGE_CANDIDATE:
            assert record.run_id is not None
            assert record.document_key is not None
            assert record.page_hash is not None
            linked = await self._artifact_page_is_linked(
                record.run_id,
                record.page_hash,
            )
            deleted = False
            if not linked:
                deleted = await self._documents.delete(record.document_key)
            return DurableRetentionCleanupResult(
                deleted_documents=int(deleted)
            )
        if record.kind is DurableCleanupRecordKind.RUN:
            assert record.run_id is not None
            return await self.cleanup_run(record.run_id, now=now)
        assert record.session_id_hash is not None
        assert record.tombstone_seconds is not None
        deleted = await self.expire_session(
            record.session_id_hash,
            now=now,
            tombstone_seconds=record.tombstone_seconds,
        )
        return DurableRetentionCleanupResult(deleted_documents=int(deleted))

    async def _tracked_put_for_resource(
        self,
        *,
        resource_hash: str,
        kind: str,
        payload: bytes,
        media_type: str,
        retention_class: str,
        expires_at: datetime,
        now: datetime,
    ) -> ContentRefV1:
        digest = hashlib.sha256(payload).hexdigest()
        pending_until = now + self._pending_safety
        pending_ref = ContentRefV1(
            object_id=canonical_content_object_id(kind, digest),
            sha256=digest,
            byte_length=len(payload),
            media_type=media_type,
            encryption_version="retention-pending-v1",
            retention_class=retention_class,
        )
        await self.schedule_cleanup(
            DurableCleanupRecordV1.create(
                kind=DurableCleanupRecordKind.OBJECT_REFERENCE,
                not_before=pending_until,
                run_id_hash=resource_hash,
                retention_class=retention_class,
                content_ref=pending_ref,
            )
        )
        for _ in range(self._maximum_cas_attempts):
            await self.acquire_pending_reference(
                object_kind=kind,
                object_digest=digest,
                run_id_hash=resource_hash,
                retention_class=retention_class,
                pending_until=pending_until,
                now=now,
            )
            reference = await self._content.put_bytes(
                kind=kind,
                payload=payload,
                media_type=media_type,
                retention_class=retention_class,
            )
            _assert_committed_reference(
                reference,
                object_id=pending_ref.object_id,
                digest=digest,
                byte_length=len(payload),
                media_type=media_type,
                retention_class=retention_class,
            )
            try:
                await self._content.get_bytes(reference)
            except DurableLoopContentError:
                await asyncio.sleep(0)
                continue
            await self.promote_reference_live(
                object_kind=kind,
                object_digest=digest,
                run_id_hash=resource_hash,
                retention_class=retention_class,
                deadline=expires_at,
                now=now,
            )
            if not await self.verify_live_reference(
                object_kind=kind,
                object_digest=digest,
                run_id_hash=resource_hash,
                retention_class=retention_class,
                now=now,
            ):
                await asyncio.sleep(0)
                continue
            await self.schedule_cleanup(
                DurableCleanupRecordV1.create(
                    kind=DurableCleanupRecordKind.OBJECT_REFERENCE,
                    not_before=expires_at,
                    run_id_hash=resource_hash,
                    retention_class=retention_class,
                    content_ref=reference,
                )
            )
            return reference
        raise DurableRetentionBusyError(
            "resource content did not clear a deletion fence"
        )

    async def _schedule_session_cleanup(
        self,
        resource: DurableSessionResourceV1,
        *,
        tombstone_seconds: int,
    ) -> None:
        await self.schedule_cleanup(
            DurableCleanupRecordV1.create(
                kind=DurableCleanupRecordKind.SESSION,
                not_before=resource.expires_at,
                session_id_hash=resource.session_id_hash,
                tombstone_seconds=tombstone_seconds,
            )
        )

    async def _finish_terminalization(
        self,
        header: DurableRunResourceHeaderV1,
    ) -> None:
        if header.expiry is None:
            raise DurableRetentionError("terminal header is missing expiry")
        await self._persist_run_tombstone(header)
        await self._terminalize_admission_receipt(header)
        await self.schedule_cleanup(
            DurableCleanupRecordV1.create(
                kind=DurableCleanupRecordKind.RUN,
                not_before=header.expiry.result_expires_at,
                run_id=header.run_id,
            )
        )
        await self.schedule_cleanup(
            DurableCleanupRecordV1.create(
                kind=DurableCleanupRecordKind.RUN,
                not_before=header.expiry.tombstone_expires_at,
                run_id=header.run_id,
            )
        )
        durable_history_deadline = max(
            header.expiry.receipts_expire_at,
            header.expiry.skills_expire_at,
            header.expiry.tombstone_expires_at,
        )
        await self.schedule_cleanup(
            DurableCleanupRecordV1.create(
                kind=DurableCleanupRecordKind.RUN,
                not_before=durable_history_deadline,
                run_id=header.run_id,
            )
        )

    async def _persist_run_tombstone(
        self,
        header: DurableRunResourceHeaderV1,
    ) -> DurableRunTombstoneV1:
        if (
            header.expiry is None
            or header.terminal_at is None
            or header.status not in _TERMINAL_STATUSES
        ):
            raise DurableRetentionError("terminal header is incomplete")
        public_expiry = max(
            header.expiry.events_expires_at,
            header.expiry.result_expires_at,
        )
        tombstone_expiry = header.expiry.tombstone_expires_at
        resource_expired_at = min(
            public_expiry,
            tombstone_expiry - timedelta(microseconds=1),
        )
        tombstone = DurableRunTombstoneV1(
            run_id_hash=retained_identifier_hash("run", header.run_id),
            session_id_hash=retained_identifier_hash(
                "session",
                header.session_id,
            ),
            owner_hash=header.owner_hash,
            access_namespace_hash=header.access_namespace_hash,
            terminal_status=header.status,
            expiry_class="run",
            terminal_at=header.terminal_at,
            resource_expired_at=resource_expired_at,
            tombstone_expires_at=tombstone_expiry,
        )
        await self._create_or_verify(
            _run_tombstone_key(header.run_id),
            tombstone,
            DurableRunTombstoneV1,
        )
        return tombstone

    async def _terminalize_admission_receipt(
        self,
        header: DurableRunResourceHeaderV1,
    ) -> None:
        if header.expiry is None or header.status not in _TERMINAL_STATUSES:
            raise DurableRetentionError("terminal header is incomplete")
        loaded = await self.read_admission_receipt(
            owner_hash=header.owner_hash,
            request_id_hash=header.request_id_hash,
        )
        if loaded is None:
            return
        for _ in range(self._maximum_cas_attempts):
            receipt, revision = loaded
            if receipt.state is DurableAdmissionReceiptState.TERMINAL:
                if (
                    receipt.terminal_status is header.status
                    and receipt.expires_at == header.expiry.idempotency_expires_at
                ):
                    return
                raise DurableRetentionConflictError(
                    "admission receipt has a conflicting terminal disposition"
                )
            replacement = receipt.model_copy(
                update={
                    "expires_at": header.expiry.idempotency_expires_at,
                    "record_version": receipt.record_version + 1,
                    "state": DurableAdmissionReceiptState.TERMINAL,
                    "terminal_status": header.status,
                    "updated_at": header.terminal_at,
                }
            )
            if await self._documents.replace(
                _admission_receipt_key(
                    header.owner_hash,
                    header.request_id_hash,
                ),
                canonical_json_bytes(replacement),
                revision=revision,
            ):
                return
            loaded = await self.read_admission_receipt(
                owner_hash=header.owner_hash,
                request_id_hash=header.request_id_hash,
            )
            if loaded is None:
                raise DurableRetentionError(
                    "admission receipt disappeared during terminalization"
                )
        raise DurableRetentionBusyError(
            "admission receipt terminalization CAS retry limit exceeded"
        )

    async def _read_run_tombstone(
        self,
        run_id: str,
    ) -> tuple[DurableRunTombstoneV1, str] | None:
        document = await self._documents.get(_run_tombstone_key(run_id))
        if document is None:
            return None
        return (
            DurableRunTombstoneV1.model_validate_json(document.payload),
            document.revision,
        )

    async def _read_session_tombstone(
        self,
        session_id_hash: str,
    ) -> tuple[DurableSessionTombstoneV1, str] | None:
        document = await self._documents.get(
            _session_tombstone_key(session_id_hash)
        )
        if document is None:
            return None
        return (
            DurableSessionTombstoneV1.model_validate_json(document.payload),
            document.revision,
        )

    async def _load_or_create_object_root(
        self,
        object_kind: str,
        object_digest: str,
        *,
        now: datetime,
    ) -> tuple[DurableObjectReferenceV1, str]:
        key = _object_root_key(object_kind, object_digest)
        for _ in range(self._maximum_cas_attempts):
            document = await self._documents.get(key)
            if document is not None:
                root = _parse_object_reference(
                    document,
                    object_kind=object_kind,
                    object_digest=object_digest,
                )
                if root.state is DurableObjectReferenceState.ABSENT:
                    replacement = root.model_copy(
                        update={
                            "record_version": root.record_version + 1,
                            "state": DurableObjectReferenceState.PENDING,
                            "updated_at": now,
                        }
                    )
                    if await self._documents.replace(
                        key,
                        canonical_json_bytes(replacement),
                        revision=document.revision,
                    ):
                        refreshed = await self._documents.get(key)
                        if refreshed is None:
                            continue
                        return replacement, refreshed.revision
                    continue
                return root, document.revision
            root = DurableObjectReferenceV1(
                object_kind=object_kind,
                object_digest=object_digest,
                state=DurableObjectReferenceState.PENDING,
                record_version=1,
                updated_at=now,
            )
            if await self._documents.create(key, canonical_json_bytes(root)):
                created = await self._documents.get(key)
                if created is not None:
                    return root, created.revision
        raise DurableRetentionBusyError("object root create CAS retry limit exceeded")

    async def _upsert_object_run_reference(
        self,
        *,
        object_kind: str,
        object_digest: str,
        run_id_hash: str,
        retention_class: str,
        deadline: datetime,
        pending: bool,
        now: datetime,
    ) -> None:
        key = _object_shard_key(
            object_kind,
            object_digest,
            self._object_shard(run_id_hash),
        )
        for _ in range(self._maximum_cas_attempts):
            document = await self._documents.get(key)
            if document is None:
                references: tuple[DurableObjectRunReferenceV1, ...] = ()
                record_version = 0
            else:
                shard = _parse_object_reference(
                    document,
                    object_kind=object_kind,
                    object_digest=object_digest,
                )
                references = shard.references
                record_version = shard.record_version
            updated = DurableObjectRunReferenceV1(
                run_id_hash=run_id_hash,
                retention_class=retention_class,
                deadline=deadline,
                pending=pending,
            )
            retained = tuple(
                item
                for item in references
                if (
                    item.run_id_hash,
                    item.retention_class,
                )
                != (run_id_hash, retention_class)
            )
            if len(retained) >= MAX_RUN_ARTIFACTS_PER_PAGE:
                raise DurableRetentionError("object reference shard is full")
            merged = (*retained, updated)
            state = _object_state(merged)
            replacement = DurableObjectReferenceV1(
                object_kind=object_kind,
                object_digest=object_digest,
                state=state,
                references=merged,
                record_version=record_version + 1,
                updated_at=now,
            )
            payload = canonical_json_bytes(replacement)
            if document is None:
                if await self._documents.create(key, payload):
                    return
            elif await self._documents.replace(
                key,
                payload,
                revision=document.revision,
            ):
                return
        raise DurableRetentionBusyError("object shard CAS retry limit exceeded")

    async def _read_object_shard(
        self,
        object_kind: str,
        object_digest: str,
        run_id_hash: str,
    ) -> tuple[DurableObjectReferenceV1, str] | None:
        document = await self._documents.get(
            _object_shard_key(
                object_kind,
                object_digest,
                self._object_shard(run_id_hash),
            )
        )
        if document is None:
            return None
        return (
            _parse_object_reference(
                document,
                object_kind=object_kind,
                object_digest=object_digest,
            ),
            document.revision,
        )

    async def _remove_due_object_run_reference(
        self,
        *,
        object_kind: str,
        object_digest: str,
        run_id_hash: str,
        retention_class: str,
        now: datetime,
    ) -> bool:
        key = _object_shard_key(
            object_kind,
            object_digest,
            self._object_shard(run_id_hash),
        )
        for _ in range(self._maximum_cas_attempts):
            document = await self._documents.get(key)
            if document is None:
                return False
            shard = _parse_object_reference(
                document,
                object_kind=object_kind,
                object_digest=object_digest,
            )
            target = next(
                (
                    item
                    for item in shard.references
                    if item.run_id_hash == run_id_hash
                    and item.retention_class == retention_class
                ),
                None,
            )
            if target is None:
                return False
            if _as_utc(target.deadline, "deadline") > now:
                return False
            retained = tuple(item for item in shard.references if item != target)
            replacement = shard.model_copy(
                update={
                    "record_version": shard.record_version + 1,
                    "references": retained,
                    "state": _object_state(retained),
                    "updated_at": now,
                }
            )
            if await self._documents.replace(
                key,
                canonical_json_bytes(replacement),
                revision=document.revision,
            ):
                await self._touch_object_root(
                    object_kind,
                    object_digest,
                    now=now,
                )
                return True
        raise DurableRetentionBusyError(
            "object reference release CAS retry limit exceeded"
        )

    async def _touch_object_root(
        self,
        object_kind: str,
        object_digest: str,
        *,
        now: datetime,
    ) -> None:
        for _ in range(self._maximum_cas_attempts):
            document = await self._documents.get(
                _object_root_key(object_kind, object_digest)
            )
            if document is None:
                return
            root = _parse_object_reference(
                document,
                object_kind=object_kind,
                object_digest=object_digest,
            )
            if root.state is DurableObjectReferenceState.DELETING:
                return
            state = await self._aggregate_object_state(
                object_kind,
                object_digest,
            )
            if state is DurableObjectReferenceState.ABSENT:
                state = DurableObjectReferenceState.PENDING
            replacement = root.model_copy(
                update={
                    "record_version": root.record_version + 1,
                    "state": state,
                    "updated_at": now,
                }
            )
            if await self._documents.replace(
                _object_root_key(object_kind, object_digest),
                canonical_json_bytes(replacement),
                revision=document.revision,
            ):
                return
        raise DurableRetentionBusyError("object root touch CAS retry limit exceeded")

    async def _try_delete_unreferenced_object(
        self,
        reference: ContentRefV1,
        *,
        object_kind: str,
        object_digest: str,
        now: datetime,
    ) -> bool:
        root_key = _object_root_key(object_kind, object_digest)
        for _ in range(self._maximum_cas_attempts):
            root_document = await self._documents.get(root_key)
            if root_document is None:
                return False
            root = _parse_object_reference(
                root_document,
                object_kind=object_kind,
                object_digest=object_digest,
            )
            if root.state is DurableObjectReferenceState.ABSENT:
                return False
            if root.state is DurableObjectReferenceState.DELETING:
                return False
            snapshots = await self._object_shard_snapshots(
                object_kind,
                object_digest,
            )
            if any(
                _as_utc(item.deadline, "deadline") > now
                for shard, _ in snapshots
                for item in shard.references
            ):
                return False
            if any(shard.references for shard, _ in snapshots):
                await self._prune_expired_object_shards(
                    object_kind,
                    object_digest,
                    snapshots,
                    now=now,
                )
                continue
            fence = canonical_hash(
                {
                    "object_digest": object_digest,
                    "object_kind": object_kind,
                    "record_version": root.record_version + 1,
                }
            )
            deleting = root.model_copy(
                update={
                    "deletion_fence": fence,
                    "record_version": root.record_version + 1,
                    "state": DurableObjectReferenceState.DELETING,
                    "updated_at": now,
                }
            )
            if not await self._documents.replace(
                root_key,
                canonical_json_bytes(deleting),
                revision=root_document.revision,
            ):
                continue
            fenced_document = await self._documents.get(root_key)
            if fenced_document is None:
                raise DurableRetentionError("object deletion fence disappeared")
            if not await self._shards_match_snapshots(
                object_kind,
                object_digest,
                snapshots,
            ):
                await self._abort_object_deletion(
                    deleting,
                    revision=fenced_document.revision,
                    now=now,
                )
                continue
            deleted = await self._content.delete_bytes(reference)
            absent = deleting.model_copy(
                update={
                    "deletion_fence": None,
                    "record_version": deleting.record_version + 1,
                    "state": DurableObjectReferenceState.ABSENT,
                    "updated_at": now,
                }
            )
            if not await self._documents.replace(
                root_key,
                canonical_json_bytes(absent),
                revision=fenced_document.revision,
            ):
                raise DurableRetentionError(
                    "object deletion fence could not be marked absent"
                )
            for shard_index, (_, revision) in enumerate(snapshots):
                if revision is not None:
                    await self._documents.delete(
                        _object_shard_key(
                            object_kind,
                            object_digest,
                            shard_index,
                        ),
                        revision=revision,
                    )
            return deleted
        raise DurableRetentionBusyError("object deletion CAS retry limit exceeded")

    async def _abort_object_deletion(
        self,
        deleting: DurableObjectReferenceV1,
        *,
        revision: str,
        now: datetime,
    ) -> None:
        state = await self._aggregate_object_state(
            deleting.object_kind,
            deleting.object_digest,
        )
        if state is DurableObjectReferenceState.ABSENT:
            state = DurableObjectReferenceState.PENDING
        replacement = deleting.model_copy(
            update={
                "deletion_fence": None,
                "record_version": deleting.record_version + 1,
                "state": state,
                "updated_at": now,
            }
        )
        if not await self._documents.replace(
            _object_root_key(
                deleting.object_kind,
                deleting.object_digest,
            ),
            canonical_json_bytes(replacement),
            revision=revision,
        ):
            raise DurableRetentionBusyError(
                "object deletion fence changed before abort"
            )

    async def _aggregate_object_state(
        self,
        object_kind: str,
        object_digest: str,
    ) -> DurableObjectReferenceState:
        references = tuple(
            item
            for shard, _ in await self._object_shard_snapshots(
                object_kind,
                object_digest,
            )
            for item in shard.references
        )
        return _object_state(references)

    async def _object_shard_snapshots(
        self,
        object_kind: str,
        object_digest: str,
    ) -> tuple[tuple[DurableObjectReferenceV1, str | None], ...]:
        snapshots: list[tuple[DurableObjectReferenceV1, str | None]] = []
        for shard_index in range(self._object_shards):
            document = await self._documents.get(
                _object_shard_key(
                    object_kind,
                    object_digest,
                    shard_index,
                )
            )
            if document is None:
                snapshots.append(
                    (
                        DurableObjectReferenceV1(
                            object_kind=object_kind,
                            object_digest=object_digest,
                            state=DurableObjectReferenceState.ABSENT,
                            record_version=1,
                            updated_at=datetime(1970, 1, 1, tzinfo=UTC),
                        ),
                        None,
                    )
                )
            else:
                snapshots.append(
                    (
                        _parse_object_reference(
                            document,
                            object_kind=object_kind,
                            object_digest=object_digest,
                        ),
                        document.revision,
                    )
                )
        return tuple(snapshots)

    async def _shards_match_snapshots(
        self,
        object_kind: str,
        object_digest: str,
        snapshots: tuple[
            tuple[DurableObjectReferenceV1, str | None],
            ...,
        ],
    ) -> bool:
        for shard_index, (_, expected_revision) in enumerate(snapshots):
            document = await self._documents.get(
                _object_shard_key(
                    object_kind,
                    object_digest,
                    shard_index,
                )
            )
            observed_revision = None if document is None else document.revision
            if observed_revision != expected_revision:
                return False
        return True

    async def _prune_expired_object_shards(
        self,
        object_kind: str,
        object_digest: str,
        snapshots: tuple[
            tuple[DurableObjectReferenceV1, str | None],
            ...,
        ],
        *,
        now: datetime,
    ) -> None:
        for shard_index, (shard, revision) in enumerate(snapshots):
            if revision is None:
                continue
            retained = tuple(
                item
                for item in shard.references
                if _as_utc(item.deadline, "deadline") > now
            )
            if retained == shard.references:
                continue
            replacement = shard.model_copy(
                update={
                    "record_version": shard.record_version + 1,
                    "references": retained,
                    "state": _object_state(retained),
                    "updated_at": now,
                }
            )
            if not await self._documents.replace(
                _object_shard_key(
                    object_kind,
                    object_digest,
                    shard_index,
                ),
                canonical_json_bytes(replacement),
                revision=revision,
            ):
                return

    async def _artifact_is_indexed(
        self,
        header: DurableRunResourceHeaderV1,
        artifact: DurableRunArtifactV1,
    ) -> bool:
        return any(
            candidate == artifact
            for page, _ in await self._artifact_pages(header)
            for candidate in page.artifacts
        )

    async def _artifact_page_is_linked(
        self,
        run_id: str,
        page_hash: str,
    ) -> bool:
        loaded = await self.get_run_resource(run_id)
        if loaded is None:
            return False
        header, _ = loaded
        return any(
            page.page_hash == page_hash
            for page, _ in await self._artifact_pages(header)
        )

    async def _artifact_pages(
        self,
        header: DurableRunResourceHeaderV1,
    ) -> tuple[tuple[DurableRunArtifactPageV1, str], ...]:
        if header.artifact_page_head_hash is None:
            return ()
        pages: list[tuple[DurableRunArtifactPageV1, str]] = []
        current_hash: str | None = header.artifact_page_head_hash
        expected_index = header.artifact_page_count - 1
        while current_hash is not None:
            document = await self._documents.get(
                _artifact_page_key(header.run_id, current_hash)
            )
            if document is None:
                raise DurableRetentionError("run artifact page chain is incomplete")
            page = DurableRunArtifactPageV1.model_validate_json(document.payload)
            if (
                page.run_id != header.run_id
                or page.page_hash != current_hash
                or page.page_index != expected_index
            ):
                raise DurableRetentionError("run artifact page chain is invalid")
            pages.append((page, document.revision))
            current_hash = page.previous_page_hash
            expected_index -= 1
        if len(pages) != header.artifact_page_count:
            raise DurableRetentionError("run artifact page count is invalid")
        return tuple(pages)

    async def _create_or_verify[ModelT: BaseModel](
        self,
        key: str,
        value: ModelT,
        model: type[ModelT],
    ) -> ModelT:
        payload = canonical_json_bytes(value)
        if await self._documents.create(key, payload):
            return value
        existing = await self._documents.get(key)
        if existing is None:
            raise DurableRetentionError("retained document disappeared after create race")
        observed = model.model_validate_json(existing.payload)
        if observed != value:
            raise DurableRetentionConflictError("retained document conflicts")
        return observed

    def _object_shard(self, run_id_hash: str) -> int:
        if _SHA256.fullmatch(run_id_hash) is None:
            raise ValueError("run reference hash is invalid")
        return int(run_id_hash[:8], 16) % self._object_shards


def _create_artifact_page(
    header: DurableRunResourceHeaderV1,
    artifact: DurableRunArtifactV1,
) -> DurableRunArtifactPageV1:
    values = {
        "artifacts": [artifact.model_dump(mode="json")],
        "page_index": header.artifact_page_count,
        "previous_page_hash": header.artifact_page_head_hash,
        "run_id": header.run_id,
    }
    return DurableRunArtifactPageV1(
        run_id=header.run_id,
        page_index=header.artifact_page_count,
        artifacts=(artifact,),
        previous_page_hash=header.artifact_page_head_hash,
        page_hash=canonical_hash(values),
    )


def _canonical_reference_identity(reference: ContentRefV1) -> tuple[str, str]:
    parts = reference.object_id.split("/")
    if (
        len(parts) != 3
        or parts[0] != "objects"
        or parts[2] != reference.sha256
    ):
        raise DurableRetentionLegacyExcludedError(
            "non-canonical content reference is excluded from automatic cleanup"
        )
    _validate_object_identity(parts[1], parts[2])
    return parts[1], parts[2]


def _assert_committed_reference(
    reference: ContentRefV1,
    *,
    object_id: str,
    digest: str,
    byte_length: int,
    media_type: str,
    retention_class: str,
) -> None:
    if (
        reference.object_id != object_id
        or reference.sha256 != digest
        or reference.byte_length != byte_length
        or reference.media_type != media_type
        or reference.retention_class != retention_class
    ):
        raise DurableRetentionConflictError(
            "content store returned a non-canonical object reference"
        )


def _validate_content_kind(kind: str) -> None:
    if _CONTENT_KIND.fullmatch(kind) is None:
        raise ValueError("content kind is invalid")


def _validate_object_identity(kind: str, digest: str) -> None:
    _validate_content_kind(kind)
    if _SHA256.fullmatch(digest) is None:
        raise ValueError("content digest is invalid")


def _parse_object_reference(
    document: KeyedDocument,
    *,
    object_kind: str,
    object_digest: str,
) -> DurableObjectReferenceV1:
    value = DurableObjectReferenceV1.model_validate_json(document.payload)
    if value.object_kind != object_kind or value.object_digest != object_digest:
        raise DurableRetentionConflictError(
            "object reference document identity conflicts"
        )
    return value


def _object_state(
    references: tuple[DurableObjectRunReferenceV1, ...],
) -> DurableObjectReferenceState:
    if any(not item.pending for item in references):
        return DurableObjectReferenceState.LIVE
    if references:
        return DurableObjectReferenceState.PENDING
    return DurableObjectReferenceState.ABSENT


def _assert_session_owner(
    resource: DurableSessionResourceV1,
    *,
    owner_hash: str,
    access_namespace_hash: str,
) -> None:
    if (
        resource.owner_hash != owner_hash
        or resource.access_namespace_hash != access_namespace_hash
    ):
        raise DurableRetentionConflictError("session ownership conflicts")


def _assert_session_tombstone_owner(
    tombstone: DurableSessionTombstoneV1,
    *,
    owner_hash: str,
    access_namespace_hash: str,
) -> None:
    if (
        tombstone.owner_hash != owner_hash
        or tombstone.access_namespace_hash != access_namespace_hash
    ):
        raise DurableRetentionConflictError("session tombstone ownership conflicts")


def _cleanup_record_hash(value: object) -> str:
    return canonical_hash(value)


def _cleanup_page_hash(
    *,
    bucket: str,
    page_index: int,
    records: tuple[DurableCleanupRecordV1, ...],
    previous_page_hash: str | None,
) -> str:
    return canonical_hash(
        {
            "bucket": bucket,
            "page_index": page_index,
            "previous_page_hash": previous_page_hash,
            "records": [record.model_dump(mode="json") for record in records],
        }
    )


def _cleanup_bucket(value: datetime) -> str:
    return _cleanup_bucket_start(value).strftime("%Y%m%d%H")


def _cleanup_bucket_start(value: datetime) -> datetime:
    timestamp = int(_as_utc(value, "cleanup_time").timestamp())
    bucket_start = timestamp - (timestamp % _CLEANUP_BUCKET_SECONDS)
    return datetime.fromtimestamp(bucket_start, tz=UTC)


def _run_header_key(run_id: str) -> str:
    return f"retention/runs/{retained_identifier_hash('run', run_id)}/header"


def _run_tombstone_key(run_id: str) -> str:
    return f"retention/runs/{retained_identifier_hash('run', run_id)}/tombstone"


def _artifact_page_key(run_id: str, page_hash: str) -> str:
    return (
        f"retention/runs/{retained_identifier_hash('run', run_id)}"
        f"/artifacts/{page_hash}"
    )


def _admission_receipt_key(owner_hash: str, request_id_hash: str) -> str:
    key_hash = canonical_hash(
        {
            "owner_hash": owner_hash,
            "request_id_hash": request_id_hash,
        }
    )
    return f"retention/admissions/{key_hash}"


def _object_root_key(kind: str, digest: str) -> str:
    return f"retention/objects/{kind}/{digest}/root"


def _object_shard_key(kind: str, digest: str, shard: int) -> str:
    return f"retention/objects/{kind}/{digest}/shards/{shard:03d}"


def _session_resource_key(session_id_hash: str) -> str:
    return f"retention/sessions/{session_id_hash}/resource"


def _session_tombstone_key(session_id_hash: str) -> str:
    return f"retention/sessions/{session_id_hash}/tombstone"


def _cleanup_head_key(bucket: str) -> str:
    return f"retention/cleanup/{bucket}/head"


def _cleanup_page_key(bucket: str, page_hash: str) -> str:
    return f"retention/cleanup/{bucket}/pages/{page_hash}"


def _as_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _merge_cleanup_results(
    left: DurableRetentionCleanupResult,
    right: DurableRetentionCleanupResult,
    *,
    examined_increment: int,
) -> DurableRetentionCleanupResult:
    return DurableRetentionCleanupResult(
        examined=left.examined + right.examined + examined_increment,
        released_references=(
            left.released_references + right.released_references
        ),
        deleted_objects=left.deleted_objects + right.deleted_objects,
        deleted_documents=left.deleted_documents + right.deleted_documents,
        legacy_excluded=left.legacy_excluded or right.legacy_excluded,
    )
