from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import azure.durable_functions as df
import pytest
from azure.functions.timer import TimerRequest

from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    ToolsFilter,
    TriggerSpec,
)
from azure_functions_agents.experimental import durable_loop_http
from azure_functions_agents.experimental.durable_loop_activities import (
    InMemoryDurableContentStore,
)
from azure_functions_agents.experimental.durable_loop_config import DurableLoopSettings
from azure_functions_agents.experimental.durable_loop_http import (
    create_durable_trigger_admission_callback,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    DurableTriggerAdmissionRecordV1,
    DurableTriggerAdmissionState,
    canonical_hash,
)
from azure_functions_agents.experimental.durable_loop_receipts import (
    InMemoryDurableKeyedDocumentStore,
)
from azure_functions_agents.experimental.durable_loop_registration import (
    DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
    DURABLE_LOOP_ORCHESTRATOR_V4_NAME,
)
from azure_functions_agents.experimental.durable_trigger_admission import (
    DURABLE_TRIGGER_ATTEMPT_ACTIVITY_NAME,
    DURABLE_TRIGGER_OUTBOX_ORCHESTRATOR_NAME,
    DURABLE_TRIGGER_SWEEPER_FUNCTION_NAME,
    DurableTriggerAdmissionAttemptV1,
    DurableTriggerAdmissionConflictError,
    DurableTriggerAdmissionRuntime,
    DurableTriggerAttemptOutcome,
    DurableTriggerPendingLedger,
    DurableTriggerRegistration,
    DurableTriggerStageResult,
    extract_bounded_json_path,
    make_durable_binding_handler,
    make_durable_http_handler,
    register_durable_trigger_admission_runtime,
    validate_durable_trigger_registration,
)


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class _Client:
    def __init__(self) -> None:
        self.statuses: dict[str, object] = {}
        self.starts: list[tuple[str, str, object]] = []

    async def get_status(self, instance_id: str) -> object | None:
        return self.statuses.get(instance_id)

    async def start_new(
        self,
        name: str,
        *,
        instance_id: str,
        client_input: object,
    ) -> str:
        self.starts.append((name, instance_id, client_input))
        self.statuses[instance_id] = object()
        return instance_id


class _SharedAdmissionClient(_Client):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events
        self.lose_v4_start_ack = False

    async def start_new(
        self,
        name: str,
        *,
        instance_id: str,
        client_input: object,
    ) -> str:
        self.starts.append((name, instance_id, client_input))
        if name == DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME:
            assert isinstance(client_input, dict)
            self.statuses[instance_id] = SimpleNamespace(
                output={
                    "committed_generation": 0,
                    "disposition": "admitted",
                    "run_id": client_input["run_id"],
                },
                runtime_status=SimpleNamespace(name="Completed"),
            )
            return instance_id
        if name == DURABLE_LOOP_ORCHESTRATOR_V4_NAME:
            self.events.append("start")
        self.statuses[instance_id] = SimpleNamespace(
            output=None,
            runtime_status=SimpleNamespace(name="Pending"),
        )
        if name == DURABLE_LOOP_ORCHESTRATOR_V4_NAME and self.lose_v4_start_ack:
            raise RuntimeError("injected committed start acknowledgement loss")
        return instance_id


class _Request:
    def __init__(
        self,
        body: object,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self.headers = headers or {}

    async def json(self) -> object:
        return self._body


class _Callback:
    def __init__(self, *outcomes: DurableTriggerAttemptOutcome) -> None:
        self.outcomes = list(outcomes)
        self.records: list[object] = []

    async def __call__(
        self,
        record: DurableTriggerAdmissionRecordV1,
        client: df.DurableOrchestrationClient,
    ) -> DurableTriggerAdmissionAttemptV1:
        del client
        self.records.append(record)
        outcome = self.outcomes.pop(0)
        return DurableTriggerAdmissionAttemptV1(outcome=outcome)


def _runtime(
    *,
    callback: Any = None,
    deadline_seconds: int = 300,
) -> tuple[DurableTriggerAdmissionRuntime, _Clock]:
    clock = _Clock()
    runtime = DurableTriggerAdmissionRuntime(
        DurableTriggerPendingLedger(InMemoryDurableKeyedDocumentStore()),
        admission_deadline_seconds=deadline_seconds,
        content=InMemoryDurableContentStore(),
        callback=callback,
        app_identity=lambda: "app:test",
        now=clock,
    )
    return runtime, clock


def _registration(
    trigger_type: str = "connector_trigger",
) -> DurableTriggerRegistration:
    return DurableTriggerRegistration(
        trigger_type=trigger_type,  # type: ignore[arg-type]
        registration_id="1" * 64,
        agent_slug="daily-report",
        owner={"kind": "app"} if trigger_type != "http_trigger" else None,
        event_id_path="$.event.id" if trigger_type == "connector_trigger" else None,
        session_id_path=None,
        connection_hash="2" * 64 if trigger_type == "connector_trigger" else None,
        allow_human_input=False,
        response_schema_hash=None,
        auth_mode="function",
        ownership_class="function_app",
    )


def _resolved_http_agent() -> ResolvedAgent:
    return ResolvedAgent(
        name="Daily Report",
        description="desc",
        trigger=TriggerSpec(
            type="http_trigger",
            args={"route": "reports", "http_auth": "function"},
        ),
        instructions="run",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model=None,
        timeout=1,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=__file__,
    )


def _registered_functions(app: df.DFApp) -> dict[str, list[str]]:
    functions: dict[str, list[str]] = {}
    for builder in app._function_builders:
        function = builder._function
        functions[function._name] = [
            binding.get_dict_repr()["type"] for binding in function._bindings
        ]
    return functions


async def _stage(
    runtime: DurableTriggerAdmissionRuntime,
    *,
    event_id: str = "evt-1",
    payload: dict[str, object] | None = None,
) -> DurableTriggerStageResult:
    return await runtime.stage(
        registration=_registration(),
        owner_hash="3" * 64,
        initiator_hash="4" * 64,
        stable_event_id=event_id,
        session_id="session-1",
        payload=payload or {"event": {"id": event_id}},
        prompt=f"event {event_id}",
    )


@pytest.mark.asyncio
async def test_pending_ledger_create_once_dedupes_concurrent_staging() -> None:
    runtime, _clock = _runtime()

    first, second = await asyncio.gather(_stage(runtime), _stage(runtime))

    assert first.record == second.record
    assert sorted((first.created, second.created)) == [False, True]
    assert len(await runtime.ledger.pending()) == 1
    record = first.record
    assert record is not None
    assert second.record is not None
    assert record.run_id == second.record.run_id
    assert record.record_key == second.record.record_key


@pytest.mark.asyncio
async def test_pending_ledger_rejects_same_event_with_different_body() -> None:
    runtime, _clock = _runtime()
    await _stage(runtime)

    with pytest.raises(DurableTriggerAdmissionConflictError):
        await _stage(runtime, payload={"event": {"id": "evt-1"}, "changed": True})


@pytest.mark.asyncio
async def test_pending_ledger_rolls_over_and_walks_one_shard_chain() -> None:
    runtime, _clock = _runtime()
    selected: list[str] = []
    target_shard: int | None = None
    for index in range(10000):
        event_id = f"evt-{index}"
        event_hash = canonical_hash({"event_id": event_id})
        dedupe_key = canonical_hash(
            {
                "kind": "trigger_admission_identity",
                "owner_hash": "3" * 64,
                "schema_version": "1",
                "stable_event_id_hash": event_hash,
                "trigger_registration": "1" * 64,
            }
        )
        shard = int(dedupe_key[:8], 16) % 16
        if target_shard is None:
            target_shard = shard
        if shard == target_shard:
            selected.append(event_id)
        if len(selected) == 129:
            break

    for event_id in selected:
        await _stage(runtime, event_id=event_id)

    assert len(selected) == 129
    assert len(await runtime.ledger.pending()) == 129
    replay = await _stage(runtime, event_id=selected[0])
    assert replay.run_id
    assert replay.created is False


@pytest.mark.asyncio
async def test_outbox_attempt_retries_then_retains_admitted_disposition() -> None:
    callback = _Callback(
        DurableTriggerAttemptOutcome.RETRY,
        DurableTriggerAttemptOutcome.ADMITTED,
    )
    runtime, _clock = _runtime(callback=callback)
    staged = await _stage(runtime)
    record = staged.record
    assert record is not None

    client = _Client()
    first = await runtime.attempt(record, client)  # type: ignore[arg-type]
    second = await runtime.attempt(record, client)  # type: ignore[arg-type]
    replay = await runtime.attempt(record, client)  # type: ignore[arg-type]

    assert first.outcome is DurableTriggerAttemptOutcome.RETRY
    assert second.outcome is DurableTriggerAttemptOutcome.ADMITTED
    assert replay.outcome is DurableTriggerAttemptOutcome.ADMITTED
    assert len(callback.records) == 2
    receipt = await runtime.ledger.disposition(record)
    assert receipt is not None
    assert receipt.record.state is DurableTriggerAdmissionState.ADMITTED
    assert receipt.expires_at == record.staged_at + timedelta(days=90)
    assert await runtime.ledger.compact_terminal() == 1
    assert await runtime.ledger.pending() == ()
    compacted_replay = await _stage(runtime)
    assert compacted_replay.record is None
    assert compacted_replay.run_id == record.run_id
    assert compacted_replay.session_id == record.session_id
    with pytest.raises(DurableTriggerAdmissionConflictError):
        await _stage(runtime, payload={"event": {"id": "evt-1"}, "changed": True})


def test_runtime_registers_outbox_activity_and_monitored_sweeper() -> None:
    app = df.DFApp()

    register_durable_trigger_admission_runtime(
        app,
        admission_deadline_seconds=300,
        documents=InMemoryDurableKeyedDocumentStore(),
        content=InMemoryDurableContentStore(),
    )

    functions = _registered_functions(app)
    assert functions[DURABLE_TRIGGER_OUTBOX_ORCHESTRATOR_NAME] == [
        "orchestrationTrigger"
    ]
    assert functions[DURABLE_TRIGGER_ATTEMPT_ACTIVITY_NAME] == [
        "activityTrigger",
        "durableClient",
    ]
    assert functions[DURABLE_TRIGGER_SWEEPER_FUNCTION_NAME] == [
        "durableClient",
        "timerTrigger",
    ]


def test_runtime_registration_injects_production_shared_admission_callback() -> None:
    runtime = register_durable_trigger_admission_runtime(
        df.DFApp(),
        admission_deadline_seconds=300,
        documents=InMemoryDurableKeyedDocumentStore(),
        content=InMemoryDurableContentStore(),
        settings=DurableLoopSettings(),
        resolved_agents={"daily-report": _resolved_http_agent()},
    )

    assert runtime.callback.__name__ == "admit"


@pytest.mark.asyncio
async def test_sweeper_expires_deadline_without_calling_admission() -> None:
    callback = _Callback(DurableTriggerAttemptOutcome.ADMITTED)
    runtime, clock = _runtime(callback=callback, deadline_seconds=10)
    staged = await _stage(runtime)
    record = staged.record
    assert record is not None
    clock.value += timedelta(seconds=10)

    recovered = await runtime.sweep(_Client())

    assert recovered == 0
    assert callback.records == []
    receipt = await runtime.ledger.disposition(record)
    assert receipt is not None
    assert receipt.record.state is DurableTriggerAdmissionState.EXPIRED


@pytest.mark.asyncio
async def test_sweeper_recovers_host_loss_after_staging() -> None:
    runtime, _clock = _runtime()
    staged = await _stage(runtime)
    record = staged.record
    assert record is not None
    client = _Client()

    assert await runtime.sweep(client) == 1
    assert await runtime.sweep(client) == 1
    assert len(client.starts) == 1
    assert client.starts[0][1].startswith("dta-")
    assert client.starts == [
        (
            DURABLE_TRIGGER_OUTBOX_ORCHESTRATOR_NAME,
            client.starts[0][1],
            record.model_dump(mode="json"),
        )
    ]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$.event.id", "evt-1"),
        ("$.items[1].partition", 8),
        ("$.enabled", False),
    ],
)
def test_connector_paths_extract_one_bounded_scalar(
    path: str,
    expected: object,
) -> None:
    payload = {
        "event": {"id": "evt-1"},
        "items": [{"partition": 7}, {"partition": 8}],
        "enabled": False,
    }

    assert extract_bounded_json_path(payload, path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "$.missing",
        "$.event",
        "$.items[*]",
        "$..event",
        "$",
    ],
)
def test_connector_paths_fail_closed(path: str) -> None:
    with pytest.raises(ValueError):
        extract_bounded_json_path(
            {"event": {"id": "evt-1"}, "items": []},
            path,
        )


@pytest.mark.asyncio
async def test_monitored_timer_next_is_normalized_and_deduped() -> None:
    runtime, _clock = _runtime()
    registration = _registration("timer_trigger")
    handler = make_durable_binding_handler(runtime, registration)
    client = _Client()
    timer = TimerRequest(
        past_due=True,
        schedule_status={
            "last": "2026-09-29T11:59:00Z",
            "next": "2026-09-29T12:00:00-07:00",
        },
    )

    await handler(timer, client)
    await handler(timer, client)

    [record] = await runtime.ledger.pending()
    assert record.stable_event_id_hash
    assert len(client.starts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_type", ["timer_trigger", "connector_trigger"])
async def test_staged_trigger_uses_shared_admission_once_and_starts_after_observation(
    trigger_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    durable_input = SimpleNamespace(
        identity=SimpleNamespace(
            orchestration_version=DURABLE_LOOP_ORCHESTRATOR_V4_NAME
        ),
        model_dump=lambda **_kwargs: {"schema_version": "2"},
    )

    async def trigger_metadata(**_kwargs: object) -> object:
        return object()

    async def persist_trigger_input(
        _metadata: object,
        **_kwargs: object,
    ) -> object:
        return durable_input

    async def initialize_observations(_durable_input: object) -> None:
        events.append("observations")

    monkeypatch.setattr(
        durable_loop_http,
        "_trigger_run_metadata",
        trigger_metadata,
    )
    monkeypatch.setattr(
        durable_loop_http,
        "_persist_trigger_run_input",
        persist_trigger_input,
    )
    monkeypatch.setattr(
        durable_loop_http,
        "initialize_durable_run_observations",
        initialize_observations,
    )
    callback = create_durable_trigger_admission_callback(
        settings=DurableLoopSettings(),
        resolved_agents={"daily-report": _resolved_http_agent()},
    )
    runtime, _clock = _runtime(callback=callback)
    registration = _registration(trigger_type)
    handler = make_durable_binding_handler(runtime, registration)
    client = _SharedAdmissionClient(events)
    client.lose_v4_start_ack = trigger_type == "connector_trigger"
    trigger_data: object
    if trigger_type == "timer_trigger":
        trigger_data = TimerRequest(
            past_due=False,
            schedule_status={
                "last": "2026-09-29T19:59:00Z",
                "next": "2026-09-29T20:00:00Z",
            },
        )
    else:
        trigger_data = {"event": {"id": "evt-1"}}

    await handler(trigger_data, client)
    await handler(trigger_data, client)
    [record] = await runtime.ledger.pending()
    first = await runtime.attempt(record, client)
    second = await runtime.attempt(record, client)

    assert first.outcome is DurableTriggerAttemptOutcome.ADMITTED
    assert second.outcome is DurableTriggerAttemptOutcome.ADMITTED
    admission_starts = [
        start for start in client.starts if start[0] == DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME
    ]
    run_starts = [
        start for start in client.starts if start[0] == DURABLE_LOOP_ORCHESTRATOR_V4_NAME
    ]
    assert len(admission_starts) == 1
    assert len(run_starts) == 1
    admission_payload = admission_starts[0][2]
    assert isinstance(admission_payload, dict)
    assert admission_payload["run_id"] == record.run_id
    assert admission_payload["request_hash"] == record.request_hash
    assert admission_payload["request_id_hash"] == record.stable_event_id_hash
    assert admission_payload["session_entity_key"] == canonical_hash(
        {"owner_hash": record.owner_hash, "session_id": record.session_id}
    )
    assert events == ["observations", "start"]


@pytest.mark.asyncio
async def test_timer_rejects_missing_or_sentinel_next_before_staging() -> None:
    runtime, _clock = _runtime()
    handler = make_durable_binding_handler(
        runtime,
        _registration("timer_trigger"),
    )

    for next_value in (None, "0001-01-01T00:00:00Z"):
        timer = TimerRequest(
            past_due=False,
            schedule_status={"last": None, "next": next_value},
        )
        with pytest.raises(ValueError, match=r"ScheduleStatus\.Next"):
            await handler(timer, _Client())

    assert await runtime.ledger.pending() == ()


@pytest.mark.asyncio
async def test_http_stages_before_202_and_derives_stable_session() -> None:
    runtime, _clock = _runtime()
    registration = _registration("http_trigger")
    handler = make_durable_http_handler(
        runtime,
        registration,
        BuiltinEndpointsConfig().http_auth,
        resolved=_resolved_http_agent(),
    )
    client = _Client()
    request = _Request(
        {"prompt": "hello"},
        headers={"Idempotency-Key": "request-1"},
    )

    first = await handler(request, client)  # type: ignore[arg-type]
    second = await handler(request, client)  # type: ignore[arg-type]

    assert first.status_code == second.status_code == 202
    first_body = json.loads(bytes(first.body))
    second_body = json.loads(bytes(second.body))
    assert first_body["run_id"] == second_body["run_id"]
    assert first_body["session_id"] == second_body["session_id"]
    assert first.headers["location"].endswith(first_body["run_id"])
    assert len(await runtime.ledger.pending()) == 1
    assert len(client.starts) == 1


@pytest.mark.asyncio
async def test_http_honors_valid_session_and_reports_body_conflict() -> None:
    runtime, _clock = _runtime()
    handler = make_durable_http_handler(
        runtime,
        _registration("http_trigger"),
        BuiltinEndpointsConfig().http_auth,
        resolved=_resolved_http_agent(),
    )
    headers = {
        "Idempotency-Key": "request-1",
        "x-ms-session-id": "caller-session",
    }

    accepted = await handler(  # type: ignore[arg-type]
        _Request({"prompt": "hello"}, headers=headers),
        _Client(),
    )
    conflict = await handler(  # type: ignore[arg-type]
        _Request({"prompt": "different"}, headers=headers),
        _Client(),
    )
    session_conflict = await handler(  # type: ignore[arg-type]
        _Request(
            {"prompt": "hello"},
            headers={
                "Idempotency-Key": "request-1",
                "x-ms-session-id": "other-session",
            },
        ),
        _Client(),
    )

    assert accepted.status_code == 202
    assert accepted.headers["x-ms-session-id"] == "caller-session"
    assert conflict.status_code == 409
    assert session_conflict.status_code == 409


@pytest.mark.asyncio
async def test_http_validates_input_schema_before_staging() -> None:
    runtime, _clock = _runtime()
    handler = make_durable_http_handler(
        runtime,
        _registration("http_trigger"),
        BuiltinEndpointsConfig().http_auth,
        resolved=_resolved_http_agent(),
        input_schema={
            "type": "object",
            "required": ["prompt"],
            "properties": {"prompt": {"type": "string"}},
        },
    )

    response = await handler(  # type: ignore[arg-type]
        _Request({"message": "hello"}, headers={"Idempotency-Key": "request-1"}),
        _Client(),
    )

    assert response.status_code == 400
    assert await runtime.ledger.pending() == ()


def test_connector_registration_validates_paths_and_owner() -> None:
    resolved = ResolvedAgent(
        name="Daily Report",
        description="desc",
        trigger=TriggerSpec(type="connector_trigger", args={}),
        instructions="run",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model=None,
        timeout=1,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=__file__,
    )
    params = {
        "connection_name": "office365",
        "durable_owner": {"kind": "app"},
        "event_id_path": "$.event.id",
        "session_id_path": "$.thread.id",
    }

    registration = validate_durable_trigger_registration(
        resolved,
        "connector_trigger",
        params,
        function_name="daily-report",
    )

    assert registration.event_id_path == "$.event.id"
    assert registration.session_id_path == "$.thread.id"
    assert registration.connection_hash is not None
