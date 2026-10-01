"""Durable, idempotent admission staging for authored triggers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, cast
from uuid import UUID, uuid5

import azure.durable_functions as df
import azure.functions as func
from azurefunctions.extensions.http.fastapi import Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .._logger import logger
from ..config import EndpointAuthConfig, ResolvedAgent
from ..registration._auth import AuthError, resolve_owner_principal
from ..registration._handlers import _build_http_prompt, validate_request_body
from ..registration._trigger_serialization import serialize_trigger_data
from ..session_state import (
    EntraPrincipal,
    FunctionAppPrincipal,
    resolve_function_app_identity,
)
from ..strict_json import assert_json_value, canonical_json_bytes
from .durable_loop_activities import (
    BlobDurableContentStore,
    DurableContentStore,
)
from .durable_loop_config import DurableLoopSettings
from .durable_loop_protocol import (
    DURABLE_LOOP_SCHEMA_VERSION,
    MAX_TRIGGER_LEDGER_RECORDS_PER_PAGE,
    ContentRefV1,
    DurableTriggerAdmissionRecordV1,
    DurableTriggerAdmissionState,
    DurableTriggerBindingPrincipalV1,
    DurableTriggerPendingLedgerV1,
    DurableTriggerType,
    canonical_hash,
    deterministic_trigger_record_key,
)
from .durable_loop_receipts import (
    BlobDurableKeyedDocumentStore,
    DurableKeyedDocumentStore,
    KeyedDocument,
)
from .durable_retention import DurableRetentionManager, retained_identifier_hash

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

DURABLE_TRIGGER_OUTBOX_ORCHESTRATOR_NAME = "durable_trigger_admission_outbox_v1"
DURABLE_TRIGGER_ATTEMPT_ACTIVITY_NAME = "durable_trigger_admission_attempt_v1"
DURABLE_TRIGGER_SWEEPER_SCHEDULE = "0 */2 * * * *"
DURABLE_TRIGGER_SWEEPER_FUNCTION_NAME = (
    "azure_functions_agents_durable_trigger_admission_sweeper"
)

_OUTBOX_NAMESPACE = UUID("30ca5fc7-0537-4c20-887c-7584a3a751a8")
_RUN_NAMESPACE = UUID("d3620d3e-af90-4ae1-ac61-b42d2206a1f7")
_SUPPORTED_TRIGGER_TYPES = frozenset(
    {"http_trigger", "timer_trigger", "connector_trigger"}
)
_PROTOCOL_TRIGGER_TYPES = {
    "http_trigger": DurableTriggerType.HTTP,
    "timer_trigger": DurableTriggerType.TIMER,
    "connector_trigger": DurableTriggerType.CONNECTOR,
}
_RUNTIME_ONLY_ARGS = frozenset(
    {"allow_human_input", "durable_owner", "event_id_path", "session_id_path"}
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$"
_MAX_PATH_LENGTH = 256
_MAX_PATH_SEGMENTS = 16
_MAX_ID_BYTES = 1024
_MAX_SESSION_ID_LENGTH = 128
_MAX_RECORD_BYTES = 320 * 1024
_SHARD_COUNT = 16
_MAX_PAGES_PER_SHARD = 256
_MAX_CAS_ATTEMPTS = 64
_SWEEPER_BATCH_SIZE = 256
_OUTBOX_RETRY_SECONDS = 30
_PATH_TOKEN = re.compile(r"(?:\.([A-Za-z_][A-Za-z0-9_-]*))|(?:\[(0|[1-9][0-9]*)\])")


class DurableTriggerAdmissionError(RuntimeError):
    """Durable trigger staging or reconciliation failed."""


class DurableTriggerAdmissionConflictError(DurableTriggerAdmissionError):
    """One stable trigger identity was reused with different content."""


class DurableTriggerLedgerCapacityError(DurableTriggerAdmissionError):
    """A bounded pending-ledger shard has reached its safety limit."""


class DurableTriggerAttemptOutcome(StrEnum):
    """Result of one call into the shared durable admission seam."""

    ADMITTED = "admitted"
    RETRY = "retry"
    TERMINAL = "terminal"


class DurableTriggerAdmissionAttemptV1(BaseModel):
    """Bounded response from the shared admission callback."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    outcome: DurableTriggerAttemptOutcome
    error_code: Annotated[
        str,
        Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$"),
    ] | None = None


class DurableTriggerTerminalReceiptV1(BaseModel):
    """Retention envelope around the canonical terminal trigger record."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    record: DurableTriggerAdmissionRecordV1
    expires_at: datetime


class DurableTriggerAdmissionCallback(Protocol):
    """Shared admission seam; implementations must not execute the model inline."""

    async def __call__(
        self,
        record: DurableTriggerAdmissionRecordV1,
        client: df.DurableOrchestrationClient,
    ) -> DurableTriggerAdmissionAttemptV1:
        """Attempt the existing durable start contract for one staged record."""


@dataclass(frozen=True, slots=True)
class DurableTriggerRegistration:
    """Validated runtime-only trigger authoring and stable identity."""

    trigger_type: Literal["http_trigger", "timer_trigger", "connector_trigger"]
    registration_id: str
    agent_slug: str
    owner: Mapping[str, str] | None
    event_id_path: str | None
    session_id_path: str | None
    connection_hash: str | None
    allow_human_input: bool
    response_schema_hash: str | None
    auth_mode: str
    ownership_class: str


@dataclass(frozen=True, slots=True)
class DurableTriggerStageResult:
    """Staging result that can replay after content-bearing ledger compaction."""

    run_id: str
    session_id: str
    created: bool
    record: DurableTriggerAdmissionRecordV1 | None


class _UnavailableAdmission:
    async def __call__(
        self,
        record: DurableTriggerAdmissionRecordV1,
        client: df.DurableOrchestrationClient,
    ) -> DurableTriggerAdmissionAttemptV1:
        del client, record
        return DurableTriggerAdmissionAttemptV1(
            outcome=DurableTriggerAttemptOutcome.RETRY,
            error_code="shared_admission_unavailable",
        )


class _LazyBlobDocumentStore:
    """Delay Blob configuration and client creation until the first trigger delivery."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._store: DurableKeyedDocumentStore | None = None

    async def get(self, key: str) -> KeyedDocument | None:
        return await (await self._resolve()).get(key)

    async def create(self, key: str, payload: bytes) -> bool:
        return await (await self._resolve()).create(key, payload)

    async def replace(
        self,
        key: str,
        payload: bytes,
        *,
        revision: str,
    ) -> bool:
        return await (await self._resolve()).replace(
            key,
            payload,
            revision=revision,
        )

    async def delete(self, key: str, *, revision: str | None = None) -> bool:
        return await (await self._resolve()).delete(key, revision=revision)

    async def _resolve(self) -> DurableKeyedDocumentStore:
        if self._store is not None:
            return self._store
        async with self._lock:
            if self._store is None:
                self._store = BlobDurableKeyedDocumentStore.from_environment()
            return self._store


class _LazyBlobContentStore:
    """Delay Blob content-store creation until the first trigger delivery."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._store: DurableContentStore | None = None

    async def put_bytes(
        self,
        *,
        kind: str,
        payload: bytes,
        media_type: str,
        retention_class: str,
    ) -> ContentRefV1:
        return await (await self._resolve()).put_bytes(
            kind=kind,
            payload=payload,
            media_type=media_type,
            retention_class=retention_class,
        )

    async def get_bytes(self, reference: ContentRefV1) -> bytes:
        return await (await self._resolve()).get_bytes(reference)

    async def delete_bytes(self, reference: ContentRefV1) -> bool:
        return await (await self._resolve()).delete_bytes(reference)

    async def _resolve(self) -> DurableContentStore:
        if self._store is not None:
            return self._store
        async with self._lock:
            if self._store is None:
                self._store = BlobDurableContentStore.from_environment()
            return self._store


class DurableTriggerPendingLedger:
    """Strict CAS-sharded pending ledger with immutable overflow pages."""

    def __init__(self, documents: DurableKeyedDocumentStore) -> None:
        self._documents = documents

    async def stage(
        self,
        record: DurableTriggerAdmissionRecordV1,
    ) -> DurableTriggerStageResult:
        """Append once, dedupe identical delivery, and reject content conflicts."""
        payload = canonical_json_bytes(record)
        if len(payload) > _MAX_RECORD_BYTES:
            raise ValueError("durable trigger admission record is too large")
        dedupe_key = _dedupe_key(record)
        shard = _record_shard(dedupe_key)
        head_key = _head_key(shard)
        for _ in range(_MAX_CAS_ATTEMPTS):
            terminal = await self._disposition_for_dedupe(dedupe_key)
            if terminal is not None:
                return self._terminal_replay(record, terminal)
            document = await self._documents.get(head_key)
            if document is None:
                head = _ledger_page(
                    shard=shard,
                    page_index=0,
                    records=(record,),
                )
                if await self._documents.create(head_key, canonical_json_bytes(head)):
                    return _stage_result(record, created=True)
                continue
            head = DurableTriggerPendingLedgerV1.model_validate_json(document.payload)
            existing = await self._find_in_chain(head, dedupe_key)
            if existing is not None:
                if existing.request_hash != record.request_hash:
                    raise DurableTriggerAdmissionConflictError(
                        "stable trigger identity was reused with different content"
                    )
                return _stage_result(existing, created=False)
            if len(head.records) < MAX_TRIGGER_LEDGER_RECORDS_PER_PAGE:
                replacement = _ledger_page(
                    shard=shard,
                    page_index=head.page_index,
                    records=(*head.records, record),
                    previous_page_hash=head.previous_page_hash,
                )
            else:
                await self._ensure_overflow_capacity(head)
                overflow_payload = canonical_json_bytes(head)
                overflow_key = _page_key(shard, head.page_hash)
                created = await self._documents.create(overflow_key, overflow_payload)
                if not created:
                    existing_page = await self._documents.get(overflow_key)
                    if (
                        existing_page is None
                        or existing_page.payload != overflow_payload
                    ):
                        raise DurableTriggerAdmissionError(
                            "durable trigger overflow page collision"
                        )
                replacement = _ledger_page(
                    shard=shard,
                    page_index=head.page_index + 1,
                    records=(record,),
                    previous_page_hash=head.page_hash,
                )
            if await self._documents.replace(
                head_key,
                canonical_json_bytes(replacement),
                revision=document.revision,
            ):
                return _stage_result(record, created=True)
        raise DurableTriggerAdmissionError(
            "durable trigger ledger CAS retry limit exceeded"
        )

    @staticmethod
    def _terminal_replay(
        record: DurableTriggerAdmissionRecordV1,
        terminal: DurableTriggerTerminalReceiptV1,
    ) -> DurableTriggerStageResult:
        if terminal.record.request_hash != record.request_hash:
            raise DurableTriggerAdmissionConflictError(
                "stable trigger identity was reused with different content"
            )
        return DurableTriggerStageResult(
            run_id=terminal.record.run_id,
            session_id=terminal.record.session_id,
            created=False,
            record=None,
        )

    async def pending(
        self,
        *,
        limit: int = _SWEEPER_BATCH_SIZE,
    ) -> tuple[DurableTriggerAdmissionRecordV1, ...]:
        """Return a deterministic bounded set of unresolved entries."""
        if limit < 1 or limit > _SWEEPER_BATCH_SIZE:
            raise ValueError("pending ledger limit is invalid")
        records: list[DurableTriggerAdmissionRecordV1] = []
        for shard in range(_SHARD_COUNT):
            document = await self._documents.get(_head_key(shard))
            if document is None:
                continue
            head = DurableTriggerPendingLedgerV1.model_validate_json(document.payload)
            async for record in self._walk_chain(head):
                if await self.disposition(record) is None:
                    records.append(record)
                    if len(records) >= limit:
                        return tuple(sorted(records, key=_record_order))
        return tuple(sorted(records, key=_record_order))

    async def disposition(
        self,
        record: DurableTriggerAdmissionRecordV1,
    ) -> DurableTriggerTerminalReceiptV1 | None:
        return await self._disposition_for_dedupe(_dedupe_key(record))

    async def _disposition_for_dedupe(
        self,
        dedupe_key: str,
    ) -> DurableTriggerTerminalReceiptV1 | None:
        document = await self._documents.get(_receipt_key(dedupe_key))
        if document is None:
            return None
        return DurableTriggerTerminalReceiptV1.model_validate_json(
            document.payload
        )

    async def complete(
        self,
        record: DurableTriggerAdmissionRecordV1,
        state: DurableTriggerAdmissionState,
        *,
        completed_at: datetime,
        expires_at: datetime,
        error_code: str | None = None,
    ) -> DurableTriggerTerminalReceiptV1:
        """Create one terminal receipt, verifying any racing writer agrees."""
        terminal_at = _utc_datetime(completed_at, "completed_at")
        terminal_reason = error_code
        if state is not DurableTriggerAdmissionState.ADMITTED and terminal_reason is None:
            terminal_reason = "admission_rejected"
        terminal_record = DurableTriggerAdmissionRecordV1.model_validate(
            {
                **record.model_dump(),
                "admitted_at": (
                    terminal_at
                    if state is DurableTriggerAdmissionState.ADMITTED
                    else None
                ),
                "attempts": record.attempts + 1,
                "state": state,
                "terminal_reason_code": terminal_reason,
            },
            strict=True,
        )
        receipt = DurableTriggerTerminalReceiptV1(
            record=terminal_record,
            expires_at=_utc_datetime(expires_at, "expires_at"),
        )
        key = _receipt_key(_dedupe_key(record))
        payload = canonical_json_bytes(receipt)
        if await self._documents.create(key, payload):
            return receipt
        existing = await self._documents.get(key)
        if existing is None:
            raise DurableTriggerAdmissionError(
                "durable trigger terminal receipt disappeared"
            )
        observed = DurableTriggerTerminalReceiptV1.model_validate_json(
            existing.payload
        )
        if observed != receipt:
            raise DurableTriggerAdmissionConflictError(
                "durable trigger terminal disposition conflicts"
            )
        return observed

    async def compact_terminal(self) -> int:
        """Remove only receipted terminal entries from bounded active shard chains."""
        removed = 0
        for shard in range(_SHARD_COUNT):
            removed += await self._compact_shard(shard)
        return removed

    async def _compact_shard(self, shard: int) -> int:
        head_key = _head_key(shard)
        for _ in range(_MAX_CAS_ATTEMPTS):
            document = await self._documents.get(head_key)
            if document is None:
                return 0
            head = DurableTriggerPendingLedgerV1.model_validate_json(document.payload)
            records = [record async for record in self._walk_chain(head)]
            active: list[DurableTriggerAdmissionRecordV1] = []
            for record in records:
                if await self.disposition(record) is None:
                    active.append(record)
            if len(active) == len(records):
                return 0
            replacement = await self._replacement_head(
                shard,
                active,
            )
            if await self._documents.replace(
                head_key,
                canonical_json_bytes(replacement),
                revision=document.revision,
            ):
                return len(records) - len(active)
        raise DurableTriggerAdmissionError(
            "durable trigger compaction CAS retry limit exceeded"
        )

    async def _replacement_head(
        self,
        shard: int,
        records: list[DurableTriggerAdmissionRecordV1],
    ) -> DurableTriggerPendingLedgerV1:
        chunks = [
            tuple(
                records[
                    index : index + MAX_TRIGGER_LEDGER_RECORDS_PER_PAGE
                ]
            )
            for index in range(
                0,
                len(records),
                MAX_TRIGGER_LEDGER_RECORDS_PER_PAGE,
            )
        ]
        if not chunks:
            chunks = [()]
        previous_page_hash: str | None = None
        for page_index, chunk in enumerate(reversed(chunks[1:])):
            page = _ledger_page(
                shard=shard,
                page_index=page_index,
                records=chunk,
                previous_page_hash=previous_page_hash,
            )
            payload = canonical_json_bytes(page)
            key = _page_key(shard, page.page_hash)
            if not await self._documents.create(key, payload):
                existing = await self._documents.get(key)
                if existing is None or existing.payload != payload:
                    raise DurableTriggerAdmissionError(
                        "durable trigger compacted page collision"
                    )
            previous_page_hash = page.page_hash
        return _ledger_page(
            shard=shard,
            page_index=len(chunks) - 1,
            records=chunks[0],
            previous_page_hash=previous_page_hash,
        )

    async def _find_in_chain(
        self,
        head: DurableTriggerPendingLedgerV1,
        dedupe_key: str,
    ) -> DurableTriggerAdmissionRecordV1 | None:
        async for record in self._walk_chain(head):
            if _dedupe_key(record) == dedupe_key:
                return record
        return None

    async def _ensure_overflow_capacity(
        self,
        head: DurableTriggerPendingLedgerV1,
    ) -> None:
        page_hash = head.previous_page_hash
        pages = 0
        while page_hash is not None:
            pages += 1
            if pages >= _MAX_PAGES_PER_SHARD:
                raise DurableTriggerLedgerCapacityError(
                    "durable trigger pending shard exceeded its bounded page chain"
                )
            document = await self._documents.get(_page_key(head.shard, page_hash))
            if document is None:
                raise DurableTriggerAdmissionError(
                    "durable trigger pending page is missing"
                )
            page = DurableTriggerPendingLedgerV1.model_validate_json(
                document.payload
            )
            if page.shard != head.shard:
                raise DurableTriggerAdmissionError(
                    "durable trigger pending page belongs to another shard"
                )
            page_hash = page.previous_page_hash

    async def _walk_chain(
        self,
        head: DurableTriggerPendingLedgerV1,
    ) -> AsyncIterator[DurableTriggerAdmissionRecordV1]:
        for record in head.records:
            yield record
        page_hash = head.previous_page_hash
        pages = 0
        while page_hash is not None:
            pages += 1
            if pages > _MAX_PAGES_PER_SHARD:
                raise DurableTriggerLedgerCapacityError(
                    "durable trigger pending shard exceeded its bounded page chain"
                )
            document = await self._documents.get(_page_key(head.shard, page_hash))
            if document is None:
                raise DurableTriggerAdmissionError(
                    "durable trigger pending page is missing"
                )
            page = DurableTriggerPendingLedgerV1.model_validate_json(
                document.payload
            )
            if page.shard != head.shard:
                raise DurableTriggerAdmissionError(
                    "durable trigger pending page belongs to another shard"
                )
            for record in page.records:
                yield record
            page_hash = page.previous_page_hash


class DurableTriggerAdmissionRuntime:
    """Stages trigger requests and reconciles their deterministic outboxes."""

    def __init__(
        self,
        ledger: DurableTriggerPendingLedger,
        *,
        admission_deadline_seconds: int,
        receipt_retention_seconds: int = 90 * 24 * 60 * 60,
        content: DurableContentStore | None = None,
        retention: DurableRetentionManager | None = None,
        callback: DurableTriggerAdmissionCallback | None = None,
        app_identity: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if admission_deadline_seconds < 1:
            raise ValueError("trigger admission deadline must be positive")
        if receipt_retention_seconds < admission_deadline_seconds:
            raise ValueError(
                "trigger receipt retention must cover the admission deadline"
            )
        self.ledger = ledger
        self.admission_deadline_seconds = admission_deadline_seconds
        self.receipt_retention_seconds = receipt_retention_seconds
        self.content = content or _LazyBlobContentStore()
        self.retention = retention
        self.callback = callback or _UnavailableAdmission()
        self._app_identity = app_identity or (
            lambda: resolve_function_app_identity().logical_id
        )
        self._now = now or (lambda: datetime.now(UTC))

    def now(self) -> datetime:
        return _utc_datetime(self._now(), "current time")

    def app_identity(self) -> str:
        value = self._app_identity()
        if not isinstance(value, str) or not value:
            raise ValueError("stable Function App identity is unavailable")
        return value

    async def stage(
        self,
        *,
        registration: DurableTriggerRegistration,
        owner_hash: str,
        initiator_hash: str,
        stable_event_id: JsonScalar,
        session_id: str,
        payload: JsonValue,
        prompt: str,
    ) -> DurableTriggerStageResult:
        """Build and append one deterministic admission record."""
        _validate_session_id(session_id)
        event_id = _normalize_scalar(stable_event_id, "stable event ID")
        assert_json_value(payload)
        if not prompt:
            raise ValueError("durable trigger prompt is empty")
        normalized_body_hash = canonical_hash(payload)
        prompt_bytes = prompt.encode("utf-8")
        payload_bytes = canonical_json_bytes(payload)
        event_id_hash = canonical_hash({"event_id": event_id})
        record_key = deterministic_trigger_record_key(
            owner_hash=owner_hash,
            trigger_registration=registration.registration_id,
            stable_event_id_hash=event_id_hash,
            normalized_body_hash=normalized_body_hash,
        )
        run_id = str(uuid5(_RUN_NAMESPACE, record_key))
        observed_now = self.now()
        admission_deadline = observed_now + timedelta(
            seconds=self.admission_deadline_seconds
        )
        if self.retention is None:
            payload_ref, prompt_ref = await asyncio.gather(
                self.content.put_bytes(
                    kind="trigger-payload",
                    payload=payload_bytes,
                    media_type="application/json",
                    retention_class="trigger_pending",
                ),
                self.content.put_bytes(
                    kind="trigger-prompt",
                    payload=prompt_bytes,
                    media_type="text/plain; charset=utf-8",
                    retention_class="trigger_pending",
                ),
            )
        else:
            resource_hash = retained_identifier_hash("trigger", run_id)
            payload_ref, prompt_ref = await asyncio.gather(
                self.retention.tracked_put_for_resource(
                    resource_hash=resource_hash,
                    kind="trigger-payload",
                    payload=payload_bytes,
                    media_type="application/json",
                    retention_class="trigger_pending",
                    expires_at=admission_deadline,
                    now=observed_now,
                ),
                self.retention.tracked_put_for_resource(
                    resource_hash=resource_hash,
                    kind="trigger-prompt",
                    payload=prompt_bytes,
                    media_type="text/plain; charset=utf-8",
                    retention_class="trigger_pending",
                    expires_at=admission_deadline,
                    now=observed_now,
                ),
            )
        request_hash = canonical_hash(
            {
                "agent_slug": registration.agent_slug,
                "allow_human_input": registration.allow_human_input,
                "normalized_body_hash": normalized_body_hash,
                "owner_hash": owner_hash,
                "prompt_hash": hashlib.sha256(prompt_bytes).hexdigest(),
                "response_schema_hash": registration.response_schema_hash,
                "session_id": session_id,
                "stable_event_id": event_id,
                "trigger_registration_id": registration.registration_id,
                "trigger_type": registration.trigger_type,
            }
        )
        record = DurableTriggerAdmissionRecordV1(
            record_key=record_key,
            owner_hash=owner_hash,
            access_namespace_hash=_access_namespace_hash(
                app_identity=self.app_identity(),
                auth_mode=registration.auth_mode,
                ownership_class=registration.ownership_class,
            ),
            initiator_hash=initiator_hash,
            agent_slug=registration.agent_slug,
            trigger_type=_PROTOCOL_TRIGGER_TYPES[registration.trigger_type],
            trigger_registration=registration.registration_id,
            stable_event_id_hash=event_id_hash,
            normalized_body_hash=normalized_body_hash,
            request_hash=request_hash,
            session_id=session_id,
            run_id=run_id,
            payload_ref=payload_ref,
            prompt_ref=prompt_ref,
            staged_at=observed_now,
            admission_deadline=admission_deadline,
        )
        return await self.ledger.stage(record)

    async def best_effort_start(
        self,
        client: Any,
        record: DurableTriggerAdmissionRecordV1,
    ) -> None:
        """Start the deterministic outbox without weakening durable staging."""
        try:
            await _start_outbox(client, record)
        except Exception:
            logger.exception(
                "Durable trigger staged but deterministic outbox start failed."
            )

    async def attempt(
        self,
        record: DurableTriggerAdmissionRecordV1,
        client: df.DurableOrchestrationClient,
    ) -> DurableTriggerAdmissionAttemptV1:
        """Invoke only the shared durable admission callback and retain terminals."""
        existing = await self.ledger.disposition(record)
        if existing is not None:
            outcome = (
                DurableTriggerAttemptOutcome.ADMITTED
                if existing.record.state is DurableTriggerAdmissionState.ADMITTED
                else DurableTriggerAttemptOutcome.TERMINAL
            )
            return DurableTriggerAdmissionAttemptV1(
                outcome=outcome,
                error_code=existing.record.terminal_reason_code,
            )
        observed_now = self.now()
        if observed_now >= record.admission_deadline:
            await self.ledger.complete(
                record,
                DurableTriggerAdmissionState.EXPIRED,
                completed_at=observed_now,
                expires_at=record.staged_at
                + timedelta(seconds=self.receipt_retention_seconds),
                error_code="admission_deadline_expired",
            )
            return DurableTriggerAdmissionAttemptV1(
                outcome=DurableTriggerAttemptOutcome.TERMINAL,
                error_code="admission_deadline_expired",
            )
        attempt = await self.callback(record, client)
        if attempt.outcome is DurableTriggerAttemptOutcome.RETRY:
            return attempt
        state = (
            DurableTriggerAdmissionState.ADMITTED
            if attempt.outcome is DurableTriggerAttemptOutcome.ADMITTED
            else DurableTriggerAdmissionState.REJECTED
        )
        await self.ledger.complete(
            record,
            state,
            completed_at=observed_now,
            expires_at=record.staged_at
            + timedelta(seconds=self.receipt_retention_seconds),
            error_code=attempt.error_code,
        )
        return attempt

    async def sweep(self, client: Any) -> int:
        """Recover staged requests after loss between staging and outbox start."""
        recovered = 0
        observed_now = self.now()
        for record in await self.ledger.pending():
            if observed_now >= record.admission_deadline:
                await self.ledger.complete(
                    record,
                    DurableTriggerAdmissionState.EXPIRED,
                    completed_at=observed_now,
                    expires_at=record.staged_at
                    + timedelta(seconds=self.receipt_retention_seconds),
                    error_code="admission_deadline_expired",
                )
                continue
            await self.best_effort_start(client, record)
            recovered += 1
        await self.ledger.compact_terminal()
        return recovered


def validate_durable_trigger_registration(
    resolved: ResolvedAgent,
    trigger_type: str,
    trigger_params: Mapping[str, object],
    *,
    function_name: str,
) -> DurableTriggerRegistration:
    """Validate durable-only trigger authoring and freeze stable registration data."""
    if trigger_type not in _SUPPORTED_TRIGGER_TYPES:
        raise ValueError(
            f"Agent '{resolved.name}' ({resolved.source_file}): durable-loop mode "
            "supports only http_trigger, timer_trigger, and connector_trigger"
        )
    agent_slug = resolved.slug or function_name
    if not re.fullmatch(_IDENTIFIER_PATTERN, agent_slug):
        raise ValueError("durable trigger agent slug is invalid")
    allow_human_input = _strict_bool(
        trigger_params.get("allow_human_input", False),
        "allow_human_input",
    )
    owner: Mapping[str, str] | None = None
    event_id_path: str | None = None
    session_id_path: str | None = None
    connection_hash: str | None = None
    auth_mode = resolved.builtin_endpoints.http_auth.mode
    ownership_class = _ownership_class(auth_mode)
    if trigger_type == "http_trigger":
        _validate_http_authoring(resolved, trigger_params)
    else:
        owner = _validate_background_owner(resolved, trigger_params)
        if trigger_type == "timer_trigger":
            if trigger_params.get("use_monitor") is not True:
                raise ValueError(
                    "durable timer_trigger requires trigger.args.use_monitor: true"
                )
            if trigger_params.get("run_on_startup", False) is not False:
                raise ValueError(
                    "durable timer_trigger rejects trigger.args.run_on_startup"
                )
        else:
            event_id_path = _validate_authored_path(
                trigger_params.get("event_id_path"),
                "event_id_path",
                required=True,
            )
            session_id_path = _validate_authored_path(
                trigger_params.get("session_id_path"),
                "session_id_path",
                required=False,
            )
            connection_identity = {
                key: value
                for key, value in trigger_params.items()
                if key not in _RUNTIME_ONLY_ARGS
            }
            if not connection_identity:
                raise ValueError(
                    "durable connector_trigger requires stable connector binding identity"
                )
            assert_json_value(connection_identity)
            connection_hash = canonical_hash(connection_identity)
    trigger_configuration = dict(trigger_params)
    assert_json_value(trigger_configuration)
    registration_id = canonical_hash(
        {
            "agent_slug": agent_slug,
            "function_name": function_name,
            "trigger_configuration": trigger_configuration,
            "trigger_type": trigger_type,
        }
    )
    response_schema_hash = (
        canonical_hash(resolved.response_schema)
        if resolved.response_schema is not None
        else None
    )
    return DurableTriggerRegistration(
        trigger_type=trigger_type,  # type: ignore[arg-type]
        registration_id=registration_id,
        agent_slug=agent_slug,
        owner=owner,
        event_id_path=event_id_path,
        session_id_path=session_id_path,
        connection_hash=connection_hash,
        allow_human_input=allow_human_input,
        response_schema_hash=response_schema_hash,
        auth_mode=auth_mode,
        ownership_class=ownership_class,
    )


def durable_binding_decorator_args(
    trigger_params: Mapping[str, object],
) -> dict[str, object]:
    """Remove runtime-only arguments before calling a Functions decorator."""
    return {
        key: value
        for key, value in trigger_params.items()
        if key not in _RUNTIME_ONLY_ARGS
    }


def make_durable_binding_handler(
    runtime: DurableTriggerAdmissionRuntime,
    registration: DurableTriggerRegistration,
) -> Callable[..., Awaitable[None]]:
    """Create a timer/connector handler that only stages and starts an outbox."""

    async def handle(trigger_data, client: str) -> None:  # type: ignore[no-untyped-def]
        payload = _serialized_payload(trigger_data)
        prompt = _binding_prompt(registration.trigger_type, payload)
        stable_event_id: JsonScalar
        app_identity = runtime.app_identity()
        if registration.trigger_type == "timer_trigger":
            stable_event_id = _timer_occurrence(payload)
            session_id = _derived_session_id(
                {
                    "agent_slug": registration.agent_slug,
                    "app_identity": app_identity,
                    "registration_id": registration.registration_id,
                }
            )
        else:
            if registration.event_id_path is None:
                raise DurableTriggerAdmissionError(
                    "durable connector event path is unavailable"
                )
            stable_event_id = extract_bounded_json_path(
                payload,
                registration.event_id_path,
            )
            session_material: object = registration.registration_id
            if registration.session_id_path is not None:
                session_material = extract_bounded_json_path(
                    payload,
                    registration.session_id_path,
                )
            session_id = _derived_session_id(
                {
                    "agent_slug": registration.agent_slug,
                    "app_identity": app_identity,
                    "registration_id": registration.registration_id,
                    "session": session_material,
                }
            )
        owner_hash = _configured_owner_hash(registration.owner)
        principal = _trigger_binding_principal(
            app_identity=app_identity,
            registration=registration,
        )
        staged = await runtime.stage(
            registration=registration,
            owner_hash=owner_hash,
            initiator_hash=principal.principal_hash,
            stable_event_id=stable_event_id,
            session_id=session_id,
            payload=payload,
            prompt=prompt,
        )
        if staged.record is not None:
            await runtime.best_effort_start(client, staged.record)

    handle.__name__ = f"durable_trigger_{registration.agent_slug}"
    return handle


def make_durable_http_handler(
    runtime: DurableTriggerAdmissionRuntime,
    registration: DurableTriggerRegistration,
    auth: EndpointAuthConfig,
    *,
    resolved: ResolvedAgent,
    input_schema: dict[str, Any] | None = None,
) -> Callable[[Request, Any], Awaitable[Response]]:
    """Create an HTTP handler that returns only after durable staging."""

    async def handle(req: Request, client: str) -> Response:
        owner = _http_owner(req, auth)
        if isinstance(owner, AuthError):
            return _json_response(
                {"error": owner.message},
                status_code=owner.status_code,
            )
        idempotency_key = _header(req, "Idempotency-Key")
        if idempotency_key is None:
            return _json_response(
                {"error": "Idempotency-Key is required."},
                status_code=400,
            )
        try:
            stable_event_id = _normalize_scalar(
                idempotency_key,
                "Idempotency-Key",
            )
            payload = await _http_json_payload(req)
            validation_error = validate_request_body(payload, input_schema)
            if validation_error is not None:
                return validation_error
            body_json = canonical_json_bytes(payload).decode("utf-8")
            prompt = _build_http_prompt(resolved, body_json)
            owner_hash = _http_owner_hash(owner)
            session_id = _header(req, "x-ms-session-id")
            if session_id is None:
                session_id = _derived_session_id(
                    {
                        "idempotency_key": stable_event_id,
                        "owner_hash": owner_hash,
                        "registration_id": registration.registration_id,
                    }
                )
            _validate_session_id(session_id)
            staged = await runtime.stage(
                registration=registration,
                owner_hash=owner_hash,
                initiator_hash=owner_hash,
                stable_event_id=stable_event_id,
                session_id=session_id,
                payload=payload,
                prompt=prompt,
            )
        except DurableTriggerAdmissionConflictError:
            return _json_response(
                {"error": "idempotency_conflict"},
                status_code=409,
            )
        except (TypeError, ValueError) as exc:
            return _json_response({"error": str(exc)}, status_code=400)
        if staged.record is not None:
            await runtime.best_effort_start(client, staged.record)
        location = f"/experimental/durable-agent-runs/{staged.run_id}"
        return _json_response(
            {
                "run_id": staged.run_id,
                "session_id": staged.session_id,
                "status": "accepted",
                "links": {
                    "status": location,
                    "result": f"{location}/result",
                    "events": f"{location}/events",
                },
            },
            status_code=202,
            headers={"Location": location, "x-ms-session-id": staged.session_id},
        )

    handle.__name__ = f"durable_http_trigger_{registration.agent_slug}"
    return handle


def extract_bounded_json_path(payload: JsonValue, path: str) -> JsonScalar:
    """Resolve the authored deterministic JSON-path subset to one scalar."""
    tokens = _parse_path(path)
    current: JsonValue = payload
    for token in tokens:
        if isinstance(token, str):
            if not isinstance(current, dict) or token not in current:
                raise ValueError("authored trigger path did not resolve")
            current = current[token]
        else:
            if not isinstance(current, list) or token >= len(current):
                raise ValueError("authored trigger path did not resolve")
            current = current[token]
    if isinstance(current, dict | list) or current is None:
        raise ValueError("authored trigger path must resolve to one non-empty scalar")
    _normalize_scalar(current, "authored trigger path result")
    return current


def register_durable_trigger_admission_runtime(
    app: func.FunctionApp,
    *,
    admission_deadline_seconds: int,
    receipt_retention_seconds: int = 90 * 24 * 60 * 60,
    documents: DurableKeyedDocumentStore | None = None,
    content: DurableContentStore | None = None,
    callback: DurableTriggerAdmissionCallback | None = None,
    settings: DurableLoopSettings | None = None,
    resolved_agents: Mapping[str, ResolvedAgent] | None = None,
) -> DurableTriggerAdmissionRuntime:
    """Register the private outbox/activity/sweeper and return trigger runtime."""
    content_store = content or _LazyBlobContentStore()
    document_store = documents or _LazyBlobDocumentStore()
    retention = DurableRetentionManager(document_store, content_store)
    if callback is None and settings is not None and resolved_agents is not None:
        from .durable_loop_http import create_durable_trigger_admission_callback

        callback = create_durable_trigger_admission_callback(
            settings=settings,
            resolved_agents=resolved_agents,
            content=content_store,
            retention=retention,
        )
    runtime = DurableTriggerAdmissionRuntime(
        DurableTriggerPendingLedger(document_store),
        admission_deadline_seconds=admission_deadline_seconds,
        receipt_retention_seconds=receipt_retention_seconds,
        content=content_store,
        retention=retention,
        callback=callback,
    )
    blueprint = df.Blueprint()

    @blueprint.durable_client_input(  # type: ignore[untyped-decorator]
        client_name="client"
    )
    @blueprint.activity_trigger(  # type: ignore[untyped-decorator]
        input_name="payload",
        activity=DURABLE_TRIGGER_ATTEMPT_ACTIVITY_NAME,
    )
    async def durable_trigger_admission_attempt_v1(
        payload: dict[str, object],
        client: df.DurableOrchestrationClient,
    ) -> dict[str, object]:
        record = DurableTriggerAdmissionRecordV1.model_validate(
            payload,
            strict=True,
        )
        result = await runtime.attempt(record, client)
        return cast(dict[str, object], result.model_dump(mode="json"))

    @blueprint.orchestration_trigger(  # type: ignore[untyped-decorator]
        context_name="context",
        orchestration=DURABLE_TRIGGER_OUTBOX_ORCHESTRATOR_NAME,
    )
    def durable_trigger_admission_outbox_v1(
        context: df.DurableOrchestrationContext,
    ) -> Any:
        payload = context.get_input()
        while True:
            attempt = yield context.call_activity(
                DURABLE_TRIGGER_ATTEMPT_ACTIVITY_NAME,
                payload,
            )
            if attempt.get("outcome") != DurableTriggerAttemptOutcome.RETRY.value:
                return attempt
            deadline = datetime.fromisoformat(payload["admission_deadline"])
            retry_at = context.current_utc_datetime + timedelta(
                seconds=_OUTBOX_RETRY_SECONDS
            )
            if retry_at >= deadline:
                retry_at = deadline
            yield context.create_timer(retry_at)

    app.register_blueprint(blueprint)

    async def sweep_pending(timer: func.TimerRequest, client: str) -> None:
        del timer
        await runtime.sweep(client)

    sweep_pending.__name__ = DURABLE_TRIGGER_SWEEPER_FUNCTION_NAME
    decorated = app.durable_client_input(client_name="client")(sweep_pending)
    decorated = app.timer_trigger(
        schedule=DURABLE_TRIGGER_SWEEPER_SCHEDULE,
        arg_name="timer",
        use_monitor=True,
    )(decorated)
    app.function_name(name=DURABLE_TRIGGER_SWEEPER_FUNCTION_NAME)(decorated)
    return runtime


async def _start_outbox(
    client: Any,
    record: DurableTriggerAdmissionRecordV1,
) -> None:
    outbox_instance_id = _outbox_instance_id(record.record_key)
    existing = await client.get_status(outbox_instance_id)
    if existing is not None:
        return
    try:
        await client.start_new(
            DURABLE_TRIGGER_OUTBOX_ORCHESTRATOR_NAME,
            instance_id=outbox_instance_id,
            client_input=record.model_dump(mode="json"),
        )
    except Exception:
        if await client.get_status(outbox_instance_id) is None:
            raise


def _validate_http_authoring(
    resolved: ResolvedAgent,
    trigger_params: Mapping[str, object],
) -> None:
    from ..config.http_auth import resolve_http_trigger_auth

    if "http_auth" not in trigger_params:
        raise ValueError("durable http_trigger requires declared trigger.args.http_auth")
    trigger_auth = resolve_http_trigger_auth(trigger_params)
    public_auth = resolved.builtin_endpoints.http_auth
    if trigger_auth.model_dump(mode="json") != public_auth.model_dump(mode="json"):
        raise ValueError(
            "durable http_trigger http_auth must equal builtin_endpoints.http_auth"
        )
    if "durable_owner" in trigger_params:
        raise ValueError("durable http_trigger does not accept durable_owner")
    if "event_id_path" in trigger_params or "session_id_path" in trigger_params:
        raise ValueError("durable http_trigger does not accept connector identity paths")
    if trigger_params.get("allow_human_input", False) is not False:
        raise ValueError(
            "durable http_trigger does not accept background allow_human_input"
        )


def _validate_background_owner(
    resolved: ResolvedAgent,
    trigger_params: Mapping[str, object],
) -> Mapping[str, str]:
    raw = trigger_params.get("durable_owner")
    if not isinstance(raw, dict):
        raise ValueError(
            "durable timer/connector trigger requires trigger.args.durable_owner"
        )
    kind = raw.get("kind")
    public_auth = resolved.builtin_endpoints.http_auth
    if kind == "app":
        if set(raw) != {"kind"} or public_auth.mode not in {"function", "admin"}:
            raise ValueError(
                "durable_owner kind app requires function or admin public auth"
            )
        return {"kind": "app"}
    if kind != "entra_principal" or set(raw) != {
        "kind",
        "tenant_id",
        "object_id",
    }:
        raise ValueError("durable_owner is invalid")
    if public_auth.mode != "entra":
        raise ValueError("durable_owner kind entra_principal requires entra public auth")
    tenant_id = raw.get("tenant_id")
    object_id = raw.get("object_id")
    if not isinstance(tenant_id, str) or not isinstance(object_id, str):
        raise ValueError("durable_owner Entra identifiers are invalid")
    principal = EntraPrincipal.create(tenant_id=tenant_id, object_id=object_id)
    if (
        public_auth.entra is not None
        and public_auth.entra.tenant_id is not None
        and public_auth.entra.tenant_id.casefold() != principal.tenant_id
    ):
        raise ValueError("durable_owner tenant is outside the Entra allowlist")
    return {
        "kind": "entra_principal",
        "tenant_id": principal.tenant_id,
        "object_id": principal.object_id,
    }


def _configured_owner_hash(owner: Mapping[str, str] | None) -> str:
    if owner is None:
        raise DurableTriggerAdmissionError("background durable owner is unavailable")
    if owner["kind"] == "app":
        return canonical_hash({"kind": "function_app"})
    return canonical_hash(
        {
            "kind": "entra_user",
            "object_id": owner["object_id"],
            "tenant_id": owner["tenant_id"],
        }
    )


def _http_owner(
    req: Request,
    auth: EndpointAuthConfig,
) -> FunctionAppPrincipal | EntraPrincipal | Literal["anonymous"] | AuthError:
    if auth.mode == "anonymous":
        return "anonymous"
    owner = resolve_owner_principal(req.headers.get, auth)
    if isinstance(owner, FunctionAppPrincipal | EntraPrincipal | AuthError):
        return owner
    return AuthError(401, "Unsupported durable trigger owner identity.")


def _http_owner_hash(
    owner: FunctionAppPrincipal | EntraPrincipal | Literal["anonymous"],
) -> str:
    if owner == "anonymous":
        return canonical_hash({"kind": "anonymous_app"})
    if isinstance(owner, FunctionAppPrincipal):
        return canonical_hash({"kind": "function_app"})
    if isinstance(owner, EntraPrincipal):
        return canonical_hash(
            {
                "kind": "entra_user",
                "object_id": owner.object_id,
                "tenant_id": owner.tenant_id,
            }
        )
    raise PermissionError("unsupported durable trigger owner")


async def _http_json_payload(req: Request) -> JsonValue:
    try:
        payload: object = await req.json()
    except Exception as exc:
        raise ValueError("request body must be valid JSON") from exc
    assert_json_value(payload)
    return payload  # type: ignore[return-value]


def _serialized_payload(trigger_data: object) -> JsonValue:
    payload: object = json.loads(serialize_trigger_data(trigger_data))
    assert_json_value(payload)
    return payload  # type: ignore[return-value]


def _timer_occurrence(payload: JsonValue) -> str:
    if not isinstance(payload, dict):
        raise ValueError("durable timer payload is invalid")
    status = payload.get("schedule_status")
    if not isinstance(status, dict):
        raise ValueError("durable timer ScheduleStatus.Next is required")
    value = status.get("next")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("durable timer ScheduleStatus.Next is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("durable timer ScheduleStatus.Next is invalid") from None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.year <= 1
    ):
        raise ValueError("durable timer ScheduleStatus.Next is invalid")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_authored_path(
    value: object,
    field_name: str,
    *,
    required: bool,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"durable connector {field_name} must be a string")
    _parse_path(value)
    return value


def _parse_path(path: str) -> tuple[str | int, ...]:
    if not path.startswith("$") or len(path) > _MAX_PATH_LENGTH:
        raise ValueError("authored trigger path is invalid")
    position = 1
    tokens: list[str | int] = []
    while position < len(path):
        match = _PATH_TOKEN.match(path, position)
        if match is None:
            raise ValueError("authored trigger path is invalid")
        name, index = match.groups()
        tokens.append(name if name is not None else int(index))
        if len(tokens) > _MAX_PATH_SEGMENTS:
            raise ValueError("authored trigger path has too many segments")
        position = match.end()
    if not tokens:
        raise ValueError("authored trigger path must select a field")
    return tuple(tokens)


def _normalize_scalar(value: JsonScalar, field_name: str) -> str:
    if value is None or isinstance(value, dict | list):
        raise ValueError(f"{field_name} must be one non-empty scalar")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    rendered = (
        value.strip()
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, allow_nan=False)
    )
    if not rendered or len(rendered.encode("utf-8")) > _MAX_ID_BYTES:
        raise ValueError(f"{field_name} is empty or oversized")
    return rendered


def _derived_session_id(value: object) -> str:
    return f"dta-{canonical_hash(value)[:48]}"


def _validate_session_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_SESSION_ID_LENGTH
        or any(character.isspace() for character in value)
    ):
        raise ValueError("x-ms-session-id is invalid")


def _strict_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"durable trigger {field_name} must be a boolean")
    return value


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)


def _header(req: Request, name: str) -> str | None:
    value = req.headers.get(name)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _json_response(
    body: Mapping[str, object],
    *,
    status_code: int,
    headers: Mapping[str, str] | None = None,
) -> Response:
    return Response(
        content=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        status_code=status_code,
        media_type="application/json",
        headers=dict(headers or {}),
    )


def _head_key(shard: int) -> str:
    return f"trigger-admission/shards/{shard:02x}"


def _page_key(shard: int, page_hash: str) -> str:
    return f"trigger-admission/pages/{shard:02x}/{page_hash}"


def _receipt_key(dedupe_key: str) -> str:
    return f"trigger-admission/receipts/{dedupe_key}"


def _record_shard(dedupe_key: str) -> int:
    return int(dedupe_key[:8], 16) % _SHARD_COUNT


def _record_order(
    record: DurableTriggerAdmissionRecordV1,
) -> tuple[datetime, str]:
    return record.staged_at, record.record_key


def _dedupe_key(record: DurableTriggerAdmissionRecordV1) -> str:
    return canonical_hash(
        {
            "kind": "trigger_admission_identity",
            "owner_hash": record.owner_hash,
            "schema_version": DURABLE_LOOP_SCHEMA_VERSION,
            "stable_event_id_hash": record.stable_event_id_hash,
            "trigger_registration": record.trigger_registration,
        }
    )


def _ledger_page(
    *,
    shard: int,
    page_index: int,
    records: tuple[DurableTriggerAdmissionRecordV1, ...],
    previous_page_hash: str | None = None,
) -> DurableTriggerPendingLedgerV1:
    page_hash = canonical_hash(
        {
            "page_index": page_index,
            "previous_page_hash": previous_page_hash,
            "records": [record.model_dump(mode="json") for record in records],
            "shard": shard,
        }
    )
    return DurableTriggerPendingLedgerV1(
        shard=shard,
        page_index=page_index,
        records=records,
        previous_page_hash=previous_page_hash,
        page_hash=page_hash,
    )


def _outbox_instance_id(record_key: str) -> str:
    return f"dta-{uuid5(_OUTBOX_NAMESPACE, record_key)}"


def _ownership_class(auth_mode: str) -> str:
    return {
        "admin": "function_app",
        "anonymous": "anonymous_app",
        "entra": "entra_user",
        "function": "function_app",
    }[auth_mode]


def _access_namespace_hash(
    *,
    app_identity: str,
    auth_mode: str,
    ownership_class: str,
) -> str:
    return canonical_hash(
        {
            "app_identity_hash": canonical_hash({"app_identity": app_identity}),
            "auth_mode": auth_mode,
            "ownership_class": ownership_class,
            "route_family": "experimental_durable_agent_runs",
            "schema_version": DURABLE_LOOP_SCHEMA_VERSION,
        }
    )


def durable_access_namespace_hash(auth_mode: str) -> str:
    """Freeze the public durable route namespace for an endpoint auth mode."""
    return _access_namespace_hash(
        app_identity=resolve_function_app_identity().logical_id,
        auth_mode=auth_mode,
        ownership_class=_ownership_class(auth_mode),
    )


def _trigger_binding_principal(
    *,
    app_identity: str,
    registration: DurableTriggerRegistration,
) -> DurableTriggerBindingPrincipalV1:
    app_identity_hash = canonical_hash({"app_identity": app_identity})
    principal_hash = canonical_hash(
        {
            "agent_slug": registration.agent_slug,
            "app_identity_hash": app_identity_hash,
            "connection_identity_hash": registration.connection_hash,
            "kind": "trigger_binding",
            "schema_version": DURABLE_LOOP_SCHEMA_VERSION,
            "trigger_registration": registration.registration_id,
        }
    )
    return DurableTriggerBindingPrincipalV1(
        app_identity_hash=app_identity_hash,
        agent_slug=registration.agent_slug,
        trigger_registration=registration.registration_id,
        connection_identity_hash=registration.connection_hash,
        principal_hash=principal_hash,
    )


def _binding_prompt(trigger_type: str, payload: JsonValue) -> str:
    data_json = canonical_json_bytes(payload).decode("utf-8")
    return f"Triggered by: {trigger_type}\n\nTrigger data:\n```json\n{data_json}\n```"


def _stage_result(
    record: DurableTriggerAdmissionRecordV1,
    *,
    created: bool,
) -> DurableTriggerStageResult:
    return DurableTriggerStageResult(
        run_id=record.run_id,
        session_id=record.session_id,
        created=created,
        record=record,
    )
