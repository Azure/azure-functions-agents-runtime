"""Generic, best-effort public observations for durable agent runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .._logger import logger
from ..strict_json import canonical_json_bytes, decode_json_object
from .durable_loop_protocol import (
    DURABLE_LOOP_ORCHESTRATOR_V4_NAME,
    ContentRefV1,
    DurableAssistantDeltaPayloadV1,
    DurableHumanInputRequiredPayloadV1,
    DurableLoopRunStatus,
    DurableMessageCommittedPayloadV1,
    DurableModelProgressPayloadV1,
    DurableObservationDegradedPayloadV1,
    DurablePublicEventPayload,
    DurablePublicEventType,
    DurablePublicEventV2,
    DurableRunStartedPayloadV1,
    DurableSkillLoadPayloadV1,
    DurableSkillSearchPayloadV1,
    DurableTerminalEventPayloadV1,
    DurableToolEventPayloadV1,
    HumanInputState,
    ToolProvenance,
    ToolResultStatus,
    canonical_hash,
)

if TYPE_CHECKING:
    from .durable_loop_activities import DurableContentStore
    from .durable_loop_receipts import DurableKeyedDocumentStore, KeyedDocument
    from .durable_retention import DurableRetentionManager

MAX_DURABLE_RUN_OBSERVATION_BATCH = 64
MAX_DURABLE_RUN_REPLAY_EVENTS = 128
MAX_DURABLE_RUN_ASSISTANT_TEXT_BYTES = 256 * 1024
MAX_DURABLE_RUN_TOOLS = 2048
_CAS_ATTEMPTS = 8
_OPERATION_DEADLINE_SECONDS = 10.0


class DurableRunObservationError(RuntimeError):
    """The optional observation journal could not be read safely."""


class DurableRunReplayDisposition(StrEnum):
    """How a public observation replay should replace or advance client state."""

    DELTAS = "deltas"
    SNAPSHOT_REQUIRED = "snapshot_required"
    CURSOR_AHEAD = "cursor_ahead"


class _ObservationModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class DurableRunObservationInitializationV2(_ObservationModel):
    """Create-once non-secret binding for a generic schema-v2 journal."""

    schema_version: Literal["2"] = "2"
    frame_schema_version: Literal["2"] = "2"
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    session_id: Annotated[str, Field(min_length=1, max_length=128)]
    owner_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    created_at: datetime
    events_expires_at: datetime

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        created = _as_utc(self.created_at)
        expires = _as_utc(self.events_expires_at)
        if expires <= created:
            raise ValueError("observation expiry must follow creation")
        return self


class DurableRunToolProjectionV2(_ObservationModel):
    """Last public state for one deterministic tool call."""

    schema_version: Literal["2"] = "2"
    call_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    tool_name: Annotated[str, Field(min_length=1, max_length=128)]
    status: ToolResultStatus | None = None
    updated_at: datetime


class DurableRunObservationProjectionV2(_ObservationModel):
    """Bounded generic projection used when older deltas are compacted."""

    schema_version: Literal["2"] = "2"
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    session_id: Annotated[str, Field(min_length=1, max_length=128)]
    through_sequence: Annotated[int, Field(ge=0)]
    status: DurableLoopRunStatus
    phase: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]
    updated_at: datetime
    model_step: Annotated[int, Field(ge=0)] = 0
    completed_skill_searches: Annotated[int, Field(ge=0)] = 0
    completed_skill_loads: Annotated[int, Field(ge=0)] = 0
    assistant_text: Annotated[
        str,
        Field(max_length=MAX_DURABLE_RUN_ASSISTANT_TEXT_BYTES),
    ] = ""
    tools: Annotated[
        tuple[DurableRunToolProjectionV2, ...],
        Field(max_length=MAX_DURABLE_RUN_TOOLS),
    ] = ()
    human_input: DurableHumanInputRequiredPayloadV1 | None = None
    terminal: DurableTerminalEventPayloadV1 | None = None
    degraded: DurableObservationDegradedPayloadV1 | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        _as_utc(self.updated_at)
        if len(self.assistant_text.encode("utf-8")) > MAX_DURABLE_RUN_ASSISTANT_TEXT_BYTES:
            raise ValueError("observation projection assistant text exceeds its byte limit")
        keys = [tool.call_key for tool in self.tools]
        if len(keys) != len(set(keys)):
            raise ValueError("observation projection tool keys must be unique")
        return self


class DurableRunObservationSnapshotV2(_ObservationModel):
    """A complete generic projection through one public sequence watermark."""

    schema_version: Literal["2"] = "2"
    type: Literal["snapshot"] = "snapshot"
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    sequence: Annotated[int, Field(ge=0)]
    timestamp: datetime
    projection: DurableRunObservationProjectionV2

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        _as_utc(self.timestamp)
        if (
            self.projection.run_id != self.run_id
            or self.projection.through_sequence != self.sequence
        ):
            raise ValueError("observation snapshot does not match its projection")
        return self


class DurableRunObservationReplayV2(_ObservationModel):
    """A consistent generic snapshot or contiguous schema-v2 event page."""

    schema_version: Literal["2"] = "2"
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    requested_after_sequence: Annotated[int, Field(ge=0)]
    disposition: DurableRunReplayDisposition
    through_sequence: Annotated[int, Field(ge=0)]
    snapshot: DurableRunObservationSnapshotV2 | None = None
    events: Annotated[
        tuple[DurablePublicEventV2, ...],
        Field(max_length=MAX_DURABLE_RUN_REPLAY_EVENTS),
    ] = ()

    @model_validator(mode="after")
    def validate_replay(self) -> Self:
        sequences = [event.sequence for event in self.events]
        if sequences != sorted(set(sequences)):
            raise ValueError("public event sequences must be strictly increasing")
        if any(
            event.run_id != self.run_id or event.sequence > self.through_sequence
            for event in self.events
        ):
            raise ValueError("public replay event does not match its run or watermark")
        if self.disposition is DurableRunReplayDisposition.CURSOR_AHEAD:
            if self.snapshot is not None or self.events:
                raise ValueError("cursor-ahead replay cannot contain frames")
            return self
        if self.disposition is DurableRunReplayDisposition.DELTAS:
            if self.snapshot is not None:
                raise ValueError("delta replay cannot contain a snapshot")
            minimum = self.requested_after_sequence
        else:
            if self.snapshot is None:
                raise ValueError("snapshot replay requires a snapshot")
            if self.snapshot.run_id != self.run_id:
                raise ValueError("public replay snapshot does not match its run")
            minimum = self.snapshot.sequence
        expected = list(range(minimum + 1, minimum + 1 + len(sequences)))
        if sequences != expected:
            raise ValueError("public replay deltas must be contiguous")
        return self


class _ObservationBatchV2(_ObservationModel):
    schema_version: Literal["2"] = "2"
    run_id: str
    events: Annotated[
        tuple[DurablePublicEventV2, ...],
        Field(min_length=1, max_length=MAX_DURABLE_RUN_OBSERVATION_BATCH),
    ]

    @model_validator(mode="after")
    def validate_events(self) -> Self:
        sequences = [event.sequence for event in self.events]
        if any(event.run_id != self.run_id for event in self.events):
            raise ValueError("observation batch events must match the run")
        if sequences != list(range(sequences[0], sequences[0] + len(sequences))):
            raise ValueError("observation batch events must be contiguous")
        return self


class _ObservationBatchReferenceV2(_ObservationModel):
    first_sequence: Annotated[int, Field(ge=1)]
    through_sequence: Annotated[int, Field(ge=1)]
    event_count: Annotated[
        int,
        Field(ge=1, le=MAX_DURABLE_RUN_OBSERVATION_BATCH),
    ]
    reference: ContentRefV1


class _ObservationManifestV2(_ObservationModel):
    schema_version: Literal["2"] = "2"
    frame_schema_version: Literal["2"] = "2"
    run_id: str
    published_revision: Annotated[int, Field(ge=0)] = 0
    through_sequence: Annotated[int, Field(ge=0)] = 0
    snapshot_ref: ContentRefV1 | None = None
    snapshot_through_sequence: Annotated[int, Field(ge=0)] = 0
    terminal_published: bool = False
    batches: Annotated[
        tuple[_ObservationBatchReferenceV2, ...],
        Field(max_length=MAX_DURABLE_RUN_REPLAY_EVENTS),
    ] = ()

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.snapshot_ref is None:
            if self.snapshot_through_sequence:
                raise ValueError("observation manifest has an invalid snapshot watermark")
        elif not 0 < self.snapshot_through_sequence <= self.through_sequence:
            raise ValueError("observation manifest has an invalid snapshot range")
        expected = self.snapshot_through_sequence + 1
        for batch in self.batches:
            if (
                batch.first_sequence != expected
                or batch.through_sequence - batch.first_sequence + 1
                != batch.event_count
            ):
                raise ValueError("observation manifest batches are not contiguous")
            expected = batch.through_sequence + 1
        if self.through_sequence != expected - 1:
            raise ValueError("observation manifest watermark is invalid")
        return self


class _ManifestSnapshot:
    def __init__(self, manifest: _ObservationManifestV2, revision: str) -> None:
        self.manifest = manifest
        self.revision = revision


class DurableRunObservationJournal:
    """Content-first schema-v2 journal sharing the durable Blob stores."""

    def __init__(
        self,
        *,
        content: DurableContentStore,
        documents: DurableKeyedDocumentStore,
        retention: DurableRetentionManager | None = None,
    ) -> None:
        self._observation_content = content
        self._observation_documents = documents
        self._observation_retention = retention

    async def create_observation_initialization_once(
        self,
        *,
        initialization: DurableRunObservationInitializationV2,
    ) -> tuple[DurableRunObservationInitializationV2, bool]:
        """Create a generic journal binding once and return the winning record."""
        key = _initialization_key(initialization.run_id)
        payload = canonical_json_bytes(initialization)
        for _ in range(_CAS_ATTEMPTS):
            current = await self.load_observation_initialization(
                run_id=initialization.run_id
            )
            if current is not None:
                await self._index_observation_document(
                    initialization=current,
                    key=key,
                    kind="observation-initialization",
                )
                return current, False
            try:
                if await self._observation_documents.create(key, payload):
                    await self._index_observation_document(
                        initialization=initialization,
                        key=key,
                        kind="observation-initialization",
                    )
                    return initialization, True
            except Exception as exc:
                raise DurableRunObservationError(
                    "durable run observation initialization failed"
                ) from exc
        raise DurableRunObservationError(
            "durable run observation initialization exhausted CAS attempts"
        )

    async def load_observation_initialization(
        self,
        *,
        run_id: str,
    ) -> DurableRunObservationInitializationV2 | None:
        """Load the generic journal binding without consulting chat settings."""
        try:
            document = await self._observation_documents.get(_initialization_key(run_id))
        except Exception as exc:
            raise DurableRunObservationError(
                "durable run observation initialization read failed"
            ) from exc
        if document is None:
            return None
        initialization = _parse_model(
            document.payload,
            DurableRunObservationInitializationV2,
        )
        if initialization.run_id != run_id:
            raise DurableRunObservationError(
                "durable run observation initialization does not match the run"
            )
        return initialization

    async def publish_public_event(
        self,
        *,
        run_id: str,
        event_type: DurablePublicEventType,
        payload: DurablePublicEventPayload,
        timestamp: datetime,
        deadline: datetime | None = None,
    ) -> DurablePublicEventV2 | None:
        """Best-effort publish one strict generic event and assign its sequence."""
        safe_deadline = _safe_deadline(deadline)
        initialization = await self.load_observation_initialization(run_id=run_id)
        if initialization is None:
            raise DurableRunObservationError(
                "durable run observation initialization is unavailable"
            )
        for _ in range(_CAS_ATTEMPTS):
            current = await self._read_observation_manifest(run_id, safe_deadline)
            manifest = (
                current.manifest
                if current is not None
                else _ObservationManifestV2(run_id=run_id)
            )
            if manifest.terminal_published:
                return None
            sequence = manifest.through_sequence + 1
            frame = DurablePublicEventV2(
                run_id=run_id,
                sequence=sequence,
                timestamp=timestamp,
                type=event_type,
                payload=payload,
            )
            batch = _ObservationBatchV2(run_id=run_id, events=(frame,))
            if len(manifest.batches) >= MAX_DURABLE_RUN_REPLAY_EVENTS:
                candidate = await self._compacted_candidate(
                    manifest,
                    frame,
                    expires_at=initialization.events_expires_at,
                    deadline=safe_deadline,
                )
            else:
                reference = await self._put_content(
                    run_id=run_id,
                    kind="durable-run-observation-batch-v2",
                    payload=canonical_json_bytes(batch),
                    expires_at=initialization.events_expires_at,
                    deadline=safe_deadline,
                )
                batch_reference = _ObservationBatchReferenceV2(
                    first_sequence=sequence,
                    through_sequence=sequence,
                    event_count=1,
                    reference=reference,
                )
                candidate = manifest.model_copy(
                    update={
                        "published_revision": manifest.published_revision + 1,
                        "through_sequence": sequence,
                        "batches": (*manifest.batches, batch_reference),
                        "terminal_published": _is_terminal_event_type(event_type),
                    }
                )
            if await self._write_observation_manifest(
                candidate,
                current,
                initialization=initialization,
                deadline=safe_deadline,
            ):
                return frame
        logger.warning("durable run observation publication exhausted CAS attempts")
        return None

    async def replay_public_events(
        self,
        *,
        run_id: str,
        after_sequence: int,
        limit: int = MAX_DURABLE_RUN_REPLAY_EVENTS,
    ) -> DurableRunObservationReplayV2:
        """Return a generic snapshot or contiguous schema-v2 event deltas."""
        if after_sequence < 0:
            raise ValueError("public event cursor must be non-negative")
        bounded_limit = min(max(limit, 1), MAX_DURABLE_RUN_REPLAY_EVENTS)
        deadline = _safe_deadline(None)
        current = await self._read_observation_manifest(run_id, deadline)
        if current is None:
            return DurableRunObservationReplayV2(
                run_id=run_id,
                requested_after_sequence=after_sequence,
                disposition=(
                    DurableRunReplayDisposition.CURSOR_AHEAD
                    if after_sequence
                    else DurableRunReplayDisposition.DELTAS
                ),
                through_sequence=0,
            )
        manifest = current.manifest
        if after_sequence > manifest.through_sequence:
            return DurableRunObservationReplayV2(
                run_id=run_id,
                requested_after_sequence=after_sequence,
                disposition=DurableRunReplayDisposition.CURSOR_AHEAD,
                through_sequence=manifest.through_sequence,
            )
        snapshot: DurableRunObservationSnapshotV2 | None = None
        minimum = after_sequence
        disposition = DurableRunReplayDisposition.DELTAS
        if (
            manifest.snapshot_ref is not None
            and after_sequence < manifest.snapshot_through_sequence
        ):
            snapshot = await self._read_observation_snapshot(
                manifest.snapshot_ref,
                deadline,
            )
            if (
                snapshot.run_id != run_id
                or snapshot.sequence != manifest.snapshot_through_sequence
            ):
                raise DurableRunObservationError(
                    "durable run observation snapshot is inconsistent"
                )
            minimum = snapshot.sequence
            disposition = DurableRunReplayDisposition.SNAPSHOT_REQUIRED
        events: list[DurablePublicEventV2] = []
        for reference in manifest.batches:
            if reference.through_sequence <= minimum:
                continue
            batch = await self._read_observation_batch(reference.reference, deadline)
            if batch.run_id != run_id or len(batch.events) != reference.event_count:
                raise DurableRunObservationError(
                    "durable run observation batch is inconsistent"
                )
            events.extend(event for event in batch.events if event.sequence > minimum)
            if len(events) >= bounded_limit:
                events = events[:bounded_limit]
                break
        return DurableRunObservationReplayV2(
            run_id=run_id,
            requested_after_sequence=after_sequence,
            disposition=disposition,
            through_sequence=manifest.through_sequence,
            snapshot=snapshot,
            events=tuple(events),
        )

    async def reconcile_public_terminal(
        self,
        *,
        run_id: str,
        status: DurableLoopRunStatus,
        result_available: bool,
        error_code: str | None,
        possibly_committed: bool = False,
    ) -> None:
        """Best-effort synthesize a missing terminal frame from authoritative state."""
        if status not in {
            DurableLoopRunStatus.COMPLETED,
            DurableLoopRunStatus.FAILED,
            DurableLoopRunStatus.CANCELLED,
        }:
            return
        page = await self.replay_public_events(
            run_id=run_id,
            after_sequence=0,
            limit=MAX_DURABLE_RUN_REPLAY_EVENTS,
        )
        projection = page.snapshot.projection if page.snapshot is not None else None
        if projection is not None and projection.terminal is not None:
            return
        if any(
            event.type
            in {
                DurablePublicEventType.RUN_COMPLETED,
                DurablePublicEventType.RUN_FAILED,
                DurablePublicEventType.RUN_CANCELLED,
            }
            for event in page.events
        ):
            return
        event_type = {
            DurableLoopRunStatus.COMPLETED: DurablePublicEventType.RUN_COMPLETED,
            DurableLoopRunStatus.FAILED: DurablePublicEventType.RUN_FAILED,
            DurableLoopRunStatus.CANCELLED: DurablePublicEventType.RUN_CANCELLED,
        }[status]
        await self.publish_public_event(
            run_id=run_id,
            event_type=event_type,
            payload=DurableTerminalEventPayloadV1(
                status=status,
                result_available=result_available,
                error_code=error_code if status is not DurableLoopRunStatus.COMPLETED else None,
                possibly_committed=possibly_committed,
            ),
            timestamp=datetime.now(UTC),
        )

    async def _compacted_candidate(
        self,
        manifest: _ObservationManifestV2,
        frame: DurablePublicEventV2,
        *,
        expires_at: datetime,
        deadline: datetime,
    ) -> _ObservationManifestV2:
        projection = await self._projection(manifest, deadline)
        projection = _apply_public_event(
            projection,
            frame,
            session_id=projection.session_id,
        )
        snapshot = DurableRunObservationSnapshotV2(
            run_id=frame.run_id,
            sequence=frame.sequence,
            timestamp=frame.timestamp,
            projection=projection,
        )
        snapshot_ref = await self._put_content(
            run_id=frame.run_id,
            kind="durable-run-observation-snapshot-v2",
            payload=canonical_json_bytes(snapshot),
            expires_at=expires_at,
            deadline=deadline,
        )
        return _ObservationManifestV2(
            run_id=frame.run_id,
            published_revision=manifest.published_revision + 1,
            through_sequence=frame.sequence,
            snapshot_ref=snapshot_ref,
            snapshot_through_sequence=frame.sequence,
            terminal_published=_is_terminal_event_type(frame.type),
        )

    async def _projection(
        self,
        manifest: _ObservationManifestV2,
        deadline: datetime,
    ) -> DurableRunObservationProjectionV2:
        initialization = await self.load_observation_initialization(
            run_id=manifest.run_id
        )
        if initialization is None:
            raise DurableRunObservationError(
                "durable run observation initialization is unavailable"
            )
        if manifest.snapshot_ref is not None:
            projection = (
                await self._read_observation_snapshot(
                    manifest.snapshot_ref,
                    deadline,
                )
            ).projection
        else:
            projection = DurableRunObservationProjectionV2(
                run_id=manifest.run_id,
                session_id=initialization.session_id,
                through_sequence=0,
                status=DurableLoopRunStatus.PENDING,
                phase="admitted",
                updated_at=initialization.created_at,
            )
        for reference in manifest.batches:
            batch = await self._read_observation_batch(reference.reference, deadline)
            for event in batch.events:
                projection = _apply_public_event(
                    projection,
                    event,
                    session_id=initialization.session_id,
                )
        return projection

    async def _read_observation_manifest(
        self,
        run_id: str,
        deadline: datetime,
    ) -> _ManifestSnapshot | None:
        document: KeyedDocument | None = await _await(
            self._observation_documents.get(_journal_key(run_id)),
            deadline,
        )
        if document is None:
            return None
        manifest = _parse_model(document.payload, _ObservationManifestV2)
        if manifest.run_id != run_id:
            raise DurableRunObservationError(
                "durable run observation manifest does not match the run"
            )
        return _ManifestSnapshot(manifest, document.revision)

    async def _write_observation_manifest(
        self,
        manifest: _ObservationManifestV2,
        current: _ManifestSnapshot | None,
        *,
        initialization: DurableRunObservationInitializationV2,
        deadline: datetime,
    ) -> bool:
        payload = canonical_json_bytes(manifest)
        key = _journal_key(manifest.run_id)
        try:
            if current is None:
                written = await _await(
                    self._observation_documents.create(key, payload),
                    deadline,
                )
            else:
                written = await _await(
                    self._observation_documents.replace(
                        key,
                        payload,
                        revision=current.revision,
                    ),
                    deadline,
                )
            if written:
                await self._index_observation_document(
                    initialization=initialization,
                    key=key,
                    kind="observation-manifest",
                )
            return written
        except TimeoutError:
            raise
        except Exception as exc:
            raise DurableRunObservationError(
                "durable run observation manifest write failed"
            ) from exc

    async def _put_content(
        self,
        *,
        run_id: str,
        kind: str,
        payload: bytes,
        expires_at: datetime,
        deadline: datetime,
    ) -> ContentRefV1:
        try:
            if self._observation_retention is not None:
                return await _await(
                    self._observation_retention.tracked_put(
                        run_id=run_id,
                        kind=kind,
                        payload=payload,
                        media_type="application/json",
                        retention_class="event",
                        expires_at=expires_at,
                        now=datetime.now(UTC),
                    ),
                    deadline,
                )
            return await _await(
                self._observation_content.put_bytes(
                    kind=kind,
                    payload=payload,
                    media_type="application/json",
                    retention_class="run",
                ),
                deadline,
            )
        except TimeoutError:
            raise
        except Exception as exc:
            raise DurableRunObservationError(
                "durable run observation content write failed"
            ) from exc

    async def _index_observation_document(
        self,
        *,
        initialization: DurableRunObservationInitializationV2,
        key: str,
        kind: str,
    ) -> None:
        if self._observation_retention is None:
            return
        await self._observation_retention.append_keyed_artifact(
            run_id=initialization.run_id,
            kind=kind,
            retention_class="event",
            document_key=key,
            expires_at=initialization.events_expires_at,
            now=datetime.now(UTC),
        )

    async def _read_observation_batch(
        self,
        reference: ContentRefV1,
        deadline: datetime,
    ) -> _ObservationBatchV2:
        try:
            payload: bytes = await _await(
                self._observation_content.get_bytes(reference),
                deadline,
            )
            return _parse_model(payload, _ObservationBatchV2)
        except TimeoutError as exc:
            raise DurableRunObservationError(
                "durable run observation batch read timed out"
            ) from exc
        except Exception as exc:
            if isinstance(exc, DurableRunObservationError):
                raise
            raise DurableRunObservationError(
                "durable run observation batch read failed"
            ) from exc

    async def _read_observation_snapshot(
        self,
        reference: ContentRefV1,
        deadline: datetime,
    ) -> DurableRunObservationSnapshotV2:
        try:
            payload: bytes = await _await(
                self._observation_content.get_bytes(reference),
                deadline,
            )
            return _parse_model(payload, DurableRunObservationSnapshotV2)
        except TimeoutError as exc:
            raise DurableRunObservationError(
                "durable run observation snapshot read timed out"
            ) from exc
        except Exception as exc:
            if isinstance(exc, DurableRunObservationError):
                raise
            raise DurableRunObservationError(
                "durable run observation snapshot read failed"
            ) from exc


def get_durable_run_observation_journal() -> DurableRunObservationJournal:
    """Return the single Blob-backed journal shared with durable-chat V1."""
    from .durable_chat_journal import get_durable_chat_journal

    journal = get_durable_chat_journal()
    if not isinstance(journal, DurableRunObservationJournal):
        raise TypeError("configured durable observation journal lacks schema-v2 support")
    return journal


async def initialize_durable_run_observations(
    durable_input: object,
    *,
    journal: DurableRunObservationJournal | None = None,
) -> DurableRunObservationInitializationV2 | None:
    """Narrow V4 admission hook usable by HTTP and trigger registration."""
    identity = getattr(durable_input, "identity", None)
    if (
        identity is None
        or getattr(identity, "orchestration_version", None)
        != DURABLE_LOOP_ORCHESTRATOR_V4_NAME
    ):
        return None
    run_id = getattr(identity, "run_id", None)
    session_id = getattr(identity, "session_id", None)
    owner_hash = getattr(identity, "owner_hash", None)
    created_at = getattr(identity, "created_at", None)
    expires_at = getattr(identity, "absolute_deadline", None)
    retention_policy = getattr(identity, "retention_policy", None)
    event_result_seconds = getattr(retention_policy, "event_result_seconds", 0)
    if not (
        isinstance(run_id, str)
        and isinstance(session_id, str)
        and isinstance(owner_hash, str)
        and isinstance(created_at, datetime)
        and isinstance(expires_at, datetime)
        and isinstance(event_result_seconds, int)
        and event_result_seconds >= 0
    ):
        raise ValueError("V4 durable input lacks a valid observation binding")
    selected = journal or get_durable_run_observation_journal()
    initialization, created = await selected.create_observation_initialization_once(
        initialization=DurableRunObservationInitializationV2(
            run_id=run_id,
            session_id=session_id,
            owner_hash=owner_hash,
            created_at=created_at,
            events_expires_at=expires_at + timedelta(seconds=event_result_seconds),
        )
    )
    if created:
        await selected.publish_public_event(
            run_id=run_id,
            event_type=DurablePublicEventType.RUN_STARTED,
            payload=DurableRunStartedPayloadV1(
                session_id=session_id,
                status=DurableLoopRunStatus.PENDING,
            ),
            timestamp=created_at,
        )
    return initialization


def render_public_sse_frame(
    frame: DurablePublicEventV2 | DurableRunObservationSnapshotV2,
) -> str:
    """Render one canonical strict schema-v2 SSE frame."""
    event_type = frame.type.value if isinstance(frame, DurablePublicEventV2) else "snapshot"
    return (
        f"id: {frame.sequence}\n"
        f"event: {event_type}\n"
        f"data: {canonical_json_bytes(frame).decode('utf-8')}\n\n"
    )


async def replay_durable_run_observations(
    *,
    run_id: str,
    after_sequence: int,
    journal: DurableRunObservationJournal | None = None,
) -> DurableRunObservationReplayV2:
    """Read V2 storage or project retained schema-v1 chat storage into V2."""
    selected = journal or get_durable_run_observation_journal()
    initialization = await selected.load_observation_initialization(run_id=run_id)
    if initialization is not None:
        return await selected.replay_public_events(
            run_id=run_id,
            after_sequence=after_sequence,
            limit=MAX_DURABLE_RUN_REPLAY_EVENTS,
        )
    legacy = _legacy_journal(selected)
    legacy_initialization = await legacy.load_run_initialization(run_id=run_id)
    if legacy_initialization is None:
        return DurableRunObservationReplayV2(
            run_id=run_id,
            requested_after_sequence=after_sequence,
            disposition=(
                DurableRunReplayDisposition.CURSOR_AHEAD
                if after_sequence
                else DurableRunReplayDisposition.DELTAS
            ),
            through_sequence=0,
        )
    page = await legacy.replay(
        run_id=run_id,
        after_sequence=after_sequence,
        limit=MAX_DURABLE_RUN_REPLAY_EVENTS,
    )
    return _legacy_replay_to_public(page, session_id=legacy_initialization.session_id)


async def reconcile_durable_run_terminal(
    *,
    run_id: str,
    status: DurableLoopRunStatus,
    result_available: bool,
    error_code: str | None,
    possibly_committed: bool = False,
    journal: DurableRunObservationJournal | None = None,
) -> None:
    """Reconcile V2 or retained V1 storage from authoritative lifecycle state."""
    selected = journal or get_durable_run_observation_journal()
    initialization = await selected.load_observation_initialization(run_id=run_id)
    if initialization is not None:
        await selected.reconcile_public_terminal(
            run_id=run_id,
            status=status,
            result_available=result_available,
            error_code=error_code,
            possibly_committed=possibly_committed,
        )
        return
    from .durable_chat_protocol import (
        DurableChatObservationBatchV1,
        DurableChatTerminalObservationV1,
    )

    legacy = _legacy_journal(selected)
    legacy_initialization = await legacy.load_run_initialization(run_id=run_id)
    if legacy_initialization is None:
        return
    terminal = DurableChatTerminalObservationV1(
        run_id=run_id,
        session_id=legacy_initialization.session_id,
        observed_at=datetime.now(UTC),
        status=status,
        result_available=result_available,
        committed_generation=legacy_initialization.committed_generation,
        error_code=error_code if status is not DurableLoopRunStatus.COMPLETED else None,
    )
    await legacy.publish(
        run_id=run_id,
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id=run_id,
            observations=(terminal,),
        ),
        deadline=_safe_deadline(None),
    )


async def replay_durable_chat_adapter(
    *,
    run_id: str,
    after_sequence: int,
    journal: DurableRunObservationJournal | None = None,
) -> Any:
    """Serve the hosted V1 contract from V2 storage or retained V1 storage."""
    selected = journal or get_durable_run_observation_journal()
    initialization = await selected.load_observation_initialization(run_id=run_id)
    if initialization is None:
        return await _legacy_journal(selected).replay(
            run_id=run_id,
            after_sequence=after_sequence,
            limit=MAX_DURABLE_RUN_REPLAY_EVENTS,
        )
    public = await selected.replay_public_events(
        run_id=run_id,
        after_sequence=after_sequence,
        limit=MAX_DURABLE_RUN_REPLAY_EVENTS,
    )
    return _public_replay_to_chat(public, initialization=initialization)


def _public_replay_to_chat(
    page: DurableRunObservationReplayV2,
    *,
    initialization: DurableRunObservationInitializationV2,
) -> Any:
    from .durable_chat_protocol import (
        DurableChatReplayDisposition,
        DurableChatReplayPageV1,
    )

    disposition = {
        DurableRunReplayDisposition.DELTAS: DurableChatReplayDisposition.DELTAS,
        DurableRunReplayDisposition.SNAPSHOT_REQUIRED: (
            DurableChatReplayDisposition.SNAPSHOT_REQUIRED
        ),
        DurableRunReplayDisposition.CURSOR_AHEAD: (
            DurableChatReplayDisposition.CURSOR_AHEAD
        ),
    }[page.disposition]
    return DurableChatReplayPageV1(
        run_id=page.run_id,
        requested_after_sequence=page.requested_after_sequence,
        disposition=disposition,
        through_sequence=page.through_sequence,
        snapshot=(
            _public_snapshot_to_chat(page.snapshot, initialization=initialization)
            if page.snapshot is not None
            else None
        ),
        events=tuple(
            _public_frame_to_chat(frame, initialization=initialization)
            for frame in page.events
        ),
    )


def _public_snapshot_to_chat(
    snapshot: DurableRunObservationSnapshotV2,
    *,
    initialization: DurableRunObservationInitializationV2,
) -> Any:
    from .durable_chat_protocol import (
        DurableChatAssistantDraftV1,
        DurableChatModelProducerV1,
        DurableChatObservationDegradationReason,
        DurableChatObservationHealthV1,
        DurableChatProducerEpochV1,
        DurableChatProgressV1,
        DurableChatRunProjectionV1,
        DurableChatSnapshotFrameV1,
        DurableChatToolProducerV1,
        DurableChatToolProgressV1,
        DurableChatToolState,
    )

    projection = snapshot.projection
    producer = DurableChatModelProducerV1(
        step_index=projection.model_step,
        observation_epoch=1,
    )
    tools = tuple(
        DurableChatToolProgressV1(
            producer=DurableChatToolProducerV1(call_key=tool.call_key),
            step_index=projection.model_step,
            tool_name=tool.tool_name,
            provenance=ToolProvenance.RUNTIME,
            state=(
                DurableChatToolState.STARTED
                if tool.status is None
                else DurableChatToolState(tool.status.value)
            ),
            updated_at=tool.updated_at,
        )
        for tool in projection.tools
    )
    health = (
        DurableChatObservationHealthV1(
            degraded=True,
            reasons=(DurableChatObservationDegradationReason.STORAGE_UNAVAILABLE,),
            dropped_observations=1,
            last_error_code=projection.degraded.code,
        )
        if projection.degraded is not None
        else DurableChatObservationHealthV1()
    )
    chat_projection = DurableChatRunProjectionV1(
        run_id=projection.run_id,
        session_id=initialization.session_id,
        published_revision=max(1, snapshot.sequence),
        through_sequence=snapshot.sequence,
        draft=(
            DurableChatAssistantDraftV1(
                producer=producer,
                text=projection.assistant_text,
                updated_at=projection.updated_at,
            )
            if projection.assistant_text
            else None
        ),
        progress=DurableChatProgressV1(
            status=projection.status,
            phase=projection.phase,
            model_steps=projection.model_step,
            tool_calls=len(
                [tool for tool in projection.tools if tool.status is not None]
            ),
            human_waits=1 if projection.human_input is not None else 0,
            step_index=projection.model_step,
            result_available=(
                projection.terminal.result_available
                if projection.terminal is not None
                else False
            ),
            updated_at=projection.updated_at,
        ),
        producer_epochs=(
            DurableChatProducerEpochV1(
                step_index=producer.step_index,
                observation_epoch=producer.observation_epoch,
            ),
        )
        if projection.assistant_text
        else (),
        tool_progress=tools,
        observation_health=health,
    )
    return DurableChatSnapshotFrameV1(
        captured_at=snapshot.timestamp,
        projection=chat_projection,
    )


def _public_frame_to_chat(
    frame: DurablePublicEventV2,
    *,
    initialization: DurableRunObservationInitializationV2,
) -> Any:
    from .durable_chat_protocol import (
        DurableChatAssistantTextObservationV1,
        DurableChatDegradedObservationV1,
        DurableChatEventFrameV1,
        DurableChatHumanInputObservationV1,
        DurableChatModelProducerV1,
        DurableChatObservationDegradationReason,
        DurableChatObservationHealthV1,
        DurableChatObservationV1,
        DurableChatProgressObservationV1,
        DurableChatProgressV1,
        DurableChatRunStatusObservationV1,
        DurableChatTerminalObservationV1,
        DurableChatToolObservationV1,
        DurableChatToolProducerV1,
        DurableChatToolProgressV1,
        DurableChatToolState,
    )

    payload = frame.payload
    observation: DurableChatObservationV1
    if isinstance(payload, DurableRunStartedPayloadV1):
        observation = DurableChatRunStatusObservationV1(
            run_id=frame.run_id,
            session_id=initialization.session_id,
            observed_at=frame.timestamp,
            status=payload.status,
            phase="run_started",
        )
    elif isinstance(payload, DurableAssistantDeltaPayloadV1):
        observation = DurableChatAssistantTextObservationV1(
            run_id=frame.run_id,
            session_id=initialization.session_id,
            observed_at=frame.timestamp,
            producer=DurableChatModelProducerV1(
                step_index=0,
                observation_epoch=1,
            ),
            delta=payload.text,
        )
    elif isinstance(payload, DurableToolEventPayloadV1):
        observation = DurableChatToolObservationV1(
            run_id=frame.run_id,
            session_id=initialization.session_id,
            observed_at=frame.timestamp,
            progress=DurableChatToolProgressV1(
                producer=DurableChatToolProducerV1(call_key=payload.call_key),
                step_index=0,
                tool_name=payload.tool_name,
                provenance=ToolProvenance.RUNTIME,
                state=(
                    DurableChatToolState.STARTED
                    if payload.status is None
                    else DurableChatToolState(payload.status.value)
                ),
                updated_at=frame.timestamp,
            ),
        )
    elif isinstance(payload, DurableHumanInputRequiredPayloadV1):
        observation = DurableChatHumanInputObservationV1(
            run_id=frame.run_id,
            session_id=initialization.session_id,
            observed_at=frame.timestamp,
            request_id=payload.request_id,
            state=HumanInputState.PENDING,
            expires_at=payload.expires_at,
        )
    elif isinstance(payload, DurableTerminalEventPayloadV1):
        observation = DurableChatTerminalObservationV1(
            run_id=frame.run_id,
            session_id=initialization.session_id,
            observed_at=frame.timestamp,
            status=payload.status,
            result_available=payload.result_available,
            error_code=payload.error_code,
        )
    elif isinstance(payload, DurableObservationDegradedPayloadV1):
        observation = DurableChatDegradedObservationV1(
            run_id=frame.run_id,
            session_id=initialization.session_id,
            observed_at=frame.timestamp,
            health=DurableChatObservationHealthV1(
                degraded=True,
                reasons=(
                    DurableChatObservationDegradationReason.STORAGE_UNAVAILABLE,
                ),
                dropped_observations=1,
                last_error_code=payload.code,
            ),
        )
    else:
        if isinstance(payload, DurableModelProgressPayloadV1):
            step_index = payload.step_index
            phase = payload.phase
        else:
            step_index = 0
            phase = frame.type.value
        observation = DurableChatProgressObservationV1(
            run_id=frame.run_id,
            session_id=initialization.session_id,
            observed_at=frame.timestamp,
            progress=DurableChatProgressV1(
                status=DurableLoopRunStatus.RUNNING,
                phase=phase,
                model_steps=step_index,
                tool_calls=0,
                human_waits=0,
                step_index=step_index,
                updated_at=frame.timestamp,
            ),
        )
    return DurableChatEventFrameV1(
        sequence=frame.sequence,
        published_revision=frame.sequence,
        event=observation,
    )


def _legacy_replay_to_public(
    page: Any,
    *,
    session_id: str,
) -> DurableRunObservationReplayV2:
    from .durable_chat_protocol import DurableChatReplayDisposition

    disposition = {
        DurableChatReplayDisposition.DELTAS: DurableRunReplayDisposition.DELTAS,
        DurableChatReplayDisposition.SNAPSHOT_REQUIRED: (
            DurableRunReplayDisposition.SNAPSHOT_REQUIRED
        ),
        DurableChatReplayDisposition.CURSOR_AHEAD: (
            DurableRunReplayDisposition.CURSOR_AHEAD
        ),
    }[page.disposition]
    snapshot = (
        _legacy_snapshot_to_public(page.snapshot)
        if page.snapshot is not None
        else None
    )
    return DurableRunObservationReplayV2(
        run_id=page.run_id,
        requested_after_sequence=page.requested_after_sequence,
        disposition=disposition,
        through_sequence=page.through_sequence,
        snapshot=snapshot,
        events=tuple(
            _legacy_frame_to_public(frame, session_id=session_id)
            for frame in page.events
        ),
    )


def _legacy_snapshot_to_public(snapshot: Any) -> DurableRunObservationSnapshotV2:
    projection = snapshot.projection
    tools = tuple(
        DurableRunToolProjectionV2(
            call_key=tool.producer.call_key,
            tool_name=tool.tool_name,
            status=_legacy_tool_status(tool.state),
            updated_at=tool.updated_at,
        )
        for tool in projection.tool_progress
    )
    public_projection = DurableRunObservationProjectionV2(
        run_id=projection.run_id,
        session_id=projection.session_id,
        through_sequence=projection.through_sequence,
        status=projection.progress.status,
        phase=projection.progress.phase,
        updated_at=projection.progress.updated_at,
        model_step=projection.progress.step_index,
        assistant_text=projection.draft.text if projection.draft is not None else "",
        tools=tools,
        degraded=(
            DurableObservationDegradedPayloadV1(
                code=projection.observation_health.last_error_code or "events_unavailable",
                status_link=f"/experimental/durable-agent-runs/{projection.run_id}",
                result_link=(
                    f"/experimental/durable-agent-runs/{projection.run_id}/result"
                ),
            )
            if projection.observation_health.degraded
            else None
        ),
    )
    return DurableRunObservationSnapshotV2(
        run_id=projection.run_id,
        sequence=projection.through_sequence,
        timestamp=snapshot.captured_at,
        projection=public_projection,
    )


def _legacy_frame_to_public(
    frame: Any,
    *,
    session_id: str,
) -> DurablePublicEventV2:
    from .durable_chat_protocol import (
        DurableChatAssistantDraftReplacedObservationV1,
        DurableChatAssistantTextObservationV1,
        DurableChatDegradedObservationV1,
        DurableChatHumanInputObservationV1,
        DurableChatModelAttemptObservationV1,
        DurableChatProgressObservationV1,
        DurableChatRunStatusObservationV1,
        DurableChatSandboxObservationEventV1,
        DurableChatTerminalObservationV1,
        DurableChatToolObservationV1,
    )

    observation = frame.event
    event_type = DurablePublicEventType.MODEL_PROGRESS
    payload: DurablePublicEventPayload
    if isinstance(observation, DurableChatAssistantTextObservationV1):
        event_type = DurablePublicEventType.ASSISTANT_DELTA
        payload = DurableAssistantDeltaPayloadV1(text=observation.delta)
    elif isinstance(observation, DurableChatToolObservationV1):
        event_type = (
            DurablePublicEventType.TOOL_STARTED
            if _legacy_tool_status(observation.progress.state) is None
            else DurablePublicEventType.TOOL_COMPLETED
        )
        payload = DurableToolEventPayloadV1(
            call_key=observation.progress.producer.call_key,
            tool_name=observation.progress.tool_name,
            status=_legacy_tool_status(observation.progress.state),
        )
    elif isinstance(observation, DurableChatHumanInputObservationV1) and (
        observation.state is HumanInputState.PENDING
    ):
        event_type = DurablePublicEventType.HUMAN_INPUT_REQUIRED
        payload = DurableHumanInputRequiredPayloadV1(
            request_id=observation.request_id,
            expires_at=observation.expires_at,
            input_link=(
                f"/experimental/durable-agent-runs/{observation.run_id}/"
                f"input/{observation.request_id}"
            ),
        )
    elif isinstance(observation, DurableChatTerminalObservationV1):
        event_type = {
            DurableLoopRunStatus.COMPLETED: DurablePublicEventType.RUN_COMPLETED,
            DurableLoopRunStatus.FAILED: DurablePublicEventType.RUN_FAILED,
            DurableLoopRunStatus.CANCELLED: DurablePublicEventType.RUN_CANCELLED,
        }[observation.status]
        payload = DurableTerminalEventPayloadV1(
            status=observation.status,
            result_available=observation.result_available,
            error_code=observation.error_code,
        )
    elif isinstance(observation, DurableChatDegradedObservationV1):
        event_type = DurablePublicEventType.OBSERVATION_DEGRADED
        payload = DurableObservationDegradedPayloadV1(
            code=observation.health.last_error_code or "events_unavailable",
            status_link=f"/experimental/durable-agent-runs/{observation.run_id}",
            result_link=(
                f"/experimental/durable-agent-runs/{observation.run_id}/result"
            ),
        )
    elif isinstance(observation, DurableChatProgressObservationV1):
        payload = DurableModelProgressPayloadV1(
            step_index=observation.progress.step_index,
            phase=observation.progress.phase,
        )
    elif isinstance(observation, DurableChatModelAttemptObservationV1):
        payload = DurableModelProgressPayloadV1(
            step_index=observation.producer.step_index,
            phase=f"model_{observation.state.value}",
        )
    elif isinstance(observation, DurableChatRunStatusObservationV1):
        payload = DurableModelProgressPayloadV1(
            step_index=0,
            phase=observation.phase,
        )
    elif isinstance(observation, DurableChatSandboxObservationEventV1):
        payload = DurableModelProgressPayloadV1(
            step_index=observation.observation.step_index,
            phase=f"sandbox_{observation.observation.state.value}",
        )
    elif isinstance(observation, DurableChatAssistantDraftReplacedObservationV1):
        payload = DurableModelProgressPayloadV1(
            step_index=observation.producer.step_index,
            phase="assistant_draft_replaced",
        )
    else:
        payload = DurableModelProgressPayloadV1(
            step_index=0,
            phase="human_input_resolved",
        )
    return DurablePublicEventV2(
        run_id=observation.run_id,
        sequence=frame.sequence,
        timestamp=observation.observed_at,
        type=event_type,
        payload=payload,
    )


def _legacy_tool_status(value: Any) -> ToolResultStatus | None:
    raw = getattr(value, "value", value)
    if raw == "started":
        return None
    try:
        return ToolResultStatus(raw)
    except ValueError:
        return ToolResultStatus.FAILED


def _is_terminal_event_type(event_type: DurablePublicEventType) -> bool:
    return event_type in {
        DurablePublicEventType.RUN_COMPLETED,
        DurablePublicEventType.RUN_FAILED,
        DurablePublicEventType.RUN_CANCELLED,
    }


def _legacy_journal(selected: DurableRunObservationJournal) -> Any:
    if all(
        hasattr(selected, name)
        for name in ("load_run_initialization", "publish", "replay")
    ):
        return selected
    from .durable_chat_journal import get_durable_chat_journal

    return get_durable_chat_journal()


def _apply_public_event(
    projection: DurableRunObservationProjectionV2,
    event: DurablePublicEventV2,
    *,
    session_id: str,
) -> DurableRunObservationProjectionV2:
    update: dict[str, object] = {
        "through_sequence": event.sequence,
        "updated_at": event.timestamp,
    }
    payload = event.payload
    if isinstance(payload, DurableRunStartedPayloadV1):
        update.update(status=payload.status, phase="run_started")
    elif isinstance(payload, DurableSkillSearchPayloadV1):
        update.update(
            phase="skill_search_completed",
            completed_skill_searches=projection.completed_skill_searches + 1,
        )
    elif isinstance(payload, DurableSkillLoadPayloadV1):
        update.update(
            phase=event.type.value,
            completed_skill_loads=(
                projection.completed_skill_loads
                + (1 if event.type is DurablePublicEventType.SKILL_LOAD_COMPLETED else 0)
            ),
        )
    elif isinstance(payload, DurableModelProgressPayloadV1):
        update.update(
            phase=payload.phase,
            model_step=payload.step_index,
            status=DurableLoopRunStatus.RUNNING,
        )
    elif isinstance(payload, DurableAssistantDeltaPayloadV1):
        update.update(
            phase="assistant_delta",
            assistant_text=_append_bounded_text(projection.assistant_text, payload.text),
            status=DurableLoopRunStatus.RUNNING,
        )
    elif isinstance(payload, DurableToolEventPayloadV1):
        tools = {tool.call_key: tool for tool in projection.tools}
        tools[payload.call_key] = DurableRunToolProjectionV2(
            call_key=payload.call_key,
            tool_name=payload.tool_name,
            status=payload.status,
            updated_at=event.timestamp,
        )
        update.update(
            phase=event.type.value,
            tools=tuple(tools[key] for key in sorted(tools)),
            status=DurableLoopRunStatus.RUNNING,
        )
    elif isinstance(payload, DurableHumanInputRequiredPayloadV1):
        update.update(
            phase="human_input_required",
            human_input=payload,
            status=DurableLoopRunStatus.WAITING,
        )
    elif isinstance(payload, DurableMessageCommittedPayloadV1):
        update.update(phase="message_committed")
    elif isinstance(payload, DurableTerminalEventPayloadV1):
        update.update(
            phase=event.type.value,
            status=payload.status,
            terminal=payload,
            human_input=None,
        )
    elif isinstance(payload, DurableObservationDegradedPayloadV1):
        update.update(phase="observation_degraded", degraded=payload)
    return projection.model_copy(update=update)


def _append_bounded_text(current: str, delta: str) -> str:
    combined = (current + delta).encode("utf-8")
    if len(combined) <= MAX_DURABLE_RUN_ASSISTANT_TEXT_BYTES:
        return combined.decode("utf-8")
    return combined[-MAX_DURABLE_RUN_ASSISTANT_TEXT_BYTES :].decode(
        "utf-8",
        errors="ignore",
    )


def _parse_model[ModelT: BaseModel](
    payload: bytes,
    model: type[ModelT],
) -> ModelT:
    try:
        decoded = decode_json_object(payload)
        return model.model_validate_json(canonical_json_bytes(decoded))
    except (UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise DurableRunObservationError(
            "durable run observation document is invalid"
        ) from exc


def _journal_key(run_id: str) -> str:
    return "durable-run-observations/journal/" + canonical_hash({"run_id": run_id})


def _initialization_key(run_id: str) -> str:
    return "durable-run-observations/initialization/" + canonical_hash(
        {"run_id": run_id}
    )


def _safe_deadline(value: datetime | None) -> datetime:
    bounded = datetime.now(UTC) + timedelta(seconds=_OPERATION_DEADLINE_SECONDS)
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return bounded
    return min(value.astimezone(UTC), bounded)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observation timestamps must be timezone-aware")
    return value.astimezone(UTC)


async def _await[ValueT](
    awaitable: Awaitable[ValueT],
    deadline: datetime,
) -> ValueT:
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError("durable run observation deadline elapsed")
    async with asyncio.timeout(remaining):
        return await awaitable
