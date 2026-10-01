from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from azure_functions_agents.config.schema import EndpointAuthConfig, EntraAuthConfig
from azure_functions_agents.experimental import durable_loop_http
from azure_functions_agents.experimental.durable_chat_journal import DurableChatJournal
from azure_functions_agents.experimental.durable_chat_protocol import (
    DurableChatAssistantTextObservationV1,
    DurableChatObservationBatchV1,
    DurableChatRunInitializationV1,
)
from azure_functions_agents.experimental.durable_loop_activities import (
    InMemoryDurableContentStore,
)
from azure_functions_agents.experimental.durable_loop_config import DurableLoopSettings
from azure_functions_agents.experimental.durable_loop_http import (
    register_durable_loop_http_routes,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    DURABLE_LOOP_ORCHESTRATOR_V4_NAME,
    ContentRefV1,
    DurableAssistantDeltaPayloadV1,
    DurableChatModelMode,
    DurableChatRunOptionsV1,
    DurableLoopRunStatus,
    DurableModelProgressPayloadV1,
    DurablePublicEventType,
    DurableRunStartedPayloadV1,
)
from azure_functions_agents.experimental.durable_loop_receipts import (
    InMemoryDurableKeyedDocumentStore,
)
from azure_functions_agents.experimental.durable_run_observations import (
    DurableRunObservationInitializationV2,
    DurableRunObservationJournal,
    DurableRunReplayDisposition,
    initialize_durable_run_observations,
    reconcile_durable_run_terminal,
    render_public_sse_frame,
    replay_durable_chat_adapter,
    replay_durable_run_observations,
)

_NOW = datetime(2026, 9, 30, tzinfo=UTC)
_HASH = "a" * 64


def _journal() -> DurableChatJournal:
    return DurableChatJournal(
        content=InMemoryDurableContentStore(),
        documents=InMemoryDurableKeyedDocumentStore(),
    )


def _initialization() -> DurableRunObservationInitializationV2:
    return DurableRunObservationInitializationV2(
        run_id="run-1",
        session_id="session-1",
        owner_hash=_HASH,
        created_at=_NOW,
        events_expires_at=_NOW + timedelta(days=30),
    )


def _reference(name: str) -> ContentRefV1:
    return ContentRefV1(
        object_id=name,
        sha256=_HASH,
        byte_length=0,
        media_type="application/json",
        encryption_version="v1",
        retention_class="run",
    )


@pytest.mark.asyncio
async def test_v2_journal_assigns_contiguous_sequences_and_canonical_sse() -> None:
    journal = _journal()
    assert isinstance(journal, DurableRunObservationJournal)
    winner, created = await journal.create_observation_initialization_once(
        initialization=_initialization()
    )
    assert created is True
    assert winner.frame_schema_version == "2"

    first = await journal.publish_public_event(
        run_id="run-1",
        event_type=DurablePublicEventType.RUN_STARTED,
        payload=DurableRunStartedPayloadV1(
            session_id="session-1",
            status=DurableLoopRunStatus.PENDING,
        ),
        timestamp=_NOW,
    )
    second = await journal.publish_public_event(
        run_id="run-1",
        event_type=DurablePublicEventType.ASSISTANT_DELTA,
        payload=DurableAssistantDeltaPayloadV1(text="hello"),
        timestamp=_NOW + timedelta(seconds=1),
    )

    assert first is not None and first.sequence == 1
    assert second is not None and second.sequence == 2
    page = await journal.replay_public_events(run_id="run-1", after_sequence=0)
    assert [event.sequence for event in page.events] == [1, 2]
    assert [event.type for event in page.events] == [
        DurablePublicEventType.RUN_STARTED,
        DurablePublicEventType.ASSISTANT_DELTA,
    ]
    rendered = render_public_sse_frame(second)
    assert rendered.startswith("id: 2\nevent: assistant_delta\ndata: ")
    data = json.loads(rendered.split("data: ", 1)[1])
    assert data["schema_version"] == "2"
    assert data["payload"] == {"schema_version": "1", "text": "hello"}
    assert "secret" not in rendered.casefold()


@pytest.mark.asyncio
async def test_v2_journal_compacts_to_snapshot_without_sequence_gaps() -> None:
    journal = _journal()
    await journal.create_observation_initialization_once(
        initialization=_initialization()
    )
    for step in range(129):
        await journal.publish_public_event(
            run_id="run-1",
            event_type=DurablePublicEventType.MODEL_PROGRESS,
            payload=DurableModelProgressPayloadV1(
                step_index=step,
                phase="model_progress",
            ),
            timestamp=_NOW + timedelta(seconds=step),
        )

    page = await journal.replay_public_events(run_id="run-1", after_sequence=0)
    assert page.disposition is DurableRunReplayDisposition.SNAPSHOT_REQUIRED
    assert page.snapshot is not None
    assert page.snapshot.sequence == 129
    assert page.snapshot.projection.model_step == 128
    assert page.events == ()

    cursor_ahead = await journal.replay_public_events(
        run_id="run-1",
        after_sequence=130,
    )
    assert cursor_ahead.disposition is DurableRunReplayDisposition.CURSOR_AHEAD
    assert cursor_ahead.through_sequence == 129


@pytest.mark.asyncio
async def test_terminal_reconciliation_is_idempotent() -> None:
    journal = _journal()
    await journal.create_observation_initialization_once(
        initialization=_initialization()
    )

    for _ in range(2):
        await reconcile_durable_run_terminal(
            run_id="run-1",
            status=DurableLoopRunStatus.COMPLETED,
            result_available=True,
            error_code=None,
            journal=journal,
        )

    page = await journal.replay_public_events(run_id="run-1", after_sequence=0)
    assert [event.type for event in page.events] == [
        DurablePublicEventType.RUN_COMPLETED
    ]


@pytest.mark.asyncio
async def test_generic_route_projects_retained_v1_events_to_v2() -> None:
    journal = _journal()
    legacy = DurableChatRunInitializationV1(
        run_id="run-1",
        session_id="session-1",
        owner_hash=_HASH,
        request_id_hash=_HASH,
        request_hash=_HASH,
        plan_ref=_reference("plan"),
        input_ref=_reference("input"),
        expires_at=_NOW + timedelta(days=1),
        committed_generation=0,
        ui=DurableChatRunOptionsV1(
            stream_response=True,
            model_mode=DurableChatModelMode.FOREGROUND,
        ),
        created_at=_NOW,
    )
    await journal.create_run_initialization_once(initialization=legacy)
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    producers = await journal.reserve_model_producers(
        run_id="run-1",
        step_index=0,
        count=1,
        deadline=deadline,
    )
    observation = DurableChatAssistantTextObservationV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=_NOW,
        producer=producers[0],
        delta="legacy",
    )
    await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(observation,),
        ),
        deadline=deadline,
    )

    page = await replay_durable_run_observations(
        run_id="run-1",
        after_sequence=0,
        journal=journal,
    )
    assert page.events[0].schema_version == "2"
    assert page.events[0].type is DurablePublicEventType.ASSISTANT_DELTA
    assert page.events[0].payload == DurableAssistantDeltaPayloadV1(text="legacy")


@pytest.mark.asyncio
async def test_hosted_route_projects_v2_events_to_strict_v1_frames() -> None:
    journal = _journal()
    await journal.create_observation_initialization_once(
        initialization=_initialization()
    )
    await journal.publish_public_event(
        run_id="run-1",
        event_type=DurablePublicEventType.ASSISTANT_DELTA,
        payload=DurableAssistantDeltaPayloadV1(text="generic"),
        timestamp=_NOW,
    )

    page = await replay_durable_chat_adapter(
        run_id="run-1",
        after_sequence=0,
        journal=journal,
    )
    assert page.schema_version == "1"
    assert page.events[0].schema_version == "1"
    assert page.events[0].event.event_type.value == "assistant_text"


@pytest.mark.asyncio
async def test_v4_initialization_is_independent_of_hosted_chat_options() -> None:
    journal = _journal()
    durable_input = SimpleNamespace(
        identity=SimpleNamespace(
            orchestration_version=DURABLE_LOOP_ORCHESTRATOR_V4_NAME,
            run_id="run-1",
            session_id="session-1",
            owner_hash=_HASH,
            created_at=_NOW,
            absolute_deadline=_NOW + timedelta(hours=1),
            retention_policy=SimpleNamespace(event_result_seconds=86400),
        )
    )

    initialized = await initialize_durable_run_observations(
        durable_input,
        journal=journal,
    )

    assert initialized is not None
    assert initialized.events_expires_at == _NOW + timedelta(hours=25)
    page = await journal.replay_public_events(run_id="run-1", after_sequence=0)
    assert [event.type for event in page.events] == [
        DurablePublicEventType.RUN_STARTED
    ]


class _App:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.routes: dict[str, dict[str, object]] = {}

    def durable_client_input(self, *, client_name: str) -> Any:
        assert client_name == "client"
        return lambda handler: handler

    def route(self, **kwargs: object) -> Any:
        def decorator(handler: Any) -> Any:
            self.handlers[handler.__name__] = handler
            self.routes[handler.__name__] = kwargs
            return handler

        return decorator


class _Request:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.path_params = {"run_id": "run-1"}
        self.query_params: dict[str, str] = {}


class _Client:
    def __init__(self) -> None:
        self.status_calls = 0

    async def get_status(self, *_args: object, **_kwargs: object) -> None:
        self.status_calls += 1
        return None


@pytest.mark.asyncio
async def test_generic_events_route_is_ungated_and_authenticates_before_data() -> None:
    app = _App()
    resolved = SimpleNamespace(
        builtin_endpoints=SimpleNamespace(
            http_auth=EndpointAuthConfig(mode="entra", entra=EntraAuthConfig())
        ),
        enabled_mcp_names=frozenset(),
        name="Main",
        slug="main",
        tools_disabled=False,
    )
    register_durable_loop_http_routes(
        app,  # type: ignore[arg-type]
        resolved=resolved,
        settings=DurableLoopSettings(),
        chat_settings=None,
    )

    assert app.routes["durable_agent_run_events_v2"]["route"] == (
        "experimental/durable-agent-runs/{run_id}/events"
    )
    client = _Client()
    response = await app.handlers["durable_agent_run_events_v2"](
        _Request(),
        client,
    )
    body = json.loads(response.body)
    assert response.status_code == 401
    assert body["schema_version"] == "1"
    assert body["code"] == "request_failed"
    assert body["status"] == 401
    assert client.status_calls == 0


def test_authorized_status_dispatches_schema_v2_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = object()

    class _V2Parser:
        @classmethod
        def model_validate_json(cls, payload: bytes) -> object:
            assert json.loads(payload)["schema_version"] == "2"
            return marker

    monkeypatch.setattr(durable_loop_http, "DurableOrchestrationInputV2", _V2Parser)

    assert (
        durable_loop_http._durable_status_input({"schema_version": "2"}) is marker
    )
