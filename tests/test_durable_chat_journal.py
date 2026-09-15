from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from azure_functions_agents.experimental import durable_chat_journal
from azure_functions_agents.experimental.durable_chat_config import DurableChatSettings
from azure_functions_agents.experimental.durable_chat_journal import (
    DurableChatJournal,
    DurableChatJournalError,
    DurableChatObserver,
)
from azure_functions_agents.experimental.durable_chat_protocol import (
    MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS,
    DurableChatAssistantTextObservationV1,
    DurableChatDegradedObservationV1,
    DurableChatFrozenDiagnosticsV1,
    DurableChatModelAttemptObservationV1,
    DurableChatModelAttemptState,
    DurableChatModelProducerV1,
    DurableChatObservationBatchV1,
    DurableChatObservationDegradationReason,
    DurableChatObservationHealthV1,
    DurableChatObservationV1,
    DurableChatProgressObservationV1,
    DurableChatReplayDisposition,
    DurableChatRunInitializationV1,
    DurableChatSandboxObservationEventV1,
    DurableChatSandboxObservationV1,
    DurableChatSandboxState,
    DurableChatTerminalObservationV1,
    DurableChatToolProducerV1,
)
from azure_functions_agents.experimental.durable_loop_activities import (
    InMemoryDurableContentStore,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    ContentRefV1,
    DurableChatModelMode,
    DurableChatRunOptionsV1,
    DurableLoopRunStatus,
    SandboxExecutionProfile,
    ToolProvenance,
)
from azure_functions_agents.experimental.durable_loop_receipts import (
    InMemoryDurableKeyedDocumentStore,
)
from azure_functions_agents.experimental.hybrid_config import HYBRID_SANDBOX_GROUP_ENV
from azure_functions_agents.strict_json import canonical_json_bytes

_NOW = datetime(2026, 9, 14, tzinfo=UTC)
_HASH = "a" * 64
_SANDBOX_GROUP_RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/demo/providers/Microsoft.App/sandboxGroups/demo-group"
)
_CONFIGURED_SANDBOX_GROUP_RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/demo/providers/Microsoft.App/sandboxGroups/configured-group"
)
_CHANGED_SANDBOX_GROUP_RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/demo/providers/Microsoft.App/sandboxGroups/changed-group"
)


def _deadline() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=1)


def _journal() -> DurableChatJournal:
    return DurableChatJournal(
        content=InMemoryDurableContentStore(),
        documents=InMemoryDurableKeyedDocumentStore(),
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


def _initialization(
    *,
    run_id: str = "run-1",
    diagnostics: DurableChatFrozenDiagnosticsV1 | None = None,
) -> DurableChatRunInitializationV1:
    return DurableChatRunInitializationV1(
        run_id=run_id,
        session_id="session-1",
        owner_hash=_HASH,
        request_id_hash=_HASH,
        request_hash=_HASH,
        plan_ref=_reference("plan"),
        input_ref=_reference("input"),
        expires_at=_NOW + timedelta(hours=1),
        committed_generation=2,
        ui=DurableChatRunOptionsV1(
            stream_response=True,
            model_mode=DurableChatModelMode.FOREGROUND,
        ),
        created_at=_NOW,
        diagnostics=diagnostics,
    )


def _text(
    *,
    producer: DurableChatModelProducerV1,
    delta: str = "thinking",
) -> DurableChatAssistantTextObservationV1:
    return DurableChatAssistantTextObservationV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=_NOW,
        producer=producer,
        delta=delta,
    )


def _model_attempt(
    producer: DurableChatModelProducerV1,
    state: DurableChatModelAttemptState,
) -> DurableChatModelAttemptObservationV1:
    return DurableChatModelAttemptObservationV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=_NOW,
        producer=producer,
        state=state,
    )


def _sandbox(
    *,
    state: DurableChatSandboxState,
    sandbox_id: str,
    observed_at: datetime,
    replaced_sandbox_id: str | None = None,
) -> DurableChatSandboxObservationEventV1:
    observation = DurableChatSandboxObservationV1(
        producer=DurableChatToolProducerV1(call_key="b" * 64),
        step_index=1,
        tool_name="lookup",
        provenance=ToolProvenance.LOCAL,
        sandbox_profile=SandboxExecutionProfile.RETAINED_SESSION,
        sandbox_group_resource_id=_SANDBOX_GROUP_RESOURCE_ID,
        sandbox_id=sandbox_id,
        state=state,
        replaced_sandbox_id=replaced_sandbox_id,
        observed_at=observed_at,
    )
    return DurableChatSandboxObservationEventV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=observed_at,
        observation=observation,
    )


def _progress(step_index: int) -> DurableChatProgressObservationV1:
    return DurableChatProgressObservationV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=_NOW,
        progress={
            "status": DurableLoopRunStatus.RUNNING,
            "phase": "model",
            "model_steps": step_index,
            "tool_calls": 0,
            "human_waits": 0,
            "step_index": step_index,
            "updated_at": _NOW,
        },
    )


def _degraded_observation() -> DurableChatDegradedObservationV1:
    return DurableChatDegradedObservationV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=_NOW,
        health=DurableChatObservationHealthV1(
            degraded=True,
            reasons=(DurableChatObservationDegradationReason.QUEUE_FULL,),
            dropped_observations=1,
            last_error_code=DurableChatObservationDegradationReason.QUEUE_FULL.value,
        ),
    )


async def _publish(
    journal: DurableChatJournal,
    *observations: DurableChatObservationV1,
) -> object:
    batch = DurableChatObservationBatchV1(
        run_id="run-1",
        observations=observations,
    )
    result = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=batch,
        deadline=_deadline(),
    )
    assert result.disposition.value == "published"
    return result


async def _projection(journal: DurableChatJournal):
    snapshot = await journal._read_manifest("run-1", _deadline())
    assert snapshot is not None
    assert snapshot.manifest.projection is not None
    return snapshot.manifest.projection


@pytest.mark.asyncio
async def test_initialization_returns_the_first_concurrent_winner() -> None:
    journal = _journal()
    first = _initialization()
    second = first.model_copy(update={"committed_generation": 3})

    winners = await asyncio.gather(
        journal.create_run_initialization_once(initialization=first),
        journal.create_run_initialization_once(initialization=second),
    )

    assert winners[0] == winners[1]
    assert await journal.load_run_initialization(run_id="run-1") == winners[0]


@pytest.mark.asyncio
async def test_diagnostics_keep_the_configured_sandbox_group_frozen_per_run() -> None:
    configured_settings = DurableChatSettings.from_environment(
        {HYBRID_SANDBOX_GROUP_ENV: _CONFIGURED_SANDBOX_GROUP_RESOURCE_ID},
        observability_enabled=False,
    )
    journal = _journal()
    initialization = _initialization(
        diagnostics=configured_settings.freeze_diagnostics(
            request_started_at=_NOW,
            request_ends_at=_NOW + timedelta(hours=1),
        )
    )
    await journal.create_run_initialization_once(initialization=initialization)
    changed_settings = DurableChatSettings.from_environment(
        {HYBRID_SANDBOX_GROUP_ENV: _CHANGED_SANDBOX_GROUP_RESOURCE_ID},
        observability_enabled=False,
    )
    winner = await journal.create_run_initialization_once(
        initialization=initialization.model_copy(
            update={
                "diagnostics": changed_settings.freeze_diagnostics(
                    request_started_at=_NOW,
                    request_ends_at=_NOW + timedelta(hours=1),
                )
            }
        )
    )
    await _publish(
        journal,
        _sandbox(
            state=DurableChatSandboxState.EXECUTING,
            sandbox_id="sandbox-actual-1",
            observed_at=_NOW,
        ),
    )
    diagnostics = await journal.read_diagnostics(run_id="run-1")

    assert winner == initialization
    assert diagnostics is not None
    assert (
        diagnostics.configured_sandbox_group_resource_id
        == _CONFIGURED_SANDBOX_GROUP_RESOURCE_ID
    )
    assert (
        diagnostics.sandbox_observations[0].sandbox_group_resource_id
        == _SANDBOX_GROUP_RESOURCE_ID
    )
    assert diagnostics.sandbox_observations[0].sandbox_id == "sandbox-actual-1"
    assert (
        changed_settings.sandbox_group_resource_id
        == _CHANGED_SANDBOX_GROUP_RESOURCE_ID
    )


@pytest.mark.asyncio
async def test_concurrent_publishers_expose_only_contiguous_published_events() -> None:
    journal = _journal()

    await asyncio.gather(
        *(_publish(journal, _progress(index)) for index in range(8))
    )

    page = await journal.replay(run_id="run-1", after_sequence=0, limit=128)

    assert page.disposition is DurableChatReplayDisposition.DELTAS
    assert [event.sequence for event in page.events] == list(
        range(1, len(page.events) + 1)
    )
    assert len(page.events) == 8


@pytest.mark.asyncio
async def test_stale_producer_and_exhausted_epoch_are_degraded_without_throwing() -> None:
    journal = _journal()
    await journal.create_run_initialization_once(initialization=_initialization())
    first, second = await journal.reserve_model_producers(
        run_id="run-1",
        step_index=0,
        count=2,
        deadline=_deadline(),
    )

    await _publish(journal, _text(producer=second))
    stale = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(_text(producer=first),),
        ),
        deadline=_deadline(),
    )
    exhausted = await journal.reserve_model_producers(
        run_id="run-1",
        step_index=0,
        count=MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS,
        deadline=_deadline(),
    )
    diagnostics = await journal.read_diagnostics(run_id="run-1")

    assert stale.disposition.value == "stale_producer"
    assert exhausted == ()
    assert diagnostics is not None
    assert (
        DurableChatObservationDegradationReason.PRODUCER_ATTEMPTS_EXHAUSTED
        in diagnostics.observation_health.reasons
    )


@pytest.mark.asyncio
async def test_prereserved_epochs_activate_on_observation_and_fence_old_steps() -> None:
    journal = _journal()
    first, retry, _, _ = await journal.reserve_model_producers(
        run_id="run-1",
        step_index=0,
        count=4,
        deadline=_deadline(),
    )

    first_result = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(
                _model_attempt(first, DurableChatModelAttemptState.STARTED),
                _text(producer=first, delta="first"),
            ),
        ),
        deadline=_deadline(),
    )
    assert first_result.disposition.value == "published"
    assert (await _projection(journal)).draft is not None
    assert (await _projection(journal)).draft.producer == first

    failed_first = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(
                _model_attempt(first, DurableChatModelAttemptState.FAILED),
                _text(producer=first, delta="same batch after failure"),
            ),
        ),
        deadline=_deadline(),
    )
    assert failed_first.disposition.value == "published"
    assert (await _projection(journal)).draft is None

    delayed_failed_text = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(_text(producer=first, delta="after failure"),),
        ),
        deadline=_deadline(),
    )
    assert delayed_failed_text.disposition.value == "stale_producer"

    later = await journal.reserve_model_producers(
        run_id="run-1",
        step_index=0,
        count=1,
        deadline=_deadline(),
    )
    assert [producer.observation_epoch for producer in later] == [5]

    retry_result = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(
                _model_attempt(retry, DurableChatModelAttemptState.STARTED),
                _text(producer=retry, delta="retry"),
            ),
        ),
        deadline=_deadline(),
    )
    assert retry_result.disposition.value == "published"
    assert (await _projection(journal)).draft is not None
    assert (await _projection(journal)).draft.producer == retry

    late_retry = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(_text(producer=first, delta="late"),),
        ),
        deadline=_deadline(),
    )
    assert late_retry.disposition.value == "stale_producer"

    next_step = (
        await journal.reserve_model_producers(
            run_id="run-1",
            step_index=1,
            count=1,
            deadline=_deadline(),
        )
    )[0]
    await _publish(
        journal,
        _model_attempt(next_step, DurableChatModelAttemptState.STARTED),
        _text(producer=next_step, delta="next"),
    )
    delayed_prior_step = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(
                _model_attempt(retry, DurableChatModelAttemptState.STARTED),
                _model_attempt(retry, DurableChatModelAttemptState.FAILED),
                _text(producer=retry, delta="delayed"),
            ),
        ),
        deadline=_deadline(),
    )

    projection = await _projection(journal)
    assert delayed_prior_step.disposition.value == "stale_producer"
    assert projection.draft is not None
    assert projection.draft.producer == next_step


@pytest.mark.asyncio
async def test_reserved_newer_model_step_fences_prior_step_before_it_observes() -> None:
    journal = _journal()
    first, _, _ = await journal.reserve_model_producers(
        run_id="run-1",
        step_index=0,
        count=3,
        deadline=_deadline(),
    )
    await _publish(journal, _text(producer=first, delta="first"))
    await journal.reserve_model_producers(
        run_id="run-1",
        step_index=1,
        count=3,
        deadline=_deadline(),
    )

    late_prior_step = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(_text(producer=first, delta="late"),),
        ),
        deadline=_deadline(),
    )

    assert late_prior_step.disposition.value == "stale_producer"
    projection = await _projection(journal)
    assert projection.draft is not None
    assert projection.draft.text == "first"


@pytest.mark.asyncio
async def test_degraded_observation_health_persists_through_progress_and_snapshot() -> None:
    journal = _journal()
    await journal.create_run_initialization_once(initialization=_initialization())
    await _publish(journal, _degraded_observation())
    await _publish(journal, _progress(1))

    after_progress = await _projection(journal)
    diagnostics = await journal.read_diagnostics(run_id="run-1")
    assert diagnostics is not None
    assert diagnostics.model_dump(mode="json")["configured_sandbox_group_resource_id"] is None
    for health in (
        after_progress.observation_health,
        diagnostics.observation_health,
    ):
        assert (
            DurableChatObservationDegradationReason.QUEUE_FULL in health.reasons
        )

    for step in range(2, 129):
        await _publish(journal, _progress(step))
    replay = await journal.replay(run_id="run-1", after_sequence=0, limit=128)

    assert replay.snapshot is not None
    assert (
        DurableChatObservationDegradationReason.QUEUE_FULL
        in replay.snapshot.projection.observation_health.reasons
    )


@pytest.mark.asyncio
async def test_compaction_returns_one_atomic_snapshot_and_rejects_future_cursor() -> None:
    journal = _journal()

    for step in range(129):
        await _publish(journal, _progress(step))

    replay = await journal.replay(run_id="run-1", after_sequence=0, limit=128)
    future = await journal.replay(run_id="run-1", after_sequence=130, limit=128)

    assert replay.disposition is DurableChatReplayDisposition.SNAPSHOT_REQUIRED
    assert replay.snapshot is not None
    assert replay.snapshot.projection.through_sequence == replay.through_sequence
    assert future.disposition is DurableChatReplayDisposition.CURSOR_AHEAD


@pytest.mark.asyncio
async def test_sandbox_history_keeps_replaced_physical_instances_after_compaction() -> None:
    journal = _journal()
    await journal.create_run_initialization_once(initialization=_initialization())
    old_sandbox = "sandbox-old"
    new_sandbox = "sandbox-new"

    await _publish(
        journal,
        _sandbox(
            state=DurableChatSandboxState.EXECUTING,
            sandbox_id=old_sandbox,
            observed_at=_NOW,
        ),
        _sandbox(
            state=DurableChatSandboxState.REPLACEMENT_INSTANCE,
            sandbox_id=new_sandbox,
            replaced_sandbox_id=old_sandbox,
            observed_at=_NOW + timedelta(seconds=1),
        ),
        _sandbox(
            state=DurableChatSandboxState.RETAINED_IDLE,
            sandbox_id=new_sandbox,
            observed_at=_NOW + timedelta(seconds=2),
        ),
        _sandbox(
            state=DurableChatSandboxState.CONFIRMED_DELETED,
            sandbox_id=new_sandbox,
            observed_at=_NOW + timedelta(seconds=3),
        ),
    )
    for step in range(125):
        await _publish(journal, _progress(step))
    await _publish(journal, _progress(126))

    replay = await journal.replay(run_id="run-1", after_sequence=0, limit=128)
    diagnostics = await journal.read_diagnostics(run_id="run-1")

    assert replay.snapshot is not None
    assert diagnostics is not None
    for observations in (
        replay.snapshot.projection.sandbox_observations,
        diagnostics.sandbox_observations,
    ):
        assert {item.sandbox_id for item in observations} >= {
            old_sandbox,
            new_sandbox,
        }
        assert any(
            item.state is DurableChatSandboxState.REPLACEMENT_INSTANCE
            and item.sandbox_id == new_sandbox
            and item.replaced_sandbox_id == old_sandbox
            for item in observations
        )
        assert any(
            item.state is DurableChatSandboxState.CONFIRMED_DELETED
            and item.sandbox_id == new_sandbox
            for item in observations
        )


@pytest.mark.asyncio
async def test_snapshot_bytes_are_reserved_before_compaction_writes() -> None:
    class RecordingContent:
        def __init__(self) -> None:
            self._inner = InMemoryDurableContentStore()
            self.written_bytes = 0

        async def put_bytes(
            self,
            *,
            kind: str,
            payload: bytes,
            media_type: str,
            retention_class: str,
        ) -> ContentRefV1:
            self.written_bytes += len(payload)
            return await self._inner.put_bytes(
                kind=kind,
                payload=payload,
                media_type=media_type,
                retention_class=retention_class,
            )

        async def get_bytes(self, reference: ContentRefV1) -> bytes:
            return await self._inner.get_bytes(reference)

    content = RecordingContent()
    journal = DurableChatJournal(
        content=content,  # type: ignore[arg-type]
        documents=InMemoryDurableKeyedDocumentStore(),
    )
    await journal.create_run_initialization_once(initialization=_initialization())

    for step in range(129):
        await _publish(journal, _progress(step))

    diagnostics = await journal.read_diagnostics(run_id="run-1")

    assert diagnostics is not None
    assert (
        diagnostics.observation_health.reserved_observation_bytes
        >= content.written_bytes
    )


@pytest.mark.asyncio
async def test_replay_event_frames_include_their_published_revision() -> None:
    journal = _journal()
    result = await _publish(journal, _progress(1))
    replay = await journal.replay(run_id="run-1", after_sequence=0, limit=128)

    assert replay.events[0].published_revision == result.published_revision


@pytest.mark.asyncio
async def test_replay_rejects_published_events_with_another_session() -> None:
    content = InMemoryDurableContentStore()
    documents = InMemoryDurableKeyedDocumentStore()
    journal = DurableChatJournal(content=content, documents=documents)
    await _publish(journal, _progress(1))
    manifest = await journal._read_manifest("run-1", _deadline())
    assert manifest is not None
    reference = manifest.manifest.batches[0]
    mismatched = DurableChatObservationBatchV1(
        run_id="run-1",
        observations=(
            DurableChatProgressObservationV1(
                run_id="run-1",
                session_id="other-session",
                observed_at=_NOW,
                progress={
                    "status": DurableLoopRunStatus.RUNNING,
                    "phase": "model",
                    "model_steps": 1,
                    "tool_calls": 0,
                    "human_waits": 0,
                    "step_index": 1,
                    "updated_at": _NOW,
                },
            ),
        ),
    )
    mismatched_reference = await content.put_bytes(
        kind="durable-chat-observation-batch",
        payload=canonical_json_bytes(mismatched),
        media_type="application/json",
        retention_class="run",
    )
    invalid_manifest = manifest.manifest.model_copy(
        update={
            "batches": (
                reference.model_copy(update={"reference": mismatched_reference}),
            )
        }
    )
    assert await documents.replace(
        durable_chat_journal._journal_key("run-1"),
        canonical_json_bytes(invalid_manifest),
        revision=manifest.revision,
    )

    with pytest.raises(DurableChatJournalError, match="session"):
        await journal.replay(run_id="run-1", after_sequence=0, limit=128)


@pytest.mark.asyncio
async def test_observer_publishes_before_explicit_drain_and_never_rethrows_failure() -> None:
    journal = _journal()
    producers = await journal.reserve_model_producers(
        run_id="run-1",
        step_index=0,
        count=1,
        deadline=_deadline(),
    )
    observer = DurableChatObserver(journal=journal, run_id="run-1")

    observer.start()
    observer.try_enqueue(observation=_text(producer=producers[0]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    page = await journal.replay(run_id="run-1", after_sequence=0, limit=128)
    drain = await observer.drain(deadline=_deadline())

    assert page.events
    assert drain.published_observations >= 0


@pytest.mark.asyncio
async def test_terminal_fence_prevents_late_drafts_from_resurrecting() -> None:
    journal = _journal()
    producer = (
        await journal.reserve_model_producers(
            run_id="run-1",
            step_index=0,
            count=1,
            deadline=_deadline(),
        )
    )[0]
    terminal = DurableChatTerminalObservationV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=_NOW,
        status=DurableLoopRunStatus.COMPLETED,
        result_available=True,
        committed_generation=2,
    )

    await _publish(journal, terminal)
    late = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(_text(producer=producer),),
        ),
        deadline=_deadline(),
    )
    duplicate_terminal = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(terminal,),
        ),
        deadline=_deadline(),
    )
    replay = await journal.replay(run_id="run-1", after_sequence=0, limit=128)

    assert late.disposition.value == "stale_producer"
    assert duplicate_terminal.disposition.value == "stale_producer"
    assert replay.snapshot is None
    assert len(replay.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "result_available", "draft_survives"),
    [
        pytest.param(
            DurableLoopRunStatus.FAILED,
            False,
            False,
            id="failed",
        ),
        pytest.param(
            DurableLoopRunStatus.CANCELLED,
            False,
            False,
            id="cancelled",
        ),
        pytest.param(
            DurableLoopRunStatus.COMPLETED,
            True,
            True,
            id="completed",
        ),
    ],
)
async def test_authoritative_terminal_reconciles_partial_drafts(
    status: DurableLoopRunStatus,
    result_available: bool,
    draft_survives: bool,
) -> None:
    journal = _journal()
    producer = (
        await journal.reserve_model_producers(
            run_id="run-1",
            step_index=0,
            count=1,
            deadline=_deadline(),
        )
    )[0]
    await _publish(
        journal,
        _model_attempt(producer, DurableChatModelAttemptState.STARTED),
        _text(producer=producer, delta="partial response"),
    )
    await _publish(journal, *(_progress(index) for index in range(64)))
    await _publish(journal, *(_progress(index) for index in range(64, 126)))
    await _publish(
        journal,
        DurableChatTerminalObservationV1(
            run_id="run-1",
            session_id="session-1",
            observed_at=_NOW,
            status=status,
            result_available=result_available,
            committed_generation=2,
            error_code="stream_timed_out"
            if status is DurableLoopRunStatus.FAILED
            else None,
        ),
    )

    replay = await journal.replay(run_id="run-1", after_sequence=0, limit=128)

    assert replay.snapshot is not None
    projection = replay.snapshot.projection
    assert projection.progress.status is status
    assert (projection.draft is not None) is draft_survives
    if projection.draft is not None:
        assert projection.draft.text == "partial response"


@pytest.mark.asyncio
async def test_failed_unpublished_content_remains_in_reserved_quota_and_degrades() -> None:
    class FailingContent:
        async def put_bytes(self, **_kwargs: object) -> ContentRefV1:
            raise RuntimeError("storage is unavailable")

        async def get_bytes(self, _reference: ContentRefV1) -> bytes:
            raise RuntimeError("storage is unavailable")

    journal = DurableChatJournal(
        content=FailingContent(),  # type: ignore[arg-type]
        documents=InMemoryDurableKeyedDocumentStore(),
    )
    await journal.create_run_initialization_once(initialization=_initialization())
    result = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(_progress(1),),
        ),
        deadline=_deadline(),
    )
    diagnostics = await journal.read_diagnostics(run_id="run-1")

    assert result.disposition.value == "degraded"
    assert diagnostics is not None
    assert diagnostics.observation_health.reserved_observation_bytes > 0
    assert (
        DurableChatObservationDegradationReason.STORAGE_UNAVAILABLE
        in diagnostics.observation_health.reasons
    )


@pytest.mark.asyncio
async def test_elapsed_storage_deadline_is_bounded_and_returns_degraded() -> None:
    journal = _journal()

    result = await journal.publish(
        run_id="run-1",
        expected_published_revision=0,
        batch=DurableChatObservationBatchV1(
            run_id="run-1",
            observations=(_progress(1),),
        ),
        deadline=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert result.disposition.value == "degraded"
    assert (
        DurableChatObservationDegradationReason.PUBLISH_TIMEOUT
        in result.health.reasons
    )
