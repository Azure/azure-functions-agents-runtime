from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from azure_functions_agents.experimental.durable_chat_protocol import (
    DURABLE_CHAT_JOURNAL_LIMITS,
    DURABLE_CHAT_PUBLISHER_DRAIN_DEADLINE_SECONDS,
    MAX_DURABLE_CHAT_CAS_ATTEMPTS,
    MAX_DURABLE_CHAT_EVENT_BATCH,
    MAX_DURABLE_CHAT_PENDING_OBSERVATIONS,
    MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS,
    MAX_DURABLE_CHAT_TOTAL_RESERVED_OBSERVATION_BYTES,
    DurableChatAgentIdentityV1,
    DurableChatAssistantDraftV1,
    DurableChatAssistantTextObservationV1,
    DurableChatBootstrapV1,
    DurableChatBrowserCursorV1,
    DurableChatBrowserPreferencesV1,
    DurableChatBrowserRunStateV1,
    DurableChatDiagnosticLinkV1,
    DurableChatDiagnosticsV1,
    DurableChatEnqueueDisposition,
    DurableChatEnqueueResultV1,
    DurableChatEventFrameV1,
    DurableChatFrozenDiagnosticsV1,
    DurableChatHttpMethod,
    DurableChatHumanInputObservationV1,
    DurableChatIntegrationAvailabilityV1,
    DurableChatIntegrationKind,
    DurableChatIntegrationMetadataV1,
    DurableChatJournalLimitsV1,
    DurableChatModelProducerV1,
    DurableChatObservationBatchV1,
    DurableChatObservationDegradationReason,
    DurableChatObservationHealthV1,
    DurableChatProducerEpochV1,
    DurableChatPublicationDisposition,
    DurableChatPublicationResultV1,
    DurableChatReplayDisposition,
    DurableChatReplayPageV1,
    DurableChatRouteDescriptorV1,
    DurableChatRouteName,
    DurableChatRunInitializationV1,
    DurableChatRunProjectionV1,
    DurableChatSandboxObservationV1,
    DurableChatSandboxState,
    DurableChatSnapshotFrameV1,
    DurableChatToolProducerV1,
    DurableChatToolProgressV1,
    DurableChatToolState,
    parse_durable_chat_document,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    ContentRefV1,
    DurableChatModelMode,
    DurableChatRunOptionsV1,
    DurableChatStartOptionsV1,
    DurableFaultProfile,
    DurableLoopPlanDocumentV1,
    DurableLoopRunStatus,
    FrozenToolCatalogV1,
    HumanInputState,
    SandboxExecutionProfile,
    ToolProvenance,
    canonical_hash,
)

_HASH = "a" * 64
_NOW = datetime(2026, 9, 14, tzinfo=UTC)
_SANDBOX_GROUP_RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/demo/providers/Microsoft.App/sandboxGroups/demo-group"
)


def _routes() -> tuple[DurableChatRouteDescriptorV1, ...]:
    base = "/api/experimental/durable-agent-runs"
    return (
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.START_RUN,
            method=DurableChatHttpMethod.POST,
            path_template=base,
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.STATUS,
            method=DurableChatHttpMethod.GET,
            path_template=f"{base}/{{run_id}}",
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.RESULT,
            method=DurableChatHttpMethod.GET,
            path_template=f"{base}/{{run_id}}/result",
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.CANCEL,
            method=DurableChatHttpMethod.POST,
            path_template=f"{base}/{{run_id}}/cancel",
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.HUMAN_INPUT_DETAIL,
            method=DurableChatHttpMethod.GET,
            path_template=f"{base}/{{run_id}}/input/{{request_id}}",
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.HUMAN_INPUT_SUBMIT,
            method=DurableChatHttpMethod.POST,
            path_template=f"{base}/{{run_id}}/input/{{request_id}}",
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.EVENTS,
            method=DurableChatHttpMethod.GET,
            path_template=f"{base}/{{run_id}}/events",
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.DIAGNOSTICS,
            method=DurableChatHttpMethod.GET,
            path_template=f"{base}/{{run_id}}/diagnostics",
        ),
    )


def _bootstrap() -> DurableChatBootstrapV1:
    return DurableChatBootstrapV1(
        agent=DurableChatAgentIdentityV1(
            slug="main",
            display_name="Durable assistant",
        ),
        routes=_routes(),
        supported_sandbox_profiles=(
            SandboxExecutionProfile.PER_CALL,
            SandboxExecutionProfile.RETAINED_SESSION,
        ),
        default_sandbox_profile=SandboxExecutionProfile.PER_CALL,
        foreground_streaming_available=True,
        integrations=DurableChatIntegrationMetadataV1(
            durable_task_scheduler=DurableChatIntegrationAvailabilityV1(
                configured=True,
            ),
            application_insights=DurableChatIntegrationAvailabilityV1(
                configured=False,
                unavailable_reason="Application Insights resource metadata is unavailable.",
            ),
        ),
        history_namespace=_HASH,
    )


def _producer() -> DurableChatModelProducerV1:
    return DurableChatModelProducerV1(step_index=0, observation_epoch=1)


def _tool_producer() -> DurableChatToolProducerV1:
    return DurableChatToolProducerV1(call_key="b" * 64)


def _health() -> DurableChatObservationHealthV1:
    return DurableChatObservationHealthV1()


def _projection() -> DurableChatRunProjectionV1:
    producer = _producer()
    return DurableChatRunProjectionV1(
        run_id="run-1",
        session_id="session-1",
        published_revision=1,
        through_sequence=0,
        draft=DurableChatAssistantDraftV1(
            producer=producer,
            text="Thinking",
            updated_at=_NOW,
        ),
        progress={
            "status": DurableLoopRunStatus.RUNNING,
            "phase": "model_stream",
            "model_steps": 0,
            "tool_calls": 0,
            "human_waits": 0,
            "step_index": 0,
            "updated_at": _NOW,
        },
        producer_epochs=(
            DurableChatProducerEpochV1(
                step_index=producer.step_index,
                observation_epoch=producer.observation_epoch,
            ),
        ),
        observation_health=_health(),
    )


def _plan(*, ui: DurableChatRunOptionsV1 | None = None) -> DurableLoopPlanDocumentV1:
    return DurableLoopPlanDocumentV1(
        instructions="Respond.",
        catalog=FrozenToolCatalogV1.create(
            tools=(),
            policy_hash=_HASH,
            package_hash="b" * 64,
        ),
        model_settings={"background": False},
        maf_core_version="1.17.0",
        provider="openai",
        model="model",
        api_version="responses-v1",
        settings={"max_model_steps": 48},
        sandbox_profile=SandboxExecutionProfile.PER_CALL,
        fault_profile=DurableFaultProfile.NONE,
        ui=ui,
    )


def test_bootstrap_is_complete_same_origin_and_owner_safe() -> None:
    bootstrap = _bootstrap()

    assert {route.name for route in bootstrap.routes} == set(DurableChatRouteName)
    assert bootstrap.history_namespace == _HASH
    serialized = bootstrap.model_dump_json()
    assert "owner_hash" not in serialized
    assert "secret" not in serialized.casefold()

    with pytest.raises(ValidationError, match="same-origin"):
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.STATUS,
            method=DurableChatHttpMethod.GET,
            path_template="https://attacker.invalid/{run_id}",
        )


def test_chat_protocol_rejects_duplicate_and_extra_fields() -> None:
    bootstrap = _bootstrap()
    payload = bootstrap.model_dump(mode="json")
    payload["owner_hash"] = _HASH

    with pytest.raises(ValueError):
        parse_durable_chat_document(json.dumps(payload), DurableChatBootstrapV1)
    with pytest.raises(ValueError):
        parse_durable_chat_document(
            bootstrap.model_dump_json()[:-1] + ',"history_namespace":"' + "b" * 64 + '"}',
            DurableChatBootstrapV1,
        )


def test_frozen_chat_options_preserve_legacy_plan_serialization() -> None:
    legacy = _plan()

    assert "ui" not in legacy.model_dump(mode="json")
    assert canonical_hash(legacy) == canonical_hash(legacy.model_dump(mode="json"))

    requested = DurableChatStartOptionsV1(stream_response=True)
    frozen = requested.freeze(model_mode=DurableChatModelMode.FOREGROUND)
    opted_in = _plan(ui=frozen)

    assert opted_in.model_dump(mode="json")["ui"] == {
        "schema_version": "1",
        "stream_response": True,
        "model_mode": "foreground",
    }
    assert canonical_hash(opted_in) != canonical_hash(legacy)
    background = DurableChatStartOptionsV1(stream_response=False).freeze(
        model_mode=DurableChatModelMode.BACKGROUND
    )
    foreground_without_stream = DurableChatStartOptionsV1(stream_response=False).freeze(
        model_mode=DurableChatModelMode.FOREGROUND
    )
    assert background.model_mode is DurableChatModelMode.BACKGROUND
    assert background.stream_response is False
    assert foreground_without_stream.stream_response is False
    with pytest.raises(ValueError, match="background"):
        requested.freeze(model_mode=DurableChatModelMode.BACKGROUND)
    with pytest.raises(ValidationError, match="background"):
        DurableChatRunOptionsV1(
            stream_response=True,
            model_mode=DurableChatModelMode.BACKGROUND,
        )


def test_initialization_binds_admission_to_frozen_references() -> None:
    initialization = DurableChatRunInitializationV1(
        run_id="run-1",
        session_id="session-1",
        owner_hash="b" * 64,
        request_id_hash="c" * 64,
        request_hash="d" * 64,
        plan_ref=ContentRefV1(
            object_id="content/plan/one",
            sha256="e" * 64,
            byte_length=10,
            media_type="application/json",
            encryption_version="v1",
            retention_class="run",
        ),
        input_ref=ContentRefV1(
            object_id="content/input/one",
            sha256="f" * 64,
            byte_length=10,
            media_type="application/json",
            encryption_version="v1",
            retention_class="run",
        ),
        created_at=_NOW,
        expires_at=_NOW + timedelta(days=1),
        committed_generation=2,
        ui=DurableChatRunOptionsV1(
            stream_response=True,
            model_mode=DurableChatModelMode.FOREGROUND,
        ),
    )

    assert initialization.committed_generation == 2
    assert initialization.ui.model_mode is DurableChatModelMode.FOREGROUND


def test_sandbox_group_metadata_defaults_when_loading_older_documents() -> None:
    bootstrap_payload = _bootstrap().model_dump(mode="json")
    del bootstrap_payload["sandbox_group_resource_id"]
    frozen = DurableChatFrozenDiagnosticsV1(
        durable_task_unavailable_reason="DTS is unavailable.",
        application_insights_unavailable_reason="Application Insights is unavailable.",
        request_started_at=_NOW,
        request_ends_at=_NOW + timedelta(days=1),
    )
    frozen_payload = frozen.model_dump(mode="json")
    del frozen_payload["sandbox_group_resource_id"]

    bootstrap = parse_durable_chat_document(
        json.dumps(bootstrap_payload),
        DurableChatBootstrapV1,
    )
    older_frozen = parse_durable_chat_document(
        json.dumps(frozen_payload),
        DurableChatFrozenDiagnosticsV1,
    )

    assert bootstrap.sandbox_group_resource_id is None
    assert older_frozen.sandbox_group_resource_id is None


def test_snapshot_replay_and_browser_cursor_are_atomic() -> None:
    projection = _projection()
    snapshot = DurableChatSnapshotFrameV1(
        captured_at=_NOW,
        projection=projection,
    )
    observation = DurableChatAssistantTextObservationV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=_NOW,
        producer=_producer(),
        delta=" more",
    )
    event = DurableChatEventFrameV1(sequence=1, event=observation)
    replay = DurableChatReplayPageV1(
        run_id="run-1",
        requested_after_sequence=0,
        disposition=DurableChatReplayDisposition.SNAPSHOT_REQUIRED,
        through_sequence=1,
        snapshot=snapshot,
        events=(event,),
    )
    parsed_event = parse_durable_chat_document(
        event.model_dump_json(),
        DurableChatEventFrameV1,
    )
    browser_state = DurableChatBrowserRunStateV1(
        history_namespace=_HASH,
        session_id="session-1",
        run_id="run-1",
        projection=projection,
        cursor=DurableChatBrowserCursorV1(
            run_id="run-1",
            after_sequence=0,
            published_revision=1,
        ),
        persisted_at=_NOW,
    )

    assert replay.events[0].event.event_type.value == "assistant_text"
    assert parsed_event.event == observation
    assert browser_state.cursor.after_sequence == browser_state.projection.through_sequence
    assert (
        DurableChatBrowserPreferencesV1(
            history_namespace=_HASH,
            details_visible=False,
        ).details_visible
        is False
    )
    with pytest.raises(ValidationError, match="cursor"):
        DurableChatBrowserRunStateV1(
            history_namespace=_HASH,
            session_id="session-1",
            run_id="run-1",
            projection=projection,
            cursor=DurableChatBrowserCursorV1(
                run_id="run-1",
                after_sequence=1,
                published_revision=1,
            ),
            persisted_at=_NOW,
        )


def test_event_frames_carry_their_publication_revision_and_replay_is_contiguous() -> None:
    observation = DurableChatAssistantTextObservationV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=_NOW,
        producer=_producer(),
        delta="one",
    )
    published = DurableChatEventFrameV1(
        sequence=1,
        published_revision=4,
        event=observation,
    )
    legacy = DurableChatEventFrameV1(sequence=1, event=observation)

    assert published.model_dump(mode="json")["published_revision"] == 4
    assert "published_revision" not in legacy.model_dump(mode="json")
    with pytest.raises(ValidationError, match="contiguous"):
        DurableChatReplayPageV1(
            run_id="run-1",
            requested_after_sequence=0,
            disposition=DurableChatReplayDisposition.DELTAS,
            through_sequence=2,
            events=(
                DurableChatEventFrameV1(sequence=2, event=observation),
            ),
        )


def test_observation_edge_states_do_not_reject_real_late_lifecycle_evidence() -> None:
    unavailable = DurableChatSandboxObservationV1(
        producer=_tool_producer(),
        step_index=1,
        tool_name="lookup",
        provenance=ToolProvenance.LOCAL,
        sandbox_profile=SandboxExecutionProfile.PER_CALL,
        sandbox_group_resource_id=_SANDBOX_GROUP_RESOURCE_ID,
        sandbox_id="sandbox-known",
        state=DurableChatSandboxState.UNAVAILABLE,
        observed_at=_NOW,
    )
    expired = DurableChatHumanInputObservationV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=_NOW + timedelta(minutes=1),
        request_id="input-1",
        state=HumanInputState.TIMED_OUT,
        expires_at=_NOW,
    )

    assert unavailable.sandbox_id == "sandbox-known"
    assert expired.expires_at < expired.observed_at


def test_diagnostics_retain_actual_local_sandbox_history_without_tool_payloads() -> None:
    sandbox = DurableChatSandboxObservationV1(
        producer=_tool_producer(),
        step_index=1,
        tool_name="lookup",
        provenance=ToolProvenance.LOCAL,
        sandbox_profile=SandboxExecutionProfile.PER_CALL,
        sandbox_group_resource_id=_SANDBOX_GROUP_RESOURCE_ID,
        sandbox_id="sandbox-actual-1",
        state=DurableChatSandboxState.CONFIRMED_DELETED,
        observed_at=_NOW,
    )
    diagnostics = DurableChatDiagnosticsV1(
        run_id="run-1",
        session_id="session-1",
        model_mode=DurableChatModelMode.FOREGROUND,
        status=DurableLoopRunStatus.COMPLETED,
        created_at=_NOW,
        updated_at=_NOW,
        expires_at=_NOW + timedelta(days=1),
        committed_generation=1,
        sandbox_observations=(sandbox,),
        observation_health=_health(),
        links=(
            DurableChatDiagnosticLinkV1(
                kind=DurableChatIntegrationKind.DURABLE_TASK_SCHEDULER,
                available=True,
                href="https://dashboard.durabletask.io/?endpoint=https%3A%2F%2Fexample.test&taskhub=hub",
            ),
            DurableChatDiagnosticLinkV1(
                kind=DurableChatIntegrationKind.APPLICATION_INSIGHTS,
                available=False,
                unavailable_reason="Application Insights is not configured.",
            ),
        ),
    )

    serialized = diagnostics.model_dump_json()
    assert "sandbox-actual-1" in serialized
    assert "arguments" not in serialized
    assert "result_ref" not in serialized
    with pytest.raises(ValidationError, match="authorization"):
        DurableChatDiagnosticLinkV1(
            kind=DurableChatIntegrationKind.DURABLE_TASK_SCHEDULER,
            available=True,
            href="https://dashboard.durabletask.io/?sig=secret",
        )


@pytest.mark.parametrize(
    "href",
    (
        "https://dashboard.durabletask.io/?code=secret",
        "https://dashboard.durabletask.io/?x-functions-key=secret",
        "https://dashboard.durabletask.io/?sharedaccesskey=secret",
        "https://portal.azure.com/#blade/example?code=secret",
    ),
)
def test_diagnostic_urls_reject_function_keys_in_queries_and_fragments(
    href: str,
) -> None:
    with pytest.raises(ValidationError, match="authorization"):
        DurableChatDiagnosticLinkV1(
            kind=DurableChatIntegrationKind.DURABLE_TASK_SCHEDULER,
            available=True,
            href=href,
        )

    link = DurableChatDiagnosticLinkV1(
        kind=DurableChatIntegrationKind.APPLICATION_INSIGHTS,
        available=True,
        href="https://portal.azure.com/#blade/Microsoft_Azure_Monitoring_Logs/LogsBlade",
    )

    assert link.href is not None


def test_observation_limits_are_fixed_and_overflow_is_degraded() -> None:
    observation = DurableChatAssistantTextObservationV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=_NOW,
        producer=_producer(),
        delta="hello",
    )
    batch = DurableChatObservationBatchV1(
        run_id="run-1",
        observations=(observation,),
    )
    health = DurableChatObservationHealthV1(
        degraded=True,
        reasons=(DurableChatObservationDegradationReason.QUEUE_FULL,),
        dropped_observations=1,
    )
    result = DurableChatEnqueueResultV1(
        disposition=DurableChatEnqueueDisposition.DROPPED,
        pending_observations=MAX_DURABLE_CHAT_PENDING_OBSERVATIONS,
        degradation_reason=DurableChatObservationDegradationReason.QUEUE_FULL,
    )
    publication = DurableChatPublicationResultV1(
        run_id="run-1",
        disposition=DurableChatPublicationDisposition.DEGRADED,
        published_revision=1,
        through_sequence=0,
        health=health,
    )

    assert batch.reserved_bytes() > 0
    assert result.degradation_reason is DurableChatObservationDegradationReason.QUEUE_FULL
    assert publication.health.degraded is True
    assert DURABLE_CHAT_JOURNAL_LIMITS.max_events_per_batch == MAX_DURABLE_CHAT_EVENT_BATCH
    assert (
        DURABLE_CHAT_JOURNAL_LIMITS.max_total_reserved_observation_bytes
        == MAX_DURABLE_CHAT_TOTAL_RESERVED_OBSERVATION_BYTES
    )
    assert DURABLE_CHAT_JOURNAL_LIMITS.max_producer_attempts == MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS
    assert DURABLE_CHAT_JOURNAL_LIMITS.max_cas_attempts == MAX_DURABLE_CHAT_CAS_ATTEMPTS
    assert (
        DURABLE_CHAT_JOURNAL_LIMITS.publisher_drain_deadline_seconds
        == DURABLE_CHAT_PUBLISHER_DRAIN_DEADLINE_SECONDS
    )
    with pytest.raises(ValidationError):
        DurableChatJournalLimitsV1(max_events_per_batch=MAX_DURABLE_CHAT_EVENT_BATCH - 1)


def test_tool_progress_uses_call_key_without_tool_payloads() -> None:
    progress = DurableChatToolProgressV1(
        producer=_tool_producer(),
        step_index=1,
        tool_name="lookup",
        provenance=ToolProvenance.LOCAL,
        state=DurableChatToolState.SUCCEEDED,
        updated_at=_NOW,
    )

    assert progress.producer.call_key == "b" * 64
