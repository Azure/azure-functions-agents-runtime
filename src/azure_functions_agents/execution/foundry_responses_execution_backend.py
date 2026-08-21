"""Foundry Hosted Agent Responses implementation of the execution lifecycle seam."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from .._observability import start_fha_responses_create_span
from ..config import DEFAULT_TIMEOUT
from ..controller.idempotency import IdempotencyAttempt, build_idempotency_attempt
from ..foundry_responses.fha_private_history import FhaResponsesRequestEnvelope
from ..session_state import (
    TERMINAL_RUN_STATUSES,
    AdmissionOutcome,
    AdmissionRecords,
    ConcurrencyConflictError,
    DurableIdempotencyRecord,
    DurableOwnerIdempotencyRecord,
    DurableProviderRunMapping,
    DurableProviderSessionBinding,
    DurableRunRecord,
    DurableSessionRecord,
    IdempotencyConflictError,
    NewSessionAdmissionRecords,
    OwnerContext,
    OwnerPartition,
    ProviderIndeterminateOutcome,
    ProviderIndeterminateReason,
    ProviderRunMappingRead,
    RowAlreadyExistsError,
    SessionRowNotFoundError,
    SessionStateStore,
    SessionStateStoreError,
    encode_label_safe_digest,
    frame_canonical_components,
    mint_run_id,
    mint_session_id,
    owner_idempotency_expiry,
    owner_partition,
    validate_session_id,
)
from ..transport.foundry_responses import (
    FoundryResponse,
    FoundryResponseCreateRequest,
    FoundryResponseEvent,
    FoundryResponseEventKind,
    FoundryResponseEventStream,
    FoundryResponsesOperationError,
    FoundryResponsesOperationErrorKind,
    FoundryResponseStatus,
    FoundryResponseText,
    FoundrySessionCreateRequest,
)
from .backend import (
    SESSION_TOMBSTONED_ERROR_CODE,
    RunContext,
    RunError,
    RunEvent,
    RunHandle,
    RunResult,
    RunState,
    RunStatus,
    SessionBindingUnavailableError,
    StartRunRequest,
)
from .binding import AgentBinding
from .foundry_responses_runtime import FoundryResponsesRuntime
from .terminal_output_validation import validate_terminal_output

FOUNDRY_RESPONSES_CANCEL_POLL_ATTEMPTS = 4
FOUNDRY_RESPONSES_CANCEL_POLL_INTERVAL_SECONDS = 0.25
FOUNDRY_RESPONSES_CANCEL_POLL_WINDOW_SECONDS = 2.0
FOUNDRY_RESPONSES_STATUS_POLL_INTERVAL_SECONDS = 0.25
FOUNDRY_RESPONSES_SUBMISSION_GRACE_SECONDS = 30.0
FOUNDRY_RESPONSES_SESSION_RETENTION_SECONDS = 30 * 24 * 60 * 60
_FOUNDRY_SESSION_DIGEST_KIND = "foundry_responses"
_FOUNDRY_SESSION_PROTOCOL = "fha1"
_FOUNDRY_SESSION_REGION = "global"

type Sleep = Callable[[float], Awaitable[None]]
type Clock = Callable[[], float]


class FoundryResponsesBackendError(RuntimeError):
    """A Foundry Responses lifecycle operation could not safely continue."""


class FoundryResponsesExecutionBackend:
    """Persist Responses mappings behind the existing owner/session/run authority."""

    def __init__(
        self,
        binding: AgentBinding,
        *,
        runtime: FoundryResponsesRuntime,
        owner: OwnerContext,
        stream_events: bool = False,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        if not binding.agent_name:
            raise ValueError("Foundry Responses execution requires an agent identity slug")
        self._binding = binding
        self._runtime = runtime
        self._owner = owner
        self._stream_events = stream_events
        self._sleep = sleep
        self._clock = clock
        self._live_streams: dict[str, FoundryResponseEventStream] = {}

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        """Admit a runtime run before creating one stored background Response."""
        state_binding = await self._runtime.get_state_store()
        store = state_binding.store
        partition = owner_partition(self._owner)
        attempt = build_idempotency_attempt(
            agent_slug=self._binding.agent_name or "",
            prompt=request.prompt,
            timeout=request.timeout,
            idempotency_key=request.idempotency_key,
        )
        if request.session_id is None:
            replay = await self._owner_idempotency_replay(store, partition, attempt)
            if replay is not None:
                return await self._replay_or_quarantine(
                    store,
                    partition,
                    replay,
                    request.prompt,
                )
            session_id = mint_session_id()
            outcome = await self._admit_new_session(
                store,
                partition,
                state_store_fingerprint=state_binding.state_store_fingerprint,
                session_id=session_id,
                request=request,
                attempt=attempt,
            )
        else:
            session_id = validate_session_id(request.session_id)
            replay = await self._session_idempotency_replay(
                store,
                partition,
                session_id,
                attempt,
            )
            if replay is not None:
                return await self._replay_or_quarantine(
                    store,
                    partition,
                    replay,
                    request.prompt,
                )
            try:
                outcome = await self._admit_existing_session(
                    store,
                    partition,
                    session_id=session_id,
                    request=request,
                    attempt=attempt,
                )
            except SessionRowNotFoundError:
                outcome = await self._admit_new_session(
                    store,
                    partition,
                    state_store_fingerprint=state_binding.state_store_fingerprint,
                    session_id=session_id,
                    request=request,
                    attempt=attempt,
                )

        if outcome.replayed:
            return await self._replay_or_quarantine(
                store,
                partition,
                outcome.run,
                request.prompt,
            )
        return await self._submit_admitted_run(
            store,
            partition,
            outcome.run,
            request.prompt,
        )

    async def get_run(self, context: RunContext) -> RunStatus:
        """Project a stored Response while retaining the Table row as authority."""
        state_binding = await self._runtime.get_state_store()
        store = state_binding.store
        partition = owner_partition(self._owner)
        run = await store.get_run(partition, context.session_id, context.run_id)
        mapping = await store.get_provider_run_mapping(
            partition,
            context.session_id,
            context.run_id,
        )
        if (
            run.record.status not in TERMINAL_RUN_STATUSES
            and datetime.now(UTC) >= run.record.expires_at
        ):
            if mapping is None:
                terminal = await self._adopt_unbound_terminal(
                    store,
                    run.record,
                    status="timed_out",
                    reason="provider_submission_not_started",
                )
                return _durable_status(
                    terminal,
                    error=RunError(
                        code="provider_submission_not_started",
                        message="Hosted response submission did not start.",
                        fault_domain="runtime",
                    ),
                )
            if mapping.record.response_state == "pending":
                terminal = await self._adopt_unbound_terminal(
                    store,
                    run.record,
                    status="timed_out",
                    reason="provider_submission_not_started",
                )
                return _durable_status(
                    terminal,
                    error=RunError(
                        code="provider_submission_not_started",
                        message="Hosted response submission did not start.",
                        fault_domain="runtime",
                    ),
                )
            if mapping.record.response_state == "submitting":
                outcome = await self._mark_indeterminate(
                    store,
                    partition,
                    context,
                    reason="provider_submission_indeterminate",
                )
                return _indeterminate_status(
                    outcome.run,
                    outcome.mapping.indeterminate_reason,
                )
            if mapping.record.response_state == "bound":
                return await self.cancel_run(context)
        if mapping is None:
            if run.record.status not in TERMINAL_RUN_STATUSES:
                await self._adopt_missing_mapping(store, run.record)
                run = await store.get_run(partition, context.session_id, context.run_id)
            return _durable_status(
                run.record,
                error=RunError(
                    code="provider_mapping_unavailable",
                    message="Hosted response mapping is unavailable.",
                    fault_domain="runtime",
                ),
            )
        if mapping.record.response_state == "indeterminate":
            return _indeterminate_status(run.record, mapping.record.indeterminate_reason)
        if mapping.record.response_state == "pending":
            if not _submission_grace_elapsed(mapping.record.created_at):
                return _durable_status(run.record)
            terminal = await self._adopt_unbound_terminal(
                store,
                run.record,
                status="timed_out",
                reason="provider_submission_not_started",
            )
            return _durable_status(
                terminal,
                error=RunError(
                    code="provider_submission_not_started",
                    message="Hosted response submission did not start.",
                    fault_domain="runtime",
                ),
            )
        if mapping.record.response_state == "submitting":
            if not _submission_grace_elapsed(mapping.record.updated_at):
                return _durable_status(run.record)
            outcome = await self._mark_indeterminate(
                store,
                partition,
                context,
                reason="provider_submission_indeterminate",
            )
            return _indeterminate_status(outcome.run, outcome.mapping.indeterminate_reason)
        if (
            mapping.record.response_state == "terminal"
            and mapping.record.provider_response_id is None
        ):
            return _durable_status(
                run.record,
                error=RunError(
                    code=run.record.status_reason or "provider_request_rejected",
                    message="Hosted response request was rejected.",
                    fault_domain="provider",
                ),
            )

        assert mapping.record.provider_response_id is not None
        transport = await self._runtime.get_transport()
        try:
            response = await transport.retrieve(mapping.record.provider_response_id)
        except FoundryResponsesOperationError as error:
            if error.kind is FoundryResponsesOperationErrorKind.NOT_FOUND:
                return await self._adopt_missing_response(store, run.record)
            return _durable_status(
                run.record,
                last_sequence=mapping.record.max_public_event_sequence,
                error=RunError(
                    code="provider_unavailable",
                    message="Hosted response status is temporarily unavailable.",
                    fault_domain="provider",
                ),
            )
        return await self._project_response(
            store,
            run.record,
            mapping,
            response,
        )

    def read_events(
        self,
        context: RunContext,
        after_sequence: int,
    ) -> AsyncIterator[RunEvent]:
        """Tail one stored response, falling back to deterministic snapshots."""
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")

        async def stream() -> AsyncIterator[RunEvent]:
            cursor = after_sequence
            state_binding = await self._runtime.get_state_store()
            store = state_binding.store
            partition = owner_partition(self._owner)
            run = await store.get_run(partition, context.session_id, context.run_id)
            mapping = await store.get_provider_run_mapping(
                partition,
                context.session_id,
                context.run_id,
            )
            if (
                run.record.status not in TERMINAL_RUN_STATUSES
                and datetime.now(UTC) >= run.record.expires_at
            ):
                await self.get_run(context)
                run = await store.get_run(
                    partition,
                    context.session_id,
                    context.run_id,
                )
                mapping = await store.get_provider_run_mapping(
                    partition,
                    context.session_id,
                    context.run_id,
                )
            if mapping is None:
                yield _runtime_error_event(
                    max(cursor + 1, 1),
                    "provider_mapping_unavailable",
                    "Hosted response events are unavailable.",
                )
                return
            if mapping.record.response_state == "indeterminate":
                yield _runtime_error_event(
                    max(cursor + 1, 1),
                    "provider_indeterminate",
                    "Hosted response execution is indeterminate.",
                )
                return
            if mapping.record.response_state == "pending":
                if not _submission_grace_elapsed(mapping.record.created_at):
                    return
                terminal = await self._adopt_unbound_terminal(
                    store,
                    run.record,
                    status="timed_out",
                    reason="provider_submission_not_started",
                )
                yield _runtime_error_event(
                    max(cursor + 1, 1),
                    terminal.status_reason or "provider_submission_not_started",
                    "Hosted response submission did not start.",
                )
                return
            if mapping.record.response_state == "submitting":
                if not _submission_grace_elapsed(mapping.record.updated_at):
                    return
                outcome = await self._mark_indeterminate(
                    store,
                    partition,
                    context,
                    reason="provider_submission_indeterminate",
                )
                yield _runtime_error_event(
                    max(cursor + 1, 1),
                    outcome.mapping.indeterminate_reason or "provider_indeterminate",
                    "Hosted response execution is indeterminate.",
                )
                return
            if (
                mapping.record.response_state == "terminal"
                and mapping.record.provider_response_id is None
            ):
                yield _runtime_error_event(
                    max(cursor + 1, 1),
                    run.record.status_reason or "provider_request_rejected",
                    "Hosted response request was rejected.",
                )
                return

            assert mapping.record.provider_response_id is not None
            transport = await self._runtime.get_transport()
            current_mapping = mapping
            provider_starting_after = (
                None if after_sequence == 0 else after_sequence - 1
            )
            live_stream = self._live_streams.pop(context.run_id, None)
            try:
                provider_events = (
                    transport.replay(
                        mapping.record.provider_response_id,
                        starting_after=provider_starting_after,
                    )
                    if live_stream is None
                    else live_stream
                )
                async for provider_event in provider_events:
                    public_sequence = provider_event.provider_sequence + 1
                    provider_status: RunStatus | None = None
                    response = (
                        provider_event.data
                        if isinstance(provider_event.data, FoundryResponse)
                        else None
                    )
                    if response is not None:
                        provider_status = validate_terminal_output(
                            self._binding,
                            _response_status(
                                run.record,
                                response,
                                last_sequence=public_sequence,
                            ),
                        )
                    public_event = _replayed_response_event(
                        run.record,
                        provider_event,
                        provider_status,
                    )
                    if (
                        public_event is not None
                        and public_event.sequence > cursor
                        and _persist_replayed_watermark(provider_event.kind)
                    ):
                        current_mapping = await self._advance_watermark_best_effort(
                            store,
                            current_mapping,
                            public_event.sequence,
                        )
                    if (
                        response is not None
                        and provider_status is not None
                        and provider_status.state in TERMINAL_RUN_STATUSES
                    ):
                        provider_status = await self._project_response(
                            store,
                            run.record,
                            current_mapping,
                            response,
                        )
                        public_event = _replayed_response_event(
                            run.record,
                            provider_event,
                            provider_status,
                        )
                    if public_event is not None and public_event.sequence > cursor:
                        cursor = public_event.sequence
                        yield public_event
                    if (
                        provider_status is not None
                        and provider_status.state in TERMINAL_RUN_STATUSES
                    ):
                        return
                    if provider_event.kind is FoundryResponseEventKind.ERROR:
                        return
            except FoundryResponsesOperationError as error:
                if error.kind is not FoundryResponsesOperationErrorKind.INVALID_REQUEST:
                    raise
            finally:
                if live_stream is not None:
                    await live_stream.close()

            while True:
                response = await transport.retrieve(mapping.record.provider_response_id)
                status = validate_terminal_output(
                    self._binding,
                    _response_status(
                        run.record,
                        response,
                        last_sequence=current_mapping.record.max_public_event_sequence,
                    ),
                )
                for event in _polled_response_events(
                    response,
                    status,
                    cursor=cursor,
                    terminal_watermark=(
                        current_mapping.record.max_public_event_sequence
                        if run.record.status in TERMINAL_RUN_STATUSES
                        else None
                    ),
                ):
                    if event.sequence <= cursor:
                        continue
                    current_mapping = await self._advance_watermark_best_effort(
                        store,
                        current_mapping,
                        event.sequence,
                    )
                    cursor = event.sequence
                    yield event
                projected_status = await self._project_response(
                    store,
                    run.record,
                    current_mapping,
                    response,
                )
                if projected_status.state in TERMINAL_RUN_STATUSES:
                    return
                await self._sleep(FOUNDRY_RESPONSES_STATUS_POLL_INTERVAL_SECONDS)

        return stream()

    async def cancel_run(self, context: RunContext) -> RunStatus:
        """Cancel a known Response only when a terminal outcome can be proven."""
        state_binding = await self._runtime.get_state_store()
        store = state_binding.store
        partition = owner_partition(self._owner)
        run = await store.get_run(partition, context.session_id, context.run_id)
        mapping = await store.get_provider_run_mapping(
            partition,
            context.session_id,
            context.run_id,
        )
        if mapping is None:
            if run.record.status not in TERMINAL_RUN_STATUSES:
                await self._adopt_missing_mapping(store, run.record)
                run = await store.get_run(partition, context.session_id, context.run_id)
            return _durable_status(
                run.record,
                error=RunError(
                    code="provider_mapping_unavailable",
                    message="Hosted response mapping is unavailable.",
                    fault_domain="runtime",
                ),
            )
        if mapping.record.response_state == "indeterminate":
            return _indeterminate_status(run.record, mapping.record.indeterminate_reason)
        if mapping.record.response_state == "pending":
            terminal = await self._adopt_unbound_terminal(
                store,
                run.record,
                status="canceled",
                reason="provider_canceled_before_submit",
            )
            return _durable_status(
                terminal,
                error=RunError(
                    code="provider_canceled_before_submit",
                    message="Hosted response was canceled before submission.",
                    fault_domain="runtime",
                ),
            )
        if mapping.record.response_state == "submitting":
            outcome = await self._mark_indeterminate(
                store,
                partition,
                context,
                reason="provider_submission_indeterminate",
            )
            return _indeterminate_status(outcome.run, outcome.mapping.indeterminate_reason)
        if (
            mapping.record.response_state == "terminal"
            and mapping.record.provider_response_id is None
        ):
            return _durable_status(
                run.record,
                error=RunError(
                    code=run.record.status_reason or "provider_request_rejected",
                    message="Hosted response request was rejected.",
                    fault_domain="provider",
                ),
            )

        assert mapping.record.provider_response_id is not None
        transport = await self._runtime.get_transport()
        try:
            response = await transport.retrieve(mapping.record.provider_response_id)
        except FoundryResponsesOperationError as error:
            if error.kind is FoundryResponsesOperationErrorKind.NOT_FOUND:
                return await self._adopt_missing_response(store, run.record)
            return await self._quarantine_termination(store, partition, context)
        status = await self._project_response(store, run.record, mapping, response)
        if status.state in TERMINAL_RUN_STATUSES:
            return status

        try:
            response = await transport.cancel(mapping.record.provider_response_id)
        except FoundryResponsesOperationError:
            return await self._quarantine_termination(store, partition, context)
        status = await self._project_response(store, run.record, mapping, response)
        if status.state in TERMINAL_RUN_STATUSES:
            return status

        deadline = self._clock() + FOUNDRY_RESPONSES_CANCEL_POLL_WINDOW_SECONDS
        for _attempt in range(FOUNDRY_RESPONSES_CANCEL_POLL_ATTEMPTS):
            if self._clock() >= deadline:
                break
            await self._sleep(FOUNDRY_RESPONSES_CANCEL_POLL_INTERVAL_SECONDS)
            try:
                response = await transport.retrieve(mapping.record.provider_response_id)
            except FoundryResponsesOperationError as error:
                if error.kind is FoundryResponsesOperationErrorKind.NOT_FOUND:
                    return await self._adopt_missing_response(store, run.record)
                break
            status = await self._project_response(store, run.record, mapping, response)
            if status.state in TERMINAL_RUN_STATUSES:
                return status
        return await self._quarantine_termination(store, partition, context)

    async def _admit_new_session(
        self,
        store: SessionStateStore,
        partition: OwnerPartition,
        *,
        state_store_fingerprint: str,
        session_id: str,
        request: StartRunRequest,
        attempt: IdempotencyAttempt | None,
    ) -> AdmissionOutcome:
        now = datetime.now(UTC)
        initial_session = _new_session(
            partition,
            session_id,
            state_store_fingerprint=state_store_fingerprint,
            now=now,
            timeout=request.timeout,
        )
        run = _new_run(
            initial_session,
            mint_run_id(),
            timeout=request.timeout,
            now=now,
            agent_slug=self._binding.agent_name or "",
        )
        admitted_session = _session_with_active_run(initial_session, run.run_id, now)
        await store.create_session(initial_session)
        if attempt is None:
            return await store.admit_run(
                AdmissionRecords.create(admitted_session, run)
            )
        owner_record = DurableOwnerIdempotencyRecord.create(
            owner_partition=partition,
            idempotency_hash=attempt.key_hash,
            request_hash=attempt.request_hash,
            session_id=session_id,
            run_id=run.run_id,
            expires_at=owner_idempotency_expiry(
                admitted_session.expires_at,
                run.expires_at,
                None,
                now,
            ),
            created_at=now,
        )
        return await store.admit_new_session_run(
            NewSessionAdmissionRecords.create(admitted_session, run, owner_record)
        )

    async def _admit_existing_session(
        self,
        store: SessionStateStore,
        partition: OwnerPartition,
        *,
        session_id: str,
        request: StartRunRequest,
        attempt: IdempotencyAttempt | None,
    ) -> AdmissionOutcome:
        session_read = await store.get_session(partition, session_id)
        now = datetime.now(UTC)
        run = _new_run(
            session_read.record,
            mint_run_id(),
            timeout=request.timeout,
            now=now,
            agent_slug=self._binding.agent_name or "",
        )
        admitted_session = _session_with_active_run(session_read.record, run.run_id, now)
        idempotency = (
            None
            if attempt is None
            else DurableIdempotencyRecord.create(
                owner_partition=partition,
                session_id=session_id,
                idempotency_hash=attempt.key_hash,
                request_hash=attempt.request_hash,
                run_id=run.run_id,
                expires_at=run.expires_at,
                created_at=now,
            )
        )
        return await store.admit_run(
            AdmissionRecords.create(admitted_session, run, idempotency),
            expected_session_etag=session_read.etag,
        )

    async def _owner_idempotency_replay(
        self,
        store: SessionStateStore,
        partition: OwnerPartition,
        attempt: IdempotencyAttempt | None,
    ) -> DurableRunRecord | None:
        if attempt is None:
            return None
        existing = await store.get_owner_idempotency(
            partition,
            attempt.key_hash,
        )
        if existing is None:
            return None
        if existing.record.request_hash != attempt.request_hash:
            raise IdempotencyConflictError(
                "idempotency key already used with a different payload",
                existing_run_id=existing.record.run_id,
            )
        return (
            await store.get_run(
                partition,
                existing.record.session_id,
                existing.record.run_id,
            )
        ).record

    async def _session_idempotency_replay(
        self,
        store: SessionStateStore,
        partition: OwnerPartition,
        session_id: str,
        attempt: IdempotencyAttempt | None,
    ) -> DurableRunRecord | None:
        if attempt is None:
            return None
        existing = await store.get_idempotency(
            partition,
            session_id,
            attempt.key_hash,
        )
        if existing is None:
            return None
        if existing.record.request_hash != attempt.request_hash:
            raise IdempotencyConflictError(
                "idempotency key already used with a different payload",
                existing_run_id=existing.record.run_id,
            )
        return (
            await store.get_run(
                partition,
                session_id,
                existing.record.run_id,
            )
        ).record

    async def _replay_or_quarantine(
        self,
        store: SessionStateStore,
        partition: OwnerPartition,
        run: DurableRunRecord,
        prompt: str,
    ) -> RunHandle:
        session_binding = await store.get_provider_session_binding(
            partition,
            run.session_id,
        )
        provider_session_id = (
            None
            if session_binding is None
            else session_binding.record.provider_session_id
        )
        mapping = await store.get_provider_run_mapping(
            partition,
            run.session_id,
            run.run_id,
        )
        if mapping is None and run.status not in TERMINAL_RUN_STATUSES:
            return await self._submit_admitted_run(
                store,
                partition,
                run,
                prompt,
            )
        elif mapping is not None and mapping.record.response_state in {
            "pending",
            "submitting",
        }:
            return _run_handle(run, provider_session_id=provider_session_id)
        refreshed = await store.get_run(partition, run.session_id, run.run_id)
        return _run_handle(
            refreshed.record,
            provider_session_id=provider_session_id,
        )

    async def _submit_admitted_run(
        self,
        store: SessionStateStore,
        partition: OwnerPartition,
        run: DurableRunRecord,
        prompt: str,
    ) -> RunHandle:
        try:
            transport = await self._runtime.get_transport()
        except BaseException:
            await asyncio.shield(
                self._adopt_unbound_terminal(
                    store,
                    run,
                    status="failed",
                    reason="provider_submission_not_started",
                )
            )
            raise FoundryResponsesBackendError(
                "Hosted response transport is unavailable."
            ) from None

        mapping = DurableProviderRunMapping.create(
            owner_partition=partition,
            session_id=run.session_id,
            run_id=run.run_id,
            response_state="pending",
            provider_response_id=None,
            max_public_event_sequence=0,
            indeterminate_reason=None,
            created_at=run.created_at,
            updated_at=run.created_at,
        )
        try:
            await store.create_provider_run_mapping(mapping)
        except RowAlreadyExistsError:
            return await self._replay_or_quarantine(
                store,
                partition,
                run,
                prompt,
            )
        persisted_mapping = await store.get_provider_run_mapping(
            partition,
            run.session_id,
            run.run_id,
        )
        if persisted_mapping is None:
            raise FoundryResponsesBackendError("Hosted response mapping is unavailable.")

        expected_provider_session_id = _provider_session_id(
            binding_fingerprint=self._runtime.binding.binding_fingerprint,
            partition=partition,
            runtime_session_id=run.session_id,
        )
        session_binding = await store.get_provider_session_binding(
            partition,
            run.session_id,
        )
        if session_binding is None:
            candidate = DurableProviderSessionBinding.create(
                owner_partition=partition,
                session_id=run.session_id,
                provider_session_id=expected_provider_session_id,
                created_at=run.created_at,
                updated_at=run.created_at,
            )
            try:
                await store.create_provider_session_binding(candidate)
            except RowAlreadyExistsError:
                pass
            except BaseException:
                await asyncio.shield(
                    self._adopt_unbound_terminal(
                        store,
                        run,
                        status="failed",
                        reason="provider_submission_not_started",
                    )
                )
                raise FoundryResponsesBackendError(
                    "Hosted response session persistence failed before submission."
                ) from None
            session_binding = await store.get_provider_session_binding(
                partition,
                run.session_id,
            )
        if session_binding is None:
            await self._adopt_unbound_terminal(
                store,
                run,
                status="failed",
                reason="provider_submission_not_started",
            )
            raise FoundryResponsesBackendError(
                "Hosted response session binding is unavailable."
            )
        provider_session_id = session_binding.record.provider_session_id
        current_binding_session = provider_session_id == expected_provider_session_id
        try:
            if current_binding_session:
                provider_session = await transport.create_session(
                    FoundrySessionCreateRequest.create(
                        agent_session_id=provider_session_id,
                        agent_version=self._runtime.binding.managed_agent_version,
                    )
                )
            else:
                provider_session = await transport.get_session(provider_session_id)
        except FoundryResponsesOperationError as error:
            await asyncio.shield(
                self._adopt_unbound_terminal(
                    store,
                    run,
                    status="failed",
                    reason="provider_submission_not_started",
                )
            )
            if (
                not current_binding_session
                and error.kind is FoundryResponsesOperationErrorKind.NOT_FOUND
            ):
                raise SessionBindingUnavailableError(
                    "The hosted provider state for this session is no longer available."
                ) from None
            raise FoundryResponsesBackendError(
                "Hosted response session resolution failed before submission."
            ) from None
        except BaseException:
            await asyncio.shield(
                self._adopt_unbound_terminal(
                    store,
                    run,
                    status="failed",
                    reason="provider_submission_not_started",
                )
            )
            raise FoundryResponsesBackendError(
                "Hosted response session resolution failed before submission."
            ) from None
        if (
            provider_session.agent_session_id != provider_session_id
            or (
                current_binding_session
                and provider_session.agent_version
                != self._runtime.binding.managed_agent_version
            )
        ):
            await self._adopt_unbound_terminal(
                store,
                run,
                status="failed",
                reason="provider_submission_not_started",
            )
            raise FoundryResponsesBackendError(
                "Hosted response session binding is invalid."
            )

        submission_issued_at = datetime.now(UTC)
        try:
            issued_etag = await store.mark_provider_submission_issued(
                previous=persisted_mapping.record,
                etag=persisted_mapping.etag,
                updated_at=submission_issued_at,
            )
        except BaseException:
            await asyncio.shield(
                self._adopt_unbound_terminal(
                    store,
                    run,
                    status="failed",
                    reason="provider_submission_not_started",
                )
            )
            raise FoundryResponsesBackendError(
                "Hosted response submission could not be fenced."
            ) from None
        persisted_mapping = ProviderRunMappingRead(
            record=DurableProviderRunMapping.create(
                owner_partition=persisted_mapping.record.owner_partition,
                session_id=persisted_mapping.record.session_id,
                run_id=persisted_mapping.record.run_id,
                response_state="submitting",
                provider_response_id=None,
                max_public_event_sequence=0,
                indeterminate_reason=None,
                created_at=persisted_mapping.record.created_at,
                updated_at=submission_issued_at,
            ),
            etag=issued_etag,
        )

        envelope = FhaResponsesRequestEnvelope(
            agent_slug=self._binding.agent_name or "",
            history_scope=partition.owner_hash,
            runtime_session_id=run.session_id,
            runtime_run_id=run.run_id,
            prompt=prompt,
        )
        live_stream: FoundryResponseEventStream | None = None
        try:
            with start_fha_responses_create_span(
                agent_name=self._binding.agent_name or "",
                runtime_session_id=run.session_id,
                runtime_run_id=run.run_id,
            ) as trace_headers:
                create_request = FoundryResponseCreateRequest.create(
                    input_text=envelope.model_dump_json(),
                    agent_session_id=session_binding.record.provider_session_id,
                    trace_headers=trace_headers,
                )
                if self._stream_events:
                    live_stream = await transport.create_stream(create_request)
                    response = live_stream.response
                else:
                    response = await transport.create(create_request)
        except FoundryResponsesOperationError as error:
            if not error.retryable:
                return await self._adopt_definitive_submission_failure(store, run)
            await self._mark_indeterminate(
                store,
                partition,
                RunContext(session_id=run.session_id, run_id=run.run_id),
                reason="provider_submission_indeterminate",
            )
            raise FoundryResponsesBackendError("Hosted response submission failed.") from None
        except BaseException:
            await asyncio.shield(
                self._mark_indeterminate(
                    store,
                    partition,
                    RunContext(session_id=run.session_id, run_id=run.run_id),
                    reason="provider_submission_indeterminate",
                )
            )
            raise FoundryResponsesBackendError(
                "Hosted response submission outcome is indeterminate."
            ) from None

        if (
            response.agent_session_id is not None
            and response.agent_session_id != session_binding.record.provider_session_id
        ):
            await self._mark_indeterminate(
                store,
                partition,
                RunContext(session_id=run.session_id, run_id=run.run_id),
                reason="provider_submission_indeterminate",
            )
            if live_stream is not None:
                await live_stream.close()
            raise FoundryResponsesBackendError(
                "Hosted response submission outcome is indeterminate."
            )
        try:
            await store.bind_provider_response_id(
                previous=persisted_mapping.record,
                etag=persisted_mapping.etag,
                provider_response_id=response.response_id,
                updated_at=datetime.now(UTC),
            )
        except BaseException:
            await asyncio.shield(
                self._mark_indeterminate(
                    store,
                    partition,
                    RunContext(session_id=run.session_id, run_id=run.run_id),
                    reason="provider_submission_indeterminate",
                )
            )
            if live_stream is not None:
                await asyncio.shield(live_stream.close())
            raise FoundryResponsesBackendError(
                "Hosted response submission outcome is indeterminate."
            ) from None

        bound_mapping = await store.get_provider_run_mapping(
            partition,
            run.session_id,
            run.run_id,
        )
        assert bound_mapping is not None
        if live_stream is not None:
            self._live_streams[run.run_id] = live_stream
        await self._project_response(store, run, bound_mapping, response)
        return _run_handle(
            run,
            provider_session_id=provider_session_id,
        )

    async def _adopt_definitive_submission_failure(
        self,
        store: SessionStateStore,
        run: DurableRunRecord,
    ) -> RunHandle:
        failed = DurableRunRecord.create(
            owner_partition=run.owner_partition,
            session_id=run.session_id,
            run_id=run.run_id,
            generation=run.generation,
            status="failed",
            result_available=False,
            status_reason="provider_request_rejected",
            expires_at=run.expires_at,
            created_at=run.created_at,
            updated_at=datetime.now(UTC),
            agent_slug=run.agent_slug,
        )
        outcome = await store.adopt_provider_terminal_run(failed)
        return _run_handle(outcome.run)

    async def _adopt_unbound_terminal(
        self,
        store: SessionStateStore,
        run: DurableRunRecord,
        *,
        status: Literal["failed", "canceled", "timed_out"],
        reason: str,
    ) -> DurableRunRecord:
        terminal = DurableRunRecord.create(
            owner_partition=run.owner_partition,
            session_id=run.session_id,
            run_id=run.run_id,
            generation=run.generation,
            status=status,
            result_available=False,
            status_reason=reason,
            expires_at=run.expires_at,
            created_at=run.created_at,
            updated_at=datetime.now(UTC),
            agent_slug=run.agent_slug,
        )
        mapping = await store.get_provider_run_mapping(
            run.owner_partition,
            run.session_id,
            run.run_id,
        )
        if mapping is None:
            return (await store.adopt_terminal_run(terminal)).run
        return (await store.adopt_provider_terminal_run(terminal)).run

    async def _project_response(
        self,
        store: SessionStateStore,
        run: DurableRunRecord,
        mapping: ProviderRunMappingRead,
        response: FoundryResponse,
    ) -> RunStatus:
        status = _response_status(
            run,
            response,
            last_sequence=mapping.record.max_public_event_sequence,
        )
        status = validate_terminal_output(self._binding, status)
        if status.state not in TERMINAL_RUN_STATUSES:
            return status
        terminal = _terminal_record(run, status)
        adopted = await store.adopt_provider_terminal_run(terminal)
        return _status_with_durable_adoption(status, adopted.run)

    async def _adopt_missing_response(
        self,
        store: SessionStateStore,
        run: DurableRunRecord,
    ) -> RunStatus:
        unavailable = RunError(
            code=SESSION_TOMBSTONED_ERROR_CODE,
            message="Hosted response is no longer available.",
            fault_domain="provider",
        )
        if run.status in TERMINAL_RUN_STATUSES:
            return _durable_status(run, error=unavailable)
        terminal = DurableRunRecord.create(
            owner_partition=run.owner_partition,
            session_id=run.session_id,
            run_id=run.run_id,
            generation=run.generation,
            status="abandoned",
            result_available=False,
            status_reason="provider_response_unavailable",
            expires_at=run.expires_at,
            created_at=run.created_at,
            updated_at=datetime.now(UTC),
            agent_slug=run.agent_slug,
        )
        adopted = await store.adopt_provider_terminal_run(terminal)
        return _durable_status(
            adopted.run,
            error=unavailable,
        )

    async def _adopt_missing_mapping(
        self,
        store: SessionStateStore,
        run: DurableRunRecord,
    ) -> None:
        terminal = DurableRunRecord.create(
            owner_partition=run.owner_partition,
            session_id=run.session_id,
            run_id=run.run_id,
            generation=run.generation,
            status="abandoned",
            result_available=False,
            status_reason="provider_mapping_unavailable",
            expires_at=run.expires_at,
            created_at=run.created_at,
            updated_at=datetime.now(UTC),
            agent_slug=run.agent_slug,
        )
        await store.adopt_terminal_run(terminal)

    async def _mark_indeterminate(
        self,
        store: SessionStateStore,
        partition: OwnerPartition,
        context: RunContext,
        *,
        reason: ProviderIndeterminateReason,
    ) -> ProviderIndeterminateOutcome:
        return await store.mark_provider_run_indeterminate(
            owner_partition=partition,
            session_id=context.session_id,
            run_id=context.run_id,
            reason=reason,
            updated_at=datetime.now(UTC),
        )

    async def _quarantine_termination(
        self,
        store: SessionStateStore,
        partition: OwnerPartition,
        context: RunContext,
    ) -> RunStatus:
        outcome = await self._mark_indeterminate(
            store,
            partition,
            context,
            reason="provider_termination_indeterminate",
        )
        return _indeterminate_status(outcome.run, outcome.mapping.indeterminate_reason)

    async def _advance_watermark_best_effort(
        self,
        store: SessionStateStore,
        mapping: ProviderRunMappingRead,
        sequence: int,
    ) -> ProviderRunMappingRead:
        if sequence <= mapping.record.max_public_event_sequence:
            return mapping
        try:
            etag = await store.advance_provider_event_watermark(
                previous=mapping.record,
                etag=mapping.etag,
                max_public_event_sequence=sequence,
                updated_at=datetime.now(UTC),
            )
        except (ConcurrencyConflictError, SessionStateStoreError):
            return mapping
        return ProviderRunMappingRead(
            record=DurableProviderRunMapping.create(
                owner_partition=mapping.record.owner_partition,
                session_id=mapping.record.session_id,
                run_id=mapping.record.run_id,
                response_state=mapping.record.response_state,
                provider_response_id=mapping.record.provider_response_id,
                max_public_event_sequence=sequence,
                indeterminate_reason=mapping.record.indeterminate_reason,
                created_at=mapping.record.created_at,
                updated_at=datetime.now(UTC),
            ),
            etag=etag,
        )


def _new_session(
    partition: OwnerPartition,
    session_id: str,
    *,
    state_store_fingerprint: str,
    now: datetime,
    timeout: float | None,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=partition,
        session_id=session_id,
        sandbox_id=None,
        generation=1,
        digest_kind=_FOUNDRY_SESSION_DIGEST_KIND,
        digest="fha-runtime",
        protocol=_FOUNDRY_SESSION_PROTOCOL,
        status="ready",
        last_activity_at=now,
        expires_at=now + timedelta(seconds=FOUNDRY_RESPONSES_SESSION_RETENTION_SECONDS),
        idle_policy_armed=False,
        active_run_id=None,
        snapshot_ids=(),
        region=_FOUNDRY_SESSION_REGION,
        state_store_fingerprint=state_store_fingerprint,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=now,
        updated_at=now,
        active_operation_id=None,
        operation_sequence=0,
    )


def _new_run(
    session: DurableSessionRecord,
    run_id: str,
    *,
    timeout: float | None,
    now: datetime,
    agent_slug: str,
) -> DurableRunRecord:
    return DurableRunRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        run_id=run_id,
        generation=session.generation,
        status="accepted",
        result_available=False,
        status_reason=None,
        expires_at=now + timedelta(seconds=timeout or DEFAULT_TIMEOUT),
        created_at=now,
        updated_at=now,
        agent_slug=agent_slug,
    )


def _session_with_active_run(
    session: DurableSessionRecord,
    run_id: str,
    updated_at: datetime,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="running",
        last_activity_at=max(session.last_activity_at, updated_at),
        expires_at=session.expires_at,
        idle_policy_armed=session.idle_policy_armed,
        active_run_id=run_id,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=max(session.updated_at, updated_at),
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


def _run_handle(
    run: DurableRunRecord,
    *,
    provider_session_id: str | None = None,
) -> RunHandle:
    return RunHandle(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.status,
        created_at=run.created_at,
        provider_session_id=provider_session_id,
    )


def _response_status(
    run: DurableRunRecord,
    response: FoundryResponse,
    *,
    last_sequence: int,
) -> RunStatus:
    if response.status is FoundryResponseStatus.QUEUED:
        state: RunState = "accepted"
        result = None
        error = None
    elif response.status is FoundryResponseStatus.IN_PROGRESS:
        state = "running"
        result = None
        error = None
    elif response.status is FoundryResponseStatus.COMPLETED:
        state = "succeeded"
        result = RunResult(
            content=response.output_text,
            content_intermediate=[],
            tool_calls=[],
            reasoning=None,
            delegate_error_count=0,
        )
        error = None
    elif response.status is FoundryResponseStatus.CANCELLED:
        state = "canceled"
        result = None
        error = RunError(
            code="provider_canceled",
            message="Hosted response was canceled.",
            fault_domain="provider",
        )
    elif response.status is FoundryResponseStatus.INCOMPLETE:
        state = "failed"
        result = None
        error = RunError(
            code="provider_incomplete",
            message="Hosted response completed incompletely.",
            fault_domain="provider",
        )
    elif response.status is FoundryResponseStatus.FAILED:
        state = "failed"
        result = None
        error = RunError(
            code="provider_failed",
            message="Hosted response failed.",
            fault_domain="provider",
        )
    else:
        state = "accepted"
        result = None
        error = RunError(
            code="provider_status_unknown",
            message="Hosted response status is unavailable.",
            fault_domain="provider",
        )
    return RunStatus(
        run_id=run.run_id,
        session_id=run.session_id,
        state=state,
        last_sequence=last_sequence,
        result_available=result is not None,
        result=result,
        error=error,
    )


def _terminal_record(run: DurableRunRecord, status: RunStatus) -> DurableRunRecord:
    assert status.state in TERMINAL_RUN_STATUSES
    return DurableRunRecord.create(
        owner_partition=run.owner_partition,
        session_id=run.session_id,
        run_id=run.run_id,
        generation=run.generation,
        status=status.state,
        result_available=status.state == "succeeded" and status.result is not None,
        status_reason=None if status.error is None else status.error.code,
        expires_at=run.expires_at,
        created_at=run.created_at,
        updated_at=datetime.now(UTC),
        agent_slug=run.agent_slug,
    )


def _status_with_durable_adoption(status: RunStatus, run: DurableRunRecord) -> RunStatus:
    return RunStatus(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.status,
        last_sequence=status.last_sequence,
        result_available=run.result_available,
        result=status.result if run.result_available else None,
        error=status.error,
    )


def _durable_status(
    run: DurableRunRecord,
    *,
    last_sequence: int = 0,
    error: RunError | None = None,
) -> RunStatus:
    return RunStatus(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.status,
        last_sequence=last_sequence,
        result_available=False,
        result=None,
        error=error,
    )


def _indeterminate_status(
    run: DurableRunRecord,
    reason: str | None,
) -> RunStatus:
    return _durable_status(
        run,
        error=RunError(
            code=reason or "provider_indeterminate",
            message="Hosted response execution is indeterminate.",
            fault_domain="provider",
        ),
    )


def _polled_response_events(
    response: FoundryResponse,
    status: RunStatus,
    *,
    cursor: int,
    terminal_watermark: int | None,
) -> tuple[RunEvent, ...]:
    timestamp = datetime.now(UTC)
    if terminal_watermark is None:
        next_sequence = max(cursor + 1, 1)
        events = (
            [
                RunEvent(
                    sequence=next_sequence,
                    type="session",
                    data={"status": status.state},
                    timestamp=timestamp,
                )
            ]
            if cursor == 0
            else []
        )
        next_sequence += len(events)
    else:
        next_sequence = terminal_watermark
        events = (
            [
                RunEvent(
                    sequence=1,
                    type="session",
                    data={"status": status.state},
                    timestamp=timestamp,
                )
            ]
            if cursor == 0
            else []
        )
    if status.state == "succeeded" and status.result is not None:
        message_sequence = (
            max(next_sequence, 2)
            if terminal_watermark is None
            else max(terminal_watermark - 1, 2)
        )
        done_sequence = (
            message_sequence + 1
            if terminal_watermark is None
            else max(terminal_watermark, 3)
        )
        events.extend(
            (
                RunEvent(
                    sequence=message_sequence,
                    type="message",
                    data={"content": response.output_text},
                    timestamp=timestamp,
                ),
                RunEvent(
                    sequence=done_sequence,
                    type="done",
                    data={"content": response.output_text},
                    timestamp=timestamp,
                ),
            )
        )
    elif status.state in TERMINAL_RUN_STATUSES:
        events.append(
            _runtime_error_event(
                (
                    max(next_sequence, 2)
                    if terminal_watermark is None
                    else max(terminal_watermark, 2)
                ),
                status.error.code if status.error is not None else "provider_failed",
                status.error.message if status.error is not None else "Hosted response failed.",
            )
        )
    return tuple(events)


def _replayed_response_event(
    run: DurableRunRecord,
    event: FoundryResponseEvent,
    status: RunStatus | None,
) -> RunEvent | None:
    sequence = event.provider_sequence + 1
    timestamp = datetime.now(UTC)
    if event.kind in {
        FoundryResponseEventKind.CREATED,
        FoundryResponseEventKind.IN_PROGRESS,
    }:
        if status is None:
            raise FoundryResponsesBackendError(
                "Hosted response lifecycle event did not include a response."
            )
        return RunEvent(
            sequence=sequence,
            type="session",
            data={"session_id": run.session_id, "status": status.state},
            timestamp=timestamp,
        )
    if event.kind is FoundryResponseEventKind.TEXT_DELTA:
        if not isinstance(event.data, FoundryResponseText):
            raise FoundryResponsesBackendError(
                "Hosted response text delta did not include text."
            )
        return RunEvent(
            sequence=sequence,
            type="delta",
            data={"content": event.data.text},
            timestamp=timestamp,
        )
    if event.kind is FoundryResponseEventKind.TEXT_DONE:
        if not isinstance(event.data, FoundryResponseText):
            raise FoundryResponsesBackendError(
                "Hosted response text completion did not include text."
            )
        return RunEvent(
            sequence=sequence,
            type="message",
            data={"content": event.data.text},
            timestamp=timestamp,
        )
    if event.kind is FoundryResponseEventKind.COMPLETED:
        if status is None:
            raise FoundryResponsesBackendError(
                "Hosted response completion did not include a response."
            )
        if status.state != "succeeded" or status.result is None:
            return _runtime_error_event(
                sequence,
                status.error.code if status.error is not None else "provider_failed",
                status.error.message if status.error is not None else "Hosted response failed.",
            )
        return RunEvent(
            sequence=sequence,
            type="done",
            data={"content": status.result.content},
            timestamp=timestamp,
        )
    if event.kind in {
        FoundryResponseEventKind.FAILED,
        FoundryResponseEventKind.CANCELLED,
        FoundryResponseEventKind.INCOMPLETE,
    }:
        if status is None:
            raise FoundryResponsesBackendError(
                "Hosted response terminal event did not include a response."
            )
        return _runtime_error_event(
            sequence,
            status.error.code if status.error is not None else "provider_failed",
            status.error.message if status.error is not None else "Hosted response failed.",
        )
    if event.kind is FoundryResponseEventKind.ERROR:
        return _runtime_error_event(
            sequence,
            "provider_stream_error",
            "Hosted response streaming failed.",
        )
    return None


def _persist_replayed_watermark(kind: FoundryResponseEventKind) -> bool:
    return kind is not FoundryResponseEventKind.TEXT_DELTA


def _runtime_error_event(sequence: int, code: str, message: str) -> RunEvent:
    return RunEvent(
        sequence=sequence,
        type="error",
        data={"code": code, "content": message},
        timestamp=datetime.now(UTC),
    )


def _provider_session_id(
    *,
    binding_fingerprint: str,
    partition: OwnerPartition,
    runtime_session_id: str,
) -> str:
    digest = hashlib.sha256(
        frame_canonical_components(
            (
                "foundry_responses_provider_session",
                "fhs1",
                binding_fingerprint,
                partition.partition_key,
                runtime_session_id,
            )
        )
    ).digest()
    return f"fhs1-{encode_label_safe_digest(digest)}"


def _submission_grace_elapsed(created_at: datetime) -> bool:
    return datetime.now(UTC) >= created_at + timedelta(
        seconds=FOUNDRY_RESPONSES_SUBMISSION_GRACE_SECONDS
    )
