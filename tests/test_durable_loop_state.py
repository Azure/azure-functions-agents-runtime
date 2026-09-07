from __future__ import annotations

from datetime import UTC, datetime

import pytest

from azure_functions_agents.experimental.durable_loop import (
    DurableLoopPlan,
    create_run_identity,
)
from azure_functions_agents.experimental.durable_loop_activities import (
    InMemoryDurableContentStore,
)
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_ENABLED_ENV,
    DurableLoopSettings,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    CheckpointStateV1,
    DurableLoopRunStatus,
    HumanInputRequestV1,
    HumanInputResponseV1,
    HumanInputState,
    HumanResponseDisposition,
    MAFMessageBundleV1,
    WorkingContextV1,
)
from azure_functions_agents.experimental.durable_loop_state import (
    DurableLoopHumanInputConflictError,
    DurableLoopIdempotencyConflictError,
    DurableLoopMixedModeError,
    DurableLoopSessionBusyError,
    InMemoryDurableLoopStateStore,
)
from azure_functions_agents.experimental.durable_loop_tools import (
    DurableToolRegistry,
)

_HASH = "a" * 64


def _settings() -> DurableLoopSettings:
    settings = DurableLoopSettings.from_environment({DURABLE_LOOP_ENABLED_ENV: "true"})
    assert settings is not None
    return settings


def _identity(
    *,
    run_id: str = "run-1",
    session_id: str = "session-1",
    request_id: str = "request-1",
    body: object = "hello",
):
    catalog = DurableToolRegistry().catalog(
        policy_hash=_HASH,
        package_hash="b" * 64,
    )
    return create_run_identity(
        run_id=run_id,
        session_id=session_id,
        request_id=request_id,
        request_body=body,
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash=catalog.catalog_hash,
        deployment_hash="e" * 64,
        tool_package_hash=catalog.package_hash,
        policy_hash=catalog.policy_hash,
        settings=_settings(),
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )


def _checkpoint(identity) -> CheckpointStateV1:
    messages = ({"role": "user", "contents": [{"type": "text", "text": "hi"}]},)
    bundle = MAFMessageBundleV1.create(
        messages=messages,
        maf_core_version="1.17.0",
        provider="openai",
        model="model",
        api_version="responses-v1",
    )
    return CheckpointStateV1(
        identity=identity,
        status=DurableLoopRunStatus.PENDING,
        audit_bundle=bundle,
        working_context=WorkingContextV1(
            bundle=bundle,
            compaction_generation=0,
            source_audit_hash=bundle.bundle_hash,
            source_start=0,
            source_end=1,
            estimated_tokens=4,
        ),
        audit_head_hash=bundle.bundle_hash,
        completed_model_steps=0,
        completed_tool_calls=0,
        human_wait_count=0,
        next_model_step=0,
        committed_session_generation=0,
        continue_as_new_generation=0,
        checkpoints_in_generation=0,
    )


@pytest.mark.asyncio
async def test_admission_deduplicates_and_rejects_conflicts_and_busy_session() -> None:
    store = InMemoryDurableLoopStateStore()
    first = _identity()

    assert not (await store.admit(first, _checkpoint(first))).replayed
    assert (await store.admit(first, _checkpoint(first))).replayed

    changed = _identity(run_id="run-2", body="different")
    with pytest.raises(DurableLoopIdempotencyConflictError):
        await store.admit(changed, _checkpoint(changed))

    other = _identity(run_id="run-3", request_id="request-2")
    with pytest.raises(DurableLoopSessionBusyError):
        await store.admit(other, _checkpoint(other))


@pytest.mark.asyncio
async def test_session_mode_rejects_durable_legacy_mix() -> None:
    store = InMemoryDurableLoopStateStore()
    await store.claim_session_mode("session-1", "legacy")
    identity = _identity()

    with pytest.raises(DurableLoopMixedModeError):
        await store.admit(identity, _checkpoint(identity))


@pytest.mark.asyncio
async def test_human_answer_first_writer_wins_and_duplicate_is_idempotent() -> None:
    store = InMemoryDurableLoopStateStore()
    content = InMemoryDurableContentStore()
    identity = _identity()
    checkpoint = _checkpoint(identity)
    await store.admit(identity, checkpoint)
    question_ref = await content.put_text(
        kind="question",
        value="Which region?",
        retention_class="run",
    )
    answer_ref = await content.put_text(
        kind="answer",
        value="westus3",
        retention_class="run",
    )
    request = HumanInputRequestV1(
        request_id="human-1",
        run_id=identity.run_id,
        session_id=identity.session_id,
        generation=1,
        turn_index=0,
        step_index=0,
        call_id="call-1",
        call_key=_HASH,
        request_hash="b" * 64,
        question_ref=question_ref,
        choices=("westus3",),
        allow_free_text=False,
        actor_policy_hash=identity.owner_hash,
        event_name="answer:1:event",
        issued_at=identity.created_at,
        expires_at=identity.active_deadline,
        record_version=1,
    )
    await store.put_human_request(request)
    response = HumanInputResponseV1(
        request_id=request.request_id,
        run_id=request.run_id,
        session_id=request.session_id,
        generation=request.generation,
        call_id=request.call_id,
        submission_id_hash="c" * 64,
        body_hash="d" * 64,
        actor_hash=identity.owner_hash,
        answer_ref=answer_ref,
        accepted_at=identity.created_at,
        schema_valid=True,
        disposition=HumanResponseDisposition.ACCEPTED,
    )

    assert not (await store.accept_human_response(response)).replayed
    assert (await store.accept_human_response(response)).replayed
    with pytest.raises(DurableLoopHumanInputConflictError):
        await store.accept_human_response(
            response.model_copy(update={"body_hash": "e" * 64})
        )
    closed = await store.close_human_request(
        identity.run_id,
        request.request_id,
        HumanInputState.TIMED_OUT,
    )
    assert closed.state is HumanInputState.ANSWERED


def test_plan_type_remains_independent_of_public_schema() -> None:
    catalog = DurableToolRegistry().catalog(
        policy_hash=_HASH,
        package_hash="b" * 64,
    )
    plan = DurableLoopPlan(
        instructions="help",
        catalog=catalog,
        model_settings={},
        maf_core_version="1.17.0",
        provider="fake",
        model="fake",
        api_version="test",
        settings=_settings(),
    )

    plan.validate()
