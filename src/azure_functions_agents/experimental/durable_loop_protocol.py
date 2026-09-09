"""Strict versioned contracts for the private durable agent loop."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as JsonSchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..strict_json import (
    DuplicateJsonKeyError,
    assert_json_value,
    canonical_json_bytes,
    decode_json_object,
)

DURABLE_LOOP_SCHEMA_VERSION: Literal["1"] = "1"
DURABLE_LOOP_ORCHESTRATOR_V1_NAME = "durable_agent_turn_orchestrator_v1"
DURABLE_LOOP_ORCHESTRATOR_V2_NAME = "durable_agent_turn_orchestrator_v2"
DURABLE_LOOP_ORCHESTRATOR_V3_NAME = "durable_agent_turn_orchestrator_v3"
MAX_DURABLE_ENVELOPE_BYTES = 32 * 1024
MAX_MAF_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_MAF_BUNDLE_MESSAGES = 4096
MAX_WORKING_CONTEXT_BYTES = 16 * 1024 * 1024
MAX_WORKING_CONTEXT_MESSAGES = 1024
MAX_TOOL_ARGUMENT_BYTES = 1024 * 1024
MAX_TOOL_RESULT_BYTES = 8 * 1024 * 1024
MAX_HUMAN_QUESTION_BYTES = 8 * 1024
MAX_HUMAN_ANSWER_BYTES = 64 * 1024
MAX_HUMAN_CHOICES = 20
MAX_HUMAN_CHOICE_CHARS = 256
MAX_HUMAN_SCHEMA_DEPTH = 8
MAX_HUMAN_SCHEMA_NODES = 128

_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_CONTENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_HUMAN_SCHEMA_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
_HUMAN_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "const",
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "properties",
        "required",
        "title",
        "type",
    }
)

SchemaVersion = Literal["1"]
_OpaqueId = Annotated[str, Field(min_length=1, max_length=128, pattern=_OPAQUE_ID_PATTERN.pattern)]
_ToolName = Annotated[str, Field(min_length=1, max_length=128, pattern=_TOOL_NAME_PATTERN.pattern)]
_Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN.pattern)]
_ShortText = Annotated[str, Field(max_length=2048)]
_NonNegativeInt = Annotated[int, Field(ge=0)]


class DurableLoopProtocolDocumentError(ValueError):
    """An untrusted durable-loop document is invalid."""


class DurableLoopRunStatus(StrEnum):
    """The six externally visible run states."""

    PENDING = "Pending"
    RUNNING = "Running"
    WAITING = "Waiting"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


class ErrorDisposition(StrEnum):
    """Whether a failed operation may have committed an external effect."""

    CERTAIN = "Certain"
    AMBIGUOUS = "Ambiguous"


class ModelOperationStatus(StrEnum):
    """A background model operation lifecycle."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ToolProvenance(StrEnum):
    """The immutable routing class for a tool."""

    RUNTIME = "runtime"
    REMOTE = "remote"
    LOCAL = "local"


class ToolBehavior(StrEnum):
    """The side-effect and scheduling policy for a tool."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    IDEMPOTENT_WRITE = "idempotent_write"


class ToolResultStatus(StrEnum):
    """A terminal tool-call outcome."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    AMBIGUOUS = "ambiguous"


class HumanInputState(StrEnum):
    """The authoritative request-record state."""

    PENDING = "pending"
    ANSWERED = "answered"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class HumanResponseDisposition(StrEnum):
    """The accepted answer delivery lifecycle."""

    ACCEPTED = "accepted"
    CONSUMED = "consumed"
    ORPHANED = "orphaned"


class HumanEventDeliveryStatus(StrEnum):
    """One outbox delivery-attempt outcome."""

    DELIVERED = "delivered"
    RETRY = "retry"
    TERMINAL = "terminal"


class BackgroundStartDisposition(StrEnum):
    """Whether a provider start acknowledgement is authoritative."""

    ACCEPTED = "accepted"
    TERMINAL = "terminal"
    LOST_ACKNOWLEDGEMENT = "lost_acknowledgement"


class SandboxExecutionProfile(StrEnum):
    """The private ACA lifecycle profile selected for one run."""

    PER_CALL = "per_call"
    RETAINED_SESSION = "retained_session"


class DurableFaultProfile(StrEnum):
    """A bounded deterministic live-qualification fault."""

    NONE = "none"
    MODEL_APIM_429_ONCE = "model_apim_429_once"
    MODEL_TIMEOUT_ONCE = "model_timeout_once"
    TOOL_ACTIVITY_ACK_LOSS_ONCE = "tool_activity_ack_loss_once"
    SANDBOX_LOSS_AFTER_CHECKPOINT = "sandbox_loss_after_checkpoint"
    CLEANUP_FAILURE_ONCE = "cleanup_failure_once"
    COMMIT_ACK_LOSS_ONCE = "commit_ack_loss_once"


class _DurableLoopModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class DurableLoopBudgetV1(_DurableLoopModel):
    """Immutable limits consumed by deterministic orchestration."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    max_model_steps: Annotated[int, Field(ge=1, le=256)]
    max_tool_calls: Annotated[int, Field(ge=0, le=2048)]
    max_total_tokens: Annotated[int, Field(ge=1, le=100000000)] = 1_000_000
    max_cost_microunits: Annotated[int, Field(ge=0, le=10000000000)] = 0
    input_cost_microunits_per_million_tokens: Annotated[
        int,
        Field(ge=0, le=10000000000),
    ] = 0
    output_cost_microunits_per_million_tokens: Annotated[
        int,
        Field(ge=0, le=10000000000),
    ] = 0
    max_external_content_bytes: Annotated[
        int,
        Field(ge=1024, le=268435456),
    ] = 32 * 1024 * 1024
    max_elapsed_seconds: Annotated[int, Field(ge=1, le=604800)]
    max_human_waits: Annotated[int, Field(ge=0, le=64)]
    human_wait_seconds: Annotated[int, Field(ge=1, le=604800)]
    max_argument_bytes: Annotated[int, Field(ge=1024, le=1048576)]
    max_result_bytes: Annotated[int, Field(ge=1024, le=8388608)]
    context_max_bytes: Annotated[int, Field(ge=65536, le=16777216)]
    context_compaction_percent: Annotated[int, Field(ge=25, le=90)]
    max_parallel_reads: Annotated[int, Field(ge=1, le=32)]
    continue_as_new_checkpoints: Annotated[int, Field(ge=1, le=100)]


class DurableRunIdentityV1(_DurableLoopModel):
    """Immutable run, deployment, policy, and budget identity."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id: _OpaqueId
    session_id: _OpaqueId
    request_id_hash: _Sha256
    request_hash: _Sha256
    owner_hash: _Sha256
    agent_slug: _ToolName
    agent_hash: _Sha256
    catalog_hash: _Sha256
    deployment_hash: _Sha256
    execution_binding_hash: _Sha256 | None = None
    tool_package_hash: _Sha256
    policy_hash: _Sha256
    orchestration_version: Annotated[str, Field(min_length=1, max_length=64)]
    created_at: datetime
    active_deadline: datetime
    absolute_deadline: datetime
    budget: DurableLoopBudgetV1

    @model_validator(mode="after")
    def validate_deadlines(self) -> Self:
        created = _as_utc(self.created_at, "created_at")
        active = _as_utc(self.active_deadline, "active_deadline")
        absolute = _as_utc(self.absolute_deadline, "absolute_deadline")
        if not created < active <= absolute:
            raise ValueError("run deadlines must satisfy created_at < active <= absolute")
        return self


class ContentRefV1(_DurableLoopModel):
    """Opaque, integrity-bound external content reference without credentials."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    object_id: Annotated[
        str,
        Field(min_length=1, max_length=256, pattern=_CONTENT_ID_PATTERN.pattern),
    ]
    sha256: _Sha256
    byte_length: Annotated[int, Field(ge=0, le=268435456)]
    media_type: Annotated[str, Field(min_length=1, max_length=128)]
    encryption_version: Annotated[str, Field(min_length=1, max_length=64)]
    retention_class: Annotated[str, Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def reject_authorization_material(self) -> Self:
        lowered = self.object_id.casefold()
        if "://" in lowered or "?" in lowered or "=" in lowered or "sig" in lowered:
            raise ValueError("content reference must not contain a URL or authorization material")
        return self


class WorkspaceArtifactV1(_DurableLoopModel):
    """One immutable sandbox workspace checkpoint and its frozen bindings."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    generation: _NonNegativeInt
    archive_ref: ContentRefV1
    parent_ref: ContentRefV1 | None = None
    manifest_hash: _Sha256
    package_hash: _Sha256
    policy_hash: _Sha256
    catalog_hash: _Sha256


class SandboxLeaseV1(_DurableLoopModel):
    """External-only retained sandbox binding with a fencing generation."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id: _OpaqueId
    session_id: _OpaqueId
    generation: Annotated[int, Field(ge=1)]
    sandbox_id_ref: ContentRefV1
    group_binding_hash: _Sha256
    manifest_hash: _Sha256
    package_hash: _Sha256
    policy_hash: _Sha256
    catalog_hash: _Sha256
    workspace_ref: ContentRefV1 | None = None
    expires_at: datetime
    owner_call_key: _Sha256 | None = None


class MAFMessageBundleV1(_DurableLoopModel):
    """Exact ordered MAF message dictionaries for a local-test-safe checkpoint."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    messages: Annotated[
        tuple[dict[str, object], ...],
        Field(min_length=1, max_length=MAX_MAF_BUNDLE_MESSAGES),
    ]
    maf_core_version: Annotated[str, Field(min_length=1, max_length=64)]
    provider: Annotated[str, Field(min_length=1, max_length=64)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    api_version: Annotated[str, Field(min_length=1, max_length=64)]
    bundle_hash: _Sha256

    @classmethod
    def create(
        cls,
        *,
        messages: tuple[dict[str, object], ...],
        maf_core_version: str,
        provider: str,
        model: str,
        api_version: str,
    ) -> MAFMessageBundleV1:
        """Create an integrity-bound bundle from exact serialized messages."""
        bundle_hash = canonical_hash(
            {
                "api_version": api_version,
                "maf_core_version": maf_core_version,
                "messages": messages,
                "model": model,
                "provider": provider,
            }
        )
        return cls(
            messages=messages,
            maf_core_version=maf_core_version,
            provider=provider,
            model=model,
            api_version=api_version,
            bundle_hash=bundle_hash,
        )

    @model_validator(mode="after")
    def validate_messages(self) -> Self:
        assert_json_value(self.messages)
        encoded = canonical_json_bytes(self.messages)
        if len(encoded) > MAX_MAF_BUNDLE_BYTES:
            raise ValueError("MAF message bundle exceeds the byte limit")
        expected = canonical_hash(
            {
                "api_version": self.api_version,
                "maf_core_version": self.maf_core_version,
                "messages": self.messages,
                "model": self.model,
                "provider": self.provider,
            }
        )
        if self.bundle_hash != expected:
            raise ValueError("MAF message bundle hash mismatch")
        return self


class WorkingContextV1(_DurableLoopModel):
    """The exact bounded model input selected from immutable audit history."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    bundle: MAFMessageBundleV1
    compaction_generation: _NonNegativeInt
    summary_ref: ContentRefV1 | None = None
    source_audit_hash: _Sha256
    source_start: _NonNegativeInt
    source_end: _NonNegativeInt
    estimated_tokens: _NonNegativeInt
    actual_tokens: _NonNegativeInt | None = None
    parent_context_hash: _Sha256 | None = None

    @model_validator(mode="after")
    def validate_source_range(self) -> Self:
        if self.source_end < self.source_start:
            raise ValueError("working-context audit range must be forward")
        if len(self.bundle.messages) > MAX_WORKING_CONTEXT_MESSAGES:
            raise ValueError("working context exceeds the message limit")
        if len(canonical_json_bytes(self.bundle.messages)) > MAX_WORKING_CONTEXT_BYTES:
            raise ValueError("working context exceeds the byte limit")
        return self


class ModelToolCallV1(_DurableLoopModel):
    """One model-emitted function call in provider order."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    call_id: _OpaqueId
    name: _ToolName
    arguments: dict[str, object]

    @model_validator(mode="after")
    def validate_arguments(self) -> Self:
        assert_json_value(self.arguments)
        if len(canonical_json_bytes(self.arguments)) > MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError("tool arguments exceed the byte limit")
        return self


class UsageV1(_DurableLoopModel):
    """Bounded provider-reported usage for one model step."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    input_tokens: _NonNegativeInt = 0
    output_tokens: _NonNegativeInt = 0
    reasoning_tokens: _NonNegativeInt = 0
    cost_microunits: _NonNegativeInt | None = None


class ModelDecisionEnvelopeV1(_DurableLoopModel):
    """One final-text or ordered-tool-call model decision."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id: _OpaqueId
    step_index: _NonNegativeInt
    model_call_key: _Sha256
    response_id_hash: _Sha256 | None = None
    deployment_hash: _Sha256
    assistant_message: dict[str, object]
    tool_calls: Annotated[tuple[ModelToolCallV1, ...], Field(max_length=128)] = ()
    final_text: Annotated[str, Field(max_length=1048576)] | None = None
    usage: UsageV1 = UsageV1()
    finish_reason: Annotated[str, Field(max_length=128)] | None = None
    attempts: Annotated[int, Field(ge=1, le=16)] = 1

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        assert_json_value(self.assistant_message)
        if self.final_text is not None and not self.final_text.strip():
            raise ValueError("model decision final text must not be blank")
        has_text = self.final_text is not None
        has_calls = bool(self.tool_calls)
        if has_text == has_calls:
            raise ValueError("model decision must contain final text or tool calls, not both")
        if len(canonical_json_bytes(self.assistant_message)) > MAX_DURABLE_ENVELOPE_BYTES:
            raise ValueError("assistant message exceeds the durable envelope limit")
        call_ids = [call.call_id for call in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("model decision call IDs must be unique")
        return self


class ModelOperationV1(_DurableLoopModel):
    """Persisted foreground/background model operation state."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    operation_key: _Sha256
    run_id: _OpaqueId
    step_index: _NonNegativeInt
    status: ModelOperationStatus
    backend_binding_hash: _Sha256
    deployment_hash: _Sha256
    provider_response_ref: ContentRefV1 | None = None
    continuation_token_ref: ContentRefV1 | None = None
    accepted_at: datetime | None = None
    deadline: datetime
    last_polled_at: datetime | None = None
    poll_count: _NonNegativeInt = 0
    decision_ref: ContentRefV1 | None = None
    terminal_retrieval_expires_at: datetime | None = None
    retrieval_is_repeatable: bool | None = None


class ToolRequestV1(_DurableLoopModel):
    """One request-hash-bound tool dispatch."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id: _OpaqueId
    session_id: _OpaqueId = "session-unknown"
    owner_hash: _Sha256 = "0" * 64
    step_index: _NonNegativeInt
    call_ordinal: _NonNegativeInt
    provider_call_id: _OpaqueId
    call_key: _Sha256
    tool_name: _ToolName
    provenance: ToolProvenance
    behavior: ToolBehavior
    arguments: dict[str, object]
    argument_byte_limit: Annotated[
        int,
        Field(ge=1024, le=MAX_TOOL_ARGUMENT_BYTES),
    ] = MAX_TOOL_ARGUMENT_BYTES
    result_byte_limit: Annotated[
        int,
        Field(ge=1024, le=MAX_TOOL_RESULT_BYTES),
    ] = MAX_TOOL_RESULT_BYTES
    request_hash: _Sha256
    policy_hash: _Sha256
    catalog_hash: _Sha256
    package_hash: _Sha256
    workspace_ref: ContentRefV1 | None = None
    sandbox_profile: SandboxExecutionProfile = SandboxExecutionProfile.PER_CALL
    fault_profile: DurableFaultProfile = DurableFaultProfile.NONE
    deadline: datetime

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        assert_json_value(self.arguments)
        if len(canonical_json_bytes(self.arguments)) > MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError("tool arguments exceed the byte limit")
        expected = tool_request_hash(
            tool_name=self.tool_name,
            arguments=self.arguments,
            behavior=self.behavior,
            provenance=self.provenance,
            argument_byte_limit=self.argument_byte_limit,
            result_byte_limit=self.result_byte_limit,
            policy_hash=self.policy_hash,
            catalog_hash=self.catalog_hash,
            package_hash=self.package_hash,
            workspace_ref=self.workspace_ref,
            sandbox_profile=self.sandbox_profile,
            fault_profile=self.fault_profile,
            owner_hash=self.owner_hash,
        )
        if self.request_hash != expected:
            raise ValueError("tool request hash mismatch")
        return self


class ToolResultV1(_DurableLoopModel):
    """One bounded tool outcome aligned to its deterministic request."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id: _OpaqueId
    step_index: _NonNegativeInt
    call_ordinal: _NonNegativeInt
    provider_call_id: _OpaqueId
    call_key: _Sha256
    request_hash: _Sha256
    tool_name: _ToolName
    status: ToolResultStatus
    value: object | None = None
    result_ref: ContentRefV1 | None = None
    provider_operation_id: _OpaqueId | None = None
    attempt: Annotated[int, Field(ge=1, le=16)] = 1
    elapsed_ms: Annotated[float, Field(ge=0.0, le=86400000.0, allow_inf_nan=False)]
    error: ErrorEnvelopeV1 | None = None
    deduplicated: bool = False
    workspace_ref: ContentRefV1 | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        assert_json_value(self.value)
        if len(canonical_json_bytes(self.value)) > MAX_TOOL_RESULT_BYTES:
            raise ValueError("tool result exceeds the byte limit")
        if self.status is ToolResultStatus.SUCCEEDED and self.error is not None:
            raise ValueError("successful tool result cannot include an error")
        if self.status is not ToolResultStatus.SUCCEEDED and self.error is None:
            raise ValueError("failed tool result must include an error")
        return self


class HumanInputRequestV1(_DurableLoopModel):
    """One unique clarification request and authoritative mailbox state."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    request_id: _OpaqueId
    run_id: _OpaqueId
    session_id: _OpaqueId
    generation: Annotated[int, Field(ge=1)]
    turn_index: _NonNegativeInt
    step_index: _NonNegativeInt
    call_id: _OpaqueId
    call_key: _Sha256
    request_hash: _Sha256
    question_ref: ContentRefV1
    choices: Annotated[
        tuple[Annotated[str, Field(max_length=MAX_HUMAN_CHOICE_CHARS)], ...],
        Field(max_length=MAX_HUMAN_CHOICES),
    ] = ()
    allow_free_text: bool
    response_schema: dict[str, object] | None = None
    actor_policy_hash: _Sha256
    event_name: Annotated[str, Field(min_length=1, max_length=192)]
    issued_at: datetime
    expires_at: datetime
    record_version: Annotated[int, Field(ge=1)]
    state: HumanInputState = HumanInputState.PENDING

    @model_validator(mode="after")
    def validate_human_request(self) -> Self:
        if self.response_schema is not None:
            validate_human_response_schema(self.response_schema)
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("human input choices must be unique")
        if not self.allow_free_text and not self.choices and self.response_schema is None:
            raise ValueError("human input request must allow one response shape")
        if _as_utc(self.expires_at, "expires_at") <= _as_utc(
            self.issued_at, "issued_at"
        ):
            raise ValueError("human input expiry must be after issue time")
        return self


class HumanInputContentV1(_DurableLoopModel):
    """Authorized human-input projection containing the user-visible question."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    request_id: _OpaqueId
    run_id: _OpaqueId
    generation: Annotated[int, Field(ge=1)]
    question: Annotated[str, Field(min_length=1, max_length=MAX_HUMAN_QUESTION_BYTES)]
    choices: Annotated[
        tuple[Annotated[str, Field(max_length=MAX_HUMAN_CHOICE_CHARS)], ...],
        Field(max_length=MAX_HUMAN_CHOICES),
    ] = ()
    allow_free_text: bool
    response_schema: dict[str, object] | None = None
    expires_at: datetime
    respond_url: Annotated[str, Field(min_length=1, max_length=512)]

    @model_validator(mode="after")
    def validate_question_bytes(self) -> Self:
        if len(self.question.encode("utf-8")) > MAX_HUMAN_QUESTION_BYTES:
            raise ValueError("human question exceeds the byte limit")
        return self


class HumanInputResponseV1(_DurableLoopModel):
    """One first-answer receipt and outbox delivery disposition."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    request_id: _OpaqueId
    run_id: _OpaqueId
    session_id: _OpaqueId
    generation: Annotated[int, Field(ge=1)]
    call_id: _OpaqueId
    submission_id_hash: _Sha256
    body_hash: _Sha256
    actor_hash: _Sha256
    answer_ref: ContentRefV1
    accepted_at: datetime
    schema_valid: bool
    disposition: HumanResponseDisposition
    delivery_attempts: _NonNegativeInt = 0
    delivered_at: datetime | None = None


class HumanEventDeliveryResultV1(_DurableLoopModel):
    """Content-free result of one external-event delivery attempt."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    status: HumanEventDeliveryStatus
    retry_after_seconds: Annotated[float, Field(ge=0.0, le=300.0, allow_inf_nan=False)] = 0.0
    terminal_status_code: Literal[404, 410] | None = None

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        if (
            self.status is HumanEventDeliveryStatus.TERMINAL
        ) != (self.terminal_status_code is not None):
            raise ValueError("terminal delivery must include a terminal status code")
        return self


class ErrorEnvelopeV1(_DurableLoopModel):
    """Sanitized typed failure safe for status and durable history."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    code: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    classification: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    retryable: bool
    disposition: ErrorDisposition = ErrorDisposition.CERTAIN
    possibly_committed: bool = False
    phase: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    step_index: _NonNegativeInt | None = None
    call_key: _Sha256 | None = None
    detail_ref: ContentRefV1 | None = None

    @model_validator(mode="after")
    def validate_ambiguity(self) -> Self:
        ambiguous = self.disposition is ErrorDisposition.AMBIGUOUS
        if ambiguous != self.possibly_committed:
            raise ValueError("ambiguous disposition must match possibly_committed")
        return self


class CheckpointStateV1(_DurableLoopModel):
    """Bounded replay state between model, tool, and human boundaries."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    identity: DurableRunIdentityV1
    status: DurableLoopRunStatus
    audit_bundle: MAFMessageBundleV1
    working_context: WorkingContextV1
    audit_head_hash: _Sha256
    completed_model_steps: _NonNegativeInt
    completed_tool_calls: _NonNegativeInt
    human_wait_count: _NonNegativeInt
    next_model_step: _NonNegativeInt
    committed_session_generation: _NonNegativeInt
    continue_as_new_generation: _NonNegativeInt
    checkpoints_in_generation: _NonNegativeInt
    input_tokens: _NonNegativeInt = 0
    output_tokens: _NonNegativeInt = 0
    reasoning_tokens: _NonNegativeInt = 0
    cost_microunits: _NonNegativeInt = 0
    parked_seconds: _NonNegativeInt = 0
    external_content_bytes: _NonNegativeInt = 0
    workspace_ref: ContentRefV1 | None = None
    pending_human_request_id: _OpaqueId | None = None
    cancellation_requested: bool = False
    repair_steps_used: _NonNegativeInt = 0
    last_error: ErrorEnvelopeV1 | None = None

    @model_validator(mode="after")
    def validate_wait_state(self) -> Self:
        waiting = self.status is DurableLoopRunStatus.WAITING
        if waiting != (self.pending_human_request_id is not None):
            raise ValueError("Waiting status must match an open human request")
        if self.audit_head_hash != self.audit_bundle.bundle_hash:
            raise ValueError("checkpoint audit head does not match the audit bundle")
        if self.working_context.source_audit_hash != self.audit_head_hash:
            raise ValueError("working context does not bind to the checkpoint audit head")
        return self


class DurableLoopStatusEnvelopeV1(_DurableLoopModel):
    """Content-free status projection safe for polling."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id: _OpaqueId
    session_id: _OpaqueId
    status: DurableLoopRunStatus
    phase: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    created_at: datetime
    updated_at: datetime
    model_steps: _NonNegativeInt
    tool_calls: _NonNegativeInt
    human_waits: _NonNegativeInt
    step_index: _NonNegativeInt
    input_tokens: _NonNegativeInt = 0
    output_tokens: _NonNegativeInt = 0
    reasoning_tokens: _NonNegativeInt = 0
    cost_microunits: _NonNegativeInt = 0
    external_content_bytes: _NonNegativeInt = 0
    parked_seconds: _NonNegativeInt = 0
    pending_human_request_id: _OpaqueId | None = None
    error: ErrorEnvelopeV1 | None = None
    result_available: bool = False
    expires_at: datetime


class DurableLoopFinalResultV1(_DurableLoopModel):
    """Authorized terminal result projection."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id: _OpaqueId
    session_id: _OpaqueId
    status: DurableLoopRunStatus
    response_ref: ContentRefV1 | None = None
    committed_generation: _NonNegativeInt | None = None
    error: ErrorEnvelopeV1 | None = None

    @model_validator(mode="after")
    def validate_terminal_result(self) -> Self:
        if self.status is DurableLoopRunStatus.COMPLETED:
            if self.response_ref is None or self.error is not None:
                raise ValueError("completed result requires only a response reference")
        elif self.response_ref is not None:
            raise ValueError("non-completed result cannot include a response reference")
        return self


class BackgroundStartResultV1(_DurableLoopModel):
    """Deterministic activity result for a background model start."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    disposition: BackgroundStartDisposition
    operation: ModelOperationV1 | None = None
    decision: ModelDecisionEnvelopeV1 | None = None
    error: ErrorEnvelopeV1 | None = None
    written_bytes: _NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_start_shape(self) -> Self:
        populated = sum(
            value is not None for value in (self.operation, self.decision, self.error)
        )
        if populated != 1:
            raise ValueError("background start must contain one outcome")
        if (
            self.disposition is BackgroundStartDisposition.LOST_ACKNOWLEDGEMENT
            and (
                self.error is None
                or self.error.disposition is not ErrorDisposition.AMBIGUOUS
            )
        ):
            raise ValueError("lost acknowledgement must be represented as ambiguous")
        return self


class BackgroundPollResultV1(_DurableLoopModel):
    """One background-operation poll result."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    operation: ModelOperationV1 | None = None
    decision: ModelDecisionEnvelopeV1 | None = None
    error: ErrorEnvelopeV1 | None = None
    written_bytes: _NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_poll_shape(self) -> Self:
        if sum(
            value is not None for value in (self.operation, self.decision, self.error)
        ) != 1:
            raise ValueError("background poll must contain one outcome")
        return self


class FrozenToolDescriptorV1(_DurableLoopModel):
    """One immutable model-visible tool schema and dispatch policy."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    name: _ToolName
    description: Annotated[str, Field(max_length=8192)]
    parameters: dict[str, object]
    provenance: ToolProvenance
    behavior: ToolBehavior
    parallel_safe: bool = False

    @model_validator(mode="after")
    def validate_descriptor(self) -> Self:
        assert_json_value(self.parameters)
        if self.parameters.get("type") != "object":
            raise ValueError("tool parameters must describe an object")
        if len(canonical_json_bytes(self.parameters)) > 128 * 1024:
            raise ValueError("tool parameter schema exceeds the byte limit")
        if self.parallel_safe and self.behavior is not ToolBehavior.READ_ONLY:
            raise ValueError("only read-only tools may be parallel safe")
        if (
            self.name == "request_human_input"
            and self.provenance is not ToolProvenance.RUNTIME
        ):
            raise ValueError("request_human_input is reserved for runtime provenance")
        return self


class FrozenToolCatalogV1(_DurableLoopModel):
    """Collision-free immutable tool inventory for one run."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    tools: Annotated[tuple[FrozenToolDescriptorV1, ...], Field(max_length=512)]
    catalog_hash: _Sha256
    policy_hash: _Sha256
    package_hash: _Sha256

    @classmethod
    def create(
        cls,
        *,
        tools: tuple[FrozenToolDescriptorV1, ...],
        policy_hash: str,
        package_hash: str,
    ) -> FrozenToolCatalogV1:
        """Create a sorted catalog and bind its canonical hash."""
        ordered = tuple(sorted(tools, key=lambda item: item.name))
        return cls(
            tools=ordered,
            catalog_hash=canonical_hash([item.model_dump(mode="json") for item in ordered]),
            policy_hash=policy_hash,
            package_hash=package_hash,
        )

    @model_validator(mode="after")
    def validate_catalog(self) -> Self:
        names = [tool.name for tool in self.tools]
        if names != sorted(names):
            raise ValueError("tool catalog must be sorted by name")
        if len(names) != len(set(names)):
            raise ValueError("tool catalog names must be unique")
        expected = canonical_hash(
            [tool.model_dump(mode="json") for tool in self.tools]
        )
        if self.catalog_hash != expected:
            raise ValueError("tool catalog hash mismatch")
        return self

    def by_name(self) -> dict[str, FrozenToolDescriptorV1]:
        """Return a deterministic name lookup."""
        return {tool.name: tool for tool in self.tools}


def validate_human_response_schema(schema: dict[str, object]) -> None:
    """Reject remote, recursive, or computationally unsafe response schemas."""
    assert_json_value(schema)
    if len(canonical_json_bytes(schema)) > MAX_HUMAN_QUESTION_BYTES:
        raise ValueError("human response schema exceeds the byte limit")
    node_count = [0]
    _validate_human_schema_node(schema, depth=0, node_count=node_count)
    try:
        Draft202012Validator.check_schema(schema)
    except JsonSchemaError as exc:
        raise ValueError("human response schema is invalid") from exc


def validate_human_response_value(
    schema: dict[str, object],
    value: object,
) -> None:
    """Validate one answer against the restricted local-only schema subset."""
    validate_human_response_schema(schema)
    assert_json_value(value)
    try:
        Draft202012Validator(schema).validate(value)
    except JsonSchemaValidationError as exc:
        raise ValueError("human answer does not match the response schema") from exc


def _validate_human_schema_node(  # noqa: PLR0912
    schema: dict[str, object],
    *,
    depth: int,
    node_count: list[int],
) -> None:
    if depth > MAX_HUMAN_SCHEMA_DEPTH:
        raise ValueError("human response schema exceeds the depth limit")
    node_count[0] += 1
    if node_count[0] > MAX_HUMAN_SCHEMA_NODES:
        raise ValueError("human response schema exceeds the node limit")
    unsupported = sorted(set(schema) - _HUMAN_SCHEMA_KEYS)
    if unsupported:
        raise ValueError(
            f"human response schema uses unsupported keywords: {unsupported}"
        )

    schema_type = schema.get("type")
    if schema_type is not None:
        values = [schema_type] if isinstance(schema_type, str) else schema_type
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(item, str) and item in _HUMAN_SCHEMA_TYPES for item in values)
            or len(values) != len(set(values))
        ):
            raise ValueError("human response schema type is invalid")

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise ValueError("human response schema properties must be an object")
        for name, child in properties.items():
            if not isinstance(name, str) or not name or len(name) > 128:
                raise ValueError("human response schema property name is invalid")
            if not isinstance(child, dict):
                raise ValueError("human response schema property must be an object")
            _validate_human_schema_node(
                child,
                depth=depth + 1,
                node_count=node_count,
            )

    required = schema.get("required")
    if required is not None:
        if (
            not isinstance(required, list)
            or not all(isinstance(item, str) and item for item in required)
            or len(required) != len(set(required))
        ):
            raise ValueError("human response schema required list is invalid")
        if isinstance(properties, dict) and not set(required) <= set(properties):
            raise ValueError("human response schema requires an unknown property")

    items = schema.get("items")
    if items is not None:
        if not isinstance(items, dict):
            raise ValueError("human response schema items must be an object")
        _validate_human_schema_node(
            items,
            depth=depth + 1,
            node_count=node_count,
        )

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        if not isinstance(additional, dict):
            raise ValueError(
                "human response schema additionalProperties must be boolean or object"
            )
        _validate_human_schema_node(
            additional,
            depth=depth + 1,
            node_count=node_count,
        )

    enum = schema.get("enum")
    if enum is not None and (
        not isinstance(enum, list) or not enum or len(enum) > MAX_HUMAN_SCHEMA_NODES
    ):
        raise ValueError("human response schema enum is invalid")

    for key in ("title", "description"):
        value = schema.get(key)
        if value is not None and (
            not isinstance(value, str) or len(value.encode("utf-8")) > 1024
        ):
            raise ValueError(f"human response schema {key} is invalid")

    for key in (
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minProperties",
        "maxProperties",
    ):
        value = schema.get(key)
        if value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= MAX_HUMAN_ANSWER_BYTES
        ):
            raise ValueError(f"human response schema {key} is invalid")

    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        value = schema.get(key)
        if value is not None and (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise ValueError(f"human response schema {key} is invalid")


class DurableLoopPlanDocumentV1(_DurableLoopModel):
    """External-only immutable plan and provider binding for one run."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    instructions: Annotated[str, Field(max_length=65536)]
    catalog: FrozenToolCatalogV1
    model_settings: dict[str, object]
    maf_core_version: Annotated[str, Field(min_length=1, max_length=64)]
    provider: Annotated[str, Field(min_length=1, max_length=64)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    api_version: Annotated[str, Field(min_length=1, max_length=64)]
    settings: dict[str, object]
    sandbox_profile: SandboxExecutionProfile = SandboxExecutionProfile.PER_CALL
    fault_profile: DurableFaultProfile = DurableFaultProfile.NONE

    @model_validator(mode="after")
    def validate_plan_json(self) -> Self:
        assert_json_value(self.model_settings)
        assert_json_value(self.settings)
        if len(canonical_json_bytes(self.model_settings)) > MAX_DURABLE_ENVELOPE_BYTES:
            raise ValueError("model settings exceed the byte limit")
        if len(canonical_json_bytes(self.settings)) > MAX_DURABLE_ENVELOPE_BYTES:
            raise ValueError("durable-loop settings exceed the byte limit")
        return self


class DurableRunDocumentV1(_DurableLoopModel):
    """External content document never copied into Durable history."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    plan: DurableLoopPlanDocumentV1
    checkpoint: CheckpointStateV1
    pending_decision: ModelDecisionEnvelopeV1 | None = None


class DurableOrchestrationInputV1(_DurableLoopModel):
    """Refs-only input safe for Durable orchestration history."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    identity: DurableRunIdentityV1
    run_document_ref: ContentRefV1
    session_entity_key: _Sha256
    completed_model_steps: _NonNegativeInt = 0
    completed_tool_calls: _NonNegativeInt = 0
    human_wait_count: _NonNegativeInt = 0
    next_model_step: _NonNegativeInt = 0
    committed_session_generation: _NonNegativeInt = 0
    continue_as_new_generation: _NonNegativeInt = 0
    checkpoints_in_generation: _NonNegativeInt = 0
    input_tokens: _NonNegativeInt = 0
    output_tokens: _NonNegativeInt = 0
    reasoning_tokens: _NonNegativeInt = 0
    cost_microunits: _NonNegativeInt = 0
    parked_seconds: _NonNegativeInt = 0
    external_content_bytes: _NonNegativeInt = 0
    working_context_bytes: _NonNegativeInt = 0
    last_compacted_step: _NonNegativeInt | None = None
    repair_steps_used: _NonNegativeInt = 0
    sandbox_profile: SandboxExecutionProfile = SandboxExecutionProfile.PER_CALL
    fault_profile: DurableFaultProfile = DurableFaultProfile.NONE

    @model_validator(mode="after")
    def validate_envelope_size(self) -> Self:
        _assert_envelope_size(self)
        return self


class ToolDispatchRefV1(_DurableLoopModel):
    """Refs-only tool call metadata used for scheduling."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    request_ref: ContentRefV1
    call_ordinal: _NonNegativeInt
    call_key: _Sha256
    request_hash: _Sha256
    tool_name: _ToolName
    provenance: ToolProvenance
    behavior: ToolBehavior
    parallel_safe: bool


class ModelStepActivityResultV1(_DurableLoopModel):
    """Refs-only one-step model activity result."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_document_ref: ContentRefV1
    step_index: _NonNegativeInt
    final_response_ref: ContentRefV1 | None = None
    background_operation_ref: ContentRefV1 | None = None
    poll_after_seconds: Annotated[
        float,
        Field(ge=0.1, le=300.0, allow_inf_nan=False),
    ] | None = None
    tool_calls: Annotated[tuple[ToolDispatchRefV1, ...], Field(max_length=128)] = ()
    error: ErrorEnvelopeV1 | None = None
    usage: UsageV1 = UsageV1()
    written_bytes: _NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_model_step_result(self) -> Self:
        outcomes = sum(
            (
                self.final_response_ref is not None,
                self.background_operation_ref is not None,
                bool(self.tool_calls),
                self.error is not None,
            )
        )
        if outcomes != 1:
            raise ValueError(
                "model-step result must contain final response, tool calls, or error"
            )
        if (self.background_operation_ref is None) != (
            self.poll_after_seconds is None
        ):
            raise ValueError(
                "background operation results must include a polling delay"
            )
        _assert_envelope_size(self)
        return self


class ToolResultRefV1(_DurableLoopModel):
    """Refs-only tool activity result."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    result_ref: ContentRefV1
    call_ordinal: _NonNegativeInt
    call_key: _Sha256
    request_hash: _Sha256
    tool_name: _ToolName
    status: ToolResultStatus
    written_bytes: _NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_envelope_size(self) -> Self:
        _assert_envelope_size(self)
        return self


class HumanWaitActivityResultV1(_DurableLoopModel):
    """Refs-only pending human request projection."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    request_ref: ContentRefV1
    request_id: _OpaqueId
    run_id: _OpaqueId
    generation: Annotated[int, Field(ge=1)]
    call_key: _Sha256
    event_name: Annotated[str, Field(min_length=1, max_length=192)]
    question_ref: ContentRefV1
    issued_at: datetime | None = None
    expires_at: datetime
    written_bytes: _NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_envelope_size(self) -> Self:
        _assert_envelope_size(self)
        return self


def canonical_hash(value: object) -> str:
    """Return a lower-case SHA-256 digest of canonical JSON."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def deterministic_model_step_key(run_id: str, step_index: int) -> str:
    """Derive the stable key for one model step."""
    return canonical_hash(
        {
            "kind": "model_step",
            "run_id": run_id,
            "schema_version": DURABLE_LOOP_SCHEMA_VERSION,
            "step_index": step_index,
        }
    )


def deterministic_call_key(
    *,
    run_id: str,
    step_index: int,
    call_ordinal: int,
    decision_hash: str,
    tool_name: str,
) -> str:
    """Derive one stable call key without trusting provider call IDs."""
    return canonical_hash(
        {
            "call_ordinal": call_ordinal,
            "decision_hash": decision_hash,
            "kind": "tool_call",
            "run_id": run_id,
            "schema_version": DURABLE_LOOP_SCHEMA_VERSION,
            "step_index": step_index,
            "tool_name": tool_name,
        }
    )


def tool_request_hash(
    *,
    tool_name: str,
    arguments: dict[str, object],
    behavior: ToolBehavior,
    provenance: ToolProvenance,
    argument_byte_limit: int = MAX_TOOL_ARGUMENT_BYTES,
    result_byte_limit: int = MAX_TOOL_RESULT_BYTES,
    policy_hash: str,
    catalog_hash: str,
    package_hash: str,
    workspace_ref: ContentRefV1 | None = None,
    sandbox_profile: SandboxExecutionProfile = SandboxExecutionProfile.PER_CALL,
    fault_profile: DurableFaultProfile = DurableFaultProfile.NONE,
    owner_hash: str = "0" * 64,
) -> str:
    """Bind a call key to exact arguments and immutable routing policy."""
    return canonical_hash(
        {
            "argument_byte_limit": argument_byte_limit,
            "arguments": arguments,
            "behavior": behavior.value,
            "catalog_hash": catalog_hash,
            "package_hash": package_hash,
            "owner_hash": owner_hash,
            "policy_hash": policy_hash,
            "provenance": provenance.value,
            "result_byte_limit": result_byte_limit,
            "sandbox_profile": sandbox_profile.value,
            "fault_profile": fault_profile.value,
            "tool_name": tool_name,
            "workspace_ref": (
                workspace_ref.model_dump(mode="json")
                if workspace_ref is not None
                else None
            ),
        }
    )


def deterministic_human_event_name(
    *, generation: int, request_id: str, nonce: str
) -> str:
    """Return a server-generated event name that is never reused."""
    digest = canonical_hash(
        {
            "generation": generation,
            "nonce": nonce,
            "request_id": request_id,
            "schema_version": DURABLE_LOOP_SCHEMA_VERSION,
        }
    )
    return f"answer:{generation}:{digest}"


def parse_durable_loop_document[ModelT: BaseModel](
    payload: bytes | str,
    model: type[ModelT],
    *,
    maximum_bytes: int = MAX_DURABLE_ENVELOPE_BYTES,
) -> ModelT:
    """Strictly parse one bounded durable-loop JSON object."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > maximum_bytes:
        raise DurableLoopProtocolDocumentError(
            "durable-loop document exceeds the byte limit"
        )
    try:
        decoded = decode_json_object(raw)
        return model.model_validate_json(canonical_json_bytes(decoded))
    except (
        DuplicateJsonKeyError,
        TypeError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise DurableLoopProtocolDocumentError(
            "invalid durable-loop protocol document"
        ) from exc


def _as_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _assert_envelope_size(model: BaseModel) -> None:
    if len(canonical_json_bytes(model)) > MAX_DURABLE_ENVELOPE_BYTES:
        raise ValueError("durable activity envelope exceeds the byte limit")
