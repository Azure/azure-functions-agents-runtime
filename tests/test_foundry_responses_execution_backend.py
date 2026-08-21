from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import azure_functions_agents._observability as observability
from azure_functions_agents.controller.idempotency import build_idempotency_attempt
from azure_functions_agents.controller.readiness import StateStoreBinding
from azure_functions_agents.execution.backend import (
    RunContext,
    RunError,
    SessionBindingUnavailableError,
    StartRunRequest,
)
from azure_functions_agents.execution.binding import AgentBinding
from azure_functions_agents.execution.foundry_application_content import (
    build_application_content_manifest,
    compute_application_content_digest,
)
from azure_functions_agents.execution.foundry_responses_binding import (
    FoundryResponsesRuntimeBinding,
    compute_foundry_responses_binding_fingerprint,
)
from azure_functions_agents.execution.foundry_responses_execution_backend import (
    FoundryResponsesBackendError,
    FoundryResponsesExecutionBackend,
)
from azure_functions_agents.execution.foundry_responses_runtime import FoundryResponsesRuntime
from azure_functions_agents.foundry_responses.fha_private_history import (
    FhaResponsesRequestEnvelope,
)
from azure_functions_agents.session_state import (
    AdmissionOutcome,
    AdoptionOutcome,
    AppIdentity,
    DurableProviderRunMapping,
    DurableProviderSessionBinding,
    DurableRunRecord,
    DurableSessionRecord,
    FunctionAppPrincipal,
    OwnerPartition,
    ProviderIndeterminateOutcome,
    ProviderRunMappingRead,
    ProviderSessionBindingRead,
    RunRead,
    SessionRead,
    owner_partition,
)
from azure_functions_agents.session_state.identity import resolve_owner_context
from azure_functions_agents.transport.foundry_responses import (
    FoundryResponse,
    FoundryResponseCreateRequest,
    FoundryResponseEvent,
    FoundryResponseEventKind,
    FoundryResponsesOperation,
    FoundryResponsesOperationError,
    FoundryResponsesOperationErrorKind,
    FoundryResponseStatus,
    FoundryResponseText,
    FoundrySession,
    FoundrySessionCreateRequest,
)

_NOW = datetime(2026, 8, 14, tzinfo=UTC)
_APP = AppIdentity.create(
    subscription_id="11111111-2222-3333-4444-555555555555",
    site_name="agent-app",
)
_PROJECT_RESOURCE_ID = (
    "/subscriptions/11111111-2222-3333-4444-555555555555"
    "/resourceGroups/agents-rg/providers/Microsoft.CognitiveServices/accounts/project/projects/demo"
)


class _Store:
    def __init__(self) -> None:
        self.session: DurableSessionRecord | None = None
        self.runs: dict[str, DurableRunRecord] = {}
        self.owner_idempotency: dict[str, tuple[str, str, str]] = {}
        self.idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self.session_bindings: dict[str, DurableProviderSessionBinding] = {}
        self.mappings: dict[str, DurableProviderRunMapping] = {}
        self.calls: list[str] = []
        self.watermark_sequences: list[int] = []

    async def create_session(self, record: DurableSessionRecord) -> str:
        assert self.session is None
        self.session = record
        self.calls.append("create_session")
        return "session-1"

    async def get_session(self, _partition: OwnerPartition, session_id: str) -> SessionRead:
        if self.session is None or self.session.session_id != session_id:
            from azure_functions_agents.session_state import SessionRowNotFoundError

            raise SessionRowNotFoundError("missing")
        return SessionRead(record=self.session, etag="session-1")

    async def get_run(
        self,
        _partition: OwnerPartition,
        _session_id: str,
        run_id: str,
    ) -> RunRead:
        return RunRead(record=self.runs[run_id], etag=f"run-{run_id}")

    async def get_owner_idempotency(
        self,
        _partition: OwnerPartition,
        idempotency_hash: str,
    ):
        record = self.owner_idempotency.get(idempotency_hash)
        if record is None:
            return None
        from azure_functions_agents.session_state import OwnerIdempotencyRead

        request_hash, session_id, run_id = record
        return OwnerIdempotencyRead(
            record=type(
                "OwnerIdempotency",
                (),
                {
                    "request_hash": request_hash,
                    "session_id": session_id,
                    "run_id": run_id,
                },
            )(),
            etag="owner-idem",
        )

    async def get_idempotency(
        self,
        _partition: OwnerPartition,
        session_id: str,
        idempotency_hash: str,
    ):
        record = self.idempotency.get((session_id, idempotency_hash))
        if record is None:
            return None
        from azure_functions_agents.session_state import IdempotencyRead

        request_hash, run_id = record
        return IdempotencyRead(
            record=type(
                "Idempotency",
                (),
                {"request_hash": request_hash, "run_id": run_id},
            )(),
            etag="idem",
        )

    async def admit_run(self, records, *, expected_session_etag: str | None = None) -> AdmissionOutcome:
        del expected_session_etag
        assert self.session is not None
        assert self.session.active_run_id is None
        self.session = records.session
        self.runs[records.run.run_id] = records.run
        if records.idempotency is not None:
            self.idempotency[(records.session.session_id, records.idempotency.idempotency_hash)] = (
                records.idempotency.request_hash,
                records.run.run_id,
            )
        self.calls.append("admit")
        return AdmissionOutcome(
            run=records.run,
            run_etag=f"run-{records.run.run_id}",
            session_etag="session-admitted",
            replayed=False,
        )

    async def admit_new_session_run(self, records, *, expected_session_etag: str | None = None) -> AdmissionOutcome:
        del expected_session_etag
        existing = self.owner_idempotency.get(records.owner_idempotency.idempotency_hash)
        if existing is not None:
            request_hash, _session_id, run_id = existing
            if request_hash != records.owner_idempotency.request_hash:
                from azure_functions_agents.session_state import IdempotencyConflictError

                raise IdempotencyConflictError("conflict", existing_run_id=run_id)
            return AdmissionOutcome(
                run=self.runs[run_id],
                run_etag=f"run-{run_id}",
                session_etag=None,
                replayed=True,
            )
        assert self.session is not None
        self.session = records.session
        self.runs[records.run.run_id] = records.run
        self.owner_idempotency[records.owner_idempotency.idempotency_hash] = (
            records.owner_idempotency.request_hash,
            records.session.session_id,
            records.run.run_id,
        )
        self.calls.append("admit_new")
        return AdmissionOutcome(
            run=records.run,
            run_etag=f"run-{records.run.run_id}",
            session_etag="session-admitted",
            replayed=False,
        )

    async def get_provider_session_binding(
        self,
        _partition: OwnerPartition,
        session_id: str,
    ) -> ProviderSessionBindingRead | None:
        record = self.session_bindings.get(session_id)
        return None if record is None else ProviderSessionBindingRead(record=record, etag="binding")

    async def create_provider_session_binding(self, record: DurableProviderSessionBinding) -> str:
        assert record.session_id not in self.session_bindings
        self.session_bindings[record.session_id] = record
        self.calls.append("reserve_session_binding")
        return "binding"

    async def get_provider_run_mapping(
        self,
        _partition: OwnerPartition,
        _session_id: str,
        run_id: str,
    ) -> ProviderRunMappingRead | None:
        record = self.mappings.get(run_id)
        return None if record is None else ProviderRunMappingRead(record=record, etag=f"mapping-{run_id}")

    async def create_provider_run_mapping(self, record: DurableProviderRunMapping) -> str:
        assert record.run_id not in self.mappings
        self.mappings[record.run_id] = record
        self.calls.append("reserve_mapping")
        return f"mapping-{record.run_id}"

    async def bind_provider_response_id(
        self,
        *,
        previous: DurableProviderRunMapping,
        etag: str,
        provider_response_id: str,
        updated_at: datetime,
    ) -> str:
        del etag
        assert self.mappings[previous.run_id] == previous
        self.mappings[previous.run_id] = DurableProviderRunMapping.create(
            owner_partition=previous.owner_partition,
            session_id=previous.session_id,
            run_id=previous.run_id,
            response_state="bound",
            provider_response_id=provider_response_id,
            max_public_event_sequence=previous.max_public_event_sequence,
            indeterminate_reason=None,
            created_at=previous.created_at,
            updated_at=updated_at,
        )
        self.calls.append("bind_response")
        return f"mapping-{previous.run_id}"

    async def mark_provider_submission_issued(
        self,
        *,
        previous: DurableProviderRunMapping,
        etag: str,
        updated_at: datetime,
    ) -> str:
        del etag
        self.mappings[previous.run_id] = DurableProviderRunMapping.create(
            owner_partition=previous.owner_partition,
            session_id=previous.session_id,
            run_id=previous.run_id,
            response_state="submitting",
            provider_response_id=None,
            max_public_event_sequence=0,
            indeterminate_reason=None,
            created_at=previous.created_at,
            updated_at=updated_at,
        )
        self.calls.append("submission_issued")
        return f"mapping-{previous.run_id}"

    async def advance_provider_event_watermark(
        self,
        *,
        previous: DurableProviderRunMapping,
        etag: str,
        max_public_event_sequence: int,
        updated_at: datetime,
    ) -> str:
        del etag
        self.watermark_sequences.append(max_public_event_sequence)
        self.mappings[previous.run_id] = DurableProviderRunMapping.create(
            owner_partition=previous.owner_partition,
            session_id=previous.session_id,
            run_id=previous.run_id,
            response_state=previous.response_state,
            provider_response_id=previous.provider_response_id,
            max_public_event_sequence=max_public_event_sequence,
            indeterminate_reason=previous.indeterminate_reason,
            created_at=previous.created_at,
            updated_at=updated_at,
        )
        return f"mapping-{previous.run_id}"

    async def mark_provider_run_indeterminate(
        self,
        *,
        owner_partition: OwnerPartition,
        session_id: str,
        run_id: str,
        reason: str,
        updated_at: datetime,
    ) -> ProviderIndeterminateOutcome:
        previous = self.mappings[run_id]
        mapping = DurableProviderRunMapping.create(
            owner_partition=owner_partition,
            session_id=session_id,
            run_id=run_id,
            response_state="indeterminate",
            provider_response_id=previous.provider_response_id,
            max_public_event_sequence=previous.max_public_event_sequence,
            indeterminate_reason=reason,  # type: ignore[arg-type]
            created_at=previous.created_at,
            updated_at=updated_at,
        )
        run = replace(
            self.runs[run_id],
            status="abandoned",
            result_available=False,
            status_reason=reason,
            updated_at=updated_at,
        )
        assert self.session is not None
        session = replace(
            self.session,
            status="quarantined",
            active_run_id=None,
            quarantine_reason=reason,
            updated_at=updated_at,
        )
        self.mappings[run_id] = mapping
        self.runs[run_id] = run
        self.session = session
        self.calls.append("indeterminate")
        return ProviderIndeterminateOutcome(
            mapping=mapping,
            mapping_etag=f"mapping-{run_id}",
            run=run,
            run_etag=f"run-{run_id}",
            session=session,
            session_etag="session-quarantined",
        )

    async def adopt_provider_terminal_run(self, terminal_run: DurableRunRecord) -> AdoptionOutcome:
        self.runs[terminal_run.run_id] = terminal_run
        mapping = self.mappings[terminal_run.run_id]
        self.mappings[terminal_run.run_id] = DurableProviderRunMapping.create(
            owner_partition=mapping.owner_partition,
            session_id=mapping.session_id,
            run_id=mapping.run_id,
            response_state="terminal",
            provider_response_id=mapping.provider_response_id,
            max_public_event_sequence=mapping.max_public_event_sequence,
            indeterminate_reason=None,
            created_at=mapping.created_at,
            updated_at=terminal_run.updated_at,
        )
        assert self.session is not None
        self.session = replace(
            self.session,
            status="ready",
            active_run_id=None,
            updated_at=terminal_run.updated_at,
        )
        self.calls.append("adopt_provider_terminal")
        return AdoptionOutcome(run=terminal_run, run_etag="terminal", slot_released=True)

    async def adopt_terminal_run(self, terminal_run: DurableRunRecord) -> AdoptionOutcome:
        self.runs[terminal_run.run_id] = terminal_run
        assert self.session is not None
        self.session = replace(self.session, status="ready", active_run_id=None)
        return AdoptionOutcome(run=terminal_run, run_etag="terminal", slot_released=True)


class _Transport:
    def __init__(self) -> None:
        self.response = FoundryResponse.create(
            response_id="private-response",
            status=FoundryResponseStatus.QUEUED,
            output_text="",
            agent_session_id=None,
            error=None,
            incomplete_details=None,
        )
        self.sessions: dict[str, FoundrySession] = {}
        self.create_session_calls: list[FoundrySessionCreateRequest] = []
        self.create_session_error: Exception | None = None
        self.get_session_calls: list[str] = []
        self.get_session_error: Exception | None = None
        self.create_calls: list[FoundryResponseCreateRequest] = []
        self.create_stream_calls: list[FoundryResponseCreateRequest] = []
        self.create_error: Exception | None = None
        self.cancel_response: FoundryResponse | None = None
        self.retrieve_responses: list[FoundryResponse] = []
        self.replay_events: list[FoundryResponseEvent] = []
        self.replay_calls: list[tuple[str, int | None]] = []
        self.replay_error: Exception | None = None
        self.live_events: list[FoundryResponseEvent] = []
        self.live_streams: list[_LiveStream] = []
        self.retrieve_calls = 0
        self.cancel_calls = 0

    async def create_session(self, request: FoundrySessionCreateRequest) -> FoundrySession:
        self.create_session_calls.append(request)
        if self.create_session_error is not None:
            raise self.create_session_error
        session = FoundrySession.create(
            agent_session_id=request.agent_session_id,
            agent_version=request.agent_version,
        )
        self.sessions[request.agent_session_id] = session
        return session

    async def get_session(self, agent_session_id: str) -> FoundrySession:
        self.get_session_calls.append(agent_session_id)
        if self.get_session_error is not None:
            raise self.get_session_error
        return self.sessions[agent_session_id]

    async def create(self, request: FoundryResponseCreateRequest) -> FoundryResponse:
        self.create_calls.append(request)
        if self.create_error is not None:
            raise self.create_error
        return self.response

    async def create_stream(
        self,
        request: FoundryResponseCreateRequest,
    ) -> _LiveStream:
        self.create_stream_calls.append(request)
        if self.create_error is not None:
            raise self.create_error
        events = self.live_events or [
            FoundryResponseEvent.create(
                provider_sequence=0,
                kind=FoundryResponseEventKind.CREATED,
                data=self.response,
            )
        ]
        stream = _LiveStream(response=self.response, events=events)
        self.live_streams.append(stream)
        return stream

    async def retrieve(self, _response_id: str) -> FoundryResponse:
        self.retrieve_calls += 1
        if self.retrieve_responses:
            return self.retrieve_responses.pop(0)
        return self.response

    async def replay(
        self,
        response_id: str,
        *,
        starting_after: int | None = None,
    ):
        self.replay_calls.append((response_id, starting_after))
        if self.replay_error is not None:
            raise self.replay_error
        for event in self.replay_events:
            if starting_after is None or event.provider_sequence > starting_after:
                yield event

    async def cancel(self, _response_id: str) -> FoundryResponse:
        self.cancel_calls += 1
        return self.cancel_response or self.response

    async def close(self) -> None:
        return None


class _LiveStream:
    def __init__(
        self,
        *,
        response: FoundryResponse,
        events: list[FoundryResponseEvent],
    ) -> None:
        self.response = response
        self._events = events
        self.close_calls = 0

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        try:
            for event in self._events:
                yield event
        finally:
            await self.close()

    async def close(self) -> None:
        if self.close_calls == 0:
            self.close_calls += 1


def _runtime(
    tmp_path,
    store: _Store,
    transport: _Transport,
    *,
    managed_agent_version: str = "v1",
) -> FoundryResponsesRuntime:
    (tmp_path / "main.agent.md").write_text("---\nname: Main\n---\n", encoding="utf-8")
    manifest = build_application_content_manifest(tmp_path)
    digest = compute_application_content_digest(tmp_path, manifest)
    binding = FoundryResponsesRuntimeBinding.create(
        project_endpoint="https://project.services.ai.azure.com/api/projects/demo",
        project_resource_id=_PROJECT_RESOURCE_ID,
        managed_agent_name="hosted-agent",
        managed_agent_version=managed_agent_version,
        application_content_manifest=manifest,
        application_content_digest=digest,
        wrapper_digest="sha256:" + ("a" * 64),
        binding_fingerprint="fha1-" + ("a" * 52),
    )
    binding = replace(
        binding,
        binding_fingerprint=compute_foundry_responses_binding_fingerprint(
            app_identity=_APP,
            project_endpoint=binding.project_endpoint,
            project_resource_id=binding.project_resource_id,
            managed_agent_name=binding.managed_agent_name,
            managed_agent_version=binding.managed_agent_version,
            application_content_manifest=binding.application_content_manifest,
            application_content_digest=binding.application_content_digest,
            wrapper_digest=binding.wrapper_digest,
        ),
    )

    async def transport_factory() -> _Transport:
        return transport

    async def store_factory() -> StateStoreBinding:
        return StateStoreBinding.create(
            store=store,  # type: ignore[arg-type]
            state_store_fingerprint="s1-" + ("a" * 52),
        )

    return FoundryResponsesRuntime.create(
        binding=binding,
        app_identity=_APP,
        transport_factory=transport_factory,
        state_store_factory=store_factory,
    )


def _backend(
    tmp_path,
    store: _Store,
    transport: _Transport,
    *,
    managed_agent_version: str = "v1",
    stream_events: bool = False,
) -> FoundryResponsesExecutionBackend:
    runtime = _runtime(
        tmp_path,
        store,
        transport,
        managed_agent_version=managed_agent_version,
    )
    owner = resolve_owner_context(_APP, "main", FunctionAppPrincipal())
    return FoundryResponsesExecutionBackend(
        AgentBinding(agent_name="main"),
        runtime=runtime,
        owner=owner,
        stream_events=stream_events,
        sleep=_no_sleep,
        clock=lambda: 0.0,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_backend_admits_and_binds_before_exposing_a_provider_response(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    backend = _backend(tmp_path, store, transport)

    handle = await backend.start_run(StartRunRequest(prompt="Hello", idempotency_key="same"))

    assert store.calls[:5] == [
        "create_session",
        "admit_new",
        "reserve_mapping",
        "reserve_session_binding",
        "submission_issued",
    ]
    assert len(transport.create_calls) == 1
    envelope = FhaResponsesRequestEnvelope.parse_json_input(transport.create_calls[0].input_text)
    assert envelope.runtime_session_id == handle.session_id
    assert envelope.runtime_run_id == handle.run_id
    assert "owner" not in transport.create_calls[0].input_text
    assert store.session_bindings[handle.session_id].provider_session_id != handle.session_id
    assert (
        handle.provider_session_id
        == store.session_bindings[handle.session_id].provider_session_id
    )
    assert len(transport.create_session_calls) == 1
    assert transport.create_session_calls[0].agent_version == "v1"
    assert (
        transport.create_session_calls[0].agent_session_id
        == store.session_bindings[handle.session_id].provider_session_id
    )
    assert store.mappings[handle.run_id].response_state == "bound"

    replay = await backend.start_run(StartRunRequest(prompt="Hello", idempotency_key="same"))

    assert replay == handle
    assert len(transport.create_calls) == 1
    assert len(transport.create_session_calls) == 1


@pytest.mark.asyncio
async def test_existing_session_reuses_provider_binding_after_application_update(
    tmp_path,
) -> None:
    store = _Store()
    transport = _Transport()
    version_7_backend = _backend(
        tmp_path,
        store,
        transport,
        managed_agent_version="7",
    )
    first = await version_7_backend.start_run(StartRunRequest(prompt="First"))
    provider_session_id = store.session_bindings[first.session_id].provider_session_id
    assert store.session is not None
    store.session = replace(store.session, status="ready", active_run_id=None)
    transport.response = FoundryResponse.create(
        response_id="private-response-v8-controller",
        status=FoundryResponseStatus.QUEUED,
        output_text="",
        agent_session_id=provider_session_id,
        error=None,
        incomplete_details=None,
    )

    version_8_backend = _backend(
        tmp_path,
        store,
        transport,
        managed_agent_version="8",
    )
    continued = await version_8_backend.start_run(
        StartRunRequest(prompt="Continue", session_id=first.session_id)
    )

    assert continued.session_id == first.session_id
    assert continued.provider_session_id == provider_session_id
    assert transport.get_session_calls == [provider_session_id]
    assert [call.agent_version for call in transport.create_session_calls] == ["7"]
    assert transport.create_calls[-1].agent_session_id == provider_session_id

    fresh_store = _Store()
    fresh_transport = _Transport()
    fresh_backend = _backend(
        tmp_path,
        fresh_store,
        fresh_transport,
        managed_agent_version="8",
    )
    await fresh_backend.start_run(StartRunRequest(prompt="Fresh"))

    assert [call.agent_version for call in fresh_transport.create_session_calls] == ["8"]
    assert fresh_transport.get_session_calls == []


@pytest.mark.asyncio
async def test_missing_retained_provider_session_requires_new_runtime_session(
    tmp_path,
) -> None:
    store = _Store()
    transport = _Transport()
    version_7_backend = _backend(
        tmp_path,
        store,
        transport,
        managed_agent_version="7",
    )
    first = await version_7_backend.start_run(StartRunRequest(prompt="First"))
    provider_session_id = store.session_bindings[first.session_id].provider_session_id
    assert store.session is not None
    store.session = replace(store.session, status="ready", active_run_id=None)
    transport.get_session_error = FoundryResponsesOperationError(
        FoundryResponsesOperation.GET_SESSION,
        FoundryResponsesOperationErrorKind.NOT_FOUND,
        retryable=False,
    )

    version_8_backend = _backend(
        tmp_path,
        store,
        transport,
        managed_agent_version="8",
    )
    with pytest.raises(SessionBindingUnavailableError):
        await version_8_backend.start_run(
            StartRunRequest(prompt="Continue", session_id=first.session_id)
        )

    assert transport.get_session_calls == [provider_session_id]
    assert [call.agent_version for call in transport.create_session_calls] == ["7"]
    assert store.session.status == "ready"


@pytest.mark.asyncio
async def test_backend_injects_w3c_context_from_one_client_span(monkeypatch, tmp_path) -> None:
    from opentelemetry import baggage
    from opentelemetry import context as otel_context
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        SpanKind,
        TraceFlags,
        TraceState,
        set_span_in_context,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("tests.fha")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    store = _Store()
    transport = _Transport()
    backend = _backend(tmp_path, store, transport)
    upstream = SpanContext(
        trace_id=int("1" * 32, 16),
        span_id=int("2" * 16, 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState([("vendor", "state")]),
    )
    upstream_token = otel_context.attach(set_span_in_context(NonRecordingSpan(upstream)))
    try:
        with tracer.start_as_current_span("agent.run main") as agent_run:
            baggage_token = otel_context.attach(
                baggage.set_baggage("private", "must-not-propagate")
            )
            try:
                handle = await backend.start_run(StartRunRequest(prompt="Hello"))
                assert observability.current_trace_id() == format(
                    agent_run.get_span_context().trace_id,
                    "032x",
                )
            finally:
                otel_context.detach(baggage_token)
        agent_run_context = agent_run.get_span_context()
    finally:
        otel_context.detach(upstream_token)

    client_spans = [
        span for span in exporter.get_finished_spans() if span.name == "fha.responses.create"
    ]
    assert len(client_spans) == 1
    client_span = client_spans[0]
    assert client_span.kind is SpanKind.CLIENT
    assert client_span.parent is not None
    assert client_span.parent.span_id == agent_run_context.span_id
    assert client_span.context.trace_id == agent_run_context.trace_id
    assert dict(client_span.attributes) == {
        observability.ATTR_FHA_AGENT_NAME: "main",
        observability.ATTR_FHA_RUNTIME_SESSION_ID: handle.session_id,
        observability.ATTR_FHA_RUNTIME_RUN_ID: handle.run_id,
    }
    assert "Hello" not in repr(client_span.attributes)
    assert "owner" not in repr(client_span.attributes)
    assert "private-response" not in repr(client_span.attributes)
    assert transport.create_calls[0].trace_headers == (
        (
            "traceparent",
            "00-"
            + format(client_span.context.trace_id, "032x")
            + "-"
            + format(client_span.context.span_id, "016x")
            + "-01",
        ),
        ("tracestate", "vendor=state"),
    )
    assert "baggage" not in dict(transport.create_calls[0].trace_headers)


@pytest.mark.asyncio
async def test_backend_omits_w3c_headers_without_an_active_worker_provider(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(observability, "_enabled", False)
    store = _Store()
    transport = _Transport()
    backend = _backend(tmp_path, store, transport)

    await backend.start_run(StartRunRequest(prompt="Hello"))

    assert transport.create_calls[0].trace_headers == ()


@pytest.mark.asyncio
async def test_idempotent_retry_resumes_after_admission_before_mapping_reservation(
    tmp_path,
) -> None:
    store = _Store()
    transport = _Transport()
    backend = _backend(tmp_path, store, transport)
    request = StartRunRequest(prompt="Hello", idempotency_key="same")
    attempt = build_idempotency_attempt(
        agent_slug="main",
        prompt=request.prompt,
        timeout=request.timeout,
        idempotency_key=request.idempotency_key,
    )
    assert attempt is not None
    partition = owner_partition(
        resolve_owner_context(_APP, "main", FunctionAppPrincipal())
    )
    admitted = await backend._admit_new_session(
        store,
        partition,
        state_store_fingerprint="s1-" + ("a" * 52),
        session_id="admitted-before-mapping",
        request=request,
        attempt=attempt,
    )
    assert await store.get_provider_run_mapping(
        partition,
        admitted.run.session_id,
        admitted.run.run_id,
    ) is None

    replay = await backend.start_run(request)

    assert replay.session_id == admitted.run.session_id
    assert replay.run_id == admitted.run.run_id
    assert len(transport.create_calls) == 1
    assert store.mappings[replay.run_id].response_state == "bound"


@pytest.mark.asyncio
async def test_backend_projects_polled_terminal_response_without_private_identifiers(
    tmp_path,
) -> None:
    store = _Store()
    transport = _Transport()
    backend = _backend(tmp_path, store, transport)
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))
    transport.response = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.COMPLETED,
        output_text="hello",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )

    events = [
        event
        async for event in backend.read_events(
            RunContext(session_id=handle.session_id, run_id=handle.run_id),
            after_sequence=0,
        )
    ]

    assert [(event.sequence, event.type) for event in events] == [
        (1, "session"),
        (2, "message"),
        (3, "done"),
    ]
    assert "private-response" not in repr(events)
    assert store.mappings[handle.run_id].max_public_event_sequence == 3

    resumed = [
        event
        async for event in backend.read_events(
            RunContext(session_id=handle.session_id, run_id=handle.run_id),
            after_sequence=2,
        )
    ]

    assert [event.sequence for event in resumed] == [3]
    assert transport.retrieve_calls == 2


@pytest.mark.asyncio
async def test_backend_streams_and_resumes_provider_text_deltas(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    in_progress = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.IN_PROGRESS,
        output_text="",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    completed = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.COMPLETED,
        output_text="haha",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    stream_events = [
        FoundryResponseEvent.create(
            provider_sequence=0,
            kind=FoundryResponseEventKind.CREATED,
            data=transport.response,
        ),
        FoundryResponseEvent.create(
            provider_sequence=1,
            kind=FoundryResponseEventKind.IN_PROGRESS,
            data=in_progress,
        ),
        FoundryResponseEvent.create(
            provider_sequence=2,
            kind=FoundryResponseEventKind.TEXT_DELTA,
            data=FoundryResponseText.create("ha"),
        ),
        FoundryResponseEvent.create(
            provider_sequence=3,
            kind=FoundryResponseEventKind.TEXT_DELTA,
            data=FoundryResponseText.create("ha"),
        ),
        FoundryResponseEvent.create(
            provider_sequence=4,
            kind=FoundryResponseEventKind.TEXT_DONE,
            data=FoundryResponseText.create("haha"),
        ),
        FoundryResponseEvent.create(
            provider_sequence=5,
            kind=FoundryResponseEventKind.COMPLETED,
            data=completed,
        ),
    ]
    transport.live_events = stream_events
    backend = _backend(tmp_path, store, transport, stream_events=True)
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))

    events = [
        event
        async for event in backend.read_events(
            RunContext(session_id=handle.session_id, run_id=handle.run_id),
            after_sequence=0,
        )
    ]

    assert [(event.sequence, event.type, event.data) for event in events] == [
        (
            1,
            "session",
            {"session_id": handle.session_id, "status": "accepted"},
        ),
        (
            2,
            "session",
            {"session_id": handle.session_id, "status": "running"},
        ),
        (3, "delta", {"content": "ha"}),
        (4, "delta", {"content": "ha"}),
        (5, "message", {"content": "haha"}),
        (6, "done", {"content": "haha"}),
    ]
    assert len(transport.create_stream_calls) == 1
    assert transport.create_calls == []
    assert transport.replay_calls == []
    assert transport.retrieve_calls == 0
    assert transport.live_streams[0].close_calls == 1
    assert store.mappings[handle.run_id].max_public_event_sequence == 6
    assert store.watermark_sequences == [1, 2, 5, 6]

    transport.replay_events = stream_events
    resumed = [
        event
        async for event in backend.read_events(
            RunContext(session_id=handle.session_id, run_id=handle.run_id),
            after_sequence=3,
        )
    ]

    assert [(event.sequence, event.type, event.data) for event in resumed] == [
        (4, "delta", {"content": "ha"}),
        (5, "message", {"content": "haha"}),
        (6, "done", {"content": "haha"}),
    ]
    assert transport.replay_calls[-1] == ("private-response", 2)


@pytest.mark.asyncio
async def test_backend_finishes_with_polling_after_partial_provider_replay(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    backend = _backend(tmp_path, store, transport)
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))
    in_progress = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.IN_PROGRESS,
        output_text="",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    completed = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.COMPLETED,
        output_text="hello",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    transport.replay_events = [
        FoundryResponseEvent.create(
            provider_sequence=0,
            kind=FoundryResponseEventKind.CREATED,
            data=transport.response,
        ),
        FoundryResponseEvent.create(
            provider_sequence=1,
            kind=FoundryResponseEventKind.IN_PROGRESS,
            data=in_progress,
        ),
        FoundryResponseEvent.create(
            provider_sequence=2,
            kind=FoundryResponseEventKind.TEXT_DELTA,
            data=FoundryResponseText.create("hel"),
        ),
    ]
    transport.retrieve_responses = [completed]

    events = [
        event
        async for event in backend.read_events(
            RunContext(session_id=handle.session_id, run_id=handle.run_id),
            after_sequence=0,
        )
    ]

    assert [(event.sequence, event.type, event.data) for event in events] == [
        (
            1,
            "session",
            {"session_id": handle.session_id, "status": "accepted"},
        ),
        (
            2,
            "session",
            {"session_id": handle.session_id, "status": "running"},
        ),
        (3, "delta", {"content": "hel"}),
        (4, "message", {"content": "hello"}),
        (5, "done", {"content": "hello"}),
    ]
    assert store.mappings[handle.run_id].max_public_event_sequence == 5

    transport.replay_error = FoundryResponsesOperationError(
        FoundryResponsesOperation.REPLAY,
        FoundryResponsesOperationErrorKind.INVALID_REQUEST,
        retryable=False,
    )
    transport.retrieve_responses = [completed]
    resumed = [
        event
        async for event in backend.read_events(
            RunContext(session_id=handle.session_id, run_id=handle.run_id),
            after_sequence=3,
        )
    ]

    assert [(event.sequence, event.type, event.data) for event in resumed] == [
        (4, "message", {"content": "hello"}),
        (5, "done", {"content": "hello"}),
    ]


@pytest.mark.asyncio
async def test_backend_streams_output_validation_failure_as_sanitized_error(
    tmp_path,
) -> None:
    store = _Store()
    transport = _Transport()
    runtime = _runtime(tmp_path, store, transport)
    owner = resolve_owner_context(_APP, "main", FunctionAppPrincipal())
    backend = FoundryResponsesExecutionBackend(
        AgentBinding(
            agent_name="main",
            output_validator=lambda _result: RunError(
                code="invalid_argument",
                message="The response did not match the configured schema.",
                fault_domain="app",
            ),
        ),
        runtime=runtime,
        owner=owner,
        sleep=_no_sleep,
        clock=lambda: 0.0,
    )
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))
    completed = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.COMPLETED,
        output_text='{"unexpected":true}',
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    transport.replay_events = [
        FoundryResponseEvent.create(
            provider_sequence=0,
            kind=FoundryResponseEventKind.CREATED,
            data=transport.response,
        ),
        FoundryResponseEvent.create(
            provider_sequence=1,
            kind=FoundryResponseEventKind.COMPLETED,
            data=completed,
        ),
    ]

    events = [
        event
        async for event in backend.read_events(
            RunContext(session_id=handle.session_id, run_id=handle.run_id),
            after_sequence=0,
        )
    ]

    assert [(event.sequence, event.type, event.data) for event in events] == [
        (
            1,
            "session",
            {"session_id": handle.session_id, "status": "accepted"},
        ),
        (
            2,
            "error",
            {
                "code": "invalid_argument",
                "content": "The response did not match the configured schema.",
            },
        ),
    ]
    assert store.runs[handle.run_id].status == "failed"


@pytest.mark.asyncio
async def test_backend_polls_until_a_stored_response_is_terminal(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    backend = _backend(tmp_path, store, transport)
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))
    in_progress = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.IN_PROGRESS,
        output_text="",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    completed = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.COMPLETED,
        output_text="hello",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    transport.retrieve_responses = [transport.response, in_progress, completed]

    events = [
        event
        async for event in backend.read_events(
            RunContext(session_id=handle.session_id, run_id=handle.run_id),
            after_sequence=0,
        )
    ]

    assert [(event.sequence, event.type) for event in events] == [
        (1, "session"),
        (2, "message"),
        (3, "done"),
    ]
    assert transport.retrieve_calls == 3


@pytest.mark.asyncio
async def test_backend_projects_terminal_provider_failure_as_a_sanitized_event(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    backend = _backend(tmp_path, store, transport)
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))
    transport.response = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.FAILED,
        output_text="",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )

    events = [
        event
        async for event in backend.read_events(
            RunContext(session_id=handle.session_id, run_id=handle.run_id),
            after_sequence=0,
        )
    ]

    assert [(event.sequence, event.type, event.data) for event in events] == [
        (1, "session", {"status": "failed"}),
        (2, "error", {"code": "provider_failed", "content": "Hosted response failed."}),
    ]


@pytest.mark.asyncio
async def test_ambiguous_create_quarantines_without_a_second_submission(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    transport.create_error = FoundryResponsesOperationError(
        FoundryResponsesOperation.CREATE,
        FoundryResponsesOperationErrorKind.TRANSIENT,
        retryable=True,
    )
    backend = _backend(tmp_path, store, transport)

    with pytest.raises(FoundryResponsesBackendError):
        await backend.start_run(StartRunRequest(prompt="Hello", idempotency_key="same"))

    assert store.session is not None
    assert store.session.status == "quarantined"
    assert next(iter(store.mappings.values())).indeterminate_reason == "provider_submission_indeterminate"
    assert len(transport.create_calls) == 1


@pytest.mark.asyncio
async def test_definitive_create_rejection_releases_slot_without_quarantine(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    transport.create_error = FoundryResponsesOperationError(
        FoundryResponsesOperation.CREATE,
        FoundryResponsesOperationErrorKind.INVALID_REQUEST,
        retryable=False,
    )
    backend = _backend(tmp_path, store, transport)

    handle = await backend.start_run(
        StartRunRequest(prompt="Hello", idempotency_key="same")
    )
    status = await backend.get_run(
        RunContext(session_id=handle.session_id, run_id=handle.run_id)
    )

    assert handle.state == "failed"
    assert status.state == "failed"
    assert status.error is not None
    assert status.error.code == "provider_request_rejected"
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.quarantine_reason is None
    assert store.mappings[handle.run_id].response_state == "terminal"
    assert store.mappings[handle.run_id].provider_response_id is None
    assert len(transport.create_calls) == 1


@pytest.mark.asyncio
async def test_failed_provider_session_creation_is_revalidated_by_next_run(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    transport.create_session_error = FoundryResponsesOperationError(
        FoundryResponsesOperation.CREATE_SESSION,
        FoundryResponsesOperationErrorKind.INVALID_REQUEST,
        retryable=False,
    )
    backend = _backend(tmp_path, store, transport)

    with pytest.raises(FoundryResponsesBackendError):
        await backend.start_run(StartRunRequest(prompt="First"))

    assert store.session is not None
    session_id = store.session.session_id
    assert store.session.status == "ready"
    assert store.session_bindings[session_id].provider_session_id
    transport.create_session_error = None

    second = await backend.start_run(
        StartRunRequest(prompt="Second", session_id=session_id)
    )

    assert second.session_id == session_id
    assert len(transport.create_session_calls) == 2
    assert len(transport.create_calls) == 1


@pytest.mark.asyncio
async def test_pending_idempotent_replay_does_not_quarantine_inflight_submission(
    tmp_path,
) -> None:
    store = _Store()
    transport = _Transport()
    backend = _backend(tmp_path, store, transport)
    handle = await backend.start_run(StartRunRequest(prompt="Hello", idempotency_key="same"))
    mapping = store.mappings[handle.run_id]
    store.mappings[handle.run_id] = replace(
        mapping,
        response_state="pending",
        provider_response_id=None,
    )

    replay = await backend.start_run(StartRunRequest(prompt="Hello", idempotency_key="same"))
    status = await backend.get_run(
        RunContext(session_id=handle.session_id, run_id=handle.run_id)
    )

    assert replay == handle
    assert status.state == "accepted"
    assert store.session is not None
    assert store.session.status == "running"
    assert "indeterminate" not in store.calls
    assert len(transport.create_calls) == 1


@pytest.mark.asyncio
async def test_stale_pending_submission_releases_without_quarantine(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    backend = _backend(tmp_path, store, transport)
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))
    mapping = store.mappings[handle.run_id]
    store.mappings[handle.run_id] = replace(
        mapping,
        response_state="pending",
        provider_response_id=None,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    status = await backend.get_run(
        RunContext(session_id=handle.session_id, run_id=handle.run_id)
    )

    assert status.state == "timed_out"
    assert status.error is not None
    assert status.error.code == "provider_submission_not_started"
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.quarantine_reason is None


@pytest.mark.asyncio
async def test_stale_submission_issued_is_quarantined_opportunistically(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    backend = _backend(tmp_path, store, transport)
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))
    mapping = store.mappings[handle.run_id]
    store.mappings[handle.run_id] = replace(
        mapping,
        response_state="submitting",
        provider_response_id=None,
        updated_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    status = await backend.get_run(
        RunContext(session_id=handle.session_id, run_id=handle.run_id)
    )

    assert status.state == "abandoned"
    assert status.error is not None
    assert status.error.code == "provider_submission_indeterminate"
    assert store.session is not None
    assert store.session.status == "quarantined"


@pytest.mark.asyncio
async def test_cancel_quarantines_when_provider_termination_cannot_be_proven(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    transport.response = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.IN_PROGRESS,
        output_text="",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    backend = _backend(tmp_path, store, transport)
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))

    status = await backend.cancel_run(RunContext(session_id=handle.session_id, run_id=handle.run_id))

    assert status.state == "abandoned"
    assert status.error is not None
    assert status.error.code == "provider_termination_indeterminate"
    assert store.session is not None
    assert store.session.status == "quarantined"


@pytest.mark.asyncio
async def test_cancel_returns_terminal_response_without_provider_cancel(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    transport.response = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.COMPLETED,
        output_text="done",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    backend = _backend(tmp_path, store, transport)
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))

    status = await backend.cancel_run(
        RunContext(session_id=handle.session_id, run_id=handle.run_id)
    )

    assert status.state == "succeeded"
    assert transport.cancel_calls == 0


@pytest.mark.asyncio
async def test_cancel_short_poll_adopts_delayed_terminal_cancellation(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    in_progress = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.IN_PROGRESS,
        output_text="",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    canceled = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.CANCELLED,
        output_text="",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    transport.response = in_progress
    transport.cancel_response = in_progress
    transport.retrieve_responses = [in_progress, canceled]
    backend = _backend(tmp_path, store, transport)
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))

    status = await backend.cancel_run(
        RunContext(session_id=handle.session_id, run_id=handle.run_id)
    )

    assert status.state == "canceled"
    assert transport.cancel_calls == 1
    assert store.session is not None
    assert store.session.status == "ready"


@pytest.mark.asyncio
async def test_expired_run_cancels_and_releases_active_slot(tmp_path) -> None:
    store = _Store()
    transport = _Transport()
    transport.response = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.IN_PROGRESS,
        output_text="",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    transport.cancel_response = FoundryResponse.create(
        response_id="private-response",
        status=FoundryResponseStatus.CANCELLED,
        output_text="",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    backend = _backend(tmp_path, store, transport)
    handle = await backend.start_run(StartRunRequest(prompt="Hello"))
    store.runs[handle.run_id] = replace(
        store.runs[handle.run_id],
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    status = await backend.get_run(
        RunContext(session_id=handle.session_id, run_id=handle.run_id)
    )

    assert status.state == "canceled"
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.active_run_id is None
