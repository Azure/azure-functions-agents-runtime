from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from azure_functions_agents.experimental.durable_loop_protocol import (
    DURABLE_LOOP_SCHEMA_VERSION,
    CheckpointStateV1,
    ContentRefV1,
    DurableLoopBudgetV1,
    DurableLoopProtocolDocumentError,
    DurableLoopRunStatus,
    DurableRunIdentityV1,
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
    deterministic_call_key,
    deterministic_human_event_name,
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
