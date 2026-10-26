"""Private harness-neutral checkpoint and reference contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from ._harness_binding import HarnessKind

# Version written into every persisted checkpoint contract for compatibility checks.
CHECKPOINT_SCHEMA_VERSION: Literal[1] = 1

# Maximum size of a pointer to externally stored content or continuation state.
MAX_REFERENCE_ENVELOPE_BYTES = 16 * 1024

# Maximum size of one request or result exchanged with an external tool executor.
MAX_EXTERNAL_WORK_ENVELOPE_BYTES = 64 * 1024

# Maximum metadata size for one ordered batch of external-work results.
MAX_EXTERNAL_WORK_RESULT_BATCH_BYTES = 128 * 1024

# Maximum control-state payload returned when a harness pauses or completes.
MAX_CHECKPOINT_OUTCOME_BYTES = 128 * 1024

# SHA-256 digests verify referenced content without placing that content in control state.
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

# Owner digests support current and future identity formats while remaining opaque.
OWNER_SCOPE_DIGEST_PATTERN = r"^[0-9a-f]{32,128}$"

# Shared limits keep persisted identity fields and reference storage validation aligned.
MAX_AGENT_SLUG_LENGTH = 128
MAX_IDENTITY_VALUE_LENGTH = 256
MAX_VERSION_VALUE_LENGTH = 128
MAX_CONTENT_TYPE_LENGTH = 255
MAX_OWNER_SCOPE_DIGEST_LENGTH = 128

type FrozenJsonValue = (
    str
    | int
    | float
    | bool
    | list[FrozenJsonValue]
    | tuple[FrozenJsonValue, ...]
    | Mapping[str, FrozenJsonValue]
    | None
)
type FrozenJsonObject = Mapping[str, FrozenJsonValue]
type _IdentityValue = Annotated[
    str,
    Field(min_length=1, max_length=MAX_IDENTITY_VALUE_LENGTH),
]
type _VersionValue = Annotated[
    str,
    Field(min_length=1, max_length=MAX_VERSION_VALUE_LENGTH),
]
type _CheckpointSchemaVersion = Literal[1]


@dataclass(frozen=True)
class SessionIdentityKey:
    """Names the fields that identify one owner-scoped agent session."""

    agent_slug: str
    owner_scope_digest: str
    entry_id: str
    session_id: str
    session_incarnation_id: str


@dataclass(frozen=True)
class TurnIdentityKey:
    """Names the session, run, and generation that identify one active turn."""

    session: SessionIdentityKey
    run_id: str
    turn_generation: int


@dataclass(frozen=True)
class ConnectorIdentityKey:
    """Names the connector and session authorized to resume persisted state."""

    harness_kind: HarnessKind
    connector_version: str
    connector_profile_id: str
    backend_namespace: str
    session: SessionIdentityKey


@dataclass(frozen=True)
class ExternalWorkOperationIdentity:
    """Track one requested tool action, including its order and retry-safe key.

    Answers: "Is this the exact operation we intended to run?"
    """

    sequence_index: int
    operation_id: str
    tool_call_id: str
    idempotency_key: str


@dataclass(frozen=True)
class ExternalWorkResultIdentity:
    """Label one tool result so it can be matched to the requested operation.

    Answers: "Does this result belong to that operation?"
    """

    sequence_index: int
    operation_id: str
    tool_call_id: str
    idempotency_key: str


@dataclass(frozen=True)
class ExternalWorkCommandIdentity:
    """Describe the approved tool implementation, schema, policy, and environment.

    Answers: "Can the currently available tool safely execute the approved command?"
    """

    adapter_kind: str
    adapter_version: str
    tool_name: str
    canonical_schema_digest: str
    policy_digest: str
    execution_placement: ExternalWorkPlacement
    implementation_compatibility_version: str


class CheckpointContractError(ValueError):
    """A persisted checkpoint value does not satisfy the active contract."""


class StorageKind(StrEnum):
    """Identifies the storage service holding referenced content or state."""

    BLOB = "blob"


class ContinuationRole(StrEnum):
    """Explains whether saved harness state resumes a turn or starts the next one."""

    MID_TURN = "mid_turn"
    TERMINAL_SEED = "terminal_seed"


class ExternalWorkPlacement(StrEnum):
    """Identifies where a requested tool operation must run."""

    RUNTIME = "runtime"
    REMOTE = "remote"
    SANDBOX = "sandbox"


class ExternalWorkStatus(StrEnum):
    """Records the final known result of work performed outside the harness."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ToolEvidenceStatus(StrEnum):
    """Summarizes what happened to a tool call for history and diagnostics."""

    REQUESTED = "requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class CheckpointFailureCategory(StrEnum):
    """Classifies stable failure domains used by Durable coordination."""

    RETRYABLE_TRANSPORT = "retryable_transport"
    TERMINAL_CONNECTOR = "terminal_connector"
    CONTINUATION = "continuation"
    CAPACITY = "capacity"
    CANCELLATION = "cancellation"
    EXTERNAL_WORK = "external_work"


class _CheckpointModel(BaseModel):
    """Base for strict persisted contracts that reject undeclared data."""

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class IntegrityMetadata(_CheckpointModel):
    """Describes the digest and size used to verify externally stored bytes."""

    algorithm: Literal["sha256"] = "sha256"
    digest: str = Field(pattern=_DIGEST_PATTERN)
    content_length: int = Field(ge=0)


class _SessionIdentity(_CheckpointModel):
    """Carries the stable identity shared by every operation in one session."""

    agent_slug: str = Field(min_length=1, max_length=MAX_AGENT_SLUG_LENGTH)
    owner_scope_digest: str = Field(pattern=OWNER_SCOPE_DIGEST_PATTERN)
    entry_id: _IdentityValue
    session_id: _IdentityValue
    session_incarnation_id: _IdentityValue

    def session_identity_key(self) -> SessionIdentityKey:
        """Return the stable business identity shared across one agent session."""
        return SessionIdentityKey(
            agent_slug=self.agent_slug,
            owner_scope_digest=self.owner_scope_digest,
            entry_id=self.entry_id,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
        )


class _TurnIdentity(_SessionIdentity):
    """Adds the active run and generation used to reject stale turn work."""

    run_id: _IdentityValue
    turn_generation: int = Field(ge=0)

    def turn_identity_key(self) -> TurnIdentityKey:
        """Return the identity used to reject work from an older turn generation."""
        return TurnIdentityKey(
            session=self.session_identity_key(),
            run_id=self.run_id,
            turn_generation=self.turn_generation,
        )


class _ConnectorIdentity(_TurnIdentity):
    """Adds the exact harness connector that owns resumable state."""

    harness_kind: HarnessKind
    connector_version: _VersionValue
    connector_profile_id: _IdentityValue
    backend_namespace: _IdentityValue

    def connector_identity_key(self) -> ConnectorIdentityKey:
        """Return the identity of the harness connector authorized to resume state."""
        return ConnectorIdentityKey(
            harness_kind=self.harness_kind,
            connector_version=self.connector_version,
            connector_profile_id=self.connector_profile_id,
            backend_namespace=self.backend_namespace,
            session=self.session_identity_key(),
        )


class ContentRef(_CheckpointModel):
    """Points to stored content without placing the content in Durable state."""

    schema_version: _CheckpointSchemaVersion
    storage_kind: StorageKind
    opaque_content_key: str = Field(min_length=1, max_length=2048)
    content_digest: str = Field(pattern=_DIGEST_PATTERN)
    content_length: int = Field(ge=0)
    content_type: str = Field(min_length=1, max_length=MAX_CONTENT_TYPE_LENGTH)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    integrity_metadata: IntegrityMetadata

    @model_validator(mode="after")
    def validate_integrity(self) -> ContentRef:
        """Keep the content pointer consistent with its integrity evidence."""
        validate_reference_expiry(
            self.created_at,
            self.expires_at,
            subject="content reference",
        )
        if self.integrity_metadata.digest != self.content_digest:
            raise ValueError("content digest does not match integrity metadata")
        if self.integrity_metadata.content_length != self.content_length:
            raise ValueError("content length does not match integrity metadata")
        return self


class ContinuationRef(_ConnectorIdentity):
    """Points to harness-native state needed to continue agent execution."""

    schema_version: _CheckpointSchemaVersion
    storage_kind: StorageKind
    opaque_state_key: str = Field(min_length=1, max_length=2048)
    state_format_version: _VersionValue
    continuation_role: ContinuationRole
    created_at: AwareDatetime
    expires_at: AwareDatetime
    integrity_metadata: IntegrityMetadata

    @model_validator(mode="after")
    def validate_expiry(self) -> ContinuationRef:
        """Require saved harness state to have a valid authorized lifetime."""
        validate_reference_expiry(
            self.created_at,
            self.expires_at,
            subject="continuation",
        )
        return self


class CheckpointUsage(_CheckpointModel):
    """Captures token usage reported when execution reaches a checkpoint."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)


class _CheckpointToolEvidenceBase(_CheckpointModel):
    """Names one tool call represented in checkpoint evidence."""

    tool_call_id: _IdentityValue
    tool_name: _IdentityValue


class RequestedToolEvidence(_CheckpointToolEvidenceBase):
    """Records a tool call that has not produced a terminal result."""

    status: Literal[ToolEvidenceStatus.REQUESTED] = ToolEvidenceStatus.REQUESTED


class SucceededToolEvidence(_CheckpointToolEvidenceBase):
    """Records a successful tool call and its referenced result."""

    status: Literal[ToolEvidenceStatus.SUCCEEDED] = ToolEvidenceStatus.SUCCEEDED
    result_ref: ContentRef


class FailedToolEvidence(_CheckpointToolEvidenceBase):
    """Records a failed tool call and its referenced structured failure."""

    status: Literal[ToolEvidenceStatus.FAILED] = ToolEvidenceStatus.FAILED
    result_ref: ContentRef


class CancelledToolEvidence(_CheckpointToolEvidenceBase):
    """Records a confirmed cancellation and its structured terminal receipt."""

    status: Literal[ToolEvidenceStatus.CANCELLED] = ToolEvidenceStatus.CANCELLED
    result_ref: ContentRef


class UnknownToolEvidence(_CheckpointToolEvidenceBase):
    """Records a tool call whose terminal effect could not be established."""

    status: Literal[ToolEvidenceStatus.OUTCOME_UNKNOWN] = (
        ToolEvidenceStatus.OUTCOME_UNKNOWN
    )


type CheckpointToolEvidence = Annotated[
    RequestedToolEvidence
    | SucceededToolEvidence
    | FailedToolEvidence
    | CancelledToolEvidence
    | UnknownToolEvidence,
    Field(discriminator="status"),
]


class CheckpointFailure(_CheckpointModel):
    """Provides a stable, safe explanation of why checkpoint execution failed."""

    category: CheckpointFailureCategory
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=1024)


class RetryableCheckpointFailure(CheckpointFailure):
    """Names a transport failure proven safe for native Durable retry."""

    category: Literal[CheckpointFailureCategory.RETRYABLE_TRANSPORT]


class RetryGuidance(_CheckpointModel):
    """Tells Durable orchestration when and whether another attempt is allowed."""

    retry_after_ms: int = Field(ge=0, le=3_600_000)
    remaining_attempts: int = Field(ge=1, le=100)


class CheckpointRequest(_ConnectorIdentity):
    """Describes one authorized agent turn submitted to a checkpoint connector."""

    schema_version: _CheckpointSchemaVersion
    deadline: AwareDatetime
    prompt_ref: ContentRef
    session_seed: ContinuationRef | None = None

    @model_validator(mode="after")
    def validate_session_seed(self) -> CheckpointRequest:
        """Accept only completed-turn state that belongs to this session."""
        seed = self.session_seed
        if seed is None:
            return self
        _validate_terminal_session_seed(self, seed)
        return self


class ExternalWorkRequest(_TurnIdentity):
    """Describes tool work that Durable orchestration must execute outside the harness."""

    schema_version: _CheckpointSchemaVersion
    sequence_index: int = Field(ge=0)
    operation_id: _IdentityValue
    tool_call_id: _IdentityValue
    tool_name: _IdentityValue
    arguments_ref: ContentRef
    adapter_kind: _VersionValue
    adapter_version: _VersionValue
    canonical_schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    execution_placement: ExternalWorkPlacement
    workspace_ref: str | None = Field(default=None, min_length=1, max_length=1024)
    implementation_compatibility_version: _VersionValue
    policy_snapshot: FrozenJsonObject
    idempotency_key: str = Field(min_length=1, max_length=512)
    deadline: AwareDatetime

    def operation_identity(self) -> ExternalWorkOperationIdentity:
        """Return the ordered idempotent identity of this request."""
        return ExternalWorkOperationIdentity(
            sequence_index=self.sequence_index,
            operation_id=self.operation_id,
            tool_call_id=self.tool_call_id,
            idempotency_key=self.idempotency_key,
        )

    def command_identity(self) -> ExternalWorkCommandIdentity:
        """Return every field that gates command compatibility."""
        return ExternalWorkCommandIdentity(
            adapter_kind=self.adapter_kind,
            adapter_version=self.adapter_version,
            tool_name=self.tool_name,
            canonical_schema_digest=self.canonical_schema_digest,
            policy_digest=self.policy_digest,
            execution_placement=self.execution_placement,
            implementation_compatibility_version=(
                self.implementation_compatibility_version
            ),
        )

    @model_validator(mode="after")
    def validate_workspace(self) -> ExternalWorkRequest:
        """Require workspace access only when work runs in the sandbox."""
        if self.execution_placement is ExternalWorkPlacement.SANDBOX:
            if self.workspace_ref is None:
                raise ValueError("sandbox external work requires a workspace reference")
        elif self.workspace_ref is not None:
            raise ValueError("workspace reference is valid only for sandbox external work")
        return self

    @field_validator("policy_snapshot", mode="after")
    @classmethod
    def freeze_policy_snapshot(cls, value: FrozenJsonObject) -> FrozenJsonObject:
        """Prevent approved execution policy from changing after admission."""
        return _freeze_json_object(value)

    @field_serializer("policy_snapshot")
    def serialize_policy_snapshot(self, value: FrozenJsonObject) -> JsonValue:
        """Convert immutable policy data into its persisted JSON shape."""
        return _thaw_json_value(value)


class ExternalWorkResult(_TurnIdentity):
    """Returns the referenced result of external work to the waiting harness."""

    schema_version: _CheckpointSchemaVersion
    sequence_index: int = Field(ge=0)
    operation_id: _IdentityValue
    tool_call_id: _IdentityValue
    idempotency_key: str = Field(min_length=1, max_length=512)
    status: ExternalWorkStatus
    result_ref: ContentRef
    diagnostics: Mapping[str, str] = Field(default_factory=dict)

    def result_identity(self) -> ExternalWorkResultIdentity:
        """Return the ordered operation identity represented by this result."""
        return ExternalWorkResultIdentity(
            sequence_index=self.sequence_index,
            operation_id=self.operation_id,
            tool_call_id=self.tool_call_id,
            idempotency_key=self.idempotency_key,
        )

    @model_validator(mode="after")
    def validate_diagnostics(self) -> ExternalWorkResult:
        """Bound operational diagnostics before they enter Durable control state."""
        if len(self.diagnostics) > 16:
            raise ValueError("external work diagnostics exceed the entry limit")
        if any(
            not key
            or len(key) > 128
            or len(value) > 1024
            for key, value in self.diagnostics.items()
        ):
            raise ValueError("external work diagnostics exceed field limits")
        object.__setattr__(self, "diagnostics", MappingProxyType(dict(self.diagnostics)))
        return self

    @field_serializer("diagnostics")
    def serialize_diagnostics(self, value: Mapping[str, str]) -> dict[str, str]:
        """Convert immutable diagnostics into their persisted JSON shape."""
        return dict(value)


class ExternalWorkResultBatch(_CheckpointModel):
    """Carries one complete result batch in original tool-call order."""

    schema_version: _CheckpointSchemaVersion
    results: tuple[ExternalWorkResult, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_order_and_identity(self) -> ExternalWorkResultBatch:
        """Require one unique result at every original request position."""
        operation_ids: set[str] = set()
        tool_call_ids: set[str] = set()
        idempotency_keys: set[str] = set()
        turn_identity: TurnIdentityKey | None = None
        for expected_index, result in enumerate(self.results):
            if result.sequence_index != expected_index:
                raise ValueError(
                    "external work results must be in original tool-call order"
                )
            if (
                result.operation_id in operation_ids
                or result.tool_call_id in tool_call_ids
                or result.idempotency_key in idempotency_keys
            ):
                raise ValueError("external work result identities must be unique")
            operation_ids.add(result.operation_id)
            tool_call_ids.add(result.tool_call_id)
            idempotency_keys.add(result.idempotency_key)
            if turn_identity is None:
                turn_identity = result.turn_identity_key()
            elif turn_identity != result.turn_identity_key():
                raise ValueError("external work results must belong to one active turn")
        return self


class ExternalWorkPendingOperation(_TurnIdentity):
    """Names one authoritative pending operation accepted by the Entity."""

    sequence_index: int = Field(ge=0)
    operation_id: _IdentityValue
    tool_call_id: _IdentityValue
    idempotency_key: str = Field(min_length=1, max_length=512)

    def operation_identity(self) -> ExternalWorkOperationIdentity:
        """Return the ordered idempotent identity of the pending operation."""
        return ExternalWorkOperationIdentity(
            sequence_index=self.sequence_index,
            operation_id=self.operation_id,
            tool_call_id=self.tool_call_id,
            idempotency_key=self.idempotency_key,
        )

    def expected_result_identity(self) -> ExternalWorkResultIdentity:
        """Return the exact result identity this pending operation accepts."""
        return ExternalWorkResultIdentity(
            sequence_index=self.sequence_index,
            operation_id=self.operation_id,
            tool_call_id=self.tool_call_id,
            idempotency_key=self.idempotency_key,
        )


class ExternalWorkResumeRequest(_ConnectorIdentity):
    """Binds one continuation to its exact pending operations and completed results."""

    schema_version: _CheckpointSchemaVersion
    continuation: ContinuationRef
    pending_operations: tuple[ExternalWorkPendingOperation, ...] = Field(min_length=1)
    results: ExternalWorkResultBatch

    @model_validator(mode="after")
    def validate_resume_binding(self) -> ExternalWorkResumeRequest:
        """Require one active turn and an exact ordered operation/result match."""
        _validate_continuation_role(
            self.continuation,
            expected_role=ContinuationRole.MID_TURN,
            subject="external work resume",
        )
        if self.connector_identity_key() != self.continuation.connector_identity_key():
            raise ValueError("continuation identity does not match resume identity")
        turn_identity = self.turn_identity_key()
        if turn_identity != self.continuation.turn_identity_key():
            raise ValueError("continuation does not match the active turn")
        if len(self.pending_operations) != len(self.results.results):
            raise ValueError("external work resume requires one result per pending operation")
        for expected_index, (pending, result) in enumerate(
            zip(self.pending_operations, self.results.results, strict=True)
        ):
            if pending.sequence_index != expected_index:
                raise ValueError(
                    "pending external work must be in original tool-call order"
                )
            if (
                pending.turn_identity_key() != turn_identity
                or result.turn_identity_key() != turn_identity
            ):
                raise ValueError("external work resume values must belong to one active turn")
            if pending.expected_result_identity() != result.result_identity():
                raise ValueError(
                    "external work result does not match the pending operation"
                )
        return self


class _CheckpointOutcomeBase(_ConnectorIdentity):
    """Carries metadata shared by every way checkpoint execution can pause or finish."""

    schema_version: _CheckpointSchemaVersion
    deadline: AwareDatetime
    provider_id: _IdentityValue
    model_id: _IdentityValue
    usage: CheckpointUsage = Field(default_factory=CheckpointUsage)
    tool_evidence: tuple[CheckpointToolEvidence, ...] = ()


class TurnCompleted(_CheckpointOutcomeBase):
    """Reports a completed turn and saves state that can seed the next turn."""

    kind: Literal["turn_completed"] = "turn_completed"
    result_ref: ContentRef
    continuation: ContinuationRef

    @model_validator(mode="after")
    def validate_continuation(self) -> TurnCompleted:
        """Save only next-turn state owned by the completed active turn."""
        _validate_active_turn_continuation(
            self,
            self.continuation,
            expected_role=ContinuationRole.TERMINAL_SEED,
            subject="completed turn",
        )
        return self


class ExternalWorkRequested(_CheckpointOutcomeBase):
    """Pauses the harness while Durable orchestration performs requested tool work."""

    kind: Literal["external_work_requested"] = "external_work_requested"
    requests: tuple[ExternalWorkRequest, ...] = Field(min_length=1)
    continuation: ContinuationRef

    @model_validator(mode="after")
    def validate_requests(self) -> ExternalWorkRequested:
        """Keep paused work dispatchable and bound to the same active turn."""
        _validate_active_turn_continuation(
            self,
            self.continuation,
            expected_role=ContinuationRole.MID_TURN,
            subject="external work",
        )
        operation_ids: set[str] = set()
        tool_call_ids: set[str] = set()
        idempotency_keys: set[str] = set()
        for expected_index, request in enumerate(self.requests):
            if request.sequence_index != expected_index:
                raise ValueError(
                    "external work requests must be in original tool-call order"
                )
            _validate_external_work_identity(self, request)
            encode_external_work_request(request)
            if (
                request.operation_id in operation_ids
                or request.tool_call_id in tool_call_ids
                or request.idempotency_key in idempotency_keys
            ):
                raise ValueError("external work request identities must be unique")
            operation_ids.add(request.operation_id)
            tool_call_ids.add(request.tool_call_id)
            idempotency_keys.add(request.idempotency_key)
        return self


class HumanInputRequested(_CheckpointOutcomeBase):
    """Pauses the harness until an authorized person supplies requested input."""

    kind: Literal["human_input_requested"] = "human_input_requested"
    question_id: _IdentityValue
    question_ref: ContentRef
    input_schema: FrozenJsonObject
    continuation: ContinuationRef

    @model_validator(mode="after")
    def validate_continuation(self) -> HumanInputRequested:
        """Keep the human-input pause bound to the same active turn."""
        _validate_active_turn_continuation(
            self,
            self.continuation,
            expected_role=ContinuationRole.MID_TURN,
            subject="human input",
        )
        return self

    @field_validator("input_schema", mode="after")
    @classmethod
    def freeze_input_schema(cls, value: FrozenJsonObject) -> FrozenJsonObject:
        """Prevent requested input requirements from changing while paused."""
        return _freeze_json_object(value)

    @field_serializer("input_schema")
    def serialize_input_schema(self, value: FrozenJsonObject) -> JsonValue:
        """Convert immutable input requirements into persisted JSON."""
        return _thaw_json_value(value)


class RetryRequested(_CheckpointOutcomeBase):
    """Asks Durable orchestration to retry a temporary harness or provider failure."""

    kind: Literal["retry_requested"] = "retry_requested"
    failure: RetryableCheckpointFailure
    guidance: RetryGuidance

    @model_validator(mode="after")
    def validate_retry_safety(self) -> RetryRequested:
        """Reject retry requests when an external effect may still be pending."""
        unsafe_statuses = {
            ToolEvidenceStatus.REQUESTED,
            ToolEvidenceStatus.OUTCOME_UNKNOWN,
        }
        if any(evidence.status in unsafe_statuses for evidence in self.tool_evidence):
            raise ValueError(
                "retry requests cannot contain pending or outcome-unknown tool evidence"
            )
        return self


class TurnFailed(_CheckpointOutcomeBase):
    """Reports a terminal turn failure that should not be retried automatically."""

    kind: Literal["turn_failed"] = "turn_failed"
    failure: CheckpointFailure


type CheckpointOutcome = Annotated[
    TurnCompleted
    | ExternalWorkRequested
    | HumanInputRequested
    | RetryRequested
    | TurnFailed,
    Field(discriminator="kind"),
]


class HarnessCheckpointConnector(Protocol):
    """Defines how a harness runs until it completes or reaches a durable pause."""

    async def run_until_checkpoint(
        self,
        request: CheckpointRequest,
    ) -> CheckpointOutcome:
        """Run an admitted turn until it completes or needs Durable coordination."""
        ...

    async def resume_from_checkpoint(
        self,
        request: ExternalWorkResumeRequest,
    ) -> CheckpointOutcome:
        """Resume only after verifying native pending calls match the request."""
        ...


def _freeze_json_object(value: FrozenJsonObject) -> FrozenJsonObject:
    return MappingProxyType(
        {key: _freeze_json_value(item) for key, item in value.items()}
    )


def _freeze_json_value(value: FrozenJsonValue) -> FrozenJsonValue:
    if isinstance(value, Mapping):
        return _freeze_json_object(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _thaw_json_value(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def is_valid_owner_scope_digest(value: str) -> bool:
    """Return whether an owner digest satisfies the persisted identity contract."""

    return re.fullmatch(OWNER_SCOPE_DIGEST_PATTERN, value) is not None


def validate_reference_expiry(
    created_at: datetime,
    expires_at: datetime,
    *,
    subject: str,
) -> None:
    """Require an aware expiry later than reference creation."""

    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError(f"{subject} expiry must include a timezone")
    if expires_at <= created_at:
        raise ValueError(f"{subject} expiry must follow creation")


def _validate_continuation_role(
    continuation: ContinuationRef,
    *,
    expected_role: ContinuationRole,
    subject: str,
) -> None:
    role_label = (
        "terminal"
        if expected_role is ContinuationRole.TERMINAL_SEED
        else "mid-turn"
    )
    if continuation.continuation_role is not expected_role:
        raise ValueError(f"{subject} requires a {role_label} continuation")


def _validate_connector_session_identity(
    owner: CheckpointRequest | _CheckpointOutcomeBase,
    continuation: ContinuationRef,
) -> None:
    if owner.connector_identity_key() != continuation.connector_identity_key():
        raise ValueError("continuation identity does not match checkpoint identity")


def _validate_active_turn_continuation(
    owner: _CheckpointOutcomeBase,
    continuation: ContinuationRef,
    *,
    expected_role: ContinuationRole,
    subject: str,
) -> None:
    _validate_continuation_role(
        continuation,
        expected_role=expected_role,
        subject=subject,
    )
    _validate_connector_session_identity(owner, continuation)
    if owner.turn_identity_key() != continuation.turn_identity_key():
        raise ValueError("continuation does not match the active turn")


def _validate_terminal_session_seed(
    request: CheckpointRequest,
    continuation: ContinuationRef,
) -> None:
    _validate_continuation_role(
        continuation,
        expected_role=ContinuationRole.TERMINAL_SEED,
        subject="checkpoint request session seed",
    )
    _validate_connector_session_identity(request, continuation)


def _validate_external_work_identity(
    outcome: _CheckpointOutcomeBase,
    request: ExternalWorkRequest,
) -> None:
    if outcome.turn_identity_key() != request.turn_identity_key():
        raise ValueError("external work identity does not match checkpoint identity")
    if outcome.deadline != request.deadline:
        raise ValueError("external work deadline does not match checkpoint deadline")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


_CONTENT_REF_ADAPTER = TypeAdapter(ContentRef)
_CONTINUATION_REF_ADAPTER = TypeAdapter(ContinuationRef)
_EXTERNAL_WORK_REQUEST_ADAPTER = TypeAdapter(ExternalWorkRequest)
_EXTERNAL_WORK_RESULT_ADAPTER = TypeAdapter(ExternalWorkResult)
_EXTERNAL_WORK_RESULT_BATCH_ADAPTER = TypeAdapter(ExternalWorkResultBatch)
_EXTERNAL_WORK_RESUME_REQUEST_ADAPTER = TypeAdapter(ExternalWorkResumeRequest)
_CHECKPOINT_OUTCOME_ADAPTER: TypeAdapter[CheckpointOutcome] = TypeAdapter(
    CheckpointOutcome
)


def _encode_contract(model: BaseModel, *, maximum: int, name: str) -> bytes:
    try:
        validated = type(model).model_validate(model.model_dump(mode="python"), strict=True)
        payload = validated.model_dump_json().encode("utf-8")
    except (ValidationError, ValueError, TypeError):
        raise CheckpointContractError(f"{name} is invalid.") from None
    if len(payload) > maximum:
        raise CheckpointContractError(f"{name} exceeds its serialized size limit.")
    return payload


def _decode_contract[T](
    adapter: TypeAdapter[T],
    payload: bytes,
    *,
    maximum: int,
    name: str,
) -> T:
    if len(payload) > maximum:
        raise CheckpointContractError(f"{name} exceeds its serialized size limit.")
    try:
        json.loads(payload, object_pairs_hook=_reject_duplicate_json_keys)
        return adapter.validate_json(payload, strict=True)
    except (ValidationError, ValueError, UnicodeDecodeError):
        raise CheckpointContractError(f"{name} is invalid.") from None


def encode_content_ref(reference: ContentRef) -> bytes:
    return _encode_contract(
        reference,
        maximum=MAX_REFERENCE_ENVELOPE_BYTES,
        name="Content reference",
    )


def decode_content_ref(payload: bytes) -> ContentRef:
    return _decode_contract(
        _CONTENT_REF_ADAPTER,
        payload,
        maximum=MAX_REFERENCE_ENVELOPE_BYTES,
        name="Content reference",
    )


def encode_continuation_ref(reference: ContinuationRef) -> bytes:
    return _encode_contract(
        reference,
        maximum=MAX_REFERENCE_ENVELOPE_BYTES,
        name="Continuation reference",
    )


def decode_continuation_ref(payload: bytes) -> ContinuationRef:
    return _decode_contract(
        _CONTINUATION_REF_ADAPTER,
        payload,
        maximum=MAX_REFERENCE_ENVELOPE_BYTES,
        name="Continuation reference",
    )


def encode_external_work_request(request: ExternalWorkRequest) -> bytes:
    return _encode_contract(
        request,
        maximum=MAX_EXTERNAL_WORK_ENVELOPE_BYTES,
        name="External work request",
    )


def decode_external_work_request(payload: bytes) -> ExternalWorkRequest:
    return _decode_contract(
        _EXTERNAL_WORK_REQUEST_ADAPTER,
        payload,
        maximum=MAX_EXTERNAL_WORK_ENVELOPE_BYTES,
        name="External work request",
    )


def encode_external_work_result(result: ExternalWorkResult) -> bytes:
    return _encode_contract(
        result,
        maximum=MAX_EXTERNAL_WORK_ENVELOPE_BYTES,
        name="External work result",
    )


def decode_external_work_result(payload: bytes) -> ExternalWorkResult:
    return _decode_contract(
        _EXTERNAL_WORK_RESULT_ADAPTER,
        payload,
        maximum=MAX_EXTERNAL_WORK_ENVELOPE_BYTES,
        name="External work result",
    )


def encode_external_work_result_batch(batch: ExternalWorkResultBatch) -> bytes:
    return _encode_contract(
        batch,
        maximum=MAX_EXTERNAL_WORK_RESULT_BATCH_BYTES,
        name="External work result batch",
    )


def decode_external_work_result_batch(payload: bytes) -> ExternalWorkResultBatch:
    return _decode_contract(
        _EXTERNAL_WORK_RESULT_BATCH_ADAPTER,
        payload,
        maximum=MAX_EXTERNAL_WORK_RESULT_BATCH_BYTES,
        name="External work result batch",
    )


def encode_external_work_resume_request(request: ExternalWorkResumeRequest) -> bytes:
    return _encode_contract(
        request,
        maximum=MAX_CHECKPOINT_OUTCOME_BYTES,
        name="External work resume request",
    )


def decode_external_work_resume_request(payload: bytes) -> ExternalWorkResumeRequest:
    return _decode_contract(
        _EXTERNAL_WORK_RESUME_REQUEST_ADAPTER,
        payload,
        maximum=MAX_CHECKPOINT_OUTCOME_BYTES,
        name="External work resume request",
    )


def encode_checkpoint_outcome(outcome: CheckpointOutcome) -> bytes:
    return _encode_contract(
        outcome,
        maximum=MAX_CHECKPOINT_OUTCOME_BYTES,
        name="Checkpoint outcome",
    )


def decode_checkpoint_outcome(payload: bytes) -> CheckpointOutcome:
    return _decode_contract(
        _CHECKPOINT_OUTCOME_ADAPTER,
        payload,
        maximum=MAX_CHECKPOINT_OUTCOME_BYTES,
        name="Checkpoint outcome",
    )
