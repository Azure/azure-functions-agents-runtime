import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from azure_functions_agents.harness._checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    MAX_CHECKPOINT_OUTCOME_BYTES,
    MAX_EXTERNAL_WORK_ENVELOPE_BYTES,
    MAX_EXTERNAL_WORK_RESULT_BATCH_BYTES,
    MAX_REFERENCE_ENVELOPE_BYTES,
    CancelledToolEvidence,
    CheckpointContractError,
    CheckpointFailure,
    CheckpointFailureCategory,
    CheckpointRequest,
    CheckpointUsage,
    ConnectorIdentityKey,
    ContentRef,
    ContinuationRef,
    ContinuationRole,
    ExternalWorkCommandIdentity,
    ExternalWorkOperationIdentity,
    ExternalWorkPendingOperation,
    ExternalWorkPlacement,
    ExternalWorkRequest,
    ExternalWorkRequested,
    ExternalWorkResult,
    ExternalWorkResultBatch,
    ExternalWorkResultIdentity,
    ExternalWorkResumeRequest,
    ExternalWorkStatus,
    FailedToolEvidence,
    HumanInputRequested,
    IntegrityMetadata,
    RequestedToolEvidence,
    RetryableCheckpointFailure,
    RetryGuidance,
    RetryRequested,
    SessionIdentityKey,
    StorageKind,
    SucceededToolEvidence,
    ToolEvidenceStatus,
    TurnCompleted,
    TurnFailed,
    TurnIdentityKey,
    UnknownToolEvidence,
    decode_checkpoint_outcome,
    decode_content_ref,
    decode_continuation_ref,
    decode_external_work_request,
    decode_external_work_result,
    decode_external_work_result_batch,
    decode_external_work_resume_request,
    encode_checkpoint_outcome,
    encode_content_ref,
    encode_continuation_ref,
    encode_external_work_request,
    encode_external_work_result,
    encode_external_work_result_batch,
    encode_external_work_resume_request,
)
from azure_functions_agents.harness._harness_binding import HarnessKind

_DIGEST = "a" * 64
_OWNER_DIGEST = "b" * 64


def _now() -> datetime:
    return datetime(2026, 10, 23, 20, 0, tzinfo=UTC)


def _content_ref(key: str = "durable/v1/scope/content/value") -> ContentRef:
    created = _now()
    return ContentRef(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        storage_kind=StorageKind.BLOB,
        opaque_content_key=key,
        content_digest=_DIGEST,
        content_length=7,
        content_type="application/json",
        created_at=created,
        expires_at=created + timedelta(days=1),
        integrity_metadata=IntegrityMetadata(digest=_DIGEST, content_length=7),
    )


def _continuation(role: ContinuationRole) -> ContinuationRef:
    created = _now()
    return ContinuationRef(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        harness_kind=HarnessKind.MAF,
        connector_version="1",
        connector_profile_id="maf-test",
        backend_namespace="hub-a",
        agent_slug="agent",
        owner_scope_digest=_OWNER_DIGEST,
        entry_id="entry",
        session_id="session",
        session_incarnation_id="incarnation",
        run_id="run",
        turn_generation=3,
        storage_kind=StorageKind.BLOB,
        opaque_state_key="durable/v1/scope/continuation/value",
        state_format_version="1",
        continuation_role=role,
        created_at=created,
        expires_at=created + timedelta(days=1),
        integrity_metadata=IntegrityMetadata(digest=_DIGEST, content_length=20),
    )


def _outcome_fields() -> dict[str, object]:
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "harness_kind": HarnessKind.MAF,
        "connector_version": "1",
        "connector_profile_id": "maf-test",
        "backend_namespace": "hub-a",
        "agent_slug": "agent",
        "owner_scope_digest": _OWNER_DIGEST,
        "entry_id": "entry",
        "session_id": "session",
        "session_incarnation_id": "incarnation",
        "run_id": "run",
        "turn_generation": 3,
        "deadline": _now() + timedelta(minutes=5),
        "provider_id": "foundry",
        "model_id": "model",
        "usage": CheckpointUsage(input_tokens=10, output_tokens=5),
    }


def _external_request() -> ExternalWorkRequest:
    return ExternalWorkRequest(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        sequence_index=0,
        operation_id="operation",
        tool_call_id="call",
        agent_slug="agent",
        owner_scope_digest=_OWNER_DIGEST,
        entry_id="entry",
        session_id="session",
        session_incarnation_id="incarnation",
        run_id="run",
        turn_generation=3,
        tool_name="lookup",
        arguments_ref=_content_ref(),
        adapter_kind="runtime_tool",
        adapter_version="1",
        canonical_schema_digest=_DIGEST,
        policy_digest=_DIGEST,
        execution_placement=ExternalWorkPlacement.RUNTIME,
        implementation_compatibility_version="1",
        policy_snapshot={"approval": "never"},
        idempotency_key="operation",
        deadline=_now() + timedelta(minutes=5),
    )


def _connector_identity_fields() -> dict[str, object]:
    return {
        key: value
        for key, value in _outcome_fields().items()
        if key not in {"schema_version", "deadline", "provider_id", "model_id", "usage"}
    }


def _external_result(**changes: object) -> ExternalWorkResult:
    request = _external_request()
    values: dict[str, object] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "sequence_index": request.sequence_index,
        "operation_id": request.operation_id,
        "tool_call_id": request.tool_call_id,
        "idempotency_key": request.idempotency_key,
        "agent_slug": request.agent_slug,
        "owner_scope_digest": request.owner_scope_digest,
        "entry_id": request.entry_id,
        "session_id": request.session_id,
        "session_incarnation_id": request.session_incarnation_id,
        "run_id": request.run_id,
        "turn_generation": request.turn_generation,
        "status": ExternalWorkStatus.SUCCEEDED,
        "result_ref": _content_ref(),
    }
    values.update(changes)
    return ExternalWorkResult(**values)


def _pending_operation(**changes: object) -> ExternalWorkPendingOperation:
    request = _external_request()
    values: dict[str, object] = {
        "sequence_index": request.sequence_index,
        "operation_id": request.operation_id,
        "tool_call_id": request.tool_call_id,
        "idempotency_key": request.idempotency_key,
        "agent_slug": request.agent_slug,
        "owner_scope_digest": request.owner_scope_digest,
        "entry_id": request.entry_id,
        "session_id": request.session_id,
        "session_incarnation_id": request.session_incarnation_id,
        "run_id": request.run_id,
        "turn_generation": request.turn_generation,
    }
    values.update(changes)
    return ExternalWorkPendingOperation(**values)


def _resume_request() -> ExternalWorkResumeRequest:
    return ExternalWorkResumeRequest(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        **_connector_identity_fields(),
        continuation=_continuation(ContinuationRole.MID_TURN),
        pending_operations=(_pending_operation(),),
        results=ExternalWorkResultBatch(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            results=(_external_result(),),
        ),
    )


@pytest.mark.parametrize(
    "outcome",
    [
        TurnCompleted(
            **_outcome_fields(),
            result_ref=_content_ref(),
            continuation=_continuation(ContinuationRole.TERMINAL_SEED),
        ),
        ExternalWorkRequested(
            **_outcome_fields(),
            requests=(_external_request(),),
            continuation=_continuation(ContinuationRole.MID_TURN),
        ),
        HumanInputRequested(
            **_outcome_fields(),
            question_id="question",
            question_ref=_content_ref(),
            input_schema={"type": "string"},
            continuation=_continuation(ContinuationRole.MID_TURN),
        ),
        RetryRequested(
            **_outcome_fields(),
            failure=RetryableCheckpointFailure(
                category=CheckpointFailureCategory.RETRYABLE_TRANSPORT,
                code="provider_timeout",
                message="The provider timed out.",
            ),
            guidance=RetryGuidance(retry_after_ms=1000, remaining_attempts=2),
        ),
        TurnFailed(
            **_outcome_fields(),
            failure=CheckpointFailure(
                category=CheckpointFailureCategory.CONTINUATION,
                code="continuation_incompatible",
                message="The continuation is incompatible.",
            ),
        ),
    ],
)
def test_checkpoint_outcomes_round_trip(outcome) -> None:
    assert decode_checkpoint_outcome(encode_checkpoint_outcome(outcome)) == outcome


def test_content_and_external_work_round_trip() -> None:
    content = _content_ref()
    continuation = _continuation(ContinuationRole.MID_TURN)
    request = _external_request()
    result = _external_result(
        result_ref=content,
        diagnostics={"receipt": "confirmed"},
    )

    assert decode_content_ref(encode_content_ref(content)) == content
    assert decode_continuation_ref(encode_continuation_ref(continuation)) == continuation
    assert decode_external_work_request(encode_external_work_request(request)) == request
    assert decode_external_work_result(encode_external_work_result(result)) == result
    batch = ExternalWorkResultBatch(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        results=(result,),
    )
    assert decode_external_work_result_batch(
        encode_external_work_result_batch(batch)
    ) == batch


def test_checkpoint_identity_keys_name_each_identity_boundary() -> None:
    request = _external_request()

    session = request.session_identity_key()
    turn = request.turn_identity_key()
    connector = _continuation(ContinuationRole.MID_TURN).connector_identity_key()

    assert session == SessionIdentityKey(
        agent_slug="agent",
        owner_scope_digest=_OWNER_DIGEST,
        entry_id="entry",
        session_id="session",
        session_incarnation_id="incarnation",
    )
    assert turn == TurnIdentityKey(
        session=session,
        run_id="run",
        turn_generation=3,
    )
    assert connector == ConnectorIdentityKey(
        harness_kind=HarnessKind.MAF,
        connector_version="1",
        connector_profile_id="maf-test",
        backend_namespace="hub-a",
        session=session,
    )
    assert request.operation_identity() == ExternalWorkOperationIdentity(
        sequence_index=0,
        operation_id="operation",
        tool_call_id="call",
        idempotency_key="operation",
    )
    assert request.command_identity() == ExternalWorkCommandIdentity(
        adapter_kind="runtime_tool",
        adapter_version="1",
        tool_name="lookup",
        canonical_schema_digest=_DIGEST,
        policy_digest=_DIGEST,
        execution_placement=ExternalWorkPlacement.RUNTIME,
        implementation_compatibility_version="1",
    )
    result = _external_result()
    assert result.result_identity() == ExternalWorkResultIdentity(
        sequence_index=0,
        operation_id="operation",
        tool_call_id="call",
        idempotency_key="operation",
    )


def test_checkpoint_json_fields_are_deeply_immutable() -> None:
    request = ExternalWorkRequest(
        **_external_request().model_dump(exclude={"policy_snapshot"}),
        policy_snapshot={"rules": [{"name": "approval"}]},
    )
    human_input = HumanInputRequested(
        **_outcome_fields(),
        question_id="question",
        question_ref=_content_ref(),
        input_schema={"properties": {"answer": {"type": "string"}}},
        continuation=_continuation(ContinuationRole.MID_TURN),
    )
    result = _external_result(
        diagnostics={"receipt": "confirmed"},
    )

    with pytest.raises(TypeError):
        request.policy_snapshot["new"] = True
    with pytest.raises(TypeError):
        request.policy_snapshot["rules"][0]["name"] = "changed"
    with pytest.raises(TypeError):
        human_input.input_schema["properties"]["answer"]["type"] = "number"
    with pytest.raises(TypeError):
        result.diagnostics["receipt"] = "changed"


def test_external_work_request_batch_requires_ordered_unique_identities() -> None:
    request = _external_request()
    duplicate = request.model_copy(update={"sequence_index": 1})

    with pytest.raises(ValidationError, match="identities must be unique"):
        ExternalWorkRequested(
            **_outcome_fields(),
            requests=(request, duplicate),
            continuation=_continuation(ContinuationRole.MID_TURN),
        )

    with pytest.raises(ValidationError, match="original tool-call order"):
        ExternalWorkRequested(
            **_outcome_fields(),
            requests=(request.model_copy(update={"sequence_index": 1}),),
            continuation=_continuation(ContinuationRole.MID_TURN),
        )


def test_external_work_result_batch_requires_ordered_unique_identities() -> None:
    result = _external_result()

    with pytest.raises(ValidationError, match="identities must be unique"):
        ExternalWorkResultBatch(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            results=(
                result,
                result.model_copy(
                    update={"sequence_index": 1, "tool_call_id": "different-call"}
                ),
            )
        )

    with pytest.raises(ValidationError, match="identities must be unique"):
        ExternalWorkResultBatch(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            results=(
                result,
                result.model_copy(
                    update={"sequence_index": 1, "operation_id": "different-operation"}
                ),
            )
        )

    with pytest.raises(ValidationError, match="original tool-call order"):
        ExternalWorkResultBatch(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            results=(result.model_copy(update={"sequence_index": 1}),)
        )

    with pytest.raises(ValidationError, match="identities must be unique"):
        ExternalWorkResultBatch(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            results=(
                result,
                result.model_copy(
                    update={
                        "sequence_index": 1,
                        "operation_id": "different-operation",
                        "tool_call_id": "different-call",
                    }
                ),
            ),
        )


def test_tool_evidence_variants_define_valid_result_shapes() -> None:
    content = _content_ref()

    assert RequestedToolEvidence(tool_call_id="call", tool_name="lookup").status is (
        ToolEvidenceStatus.REQUESTED
    )
    assert SucceededToolEvidence(
        tool_call_id="call",
        tool_name="lookup",
        result_ref=content,
    ).result_ref is content
    assert FailedToolEvidence(
        tool_call_id="call",
        tool_name="lookup",
        result_ref=content,
    ).result_ref is content
    assert CancelledToolEvidence(
        tool_call_id="call",
        tool_name="lookup",
        result_ref=content,
    ).status is ToolEvidenceStatus.CANCELLED
    assert UnknownToolEvidence(tool_call_id="call", tool_name="lookup").status is (
        ToolEvidenceStatus.OUTCOME_UNKNOWN
    )

    with pytest.raises(ValidationError, match="result_ref"):
        SucceededToolEvidence(tool_call_id="call", tool_name="lookup")
    with pytest.raises(ValidationError, match="result_ref"):
        CancelledToolEvidence(tool_call_id="call", tool_name="lookup")
    with pytest.raises(ValidationError, match="Extra inputs"):
        RequestedToolEvidence(
            tool_call_id="call",
            tool_name="lookup",
            result_ref=content,
        )


def test_checkpoint_failure_rejects_unknown_categories() -> None:
    with pytest.raises(ValidationError, match="category"):
        CheckpointFailure(
            category="made_up_policy",
            code="fixture",
            message="Fixture failure.",
        )


def test_retry_request_requires_safe_transport_failure_and_known_tool_effects() -> None:
    with pytest.raises(ValidationError, match="RETRYABLE_TRANSPORT"):
        RetryRequested(
            **_outcome_fields(),
            failure=RetryableCheckpointFailure(
                category=CheckpointFailureCategory.CANCELLATION,
                code="cancelled",
                message="The attempt was cancelled.",
            ),
            guidance=RetryGuidance(retry_after_ms=1000, remaining_attempts=2),
        )

    retryable_failure = RetryableCheckpointFailure(
        category=CheckpointFailureCategory.RETRYABLE_TRANSPORT,
        code="provider_timeout",
        message="The provider timed out before dispatch.",
    )
    for evidence in (
        RequestedToolEvidence(tool_call_id="call", tool_name="lookup"),
        UnknownToolEvidence(tool_call_id="call", tool_name="lookup"),
    ):
        with pytest.raises(ValidationError, match="pending or outcome-unknown"):
            RetryRequested(
                **_outcome_fields(),
                failure=retryable_failure,
                guidance=RetryGuidance(retry_after_ms=1000, remaining_attempts=2),
                tool_evidence=(evidence,),
            )

    retry = RetryRequested(
        **_outcome_fields(),
        failure=retryable_failure,
        guidance=RetryGuidance(retry_after_ms=1000, remaining_attempts=2),
        tool_evidence=(
            SucceededToolEvidence(
                tool_call_id="call",
                tool_name="lookup",
                result_ref=_content_ref(),
            ),
        ),
    )
    assert retry.failure.category is CheckpointFailureCategory.RETRYABLE_TRANSPORT


def test_external_work_resume_binds_continuation_pending_operations_and_results() -> None:
    request = _resume_request()

    assert (
        decode_external_work_resume_request(
            encode_external_work_resume_request(request)
        )
        == request
    )

    with pytest.raises(ValidationError, match="pending operation"):
        ExternalWorkResumeRequest(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            **_connector_identity_fields(),
            continuation=_continuation(ContinuationRole.MID_TURN),
            pending_operations=(_pending_operation(),),
            results=ExternalWorkResultBatch(
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                results=(_external_result(idempotency_key="different"),),
            ),
        )

    with pytest.raises(ValidationError, match="one active turn"):
        ExternalWorkResumeRequest(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            **_connector_identity_fields(),
            continuation=_continuation(ContinuationRole.MID_TURN),
            pending_operations=(_pending_operation(run_id="different"),),
            results=ExternalWorkResultBatch(
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                results=(_external_result(),),
            ),
        )

    with pytest.raises(ValidationError, match="requires a mid-turn continuation"):
        ExternalWorkResumeRequest(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            **_connector_identity_fields(),
            continuation=_continuation(ContinuationRole.TERMINAL_SEED),
            pending_operations=(_pending_operation(),),
            results=ExternalWorkResultBatch(
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                results=(_external_result(),),
            ),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"operation_id": "different"},
        {"tool_call_id": "different"},
        {"idempotency_key": "different"},
    ],
)
def test_external_work_resume_requires_exact_result_identity(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="pending operation"):
        ExternalWorkResumeRequest(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            **_connector_identity_fields(),
            continuation=_continuation(ContinuationRole.MID_TURN),
            pending_operations=(_pending_operation(),),
            results=ExternalWorkResultBatch(
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                results=(_external_result(**changes),),
            ),
        )


def test_external_work_resume_requires_ordered_complete_pending_set() -> None:
    with pytest.raises(ValidationError, match="original tool-call order"):
        ExternalWorkResumeRequest(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            **_connector_identity_fields(),
            continuation=_continuation(ContinuationRole.MID_TURN),
            pending_operations=(_pending_operation(sequence_index=1),),
            results=ExternalWorkResultBatch(
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                results=(_external_result(),),
            ),
        )

    with pytest.raises(ValidationError, match="one result per pending operation"):
        ExternalWorkResumeRequest(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            **_connector_identity_fields(),
            continuation=_continuation(ContinuationRole.MID_TURN),
            pending_operations=(
                _pending_operation(),
                _pending_operation(
                    sequence_index=1,
                    operation_id="operation-2",
                    tool_call_id="call-2",
                    idempotency_key="operation-2",
                ),
            ),
            results=ExternalWorkResultBatch(
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                results=(_external_result(),),
            ),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"harness_kind": HarnessKind.COPILOT},
        {"connector_version": "different"},
        {"connector_profile_id": "different"},
        {"backend_namespace": "different"},
        {"session_id": "different"},
        {"session_incarnation_id": "different"},
        {"run_id": "different"},
        {"turn_generation": 4},
    ],
)
def test_external_work_resume_rejects_mismatched_resume_identity(
    changes: dict[str, object],
) -> None:
    identity = _connector_identity_fields()
    identity.update(changes)

    with pytest.raises(ValidationError, match=r"resume identity|active turn"):
        ExternalWorkResumeRequest(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            **identity,
            continuation=_continuation(ContinuationRole.MID_TURN),
            pending_operations=(_pending_operation(),),
            results=ExternalWorkResultBatch(
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                results=(_external_result(),),
            ),
        )


def test_terminal_session_seed_keeps_its_prior_turn_identity() -> None:
    request_fields = {
        key: value
        for key, value in _outcome_fields().items()
        if key not in {"provider_id", "model_id", "usage"}
    }
    request_fields.update({"run_id": "next-run", "turn_generation": 4})

    request = CheckpointRequest(
        **request_fields,
        prompt_ref=_content_ref(),
        session_seed=_continuation(ContinuationRole.TERMINAL_SEED),
    )

    assert request.session_seed is not None
    assert request.session_seed.run_id == "run"
    assert request.session_seed.turn_generation == 3


def test_checkpoint_encoder_revalidates_copied_models() -> None:
    result = _external_result().model_copy(
        update={"diagnostics": {"oversized": "x" * 1025}}
    )

    with pytest.raises(CheckpointContractError, match="External work result is invalid"):
        encode_external_work_result(result)


def test_checkpoint_outcome_encoder_rejects_oversized_nested_request() -> None:
    oversized_request = _external_request().model_copy(
        update={"policy_snapshot": {"value": "x" * MAX_EXTERNAL_WORK_ENVELOPE_BYTES}}
    )
    outcome = ExternalWorkRequested(
        **_outcome_fields(),
        requests=(_external_request(),),
        continuation=_continuation(ContinuationRole.MID_TURN),
    ).model_copy(update={"requests": (oversized_request,)})

    with pytest.raises(CheckpointContractError, match="Checkpoint outcome is invalid"):
        encode_checkpoint_outcome(outcome)


def test_checkpoint_outcome_decoder_rejects_oversized_nested_request() -> None:
    oversized_request = _external_request().model_copy(
        update={"policy_snapshot": {"value": "x" * MAX_EXTERNAL_WORK_ENVELOPE_BYTES}}
    )
    outcome = ExternalWorkRequested(
        **_outcome_fields(),
        requests=(_external_request(),),
        continuation=_continuation(ContinuationRole.MID_TURN),
    ).model_copy(update={"requests": (oversized_request,)})
    payload = outcome.model_dump_json().encode()

    assert MAX_EXTERNAL_WORK_ENVELOPE_BYTES < len(payload) < MAX_CHECKPOINT_OUTCOME_BYTES
    with pytest.raises(CheckpointContractError, match="Checkpoint outcome is invalid"):
        decode_checkpoint_outcome(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_checkpoint_contracts_reject_non_finite_json_values(value: float) -> None:
    request = _external_request()

    with pytest.raises(ValidationError, match="finite number"):
        ExternalWorkRequest(
            **request.model_dump(exclude={"policy_snapshot"}),
            policy_snapshot={"limit": value},
        )


def test_checkpoint_codec_rejects_unknown_fields_and_versions() -> None:
    payload = encode_content_ref(_content_ref())
    unknown_field = payload[:-1] + b',"secret":"fixture-secret"}'
    wrong_version = payload.replace(b'"schema_version":1', b'"schema_version":2')

    with pytest.raises(CheckpointContractError, match="Content reference is invalid") as error:
        decode_content_ref(unknown_field)
    assert "fixture-secret" not in str(error.value)

    with pytest.raises(CheckpointContractError, match="Content reference is invalid"):
        decode_content_ref(wrong_version)


@pytest.mark.parametrize(
    ("payload", "decoder", "message"),
    [
        (
            encode_content_ref(_content_ref()),
            decode_content_ref,
            "Content reference is invalid",
        ),
        (
            encode_continuation_ref(_continuation(ContinuationRole.MID_TURN)),
            decode_continuation_ref,
            "Continuation reference is invalid",
        ),
        (
            encode_external_work_request(_external_request()),
            decode_external_work_request,
            "External work request is invalid",
        ),
        (
            encode_external_work_result(_external_result()),
            decode_external_work_result,
            "External work result is invalid",
        ),
        (
            encode_external_work_result_batch(
                ExternalWorkResultBatch(
                    schema_version=CHECKPOINT_SCHEMA_VERSION,
                    results=(_external_result(),),
                )
            ),
            decode_external_work_result_batch,
            "External work result batch is invalid",
        ),
        (
            encode_external_work_resume_request(_resume_request()),
            decode_external_work_resume_request,
            "External work resume request is invalid",
        ),
        (
            encode_checkpoint_outcome(
                TurnFailed(
                    **_outcome_fields(),
                    failure=CheckpointFailure(
                        category=CheckpointFailureCategory.CONTINUATION,
                        code="continuation_incompatible",
                        message="The continuation is incompatible.",
                    ),
                )
            ),
            decode_checkpoint_outcome,
            "Checkpoint outcome is invalid",
        ),
    ],
)
def test_checkpoint_decoders_reject_missing_schema_version(
    payload: bytes,
    decoder,
    message: str,
) -> None:
    value = json.loads(payload)
    del value["schema_version"]

    with pytest.raises(CheckpointContractError, match=message):
        decoder(json.dumps(value, separators=(",", ":")).encode())


def test_checkpoint_codec_rejects_duplicate_json_keys() -> None:
    payload = encode_content_ref(_content_ref())
    duplicate_version = payload.replace(
        b'"schema_version":1',
        b'"schema_version":2,"schema_version":1',
    )

    with pytest.raises(CheckpointContractError, match="Content reference is invalid"):
        decode_content_ref(duplicate_version)


def test_checkpoint_codec_rejects_oversized_envelope() -> None:
    with pytest.raises(CheckpointContractError, match="serialized size limit"):
        decode_content_ref(b"x" * (MAX_REFERENCE_ENVELOPE_BYTES + 1))
    with pytest.raises(CheckpointContractError, match="serialized size limit"):
        decode_external_work_result_batch(
            b"x" * (MAX_EXTERNAL_WORK_RESULT_BATCH_BYTES + 1)
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"harness_kind": HarnessKind.COPILOT},
        {"connector_version": "different"},
        {"connector_profile_id": "different"},
        {"backend_namespace": "different"},
        {"session_id": "different"},
        {"session_incarnation_id": "different"},
        {"run_id": "different"},
        {"turn_generation": 4},
    ],
)
def test_checkpoint_outcome_rejects_mismatched_continuation(
    changes: dict[str, object],
) -> None:
    continuation = _continuation(ContinuationRole.MID_TURN).model_copy(update=changes)

    with pytest.raises(ValidationError, match=r"identity|active turn"):
        ExternalWorkRequested(
            **_outcome_fields(),
            requests=(_external_request(),),
            continuation=continuation,
        )


def test_external_work_request_preserves_checkpoint_deadline() -> None:
    request = _external_request().model_copy(
        update={"deadline": _external_request().deadline + timedelta(hours=1)}
    )

    with pytest.raises(ValidationError, match="deadline"):
        ExternalWorkRequested(
            **_outcome_fields(),
            requests=(request,),
            continuation=_continuation(ContinuationRole.MID_TURN),
        )


def test_checkpoint_contracts_reject_wrong_continuation_roles() -> None:
    with pytest.raises(ValidationError, match="requires a terminal continuation"):
        CheckpointRequest(
            **{
                key: value
                for key, value in _outcome_fields().items()
                if key not in {"provider_id", "model_id", "usage"}
            },
            prompt_ref=_content_ref(),
            session_seed=_continuation(ContinuationRole.MID_TURN),
        )

    with pytest.raises(ValidationError, match="requires a terminal continuation"):
        TurnCompleted(
            **_outcome_fields(),
            result_ref=_content_ref(),
            continuation=_continuation(ContinuationRole.MID_TURN),
        )

    terminal = _continuation(ContinuationRole.TERMINAL_SEED)
    with pytest.raises(ValidationError, match="requires a mid-turn continuation"):
        ExternalWorkRequested(
            **_outcome_fields(),
            requests=(_external_request(),),
            continuation=terminal,
        )
    with pytest.raises(ValidationError, match="requires a mid-turn continuation"):
        HumanInputRequested(
            **_outcome_fields(),
            question_id="question",
            question_ref=_content_ref(),
            input_schema={"type": "string"},
            continuation=terminal,
        )


def test_external_work_rejects_invalid_workspace_placement() -> None:
    request = _external_request()

    with pytest.raises(ValidationError, match="requires a workspace reference"):
        ExternalWorkRequest(
            **request.model_dump(exclude={"execution_placement"}),
            execution_placement=ExternalWorkPlacement.SANDBOX,
        )

    with pytest.raises(ValidationError, match="valid only for sandbox"):
        ExternalWorkRequest(
            **request.model_dump(exclude={"workspace_ref"}),
            workspace_ref="workspace",
        )
