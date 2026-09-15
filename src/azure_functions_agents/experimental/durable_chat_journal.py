"""Durable, bounded observation storage for the private durable-chat UI."""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .._logger import logger
from ..strict_json import canonical_json_bytes
from .durable_chat_protocol import (
    DURABLE_CHAT_PUBLISHER_DRAIN_DEADLINE_SECONDS,
    MAX_DURABLE_CHAT_BATCH_BYTES,
    MAX_DURABLE_CHAT_CAS_ATTEMPTS,
    MAX_DURABLE_CHAT_EVENT_BATCH,
    MAX_DURABLE_CHAT_PENDING_OBSERVATIONS,
    MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS,
    MAX_DURABLE_CHAT_PRODUCER_EPOCHS,
    MAX_DURABLE_CHAT_REPLAY_EVENTS,
    MAX_DURABLE_CHAT_SANDBOX_OBSERVATIONS,
    MAX_DURABLE_CHAT_SNAPSHOT_BYTES,
    MAX_DURABLE_CHAT_TOTAL_RESERVED_OBSERVATION_BYTES,
    DurableChatAssistantDraftReplacedObservationV1,
    DurableChatAssistantDraftV1,
    DurableChatAssistantTextObservationV1,
    DurableChatDegradedObservationV1,
    DurableChatDiagnosticsV1,
    DurableChatDrainResultV1,
    DurableChatEnqueueDisposition,
    DurableChatEnqueueResultV1,
    DurableChatEventFrameV1,
    DurableChatJournalPort,
    DurableChatModelAttemptObservationV1,
    DurableChatModelAttemptState,
    DurableChatModelProducerV1,
    DurableChatObservationBatchV1,
    DurableChatObservationDegradationReason,
    DurableChatObservationDrainerPort,
    DurableChatObservationHealthV1,
    DurableChatObservationSink,
    DurableChatObservationV1,
    DurableChatProducerEpochV1,
    DurableChatProgressObservationV1,
    DurableChatProgressV1,
    DurableChatPublicationDisposition,
    DurableChatPublicationResultV1,
    DurableChatReplayDisposition,
    DurableChatReplayPageV1,
    DurableChatRunInitializationPort,
    DurableChatRunInitializationV1,
    DurableChatRunProjectionV1,
    DurableChatRunStatusObservationV1,
    DurableChatSandboxObservationEventV1,
    DurableChatSandboxObservationV1,
    DurableChatSandboxState,
    DurableChatSnapshotFrameV1,
    DurableChatTerminalObservationV1,
    DurableChatToolObservationV1,
    DurableChatToolProgressV1,
    DurableChatToolState,
    parse_durable_chat_document,
)
from .durable_loop_protocol import ContentRefV1, DurableLoopRunStatus, canonical_hash

if TYPE_CHECKING:
    from .durable_loop_activities import DurableContentStore
    from .durable_loop_receipts import DurableKeyedDocumentStore, KeyedDocument

_READ_DEADLINE_SECONDS = 10.0
_RETAINED_EVENT_LIMIT = MAX_DURABLE_CHAT_REPLAY_EVENTS
_TERMINAL_TOOL_STATES = frozenset(
    {
        DurableChatToolState.SUCCEEDED,
        DurableChatToolState.FAILED,
        DurableChatToolState.TIMED_OUT,
        DurableChatToolState.CANCELLED,
        DurableChatToolState.AMBIGUOUS,
    }
)
_SANDBOX_STATE_ORDER = {
    DurableChatSandboxState.NOT_ALLOCATED: 0,
    DurableChatSandboxState.REMOTE_NO_SANDBOX: 0,
    DurableChatSandboxState.UNAVAILABLE: 0,
    DurableChatSandboxState.EXECUTING: 1,
    DurableChatSandboxState.RETAINED_IDLE: 2,
    DurableChatSandboxState.DELETE_REQUESTED: 3,
    DurableChatSandboxState.CONFIRMED_DELETED: 4,
    DurableChatSandboxState.REPLACEMENT_INSTANCE: 5,
    DurableChatSandboxState.STALE: 6,
}


class DurableChatJournalError(RuntimeError):
    """A best-effort journal operation could not safely read published state."""


class DurableChatInitializationError(RuntimeError):
    """Authoritative admitted-run initialization could not be reconciled."""


class _ObservationByteLimitExceededError(DurableChatJournalError):
    """A compacted projection cannot be represented within the observation quota."""


class _JournalModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class _JournalBatchReferenceV1(_JournalModel):
    """One immutable batch made visible by a single manifest revision."""

    first_sequence: int = Field(ge=1)
    through_sequence: int = Field(ge=1)
    event_count: int = Field(ge=1, le=MAX_DURABLE_CHAT_EVENT_BATCH)
    published_revision: int | None = Field(default=None, ge=1)
    reference: ContentRefV1

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.through_sequence - self.first_sequence + 1 != self.event_count:
            raise ValueError("durable-chat batch sequence range is invalid")
        return self


class _DurableChatJournalManifestV1(_JournalModel):
    """Private manifest whose content references are the publication boundary."""

    schema_version: str = "1"
    run_id: str
    published_revision: int = Field(ge=0)
    through_sequence: int = Field(ge=0)
    snapshot_ref: ContentRefV1 | None = None
    snapshot_through_sequence: int = Field(default=0, ge=0)
    batches: tuple[_JournalBatchReferenceV1, ...] = Field(
        default=(),
        max_length=_RETAINED_EVENT_LIMIT,
    )
    projection: DurableChatRunProjectionV1 | None = None
    terminal_published: bool = False
    producer_epochs: tuple[tuple[int, int], ...] = Field(
        default=(),
        max_length=MAX_DURABLE_CHAT_PRODUCER_EPOCHS,
    )
    active_producer_epochs: tuple[tuple[int, int], ...] = Field(
        default=(),
        max_length=MAX_DURABLE_CHAT_PRODUCER_EPOCHS,
    )
    closed_model_producers: tuple[tuple[int, int], ...] = Field(
        default=(),
        max_length=MAX_DURABLE_CHAT_PRODUCER_EPOCHS,
    )
    health: DurableChatObservationHealthV1 = DurableChatObservationHealthV1()
    reserved_observation_bytes: int = Field(
        ge=0,
        le=MAX_DURABLE_CHAT_TOTAL_RESERVED_OBSERVATION_BYTES,
    )

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:  # noqa: PLR0912
        steps = [step for step, _ in self.producer_epochs]
        if len(steps) != len(set(steps)):
            raise ValueError("durable-chat manifest producer steps must be unique")
        if any(
            step < 0
            or epoch < 1
            or epoch > MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS
            for step, epoch in self.producer_epochs
        ):
            raise ValueError("durable-chat manifest producer epoch is invalid")
        active_steps = [step for step, _ in self.active_producer_epochs]
        if len(active_steps) != len(set(active_steps)):
            raise ValueError("durable-chat manifest active producer steps must be unique")
        reserved_epochs = dict(self.producer_epochs)
        if any(
            step < 0
            or epoch < 1
            or epoch > reserved_epochs.get(step, 0)
            for step, epoch in self.active_producer_epochs
        ):
            raise ValueError("durable-chat manifest active producer epoch is invalid")
        if len(self.closed_model_producers) != len(set(self.closed_model_producers)):
            raise ValueError("durable-chat manifest closed model producers must be unique")
        if any(
            step < 0
            or epoch < 1
            or epoch > reserved_epochs.get(step, 0)
            for step, epoch in self.closed_model_producers
        ):
            raise ValueError("durable-chat manifest closed model producer is invalid")
        if self.snapshot_ref is None:
            if self.snapshot_through_sequence != 0:
                raise ValueError("durable-chat manifest snapshot watermark is invalid")
        elif not 0 < self.snapshot_through_sequence <= self.through_sequence:
            raise ValueError("durable-chat manifest snapshot range is invalid")
        expected_sequence = self.snapshot_through_sequence + 1
        for batch in self.batches:
            if batch.first_sequence != expected_sequence:
                raise ValueError("durable-chat manifest batches are not contiguous")
            if (
                batch.published_revision is not None
                and batch.published_revision > self.published_revision
            ):
                raise ValueError(
                    "durable-chat manifest batch revision exceeds the manifest"
                )
            expected_sequence = batch.through_sequence + 1
        expected_through = expected_sequence - 1
        if self.through_sequence != expected_through:
            raise ValueError("durable-chat manifest watermark is invalid")
        if self.projection is None:
            if self.published_revision or self.through_sequence:
                raise ValueError("durable-chat manifest is missing its projection")
        elif (
            self.projection.run_id != self.run_id
            or self.projection.published_revision != self.published_revision
            or self.projection.through_sequence != self.through_sequence
        ):
            raise ValueError("durable-chat manifest projection is inconsistent")
        if self.health.reserved_observation_bytes != self.reserved_observation_bytes:
            raise ValueError("durable-chat manifest reservation accounting is inconsistent")
        return self


@dataclass(frozen=True, slots=True)
class _ManifestSnapshot:
    """A parsed manifest paired with the keyed-store compare-and-swap revision."""

    manifest: _DurableChatJournalManifestV1
    revision: str


@dataclass(frozen=True, slots=True)
class _Reservation:
    """The manifest state after a durable byte allowance was atomically reserved."""

    snapshot: _ManifestSnapshot | None
    health: DurableChatObservationHealthV1
    allowed: bool


@dataclass(frozen=True, slots=True)
class _FilteredObservations:
    """Accepted observations and their resulting active model-attempt fence."""

    observations: tuple[DurableChatObservationV1, ...]
    active_producer_epochs: tuple[tuple[int, int], ...]
    closed_model_producers: tuple[tuple[int, int], ...]


@dataclass(slots=True)
class _ProjectionState:
    """Mutable assembly state used only while publishing one immutable projection."""

    progress: DurableChatProgressV1
    draft: DurableChatAssistantDraftV1 | None
    tool_progress: dict[str, DurableChatToolProgressV1]
    sandboxes: dict[tuple[str, str, str, str], DurableChatSandboxObservationV1]
    health: DurableChatObservationHealthV1


class DurableChatJournal(DurableChatRunInitializationPort, DurableChatJournalPort):
    """Publish immutable batches and a CAS manifest without execution coupling."""

    def __init__(
        self,
        *,
        content: DurableContentStore,
        documents: DurableKeyedDocumentStore,
    ) -> None:
        self._content = content
        self._documents = documents

    async def load_run_initialization(
        self,
        *,
        run_id: str,
    ) -> DurableChatRunInitializationV1 | None:
        """Load the create-once admitted-run metadata."""
        document = await self._get_document(
            _initialization_key(run_id),
            _deadline_after(_READ_DEADLINE_SECONDS),
            error_type=DurableChatInitializationError,
        )
        if document is None:
            return None
        try:
            initialization = parse_durable_chat_document(
                document.payload,
                DurableChatRunInitializationV1,
            )
        except (UnicodeDecodeError, ValidationError, ValueError) as exc:
            raise DurableChatInitializationError(
                "durable-chat initialization record is invalid"
            ) from exc
        if initialization.run_id != run_id:
            raise DurableChatInitializationError(
                "durable-chat initialization record does not match the run"
            )
        return initialization

    async def create_run_initialization_once(
        self,
        *,
        initialization: DurableChatRunInitializationV1,
    ) -> DurableChatRunInitializationV1:
        """Create only one immutable initialization and return its winner."""
        key = _initialization_key(initialization.run_id)
        deadline = _deadline_after(_READ_DEADLINE_SECONDS)
        payload = canonical_json_bytes(initialization)
        for _ in range(MAX_DURABLE_CHAT_CAS_ATTEMPTS):
            existing = await self._get_document(
                key,
                deadline,
                error_type=DurableChatInitializationError,
            )
            if existing is not None:
                winner = await self.load_run_initialization(run_id=initialization.run_id)
                if winner is None:
                    continue
                return winner
            try:
                created = await _await_deadline(
                    self._documents.create(key, payload),
                    deadline,
                )
            except TimeoutError as exc:
                raise DurableChatInitializationError(
                    "durable-chat initialization creation timed out"
                ) from exc
            except Exception as exc:
                raise DurableChatInitializationError(
                    "durable-chat initialization creation failed"
                ) from exc
            if created:
                return initialization
        raise DurableChatInitializationError(
            "durable-chat initialization creation exhausted compare-and-swap attempts"
        )

    async def reserve_model_producers(
        self,
        *,
        run_id: str,
        step_index: int,
        count: int,
        deadline: datetime,
    ) -> tuple[DurableChatModelProducerV1, ...]:
        """Reserve a bounded monotonic epoch range outside Durable activity retries."""
        fallback_deadline = _safe_deadline(deadline)
        if step_index < 0 or count < 1 or count > MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS:
            await self._best_effort_mark_degraded(
                run_id,
                DurableChatObservationDegradationReason.PRODUCER_ATTEMPTS_EXHAUSTED,
                fallback_deadline,
            )
            return ()
        try:
            for _ in range(MAX_DURABLE_CHAT_CAS_ATTEMPTS):
                snapshot = await self._read_manifest(run_id, fallback_deadline)
                manifest = (
                    snapshot.manifest
                    if snapshot is not None
                    else _empty_manifest(run_id)
                )
                epochs = dict(manifest.producer_epochs)
                previous_epoch = epochs.get(step_index, 0)
                if previous_epoch + count > MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS:
                    await self._best_effort_mark_degraded(
                        run_id,
                        DurableChatObservationDegradationReason.PRODUCER_ATTEMPTS_EXHAUSTED,
                        fallback_deadline,
                    )
                    return ()
                epochs[step_index] = previous_epoch + count
                updated = _replace_manifest(
                    manifest,
                    producer_epochs=tuple(sorted(epochs.items())),
                )
                if await self._write_manifest(
                    run_id,
                    updated,
                    snapshot,
                    fallback_deadline,
                ):
                    return tuple(
                        DurableChatModelProducerV1(
                            step_index=step_index,
                            observation_epoch=epoch,
                        )
                        for epoch in range(previous_epoch + 1, previous_epoch + count + 1)
                    )
        except TimeoutError:
            reason = DurableChatObservationDegradationReason.PUBLISH_TIMEOUT
        except Exception as exc:
            reason = DurableChatObservationDegradationReason.STORAGE_UNAVAILABLE
            if _has_timeout_cause(exc):
                reason = DurableChatObservationDegradationReason.PUBLISH_TIMEOUT
        else:
            reason = DurableChatObservationDegradationReason.CAS_ATTEMPTS_EXHAUSTED
        await self._best_effort_mark_degraded(run_id, reason, fallback_deadline)
        return ()

    async def publish(
        self,
        *,
        run_id: str,
        expected_published_revision: int,
        batch: DurableChatObservationBatchV1,
        deadline: datetime,
    ) -> DurableChatPublicationResultV1:
        """Write batch content first and only expose it through a CAS manifest."""
        if expected_published_revision < 0 or batch.run_id != run_id:
            return _degraded_publication(
                run_id,
                health=_degraded_health(
                    DurableChatObservationHealthV1(),
                    DurableChatObservationDegradationReason.STORAGE_UNAVAILABLE,
                    dropped_observations=len(batch.observations),
                ),
            )
        try:
            return await self._publish(
                run_id=run_id,
                batch=batch,
                deadline=_safe_deadline(deadline),
            )
        except _ObservationByteLimitExceededError:
            return await self._publication_failure(
                run_id,
                DurableChatObservationDegradationReason.OBSERVATION_BYTE_LIMIT,
                len(batch.observations),
                _safe_deadline(deadline),
            )
        except TimeoutError:
            return await self._publication_failure(
                run_id,
                DurableChatObservationDegradationReason.PUBLISH_TIMEOUT,
                len(batch.observations),
                _safe_deadline(deadline),
            )
        except Exception as exc:
            logger.warning(
                "durable-chat observation publication failed: error_type=%s",
                "storage_error",
            )
            return await self._publication_failure(
                run_id,
                (
                    DurableChatObservationDegradationReason.PUBLISH_TIMEOUT
                    if _has_timeout_cause(exc)
                    else DurableChatObservationDegradationReason.STORAGE_UNAVAILABLE
                ),
                len(batch.observations),
                _safe_deadline(deadline),
            )

    async def replay(  # noqa: PLR0912
        self,
        *,
        run_id: str,
        after_sequence: int,
        limit: int,
    ) -> DurableChatReplayPageV1:
        """Read a consistent snapshot or bounded contiguous deltas."""
        if after_sequence < 0:
            raise ValueError("durable-chat replay cursor must be non-negative")
        bounded_limit = min(max(limit, 1), MAX_DURABLE_CHAT_REPLAY_EVENTS)
        deadline = _deadline_after(_READ_DEADLINE_SECONDS)
        snapshot = await self._read_manifest(
            run_id,
            deadline,
        )
        if snapshot is None:
            return DurableChatReplayPageV1(
                run_id=run_id,
                requested_after_sequence=after_sequence,
                disposition=(
                    DurableChatReplayDisposition.CURSOR_AHEAD
                    if after_sequence
                    else DurableChatReplayDisposition.DELTAS
                ),
                through_sequence=0,
            )
        manifest = snapshot.manifest
        if after_sequence > manifest.through_sequence:
            return DurableChatReplayPageV1(
                run_id=run_id,
                requested_after_sequence=after_sequence,
                disposition=DurableChatReplayDisposition.CURSOR_AHEAD,
                through_sequence=manifest.through_sequence,
            )
        expected_session_id = (
            manifest.projection.session_id
            if manifest.projection is not None
            else None
        )

        snapshot_frame: DurableChatSnapshotFrameV1 | None = None
        minimum_sequence = after_sequence
        disposition = DurableChatReplayDisposition.DELTAS
        if (
            manifest.snapshot_ref is not None
            and after_sequence < manifest.snapshot_through_sequence
        ):
            snapshot_frame = await self._read_snapshot(manifest.snapshot_ref, deadline)
            if (
                snapshot_frame.projection.run_id != run_id
                or snapshot_frame.projection.through_sequence
                != manifest.snapshot_through_sequence
                or (
                    expected_session_id is not None
                    and snapshot_frame.projection.session_id != expected_session_id
                )
            ):
                raise DurableChatJournalError("durable-chat snapshot is inconsistent")
            minimum_sequence = manifest.snapshot_through_sequence
            disposition = DurableChatReplayDisposition.SNAPSHOT_REQUIRED

        events: list[DurableChatEventFrameV1] = []
        for reference in manifest.batches:
            if reference.through_sequence <= minimum_sequence:
                continue
            stored = await self._read_batch(reference.reference, deadline)
            if (
                stored.run_id != run_id
                or len(stored.observations) != reference.event_count
            ):
                raise DurableChatJournalError("durable-chat batch event count is invalid")
            for offset, observation in enumerate(stored.observations):
                if observation.run_id != run_id:
                    raise DurableChatJournalError(
                        "durable-chat batch observation does not match the run"
                    )
                if expected_session_id is None:
                    expected_session_id = observation.session_id
                elif observation.session_id != expected_session_id:
                    raise DurableChatJournalError(
                        "durable-chat batch observation does not match the session"
                    )
                sequence = reference.first_sequence + offset
                if sequence <= minimum_sequence:
                    continue
                events.append(
                    DurableChatEventFrameV1(
                        sequence=sequence,
                        published_revision=reference.published_revision,
                        event=observation,
                    )
                )
                if len(events) >= bounded_limit:
                    return DurableChatReplayPageV1(
                        run_id=run_id,
                        requested_after_sequence=after_sequence,
                        disposition=disposition,
                        through_sequence=manifest.through_sequence,
                        snapshot=snapshot_frame,
                        events=tuple(events),
                    )
        return DurableChatReplayPageV1(
            run_id=run_id,
            requested_after_sequence=after_sequence,
            disposition=disposition,
            through_sequence=manifest.through_sequence,
            snapshot=snapshot_frame,
            events=tuple(events),
        )

    async def read_diagnostics(
        self,
        *,
        run_id: str,
    ) -> DurableChatDiagnosticsV1 | None:
        """Read only historical sandbox observations and bounded journal health."""
        initialization = await self.load_run_initialization(run_id=run_id)
        if initialization is None:
            return None
        snapshot = await self._read_manifest(
            run_id,
            _deadline_after(_READ_DEADLINE_SECONDS),
        )
        if snapshot is None or snapshot.manifest.projection is None:
            return DurableChatDiagnosticsV1(
                run_id=run_id,
                session_id=initialization.session_id,
                model_mode=initialization.ui.model_mode,
                status=DurableLoopRunStatus.PENDING,
                created_at=initialization.created_at,
                updated_at=initialization.created_at,
                expires_at=initialization.expires_at,
                committed_generation=initialization.committed_generation,
                configured_sandbox_group_resource_id=(
                    initialization.diagnostics.sandbox_group_resource_id
                    if initialization.diagnostics is not None
                    else None
                ),
                observation_health=(
                    snapshot.manifest.health
                    if snapshot is not None
                    else DurableChatObservationHealthV1()
                ),
            )
        projection = snapshot.manifest.projection
        if projection.session_id != initialization.session_id:
            raise DurableChatJournalError(
                "durable-chat diagnostics projection does not match the session"
            )
        return DurableChatDiagnosticsV1(
            run_id=run_id,
            session_id=initialization.session_id,
            model_mode=initialization.ui.model_mode,
            status=projection.progress.status,
            created_at=initialization.created_at,
            updated_at=min(projection.progress.updated_at, initialization.expires_at),
            expires_at=initialization.expires_at,
            committed_generation=initialization.committed_generation,
            configured_sandbox_group_resource_id=(
                initialization.diagnostics.sandbox_group_resource_id
                if initialization.diagnostics is not None
                else None
            ),
            sandbox_observations=projection.sandbox_observations,
            observation_health=snapshot.manifest.health,
        )

    async def _publish(
        self,
        *,
        run_id: str,
        batch: DurableChatObservationBatchV1,
        deadline: datetime,
    ) -> DurableChatPublicationResultV1:
        for _ in range(MAX_DURABLE_CHAT_CAS_ATTEMPTS):
            current = await self._read_manifest(run_id, deadline)
            manifest = current.manifest if current is not None else _empty_manifest(run_id)
            filtered = _filter_observations(manifest, batch.observations)
            observations = filtered.observations
            if not observations:
                return DurableChatPublicationResultV1(
                    run_id=run_id,
                    disposition=DurableChatPublicationDisposition.STALE_PRODUCER,
                    published_revision=manifest.published_revision,
                    through_sequence=manifest.through_sequence,
                    health=manifest.health,
                )
            stored_batch = DurableChatObservationBatchV1(
                run_id=run_id,
                observations=observations,
            )
            compacting = _should_compact(manifest, len(observations))
            reserved_bytes = (
                _snapshot_reservation_bytes(
                    manifest,
                    observations,
                    filtered.active_producer_epochs,
                )
                if compacting
                else len(canonical_json_bytes(stored_batch))
            )
            reservation = await self._reserve_bytes(
                run_id,
                reserved_bytes,
                len(observations),
                deadline,
            )
            if not reservation.allowed or reservation.snapshot is None:
                return _degraded_publication(
                    run_id,
                    health=reservation.health,
                    published_revision=(
                        reservation.snapshot.manifest.published_revision
                        if reservation.snapshot is not None
                        else manifest.published_revision
                    ),
                    through_sequence=(
                        reservation.snapshot.manifest.through_sequence
                        if reservation.snapshot is not None
                        else manifest.through_sequence
                    ),
                )
            current = reservation.snapshot
            manifest = current.manifest
            filtered = _filter_observations(manifest, batch.observations)
            observations = filtered.observations
            if not observations:
                return DurableChatPublicationResultV1(
                    run_id=run_id,
                    disposition=DurableChatPublicationDisposition.STALE_PRODUCER,
                    published_revision=manifest.published_revision,
                    through_sequence=manifest.through_sequence,
                    health=manifest.health,
                )
            stored_batch = DurableChatObservationBatchV1(
                run_id=run_id,
                observations=observations,
            )
            if _should_compact(manifest, len(observations)):
                projection = _apply_projection(
                    manifest,
                    observations=observations,
                    published_revision=manifest.published_revision + 1,
                    through_sequence=manifest.through_sequence + len(observations),
                    active_producer_epochs=filtered.active_producer_epochs,
                )
                try:
                    snapshot = DurableChatSnapshotFrameV1(
                        captured_at=_now(),
                        projection=projection,
                    )
                    snapshot_payload = canonical_json_bytes(snapshot)
                except ValueError as exc:
                    raise _ObservationByteLimitExceededError(
                        "durable-chat snapshot exceeds its limit"
                    ) from exc
                if len(snapshot_payload) > reserved_bytes:
                    # A concurrent reservation can change the compacted health
                    # projection. Retry before exposing an unreserved blob.
                    continue
                snapshot_ref = await self._write_content(
                    kind="durable-chat-observation-snapshot",
                    payload=snapshot_payload,
                    deadline=deadline,
                )
                candidate = _replace_manifest(
                    manifest,
                    published_revision=manifest.published_revision + 1,
                    through_sequence=manifest.through_sequence + len(observations),
                    projection=projection,
                    health=projection.observation_health,
                    terminal_published=manifest.terminal_published
                    or any(
                        isinstance(observation, DurableChatTerminalObservationV1)
                        for observation in observations
                    ),
                    snapshot_ref=snapshot_ref,
                    snapshot_through_sequence=manifest.through_sequence
                    + len(observations),
                    batches=(),
                    active_producer_epochs=filtered.active_producer_epochs,
                    closed_model_producers=filtered.closed_model_producers,
                )
            else:
                first_sequence = manifest.through_sequence + 1
                batch_reference = _JournalBatchReferenceV1(
                    first_sequence=first_sequence,
                    through_sequence=first_sequence + len(observations) - 1,
                    event_count=len(observations),
                    published_revision=manifest.published_revision + 1,
                    reference=await self._write_content(
                        kind="durable-chat-observation-batch",
                        payload=canonical_json_bytes(stored_batch),
                        deadline=deadline,
                    ),
                )
                candidate = _candidate_manifest(
                    manifest,
                    observations=observations,
                    batch_reference=batch_reference,
                    active_producer_epochs=filtered.active_producer_epochs,
                    closed_model_producers=filtered.closed_model_producers,
                )
            if await self._write_manifest(
                run_id,
                candidate,
                current,
                deadline,
            ):
                return DurableChatPublicationResultV1(
                    run_id=run_id,
                    disposition=DurableChatPublicationDisposition.PUBLISHED,
                    published_revision=candidate.published_revision,
                    through_sequence=candidate.through_sequence,
                    health=candidate.health,
                )
        return await self._publication_failure(
            run_id,
            DurableChatObservationDegradationReason.CAS_ATTEMPTS_EXHAUSTED,
            len(batch.observations),
            deadline,
        )

    async def _publication_failure(
        self,
        run_id: str,
        reason: DurableChatObservationDegradationReason,
        dropped_observations: int,
        deadline: datetime,
    ) -> DurableChatPublicationResultV1:
        health = await self._best_effort_mark_degraded(
            run_id,
            reason,
            deadline,
            dropped_observations=dropped_observations,
        )
        snapshot = await self._read_manifest_or_none(run_id, deadline)
        return _degraded_publication(
            run_id,
            health=health,
            published_revision=(
                snapshot.manifest.published_revision if snapshot is not None else 0
            ),
            through_sequence=(
                snapshot.manifest.through_sequence if snapshot is not None else 0
            ),
        )

    async def _reserve_bytes(
        self,
        run_id: str,
        amount: int,
        dropped_observations: int,
        deadline: datetime,
    ) -> _Reservation:
        if amount < 1 or amount > MAX_DURABLE_CHAT_BATCH_BYTES + MAX_DURABLE_CHAT_SNAPSHOT_BYTES:
            health = await self._best_effort_mark_degraded(
                run_id,
                DurableChatObservationDegradationReason.OBSERVATION_BYTE_LIMIT,
                deadline,
                dropped_observations=dropped_observations,
            )
            return _Reservation(snapshot=None, health=health, allowed=False)
        try:
            for _ in range(MAX_DURABLE_CHAT_CAS_ATTEMPTS):
                snapshot = await self._read_manifest(run_id, deadline)
                manifest = (
                    snapshot.manifest
                    if snapshot is not None
                    else _empty_manifest(run_id)
                )
                updated_total = manifest.reserved_observation_bytes + amount
                if updated_total > MAX_DURABLE_CHAT_TOTAL_RESERVED_OBSERVATION_BYTES:
                    health = _degraded_health(
                        manifest.health,
                        DurableChatObservationDegradationReason.OBSERVATION_BYTE_LIMIT,
                        dropped_observations=dropped_observations,
                    )
                    updated = _replace_manifest(
                        manifest,
                        health=health,
                        reserved_observation_bytes=manifest.reserved_observation_bytes,
                    )
                    if await self._write_manifest(run_id, updated, snapshot, deadline):
                        persisted = await self._read_manifest(run_id, deadline)
                        return _Reservation(
                            snapshot=persisted,
                            health=(
                                persisted.manifest.health
                                if persisted is not None
                                else health
                            ),
                            allowed=False,
                        )
                    continue
                health = manifest.health.model_copy(
                    update={"reserved_observation_bytes": updated_total}
                )
                updated = _replace_manifest(
                    manifest,
                    health=health,
                    reserved_observation_bytes=updated_total,
                )
                if await self._write_manifest(run_id, updated, snapshot, deadline):
                    persisted = await self._read_manifest(run_id, deadline)
                    if persisted is not None:
                        return _Reservation(
                            snapshot=persisted,
                            health=persisted.manifest.health,
                            allowed=True,
                        )
            health = await self._best_effort_mark_degraded(
                run_id,
                DurableChatObservationDegradationReason.CAS_ATTEMPTS_EXHAUSTED,
                deadline,
                dropped_observations=dropped_observations,
            )
            return _Reservation(snapshot=None, health=health, allowed=False)
        except TimeoutError:
            reason = DurableChatObservationDegradationReason.PUBLISH_TIMEOUT
        except Exception as exc:
            reason = (
                DurableChatObservationDegradationReason.PUBLISH_TIMEOUT
                if _has_timeout_cause(exc)
                else DurableChatObservationDegradationReason.STORAGE_UNAVAILABLE
            )
        health = await self._best_effort_mark_degraded(
            run_id,
            reason,
            deadline,
            dropped_observations=dropped_observations,
        )
        return _Reservation(snapshot=None, health=health, allowed=False)

    async def _best_effort_mark_degraded(
        self,
        run_id: str,
        reason: DurableChatObservationDegradationReason,
        deadline: datetime,
        *,
        dropped_observations: int = 0,
    ) -> DurableChatObservationHealthV1:
        try:
            for _ in range(MAX_DURABLE_CHAT_CAS_ATTEMPTS):
                snapshot = await self._read_manifest(run_id, deadline)
                manifest = (
                    snapshot.manifest
                    if snapshot is not None
                    else _empty_manifest(run_id)
                )
                health = _degraded_health(
                    manifest.health,
                    reason,
                    dropped_observations=dropped_observations,
                )
                updated = _replace_manifest(manifest, health=health)
                if await self._write_manifest(run_id, updated, snapshot, deadline):
                    return health
        except Exception:
            pass
        return _degraded_health(
            DurableChatObservationHealthV1(),
            reason,
            dropped_observations=dropped_observations,
        )

    async def _read_manifest(
        self,
        run_id: str,
        deadline: datetime,
    ) -> _ManifestSnapshot | None:
        document = await self._get_document(
            _journal_key(run_id),
            deadline,
            error_type=DurableChatJournalError,
        )
        if document is None:
            return None
        try:
            manifest = parse_durable_chat_document(
                document.payload,
                _DurableChatJournalManifestV1,
            )
        except (UnicodeDecodeError, ValidationError, ValueError) as exc:
            raise DurableChatJournalError("durable-chat journal manifest is invalid") from exc
        if manifest.run_id != run_id:
            raise DurableChatJournalError("durable-chat journal manifest does not match the run")
        return _ManifestSnapshot(manifest=manifest, revision=document.revision)

    async def _read_manifest_or_none(
        self,
        run_id: str,
        deadline: datetime,
    ) -> _ManifestSnapshot | None:
        try:
            return await self._read_manifest(run_id, deadline)
        except Exception:
            return None

    async def _write_manifest(
        self,
        run_id: str,
        manifest: _DurableChatJournalManifestV1,
        current: _ManifestSnapshot | None,
        deadline: datetime,
    ) -> bool:
        payload = canonical_json_bytes(manifest)
        key = _journal_key(run_id)
        try:
            if current is None:
                return await _await_deadline(
                    self._documents.create(key, payload),
                    deadline,
                )
            return await _await_deadline(
                self._documents.replace(key, payload, revision=current.revision),
                deadline,
            )
        except TimeoutError:
            raise
        except Exception as exc:
            raise DurableChatJournalError("durable-chat journal manifest write failed") from exc

    async def _write_content(
        self,
        *,
        kind: str,
        payload: bytes,
        deadline: datetime,
    ) -> ContentRefV1:
        try:
            return await _await_deadline(
                self._content.put_bytes(
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
            raise DurableChatJournalError("durable-chat content write failed") from exc

    async def _read_batch(
        self,
        reference: ContentRefV1,
        deadline: datetime,
    ) -> DurableChatObservationBatchV1:
        try:
            payload = await _await_deadline(
                self._content.get_bytes(reference),
                deadline,
            )
            return parse_durable_chat_document(
                payload,
                DurableChatObservationBatchV1,
                maximum_bytes=reference.byte_length,
            )
        except TimeoutError as exc:
            raise DurableChatJournalError("durable-chat batch read timed out") from exc
        except (UnicodeDecodeError, ValidationError, ValueError) as exc:
            raise DurableChatJournalError("durable-chat batch is invalid") from exc
        except Exception as exc:
            raise DurableChatJournalError("durable-chat batch read failed") from exc

    async def _read_snapshot(
        self,
        reference: ContentRefV1,
        deadline: datetime,
    ) -> DurableChatSnapshotFrameV1:
        try:
            payload = await _await_deadline(
                self._content.get_bytes(reference),
                deadline,
            )
            return parse_durable_chat_document(
                payload,
                DurableChatSnapshotFrameV1,
                maximum_bytes=reference.byte_length,
            )
        except TimeoutError as exc:
            raise DurableChatJournalError("durable-chat snapshot read timed out") from exc
        except (UnicodeDecodeError, ValidationError, ValueError) as exc:
            raise DurableChatJournalError("durable-chat snapshot is invalid") from exc
        except Exception as exc:
            raise DurableChatJournalError("durable-chat snapshot read failed") from exc

    async def _get_document(
        self,
        key: str,
        deadline: datetime,
        *,
        error_type: type[DurableChatJournalError] | type[DurableChatInitializationError],
    ) -> KeyedDocument | None:
        try:
            return await _await_deadline(self._documents.get(key), deadline)
        except TimeoutError as exc:
            raise error_type("durable-chat keyed document read timed out") from exc
        except Exception as exc:
            raise error_type("durable-chat keyed document read failed") from exc


class DurableChatObserver(DurableChatObservationSink, DurableChatObservationDrainerPort):
    """Non-blocking execution-side capture with a bounded best-effort publisher."""

    def __init__(self, *, journal: DurableChatJournalPort, run_id: str) -> None:
        self._journal = journal
        self._run_id = run_id
        self._pending: deque[DurableChatObservationV1] = deque()
        self._wake: asyncio.Event | None = None
        self._pump: asyncio.Task[None] | None = None
        self._drain_lock = asyncio.Lock()
        self._started = False
        self._published_revision = 0
        self._health = DurableChatObservationHealthV1()
        self._reported_local_health = False

    def start(self) -> None:
        """Start one bounded asynchronous publisher pump without awaiting storage."""
        self._started = True
        self._schedule_pump()

    def try_enqueue(
        self,
        *,
        observation: DurableChatObservationV1,
    ) -> DurableChatEnqueueResultV1:
        """Capture one bounded observation without storage I/O or blocking."""
        if observation.run_id != self._run_id:
            return self._drop(DurableChatObservationDegradationReason.STORAGE_UNAVAILABLE)
        if self._pending and _can_coalesce(self._pending[-1], observation):
            merged = _coalesce_text(self._pending[-1], observation)
            if merged is not None:
                self._pending[-1] = merged
                self._wake_pump()
                return DurableChatEnqueueResultV1(
                    disposition=DurableChatEnqueueDisposition.ENQUEUED,
                    pending_observations=len(self._pending),
                )
        if len(self._pending) >= MAX_DURABLE_CHAT_PENDING_OBSERVATIONS:
            return self._drop(DurableChatObservationDegradationReason.QUEUE_FULL)
        self._pending.append(observation)
        self._wake_pump()
        if self._started:
            self._schedule_pump()
        return DurableChatEnqueueResultV1(
            disposition=DurableChatEnqueueDisposition.ENQUEUED,
            pending_observations=len(self._pending),
        )

    async def drain(self, *, deadline: datetime) -> DurableChatDrainResultV1:
        """Drain or abandon pending observations under an independent deadline."""
        published = 0
        dropped = 0
        bounded_deadline = _safe_deadline(deadline)
        try:
            async with asyncio.timeout(_remaining_seconds(bounded_deadline)):
                async with self._drain_lock:
                    published, dropped = await self._drain_locked(bounded_deadline)
        except TimeoutError:
            dropped += self._abandon_pending(
                DurableChatObservationDegradationReason.PUBLISH_TIMEOUT
            )
        except Exception:
            dropped += self._abandon_pending(
                DurableChatObservationDegradationReason.STORAGE_UNAVAILABLE
            )
        return DurableChatDrainResultV1(
            published_observations=published,
            dropped_observations=dropped,
            health=self._health,
        )

    def _schedule_pump(self) -> None:
        if self._pump is not None and not self._pump.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._wake = self._wake or asyncio.Event()
        self._pump = loop.create_task(self._run_pump())

    def _wake_pump(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _run_pump(self) -> None:
        try:
            if self._wake is not None:
                self._wake.clear()
            while self._pending:
                await self.drain(
                    deadline=_deadline_after(
                        DURABLE_CHAT_PUBLISHER_DRAIN_DEADLINE_SECONDS
                    )
                )
        except Exception:
            self._abandon_pending(DurableChatObservationDegradationReason.STORAGE_UNAVAILABLE)

    async def _drain_locked(self, deadline: datetime) -> tuple[int, int]:
        published = 0
        dropped = 0
        while self._pending:
            observations = tuple(
                observation
                for _, observation in zip(
                    range(MAX_DURABLE_CHAT_EVENT_BATCH),
                    self._pending,
                    strict=False,
                )
            )
            report = self._degradation_observation(observations[0])
            if report is not None:
                observations = (report, *observations[: MAX_DURABLE_CHAT_EVENT_BATCH - 1])
            result = await self._journal.publish(
                run_id=self._run_id,
                expected_published_revision=self._published_revision,
                batch=DurableChatObservationBatchV1(
                    run_id=self._run_id,
                    observations=observations,
                ),
                deadline=deadline,
            )
            pending_count = len(observations) - (1 if report is not None else 0)
            if result.disposition is DurableChatPublicationDisposition.PUBLISHED:
                self._published_revision = result.published_revision
                self._remove_pending(pending_count)
                published += pending_count
                self._health = _merge_health(self._health, result.health)
                if report is not None:
                    self._reported_local_health = True
                continue
            self._remove_pending(pending_count)
            if result.disposition is DurableChatPublicationDisposition.STALE_PRODUCER:
                continue
            dropped += pending_count
            self._health = _merge_health(
                self._health,
                result.health,
                additional_dropped=pending_count,
            )
            if report is not None:
                self._reported_local_health = True
        return published, dropped

    def _degradation_observation(
        self,
        reference: DurableChatObservationV1,
    ) -> DurableChatDegradedObservationV1 | None:
        if not self._health.degraded or self._reported_local_health:
            return None
        return DurableChatDegradedObservationV1(
            run_id=self._run_id,
            session_id=reference.session_id,
            observed_at=_now(),
            health=self._health,
        )

    def _remove_pending(self, count: int) -> None:
        for _ in range(min(count, len(self._pending))):
            self._pending.popleft()

    def _drop(
        self,
        reason: DurableChatObservationDegradationReason,
    ) -> DurableChatEnqueueResultV1:
        self._health = _degraded_health(
            self._health,
            reason,
            dropped_observations=1,
        )
        return DurableChatEnqueueResultV1(
            disposition=DurableChatEnqueueDisposition.DROPPED,
            pending_observations=len(self._pending),
            degradation_reason=reason,
        )

    def _abandon_pending(
        self,
        reason: DurableChatObservationDegradationReason,
    ) -> int:
        count = len(self._pending)
        self._pending.clear()
        if count:
            self._health = _degraded_health(
                self._health,
                reason,
                dropped_observations=count,
            )
        return count


type DurableChatJournalFactory = Callable[[], DurableChatJournal]

_default_journal_instance: DurableChatJournal | None = None


def _default_durable_chat_journal() -> DurableChatJournal:
    global _default_journal_instance
    if _default_journal_instance is None:
        from .durable_loop_activities import BlobDurableContentStore
        from .durable_loop_receipts import BlobDurableKeyedDocumentStore

        _default_journal_instance = DurableChatJournal(
            content=BlobDurableContentStore.from_environment(),
            documents=BlobDurableKeyedDocumentStore.from_environment(),
        )
    return _default_journal_instance


_durable_chat_journal_factory: DurableChatJournalFactory = _default_durable_chat_journal


def get_durable_chat_journal() -> DurableChatJournal:
    """Return the configured shared-storage durable-chat journal."""
    return _durable_chat_journal_factory()


def set_durable_chat_journal_factory(factory: DurableChatJournalFactory) -> None:
    """Inject one journal factory for deterministic tests."""
    global _durable_chat_journal_factory
    _durable_chat_journal_factory = factory


def reset_durable_chat_journal_factory() -> None:
    """Restore the shared Blob-backed durable-chat journal factory."""
    global _default_journal_instance, _durable_chat_journal_factory
    _default_journal_instance = None
    _durable_chat_journal_factory = _default_durable_chat_journal


def _empty_manifest(run_id: str) -> _DurableChatJournalManifestV1:
    return _DurableChatJournalManifestV1(
        run_id=run_id,
        published_revision=0,
        through_sequence=0,
        reserved_observation_bytes=0,
    )


def _candidate_manifest(
    manifest: _DurableChatJournalManifestV1,
    *,
    observations: tuple[DurableChatObservationV1, ...],
    batch_reference: _JournalBatchReferenceV1 | None,
    active_producer_epochs: tuple[tuple[int, int], ...],
    closed_model_producers: tuple[tuple[int, int], ...],
) -> _DurableChatJournalManifestV1:
    through_sequence = manifest.through_sequence + len(observations)
    revision = manifest.published_revision + 1
    projection = _apply_projection(
        manifest,
        observations=observations,
        published_revision=revision,
        through_sequence=through_sequence,
        active_producer_epochs=active_producer_epochs,
    )
    batches = (
        manifest.batches if batch_reference is None else (*manifest.batches, batch_reference)
    )
    return _replace_manifest(
        manifest,
        published_revision=revision,
        through_sequence=through_sequence,
        batches=batches,
        projection=projection,
        health=projection.observation_health,
        active_producer_epochs=active_producer_epochs,
        closed_model_producers=closed_model_producers,
        terminal_published=manifest.terminal_published
        or any(
            isinstance(observation, DurableChatTerminalObservationV1)
            for observation in observations
        ),
    )


def _replace_manifest(
    manifest: _DurableChatJournalManifestV1,
    **updates: object,
) -> _DurableChatJournalManifestV1:
    if "reserved_observation_bytes" in updates and "health" not in updates:
        updates["health"] = manifest.health.model_copy(
            update={
                "reserved_observation_bytes": updates["reserved_observation_bytes"]
            }
        )
    values = manifest.model_dump(mode="python")
    values.update(updates)
    return _DurableChatJournalManifestV1.model_validate(values)


def _filter_observations(
    manifest: _DurableChatJournalManifestV1,
    observations: tuple[DurableChatObservationV1, ...],
) -> _FilteredObservations:
    reserved_epochs = dict(manifest.producer_epochs)
    active_epochs = dict(manifest.active_producer_epochs)
    closed_producers = set(manifest.closed_model_producers)
    active_step = max(active_epochs, default=-1)
    reserved_step = max(reserved_epochs, default=-1)
    session_id = (
        manifest.projection.session_id if manifest.projection is not None else None
    )
    terminal_tool_keys = {
        progress.producer.call_key
        for progress in (
            manifest.projection.tool_progress if manifest.projection is not None else ()
        )
        if progress.state in _TERMINAL_TOOL_STATES
    }
    run_terminal = (
        manifest.projection is not None
        and manifest.projection.progress.status
        in {
            DurableLoopRunStatus.COMPLETED,
            DurableLoopRunStatus.FAILED,
            DurableLoopRunStatus.CANCELLED,
        }
    )
    terminal_published = manifest.terminal_published
    accepted: list[DurableChatObservationV1] = []
    for observation in observations:
        is_terminal = isinstance(observation, DurableChatTerminalObservationV1)
        if run_terminal and (not is_terminal or terminal_published):
            continue
        if session_id is not None and observation.session_id != session_id:
            continue
        producer = _model_producer(observation)
        if producer is not None:
            producer_key = (producer.step_index, producer.observation_epoch)
            reserved_epoch = reserved_epochs.get(producer.step_index, 0)
            active_epoch = active_epochs.get(producer.step_index, 0)
            if (
                producer_key in closed_producers
                or producer.observation_epoch > reserved_epoch
                or producer.step_index < max(active_step, reserved_step)
                or (
                    producer.step_index == active_step
                    and producer.observation_epoch < active_epoch
                )
            ):
                continue
            if producer.step_index > active_step:
                active_epochs = {
                    step_index: epoch
                    for step_index, epoch in active_epochs.items()
                    if step_index >= producer.step_index
                }
                closed_producers = {
                    item for item in closed_producers if item[0] >= producer.step_index
                }
            active_epochs[producer.step_index] = producer.observation_epoch
            active_step = max(active_step, producer.step_index)
            if isinstance(observation, DurableChatAssistantDraftReplacedObservationV1):
                closed_producers.add(
                    (
                        observation.previous_producer.step_index,
                        observation.previous_producer.observation_epoch,
                    )
                )
            elif (
                isinstance(observation, DurableChatModelAttemptObservationV1)
                and observation.state
                in {
                    DurableChatModelAttemptState.SUPERSEDED,
                    DurableChatModelAttemptState.FAILED,
                }
            ):
                closed_producers.add(producer_key)
        if isinstance(observation, DurableChatToolObservationV1):
            call_key = observation.progress.producer.call_key
            if (
                call_key in terminal_tool_keys
                or (
                    observation.progress.state is DurableChatToolState.STARTED
                    and call_key in terminal_tool_keys
                )
            ):
                continue
            if observation.progress.state in _TERMINAL_TOOL_STATES:
                terminal_tool_keys.add(call_key)
        if is_terminal:
            run_terminal = True
            terminal_published = True
        accepted.append(observation)
    return _FilteredObservations(
        observations=tuple(accepted),
        active_producer_epochs=tuple(sorted(active_epochs.items())),
        closed_model_producers=tuple(sorted(closed_producers)),
    )


def _model_producer(
    observation: DurableChatObservationV1,
) -> DurableChatModelProducerV1 | None:
    if isinstance(
        observation,
        (
            DurableChatAssistantTextObservationV1,
            DurableChatModelAttemptObservationV1,
        ),
    ):
        return observation.producer
    if isinstance(observation, DurableChatAssistantDraftReplacedObservationV1):
        return observation.producer
    return None


def _apply_projection(
    manifest: _DurableChatJournalManifestV1,
    *,
    observations: tuple[DurableChatObservationV1, ...],
    published_revision: int,
    through_sequence: int,
    active_producer_epochs: tuple[tuple[int, int], ...] | None = None,
) -> DurableChatRunProjectionV1:
    previous = manifest.projection
    first = observations[0]
    session_id = previous.session_id if previous is not None else first.session_id
    state = _ProjectionState(
        progress=(
            previous.progress
            if previous is not None
            else DurableChatProgressV1(
                status=DurableLoopRunStatus.PENDING,
                phase="durable",
                model_steps=0,
                tool_calls=0,
                human_waits=0,
                step_index=0,
                updated_at=first.observed_at,
            )
        ),
        draft=previous.draft if previous is not None else None,
        tool_progress={
            item.producer.call_key: item
            for item in (previous.tool_progress if previous is not None else ())
        },
        sandboxes={
            _sandbox_projection_key(item): item
            for item in (previous.sandbox_observations if previous is not None else ())
        },
        health=manifest.health,
    )

    for observation in observations:
        _apply_observation(state, observation)

    epochs = tuple(
        _producer_epoch(step_index, observation_epoch)
        for step_index, observation_epoch in (
            manifest.active_producer_epochs
            if active_producer_epochs is None
            else active_producer_epochs
        )
    )
    return DurableChatRunProjectionV1(
        run_id=manifest.run_id,
        session_id=session_id,
        published_revision=published_revision,
        through_sequence=through_sequence,
        draft=state.draft,
        progress=state.progress,
        producer_epochs=epochs,
        tool_progress=tuple(
            state.tool_progress[key] for key in sorted(state.tool_progress)
        ),
        sandbox_observations=tuple(
            state.sandboxes[key] for key in sorted(state.sandboxes)
        ),
        observation_health=state.health,
    )


def _apply_observation(
    state: _ProjectionState,
    observation: DurableChatObservationV1,
) -> None:
    if isinstance(observation, DurableChatRunStatusObservationV1):
        state.progress = state.progress.model_copy(
            update={
                "status": observation.status,
                "phase": observation.phase,
                "result_available": observation.result_available,
                "updated_at": observation.observed_at,
            }
        )
        return
    if isinstance(observation, DurableChatProgressObservationV1):
        state.progress = observation.progress
        return
    if isinstance(observation, DurableChatAssistantTextObservationV1):
        _apply_text_observation(state, observation)
        return
    if isinstance(observation, DurableChatAssistantDraftReplacedObservationV1):
        state.draft = None
        return
    if isinstance(observation, DurableChatModelAttemptObservationV1):
        _apply_model_attempt_observation(state, observation)
        return
    if isinstance(observation, DurableChatToolObservationV1):
        _apply_tool_observation(state, observation)
        return
    if isinstance(observation, DurableChatSandboxObservationEventV1):
        _apply_sandbox_observation(state, observation)
        return
    if isinstance(observation, DurableChatTerminalObservationV1):
        state.progress = state.progress.model_copy(
            update={
                "status": observation.status,
                "phase": _terminal_phase(observation.status),
                "result_available": observation.result_available,
                "updated_at": observation.observed_at,
            }
        )
        if observation.status is not DurableLoopRunStatus.COMPLETED:
            state.draft = None
        return
    if isinstance(observation, DurableChatDegradedObservationV1):
        state.health = _merge_health(state.health, observation.health)


def _apply_text_observation(
    state: _ProjectionState,
    observation: DurableChatAssistantTextObservationV1,
) -> None:
    if state.draft is None or state.draft.producer != observation.producer:
        state.draft = DurableChatAssistantDraftV1(
            producer=observation.producer,
            text=observation.delta,
            updated_at=observation.observed_at,
        )
        return
    state.draft = state.draft.model_copy(
        update={
            "text": _truncate_utf8(
                state.draft.text + observation.delta,
                maximum_bytes=256 * 1024,
            ),
            "updated_at": observation.observed_at,
        }
    )


def _apply_model_attempt_observation(
    state: _ProjectionState,
    observation: DurableChatModelAttemptObservationV1,
) -> None:
    if state.draft is None:
        return
    if (
        observation.state is DurableChatModelAttemptState.STARTED
        and state.draft.producer != observation.producer
    ):
        state.draft = None
        return
    if (
        observation.state
        in {
            DurableChatModelAttemptState.SUPERSEDED,
            DurableChatModelAttemptState.FAILED,
        }
        and _producer_is_not_newer(state.draft.producer, observation.producer)
    ):
        state.draft = None


def _apply_tool_observation(
    state: _ProjectionState,
    observation: DurableChatToolObservationV1,
) -> None:
    call_key = observation.progress.producer.call_key
    current = state.tool_progress.get(call_key)
    if current is None or current.state not in _TERMINAL_TOOL_STATES:
        state.tool_progress[call_key] = observation.progress


def _apply_sandbox_observation(
    state: _ProjectionState,
    observation: DurableChatSandboxObservationEventV1,
) -> None:
    key = _sandbox_projection_key(observation.observation)
    current = state.sandboxes.get(key)
    if current is None and len(state.sandboxes) >= MAX_DURABLE_CHAT_SANDBOX_OBSERVATIONS:
        return
    if current is None or _should_update_sandbox(current, observation.observation):
        state.sandboxes[key] = observation.observation


def _producer_is_not_newer(
    current: DurableChatModelProducerV1,
    candidate: DurableChatModelProducerV1,
) -> bool:
    return (
        current.step_index < candidate.step_index
        or (
            current.step_index == candidate.step_index
            and current.observation_epoch <= candidate.observation_epoch
        )
    )


def _sandbox_projection_key(
    observation: DurableChatSandboxObservationV1,
) -> tuple[str, str, str, str]:
    call_key = observation.producer.call_key
    if observation.state is DurableChatSandboxState.REPLACEMENT_INSTANCE:
        return (
            call_key,
            "replacement",
            observation.replaced_sandbox_id or "",
            observation.sandbox_id or "",
        )
    if observation.sandbox_id is not None:
        return (
            call_key,
            "physical",
            observation.sandbox_group_resource_id or "",
            observation.sandbox_id,
        )
    return (call_key, "unbound", "", observation.state.value)


def _producer_epoch(
    step_index: int,
    observation_epoch: int,
) -> DurableChatProducerEpochV1:
    return DurableChatProducerEpochV1(
        step_index=step_index,
        observation_epoch=observation_epoch,
    )


def _should_compact(
    manifest: _DurableChatJournalManifestV1,
    observation_count: int,
) -> bool:
    return sum(batch.event_count for batch in manifest.batches) + observation_count > (
        _RETAINED_EVENT_LIMIT
    )


def _snapshot_reservation_bytes(
    manifest: _DurableChatJournalManifestV1,
    observations: tuple[DurableChatObservationV1, ...],
    active_producer_epochs: tuple[tuple[int, int], ...],
) -> int:
    """Find a bounded fixed point because snapshot health includes its reservation."""
    reserved_bytes = 0
    for _ in range(4):
        total = manifest.reserved_observation_bytes + reserved_bytes
        if total > MAX_DURABLE_CHAT_TOTAL_RESERVED_OBSERVATION_BYTES:
            return MAX_DURABLE_CHAT_BATCH_BYTES + MAX_DURABLE_CHAT_SNAPSHOT_BYTES + 1
        health = manifest.health.model_copy(
            update={"reserved_observation_bytes": total}
        )
        prospective = _replace_manifest(
            manifest,
            health=health,
            reserved_observation_bytes=total,
        )
        try:
            snapshot = DurableChatSnapshotFrameV1(
                captured_at=_now(),
                projection=_apply_projection(
                    prospective,
                    observations=observations,
                    published_revision=prospective.published_revision + 1,
                    through_sequence=prospective.through_sequence
                    + len(observations),
                    active_producer_epochs=active_producer_epochs,
                ),
            )
        except ValueError:
            return MAX_DURABLE_CHAT_BATCH_BYTES + MAX_DURABLE_CHAT_SNAPSHOT_BYTES + 1
        next_reserved_bytes = len(canonical_json_bytes(snapshot))
        if next_reserved_bytes <= reserved_bytes:
            return reserved_bytes
        reserved_bytes = next_reserved_bytes
    return reserved_bytes


def _should_update_sandbox(
    current: DurableChatSandboxObservationV1,
    incoming: DurableChatSandboxObservationV1,
) -> bool:
    if incoming.observed_at > current.observed_at:
        return True
    if incoming.observed_at < current.observed_at:
        return False
    return (
        _SANDBOX_STATE_ORDER[incoming.state]
        >= _SANDBOX_STATE_ORDER[current.state]
    )


def _terminal_phase(status: DurableLoopRunStatus) -> str:
    if status is DurableLoopRunStatus.COMPLETED:
        return "completed"
    if status is DurableLoopRunStatus.CANCELLED:
        return "cancellation"
    return "run"


def _degraded_health(
    health: DurableChatObservationHealthV1,
    reason: DurableChatObservationDegradationReason,
    *,
    dropped_observations: int = 0,
) -> DurableChatObservationHealthV1:
    reasons = (
        health.reasons
        if reason in health.reasons
        else (*health.reasons, reason)
    )
    return health.model_copy(
        update={
            "degraded": True,
            "dropped_observations": health.dropped_observations
            + dropped_observations,
            "last_error_code": reason.value,
            "reasons": reasons,
        }
    )


def _merge_health(
    primary: DurableChatObservationHealthV1,
    secondary: DurableChatObservationHealthV1,
    *,
    additional_dropped: int = 0,
) -> DurableChatObservationHealthV1:
    reasons = tuple(
        dict.fromkeys((*primary.reasons, *secondary.reasons))
    )
    return DurableChatObservationHealthV1(
        degraded=bool(reasons),
        reasons=reasons,
        dropped_observations=max(
            primary.dropped_observations,
            secondary.dropped_observations,
        )
        + additional_dropped,
        reserved_observation_bytes=max(
            primary.reserved_observation_bytes,
            secondary.reserved_observation_bytes,
        ),
        last_error_code=secondary.last_error_code or primary.last_error_code,
    )


def _degraded_publication(
    run_id: str,
    *,
    health: DurableChatObservationHealthV1,
    published_revision: int = 0,
    through_sequence: int = 0,
) -> DurableChatPublicationResultV1:
    return DurableChatPublicationResultV1(
        run_id=run_id,
        disposition=DurableChatPublicationDisposition.DEGRADED,
        published_revision=published_revision,
        through_sequence=through_sequence,
        health=health,
    )


def _can_coalesce(
    previous: DurableChatObservationV1,
    incoming: DurableChatObservationV1,
) -> bool:
    return (
        isinstance(previous, DurableChatAssistantTextObservationV1)
        and isinstance(incoming, DurableChatAssistantTextObservationV1)
        and previous.session_id == incoming.session_id
        and previous.producer == incoming.producer
        and len((previous.delta + incoming.delta).encode("utf-8")) <= 8 * 1024
    )


def _coalesce_text(
    previous: DurableChatObservationV1,
    incoming: DurableChatObservationV1,
) -> DurableChatAssistantTextObservationV1 | None:
    if not _can_coalesce(previous, incoming):
        return None
    if not isinstance(
        previous,
        DurableChatAssistantTextObservationV1,
    ) or not isinstance(incoming, DurableChatAssistantTextObservationV1):
        return None
    return previous.model_copy(
        update={
            "delta": previous.delta + incoming.delta,
            "observed_at": incoming.observed_at,
        }
    )


def _truncate_utf8(value: str, *, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _journal_key(run_id: str) -> str:
    return "durable-chat/journal/" + canonical_hash({"run_id": run_id})


def _initialization_key(run_id: str) -> str:
    return "durable-chat/initialization/" + canonical_hash({"run_id": run_id})


def _now() -> datetime:
    return datetime.now(UTC)


def _deadline_after(seconds: float) -> datetime:
    return _now() + timedelta(seconds=seconds)


def _safe_deadline(value: datetime) -> datetime:
    bounded = _deadline_after(DURABLE_CHAT_PUBLISHER_DRAIN_DEADLINE_SECONDS)
    if value.tzinfo is None or value.utcoffset() is None:
        return bounded
    return min(value.astimezone(UTC), bounded)


def _remaining_seconds(deadline: datetime) -> float:
    remaining = (deadline - _now()).total_seconds()
    if remaining <= 0:
        raise TimeoutError("durable-chat operation deadline elapsed")
    return remaining


def _has_timeout_cause(error: BaseException) -> bool:
    current: BaseException | None = error
    for _ in range(4):
        if isinstance(current, TimeoutError):
            return True
        if current is None or current.__cause__ is None:
            return False
        current = current.__cause__
    return False


async def _await_deadline[ValueT](
    awaitable: Awaitable[ValueT],
    deadline: datetime,
) -> ValueT:
    try:
        remaining = _remaining_seconds(deadline)
    except TimeoutError:
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise
    async with asyncio.timeout(remaining):
        return await awaitable
