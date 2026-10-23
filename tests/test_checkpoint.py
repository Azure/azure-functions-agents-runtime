from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from azure_functions_agents.harness._checkpoint import (
    MAX_REFERENCE_ENVELOPE_BYTES,
    CheckpointContractError,
    CheckpointFailure,
    CheckpointRequest,
    CheckpointUsage,
    ContentRef,
    ContinuationRef,
    ContinuationRole,
    ExternalWorkPlacement,
    ExternalWorkRequest,
    ExternalWorkRequested,
    ExternalWorkResult,
    ExternalWorkStatus,
    HumanInputRequested,
    IntegrityMetadata,
    RetryGuidance,
    RetryRequested,
    StorageKind,
    TurnCompleted,
    TurnFailed,
    decode_checkpoint_outcome,
    decode_content_ref,
    decode_continuation_ref,
    decode_external_work_request,
    decode_external_work_result,
    encode_checkpoint_outcome,
    encode_content_ref,
    encode_continuation_ref,
    encode_external_work_request,
    encode_external_work_result,
)
from azure_functions_agents.harness._harness_binding import HarnessKind

_DIGEST = "a" * 64
_OWNER_DIGEST = "b" * 64


def _now() -> datetime:
    return datetime(2026, 10, 23, 20, 0, tzinfo=UTC)


def _content_ref(key: str = "durable/v1/scope/content/value") -> ContentRef:
    created = _now()
    return ContentRef(
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
            failure=CheckpointFailure(
                category="provider_transient",
                code="provider_timeout",
                message="The provider timed out.",
            ),
            guidance=RetryGuidance(retry_after_ms=1000, remaining_attempts=2),
        ),
        TurnFailed(
            **_outcome_fields(),
            failure=CheckpointFailure(
                category="connector_contract",
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
    result = ExternalWorkResult(
        operation_id=request.operation_id,
        tool_call_id=request.tool_call_id,
        agent_slug=request.agent_slug,
        owner_scope_digest=request.owner_scope_digest,
        entry_id=request.entry_id,
        session_id=request.session_id,
        session_incarnation_id=request.session_incarnation_id,
        run_id=request.run_id,
        turn_generation=request.turn_generation,
        status=ExternalWorkStatus.SUCCEEDED,
        result_ref=content,
        diagnostics={"receipt": "confirmed"},
    )

    assert decode_content_ref(encode_content_ref(content)) == content
    assert decode_continuation_ref(encode_continuation_ref(continuation)) == continuation
    assert decode_external_work_request(encode_external_work_request(request)) == request
    assert decode_external_work_result(encode_external_work_result(result)) == result


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
    result = ExternalWorkResult(
        operation_id="operation",
        tool_call_id="call",
        agent_slug="agent",
        owner_scope_digest=_OWNER_DIGEST,
        entry_id="entry",
        session_id="session",
        session_incarnation_id="incarnation",
        run_id="run",
        turn_generation=3,
        status=ExternalWorkStatus.SUCCEEDED,
        result_ref=_content_ref(),
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


def test_checkpoint_encoder_revalidates_copied_models() -> None:
    request = _external_request()
    result = ExternalWorkResult(
        operation_id=request.operation_id,
        tool_call_id=request.tool_call_id,
        agent_slug=request.agent_slug,
        owner_scope_digest=request.owner_scope_digest,
        entry_id=request.entry_id,
        session_id=request.session_id,
        session_incarnation_id=request.session_incarnation_id,
        run_id=request.run_id,
        turn_generation=request.turn_generation,
        status=ExternalWorkStatus.SUCCEEDED,
        result_ref=_content_ref(),
    ).model_copy(update={"diagnostics": {"oversized": "x" * 1025}})

    with pytest.raises(CheckpointContractError, match="External work result is invalid"):
        encode_external_work_result(result)


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


def test_checkpoint_outcome_rejects_mismatched_continuation() -> None:
    continuation = _continuation(ContinuationRole.MID_TURN).model_copy(
        update={"run_id": "different"}
    )

    with pytest.raises(ValidationError, match="active turn"):
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
