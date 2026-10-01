from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from azure_functions_agents.experimental.durable_loop_protocol import (
    DURABLE_LOOP_ORCHESTRATOR_V4_NAME,
    DURABLE_LOOP_SCHEMA_VERSION,
    CheckpointStateV1,
    CheckpointStateV2,
    ContentRefV1,
    DurableLoopBudgetV1,
    DurableLoopBudgetV2,
    DurableLoopPlanDocumentV2,
    DurableLoopProtocolDocumentError,
    DurableLoopRunStatus,
    DurablePublicEventType,
    DurablePublicEventV2,
    DurableResourceExpiryV1,
    DurableRetentionPolicyV1,
    DurableRunIdentityV1,
    DurableRunIdentityV2,
    DurableRunResourceHeaderV1,
    DurableSkillCatalogSnapshotV1,
    DurableSkillContentFileV1,
    DurableSkillContentV1,
    DurableSkillLoadPayloadV1,
    DurableSkillLoadReceiptV1,
    DurableSkillMetadataV1,
    DurableTriggerAdmissionRecordV1,
    DurableTriggerAdmissionState,
    DurableTriggerType,
    ErrorDisposition,
    ErrorEnvelopeV1,
    FrozenToolCatalogV1,
    FrozenToolDescriptorV1,
    MAFMessageBundleV1,
    ModelDecisionEnvelopeV1,
    ModelOperationStatus,
    ToolBehavior,
    ToolProvenance,
    WorkingContextV1,
    canonical_hash,
    deterministic_call_key,
    deterministic_human_event_name,
    deterministic_skill_operation_key,
    deterministic_trigger_record_key,
    parse_durable_loop_document,
    validate_human_response_schema,
    validate_human_response_value,
)

_HASH = "a" * 64


def _budget() -> DurableLoopBudgetV1:
    return DurableLoopBudgetV1(
        max_model_steps=48,
        max_tool_calls=128,
        max_elapsed_seconds=14400,
        max_human_waits=8,
        human_wait_seconds=86400,
        max_argument_bytes=262144,
        max_result_bytes=1048576,
        context_max_bytes=4194304,
        context_compaction_percent=75,
        max_parallel_reads=4,
        continue_as_new_checkpoints=20,
    )


def _identity() -> DurableRunIdentityV1:
    now = datetime(2026, 9, 4, tzinfo=UTC)
    return DurableRunIdentityV1(
        run_id="run-1",
        session_id="session-1",
        request_id_hash=_HASH,
        request_hash="b" * 64,
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash="e" * 64,
        deployment_hash="f" * 64,
        tool_package_hash="1" * 64,
        policy_hash="2" * 64,
        orchestration_version="durable_agent_turn_orchestrator_v1",
        created_at=now,
        active_deadline=now + timedelta(hours=4),
        absolute_deadline=now + timedelta(days=7),
        budget=_budget(),
    )


def _context() -> WorkingContextV1:
    messages = (
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "reasoning",
                    "encrypted_content": "encrypted-reasoning-token",
                    "additional_properties": {"provider": "openai"},
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": {"region": "westus3"},
                },
            ],
        },
        {
            "role": "tool",
            "contents": [
                {
                    "type": "function_result",
                    "call_id": "call-1",
                    "name": "lookup",
                    "result": {"ok": True},
                }
            ],
        },
    )
    bundle = MAFMessageBundleV1.create(
            messages=messages,
            maf_core_version="1.17.0",
            provider="openai",
            model="reasoning-model",
            api_version="responses-v1",
        )
    return WorkingContextV1(
        bundle=bundle,
        compaction_generation=0,
        source_audit_hash=bundle.bundle_hash,
        source_start=0,
        source_end=2,
        estimated_tokens=32,
    )


def test_message_bundle_round_trip_preserves_reasoning_and_call_fields() -> None:
    context = _context()

    payload = context.model_dump_json()
    parsed = WorkingContextV1.model_validate_json(payload)

    assert parsed == context
    reasoning = parsed.bundle.messages[0]["contents"][0]  # type: ignore[index]
    assert reasoning["encrypted_content"] == "encrypted-reasoning-token"  # type: ignore[index]
    assert parsed.bundle.bundle_hash == context.bundle.bundle_hash


def test_strict_protocol_rejects_extra_and_duplicate_fields() -> None:
    ref = ContentRefV1(
        object_id="content/final/abc",
        sha256=_HASH,
        byte_length=3,
        media_type="text/plain",
        encryption_version="v1",
        retention_class="result",
    )
    payload = ref.model_dump(mode="json")
    payload["extra"] = True

    with pytest.raises(DurableLoopProtocolDocumentError):
        parse_durable_loop_document(json.dumps(payload), ContentRefV1)
    with pytest.raises(DurableLoopProtocolDocumentError):
        parse_durable_loop_document(
            '{"schema_version":"1","object_id":"a","object_id":"b"}',
            ContentRefV1,
        )


def test_content_reference_rejects_authorization_material() -> None:
    with pytest.raises(ValidationError):
        ContentRefV1(
            object_id="https://storage.test/blob?sig=secret",
            sha256=_HASH,
            byte_length=1,
            media_type="text/plain",
            encryption_version="v1",
            retention_class="run",
        )


def test_deterministic_keys_bind_order_and_request_content() -> None:
    first = deterministic_call_key(
        run_id="run-1",
        step_index=2,
        call_ordinal=0,
        decision_hash=_HASH,
        tool_name="lookup",
    )
    repeated = deterministic_call_key(
        run_id="run-1",
        step_index=2,
        call_ordinal=0,
        decision_hash=_HASH,
        tool_name="lookup",
    )
    second = deterministic_call_key(
        run_id="run-1",
        step_index=2,
        call_ordinal=1,
        decision_hash=_HASH,
        tool_name="lookup",
    )

    assert first == repeated
    assert first != second
    assert deterministic_human_event_name(
        generation=1,
        request_id="human-1",
        nonce="nonce-a",
    ) != deterministic_human_event_name(
        generation=1,
        request_id="human-1",
        nonce="nonce-b",
    )


def test_catalog_rejects_reserved_tool_collision_and_hash_drift() -> None:
    with pytest.raises(ValidationError):
        FrozenToolDescriptorV1(
            name="request_human_input",
            description="collision",
            parameters={"type": "object"},
            provenance=ToolProvenance.LOCAL,
            behavior=ToolBehavior.READ_ONLY,
        )

    descriptor = FrozenToolDescriptorV1(
        name="lookup",
        description="lookup",
        parameters={"additionalProperties": False, "type": "object"},
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.READ_ONLY,
        parallel_safe=True,
    )
    catalog = FrozenToolCatalogV1.create(
        tools=(descriptor,),
        policy_hash=_HASH,
        package_hash="b" * 64,
    )
    with pytest.raises(ValidationError):
        catalog.model_copy(update={"catalog_hash": "c" * 64}).model_validate(
            {**catalog.model_dump(), "catalog_hash": "c" * 64}
        )


def test_checkpoint_exposes_six_statuses_and_ambiguous_disposition() -> None:
    assert {status.value for status in DurableLoopRunStatus} == {
        "Pending",
        "Running",
        "Waiting",
        "Completed",
        "Failed",
        "Cancelled",
    }
    error = ErrorEnvelopeV1(
        code="write_ack_lost",
        classification="tool",
        retryable=False,
        disposition=ErrorDisposition.AMBIGUOUS,
        possibly_committed=True,
        phase="tool_step",
    )
    checkpoint = CheckpointStateV1(
        identity=_identity(),
        status=DurableLoopRunStatus.FAILED,
        audit_bundle=_context().bundle,
        working_context=_context(),
        audit_head_hash=_context().bundle.bundle_hash,
        completed_model_steps=1,
        completed_tool_calls=1,
        human_wait_count=0,
        next_model_step=1,
        committed_session_generation=0,
        continue_as_new_generation=0,
        checkpoints_in_generation=1,
        last_error=error,
    )

    assert checkpoint.last_error is not None
    assert checkpoint.last_error.disposition is ErrorDisposition.AMBIGUOUS
    assert checkpoint.schema_version == DURABLE_LOOP_SCHEMA_VERSION


def test_model_protocol_rejects_blank_final_and_supports_incomplete_status() -> None:
    assert ModelOperationStatus.INCOMPLETE.value == "incomplete"
    with pytest.raises(ValidationError, match="must not be blank"):
        ModelDecisionEnvelopeV1(
            run_id="run-1",
            step_index=0,
            model_call_key=_HASH,
            deployment_hash="b" * 64,
            assistant_message={
                "role": "assistant",
                "contents": [{"type": "text", "text": ""}],
            },
            final_text=" \n ",
        )


def test_human_response_schema_is_local_bounded_and_non_recursive() -> None:
    schema = {
        "additionalProperties": False,
        "properties": {
            "region": {
                "enum": ["eastus2", "westus3"],
                "type": "string",
            }
        },
        "required": ["region"],
        "type": "object",
    }
    validate_human_response_schema(schema)
    validate_human_response_value(schema, {"region": "eastus2"})
    with pytest.raises(ValueError, match="does not match"):
        validate_human_response_value(schema, {"region": "invalid"})
    with pytest.raises(ValueError, match="unsupported keywords"):
        validate_human_response_schema(
            {"$ref": "https://attacker.invalid/schema.json"}
        )
    with pytest.raises(ValueError, match="unsupported keywords"):
        validate_human_response_schema({"pattern": "(a+)+$"})


@pytest.mark.parametrize(
    "schema",
    [
        {
            "properties": {
                "value": {"$dynamicRef": "https://attacker.invalid/schema"}
            },
            "type": "object",
        },
        {"items": {"pattern": "(a+)+$", "type": "string"}, "type": "array"},
        {"patternProperties": {".*": {"type": "string"}}, "type": "object"},
    ],
)
def test_human_response_schema_rejects_nested_remote_or_regex_keywords(
    schema: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="unsupported keywords"):
        validate_human_response_schema(schema)


def test_human_response_schema_rejects_excessive_depth() -> None:
    schema: dict[str, object] = {"type": "string"}
    for _ in range(10):
        schema = {"items": schema, "type": "array"}

    with pytest.raises(ValueError, match="depth limit"):
        validate_human_response_schema(schema)


def test_v4_constant_and_skill_keys_do_not_change_v1_key_contract() -> None:
    legacy = deterministic_call_key(
        run_id="run-1",
        step_index=2,
        call_ordinal=0,
        decision_hash=_HASH,
        tool_name="lookup",
    )
    skill = deterministic_skill_operation_key(
        run_id="run-1",
        step_index=2,
        call_ordinal=0,
        plan_version="2",
        catalog_hash="b" * 64,
        operation_kind="load_skill",
        arguments={"skill_id": "triage"},
        expected_version="v1",
        expected_content_hash="c" * 64,
    )

    assert DURABLE_LOOP_SCHEMA_VERSION == "1"
    assert DURABLE_LOOP_ORCHESTRATOR_V4_NAME == "durable_agent_turn_orchestrator_v4"
    assert legacy == "51cc71c304360f54e0a23f68cc0b2f67d33ce115d9ff10587d3a69e0f2354a4d"
    assert skill != legacy


def test_skill_catalog_and_content_are_strict_and_hash_bound() -> None:
    metadata = DurableSkillMetadataV1(
        skill_id="triage",
        display_name="Incident triage",
        selection_description="Select for incident diagnosis.",
        version="v1",
        content_hash="b" * 64,
        tags=("incident",),
    )
    retain_until = datetime(2026, 12, 1, tzinfo=UTC)
    snapshot = DurableSkillCatalogSnapshotV1.create(
        provider_id="filesystem",
        catalog_revision="revision-1",
        metadata=(metadata,),
        snapshot_token="snapshot-1",
        retain_until=retain_until,
    )
    files = (
        DurableSkillContentFileV1(
            relative_path="SKILL.md",
            content="# Triage\nFollow the runbook.",
        ),
        DurableSkillContentFileV1(
            relative_path="references/checklist.txt",
            content="Check health.",
        ),
    )
    content = DurableSkillContentV1.create(
        skill_id="triage",
        version="v1",
        files=files,
    )

    assert snapshot.catalog_hash == canonical_hash(
        [metadata.model_dump(mode="json")]
    )
    assert content.files[0].relative_path == "SKILL.md"
    with pytest.raises(ValidationError, match="extra"):
        DurableSkillMetadataV1.model_validate(
            {**metadata.model_dump(), "provider_id": "secret-provider"}
        )
    with pytest.raises(ValidationError, match="canonical path order"):
        DurableSkillContentV1.create(
            skill_id="triage",
            version="v1",
            files=(
                files[0],
                files[1],
                files[1].model_copy(update={"relative_path": "references/a.txt"}),
            ),
        )
    with pytest.raises(ValidationError, match="catalog hash mismatch"):
        DurableSkillCatalogSnapshotV1.model_validate(
            {**snapshot.model_dump(), "catalog_hash": "c" * 64}
        )


def test_v2_checkpoint_binds_skill_catalog_receipts_and_counters() -> None:
    budget = DurableLoopBudgetV2(
        **_budget().model_dump(exclude={"schema_version"}),
    )
    identity = DurableRunIdentityV2(
        **_identity().model_dump(exclude={"schema_version", "budget", "orchestration_version"}),
        orchestration_version=DURABLE_LOOP_ORCHESTRATOR_V4_NAME,
        budget=budget,
        skill_catalog_hash="3" * 64,
        access_namespace_hash="4" * 64,
        retention_policy=DurableRetentionPolicyV1(),
    )
    catalog_ref = ContentRefV1(
        object_id="content/skill-catalog/abc",
        sha256="5" * 64,
        byte_length=100,
        media_type="application/json",
        encryption_version="v1",
        retention_class="skill_catalog",
    )
    skill_ref = ContentRefV1(
        object_id="content/skill/triage",
        sha256="6" * 64,
        byte_length=100,
        media_type="application/json",
        encryption_version="v1",
        retention_class="skill",
    )
    receipt = DurableSkillLoadReceiptV1(
        run_id=identity.run_id,
        operation_key="7" * 64,
        stable_step_id="step-1",
        skill_id="triage",
        provider_id="filesystem",
        version="v1",
        catalog_revision="revision-1",
        catalog_hash=identity.skill_catalog_hash,
        content_hash="8" * 64,
        content_ref=skill_ref,
        loaded_at=identity.created_at,
    )
    checkpoint = CheckpointStateV2(
        identity=identity,
        status=DurableLoopRunStatus.RUNNING,
        audit_bundle=_context().bundle,
        working_context=_context(),
        audit_head_hash=_context().bundle.bundle_hash,
        completed_model_steps=1,
        completed_tool_calls=0,
        human_wait_count=0,
        next_model_step=1,
        committed_session_generation=0,
        continue_as_new_generation=0,
        checkpoints_in_generation=1,
        skill_catalog_ref=catalog_ref,
        skill_catalog_hash=identity.skill_catalog_hash,
        completed_skill_searches=1,
        completed_skill_loads=1,
        loaded_skill_receipts=(receipt,),
        loaded_skill_bytes=100,
    )
    descriptor = FrozenToolDescriptorV1(
        name="lookup",
        description="lookup",
        parameters={"additionalProperties": False, "type": "object"},
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.READ_ONLY,
    )
    plan = DurableLoopPlanDocumentV2(
        instructions="Root instructions.",
        catalog=FrozenToolCatalogV1.create(
            tools=(descriptor,),
            policy_hash=_HASH,
            package_hash="b" * 64,
        ),
        model_settings={},
        maf_core_version="1.17.0",
        provider="openai",
        model="model",
        api_version="responses-v1",
        settings={},
        skill_catalog_ref=catalog_ref,
        skill_catalog_hash=identity.skill_catalog_hash,
        retention_policy=identity.retention_policy,
    )

    assert checkpoint.schema_version == "2"
    assert checkpoint.loaded_skill_receipts == (receipt,)
    assert plan.schema_version == "2"
    assert "Follow the runbook" not in checkpoint.model_dump_json()
    with pytest.raises(ValidationError, match="cannot trail"):
        checkpoint.model_copy(update={"completed_skill_loads": 0}).__class__.model_validate(
            {**checkpoint.model_dump(), "completed_skill_loads": 0}
        )


def test_public_event_payloads_are_event_specific_and_provider_free() -> None:
    payload = DurableSkillLoadPayloadV1(
        operation_key=_HASH,
        skill_id="triage",
        version="v1",
    )
    event = DurablePublicEventV2(
        run_id="run-1",
        sequence=1,
        timestamp=datetime(2026, 9, 29, tzinfo=UTC),
        type=DurablePublicEventType.SKILL_LOAD_STARTED,
        payload=payload,
    )

    assert event.schema_version == "2"
    assert "provider" not in event.model_dump_json()
    with pytest.raises(ValidationError, match="extra"):
        DurableSkillLoadPayloadV1.model_validate(
            {**payload.model_dump(), "provider_id": "filesystem"}
        )
    with pytest.raises(ValidationError, match="does not match"):
        DurablePublicEventV2(
            run_id="run-1",
            sequence=2,
            timestamp=datetime(2026, 9, 29, tzinfo=UTC),
            type=DurablePublicEventType.RUN_STARTED,
            payload=payload,
        )


def test_retention_resource_header_requires_ordered_explicit_expiry() -> None:
    created_at = datetime(2026, 9, 29, tzinfo=UTC)
    terminal_at = created_at + timedelta(hours=1)
    result_expiry = terminal_at + timedelta(days=30)
    receipt_expiry = terminal_at + timedelta(days=90)
    expiry = DurableResourceExpiryV1(
        events_expires_at=result_expiry,
        result_expires_at=result_expiry,
        human_content_expires_at=result_expiry,
        receipts_expire_at=receipt_expiry,
        skills_expire_at=terminal_at + timedelta(days=60),
        tombstone_expires_at=receipt_expiry,
        idempotency_expires_at=receipt_expiry,
    )
    header = DurableRunResourceHeaderV1(
        run_id="run-1",
        session_id="session-1",
        owner_hash=_HASH,
        access_namespace_hash="b" * 64,
        request_id_hash="c" * 64,
        status=DurableLoopRunStatus.COMPLETED,
        retention_policy=DurableRetentionPolicyV1(),
        created_at=created_at,
        updated_at=terminal_at,
        active_deadline=created_at + timedelta(hours=4),
        terminal_at=terminal_at,
        terminal_projection_ref=ContentRefV1(
            object_id="content/result/projection",
            sha256="d" * 64,
            byte_length=100,
            media_type="application/json",
            encryption_version="v1",
            retention_class="result",
        ),
        expiry=expiry,
        record_version=1,
    )

    assert header.expiry == expiry
    with pytest.raises(ValidationError, match="receipt retention"):
        DurableRetentionPolicyV1(
            event_result_seconds=7200,
            receipt_seconds=3600,
        )
    with pytest.raises(ValidationError, match="idempotency expiry"):
        DurableResourceExpiryV1.model_validate(
            {
                **expiry.model_dump(),
                "idempotency_expires_at": receipt_expiry - timedelta(seconds=1),
            }
        )


def test_trigger_admission_record_is_refs_only_and_key_bound() -> None:
    record_key = deterministic_trigger_record_key(
        owner_hash=_HASH,
        trigger_registration="timer-1",
        stable_event_id_hash="b" * 64,
        normalized_body_hash="c" * 64,
    )
    content_ref = ContentRefV1(
        object_id="content/trigger/payload",
        sha256="d" * 64,
        byte_length=10,
        media_type="application/json",
        encryption_version="v1",
        retention_class="trigger_pending",
    )
    record = DurableTriggerAdmissionRecordV1(
        record_key=record_key,
        owner_hash=_HASH,
        access_namespace_hash="e" * 64,
        initiator_hash="f" * 64,
        agent_slug="main",
        trigger_type=DurableTriggerType.TIMER,
        trigger_registration="timer-1",
        stable_event_id_hash="b" * 64,
        normalized_body_hash="c" * 64,
        request_hash="1" * 64,
        session_id="session-1",
        run_id="run-1",
        payload_ref=content_ref,
        prompt_ref=content_ref.model_copy(update={"object_id": "content/trigger/prompt"}),
        state=DurableTriggerAdmissionState.PENDING,
        staged_at=datetime(2026, 9, 29, tzinfo=UTC),
        admission_deadline=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert "payload_ref" in record.model_dump()
    assert "payload" not in record.model_dump()
    with pytest.raises(ValidationError, match="record key mismatch"):
        DurableTriggerAdmissionRecordV1.model_validate(
            {**record.model_dump(), "record_key": "2" * 64}
        )
