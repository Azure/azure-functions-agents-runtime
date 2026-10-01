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
DURABLE_LOOP_ORCHESTRATOR_V4_NAME = "durable_agent_turn_orchestrator_v4"
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
MAX_SKILL_CATALOG_RECORDS = 10_000
MAX_SKILL_CATALOG_METADATA_BYTES = 8 * 1024 * 1024
MAX_INITIAL_SKILL_METADATA_RECORDS = 32
MAX_INITIAL_SKILL_METADATA_BYTES = 24 * 1024
MAX_SKILL_SEARCH_RESULT_BYTES = 256 * 1024
MAX_SKILL_CONTENT_BYTES = 2 * 1024 * 1024
MAX_SKILL_REFERENCE_FILES = 64
MAX_LOADED_SKILLS = 64
MAX_RUN_ARTIFACTS_PER_PAGE = 256
MAX_TRIGGER_LEDGER_RECORDS_PER_PAGE = 128

_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_CONTENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_RELATIVE_SKILL_PATH_PATTERN = re.compile(
    r"^(?:SKILL\.md|references/[A-Za-z0-9][A-Za-z0-9_.\-/]{0,511})$"
)
_PUBLIC_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_./?=&%-]{0,511}$")
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
SchemaVersion2 = Literal["2"]
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


class DurableChatModelMode(StrEnum):
    """The immutable model delivery mode selected for a chat observation run."""

    FOREGROUND = "foreground"
    BACKGROUND = "background"


class DurableFaultProfile(StrEnum):
    """A bounded deterministic live-qualification fault."""

    NONE = "none"
    MODEL_APIM_429_ONCE = "model_apim_429_once"
    MODEL_TIMEOUT_ONCE = "model_timeout_once"
    TOOL_ACTIVITY_ACK_LOSS_ONCE = "tool_activity_ack_loss_once"
    SANDBOX_LOSS_AFTER_CHECKPOINT = "sandbox_loss_after_checkpoint"
    CLEANUP_FAILURE_ONCE = "cleanup_failure_once"
    COMMIT_ACK_LOSS_ONCE = "commit_ack_loss_once"


class DurableObjectReferenceState(StrEnum):
    """The CAS lifecycle for one canonical retained object."""

    PENDING = "pending"
    LIVE = "live"
    DELETING = "deleting"
    ABSENT = "absent"


class DurableTriggerAdmissionState(StrEnum):
    """The durable disposition of one staged trigger delivery."""

    PENDING = "pending"
    ADMITTED = "admitted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class DurableTriggerType(StrEnum):
    """A trigger surface admitted by the durable loop."""

    HTTP = "http"
    TIMER = "timer"
    CONNECTOR = "connector"


class DurablePublicEventType(StrEnum):
    """The generic schema-v2 public observation vocabulary."""

    RUN_STARTED = "run_started"
    SKILL_SEARCH_COMPLETED = "skill_search_completed"
    SKILL_LOAD_STARTED = "skill_load_started"
    SKILL_LOAD_COMPLETED = "skill_load_completed"
    MODEL_PROGRESS = "model_progress"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    HUMAN_INPUT_REQUIRED = "human_input_required"
    ASSISTANT_DELTA = "assistant_delta"
    MESSAGE_COMMITTED = "message_committed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    OBSERVATION_DEGRADED = "observation_degraded"


class _DurableLoopModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class DurableChatStartOptionsV1(_DurableLoopModel):
    """The explicit browser opt-in accepted by the durable start route."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    stream_response: bool

    def freeze(self, *, model_mode: DurableChatModelMode) -> DurableChatRunOptionsV1:
        """Bind the browser request to the effective delivery mode."""
        if (
            self.stream_response
            and model_mode is DurableChatModelMode.BACKGROUND
        ):
            raise ValueError(
                "chat streaming is not supported for background model mode"
            )
        return DurableChatRunOptionsV1(
            stream_response=self.stream_response,
            model_mode=model_mode,
        )


class DurableChatRunOptionsV1(_DurableLoopModel):
    """Frozen observation options persisted only for an opted-in chat run."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    stream_response: bool
    model_mode: DurableChatModelMode

    @model_validator(mode="after")
    def validate_streaming_mode(self) -> Self:
        if self.stream_response and self.model_mode is DurableChatModelMode.BACKGROUND:
            raise ValueError("chat streaming is not supported for background model mode")
        return self


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


class DurableLoopBudgetV2(DurableLoopBudgetV1):
    """V2 limits for runtime-owned skill search and load operations."""

    schema_version: SchemaVersion2 = "2"  # type: ignore[assignment]
    max_skill_searches: Annotated[int, Field(ge=0, le=64)] = 8
    max_skill_loads: Annotated[int, Field(ge=0, le=64)] = 16
    max_skill_search_result_bytes: Annotated[
        int,
        Field(ge=1024, le=MAX_SKILL_SEARCH_RESULT_BYTES),
    ] = 32 * 1024
    max_loaded_skill_bytes: Annotated[
        int,
        Field(ge=1024, le=MAX_SKILL_CONTENT_BYTES),
    ] = 512 * 1024


class DurableRetentionPolicyV1(_DurableLoopModel):
    """Frozen artifact-specific retention durations for one admitted run."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    event_result_seconds: Annotated[
        int,
        Field(ge=60 * 60, le=365 * 24 * 60 * 60),
    ] = 30 * 24 * 60 * 60
    receipt_seconds: Annotated[
        int,
        Field(ge=60 * 60, le=730 * 24 * 60 * 60),
    ] = 90 * 24 * 60 * 60
    skill_grace_seconds: Annotated[
        int,
        Field(ge=24 * 60 * 60, le=365 * 24 * 60 * 60),
    ] = 30 * 24 * 60 * 60
    human_content_seconds: Annotated[
        int,
        Field(ge=0, le=365 * 24 * 60 * 60),
    ] = 30 * 24 * 60 * 60
    session_seconds: Annotated[
        int,
        Field(ge=24 * 60 * 60, le=365 * 24 * 60 * 60),
    ] = 30 * 24 * 60 * 60
    tombstone_seconds: Annotated[
        int,
        Field(ge=60 * 60, le=730 * 24 * 60 * 60),
    ] = 90 * 24 * 60 * 60
    trigger_admission_deadline_seconds: Annotated[
        int,
        Field(ge=5 * 60, le=30 * 24 * 60 * 60),
    ] = 7 * 24 * 60 * 60

    @model_validator(mode="after")
    def validate_retention_order(self) -> Self:
        if self.receipt_seconds < self.event_result_seconds:
            raise ValueError("receipt retention must not be shorter than result retention")
        if self.human_content_seconds > self.event_result_seconds:
            raise ValueError("human content retention must not exceed result retention")
        if self.tombstone_seconds < self.receipt_seconds:
            raise ValueError("tombstone retention must not be shorter than receipt retention")
        return self


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


class DurableRunIdentityV2(DurableRunIdentityV1):
    """V2 immutable identity with skill, access, and retention bindings."""

    schema_version: SchemaVersion2 = "2"  # type: ignore[assignment]
    budget: DurableLoopBudgetV2
    skill_catalog_hash: _Sha256
    access_namespace_hash: _Sha256
    retention_policy: DurableRetentionPolicyV1


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


class DurableSkillMetadataV1(_DurableLoopModel):
    """Bounded model-visible metadata for one immutable instruction-only skill."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    skill_id: _OpaqueId
    display_name: Annotated[str, Field(min_length=1, max_length=256)]
    selection_description: Annotated[str, Field(min_length=1, max_length=320)]
    version: Annotated[str, Field(min_length=1, max_length=128)]
    content_hash: _Sha256
    tags: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=64)], ...],
        Field(max_length=32),
    ] = ()
    executable: Literal[False] = False

    @model_validator(mode="after")
    def validate_tags(self) -> Self:
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("skill metadata tags must be unique")
        return self


class DurableSkillCatalogSnapshotV1(_DurableLoopModel):
    """Internal immutable provider snapshot persisted outside Durable history."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    provider_id: _OpaqueId
    catalog_revision: Annotated[str, Field(min_length=1, max_length=128)]
    catalog_hash: _Sha256
    metadata: Annotated[
        tuple[DurableSkillMetadataV1, ...],
        Field(max_length=MAX_SKILL_CATALOG_RECORDS),
    ]
    snapshot_token: Annotated[str, Field(min_length=1, max_length=4096)]
    retain_until: datetime

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        catalog_revision: str,
        metadata: tuple[DurableSkillMetadataV1, ...],
        snapshot_token: str,
        retain_until: datetime,
    ) -> DurableSkillCatalogSnapshotV1:
        """Create a snapshot with its canonical catalog hash."""
        catalog_hash = canonical_hash(
            [item.model_dump(mode="json") for item in metadata]
        )
        return cls(
            provider_id=provider_id,
            catalog_revision=catalog_revision,
            catalog_hash=catalog_hash,
            metadata=metadata,
            snapshot_token=snapshot_token,
            retain_until=retain_until,
        )

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        _as_utc(self.retain_until, "retain_until")
        skill_ids = [item.skill_id for item in self.metadata]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("skill catalog IDs must be unique")
        expected = canonical_hash(
            [item.model_dump(mode="json") for item in self.metadata]
        )
        if self.catalog_hash != expected:
            raise ValueError("skill catalog hash mismatch")
        serialized_metadata = [item.model_dump(mode="json") for item in self.metadata]
        if len(canonical_json_bytes(serialized_metadata)) > MAX_SKILL_CATALOG_METADATA_BYTES:
            raise ValueError("skill catalog metadata exceeds the byte limit")
        return self


class DurableSkillMetadataPageV1(_DurableLoopModel):
    """One bounded deterministic metadata search page."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    catalog_hash: _Sha256
    query_hash: _Sha256
    page_size: Annotated[int, Field(ge=1, le=256)]
    metadata: Annotated[
        tuple[DurableSkillMetadataV1, ...],
        Field(max_length=256),
    ]
    next_cursor: Annotated[str, Field(min_length=1, max_length=2048)] | None = None

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if len(self.metadata) > self.page_size:
            raise ValueError("skill metadata page exceeds its frozen page size")
        if len(canonical_json_bytes(self)) > MAX_SKILL_SEARCH_RESULT_BYTES:
            raise ValueError("skill metadata page exceeds the byte limit")
        return self


class DurableSkillContentFileV1(_DurableLoopModel):
    """One canonical UTF-8 file in an instruction-only skill bundle."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=_RELATIVE_SKILL_PATH_PATTERN.pattern,
        ),
    ]
    content: Annotated[str, Field(max_length=MAX_SKILL_CONTENT_BYTES)]

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        parts = self.relative_path.split("/")
        if any(part in {".", ".."} for part in parts):
            raise ValueError("skill content paths must stay within the skill root")
        if self.relative_path != "SKILL.md" and not self.relative_path.endswith(
            (".md", ".txt", ".json", ".yaml", ".yml")
        ):
            raise ValueError("skill reference file type is not supported")
        return self


class DurableSkillContentV1(_DurableLoopModel):
    """Canonical immutable instruction content returned by a skill provider."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    skill_id: _OpaqueId
    version: Annotated[str, Field(min_length=1, max_length=128)]
    content_hash: _Sha256
    files: Annotated[
        tuple[DurableSkillContentFileV1, ...],
        Field(min_length=1, max_length=MAX_SKILL_REFERENCE_FILES + 1),
    ]
    executable: Literal[False] = False

    @classmethod
    def create(
        cls,
        *,
        skill_id: str,
        version: str,
        files: tuple[DurableSkillContentFileV1, ...],
    ) -> DurableSkillContentV1:
        """Create a content bundle with its canonical version hash."""
        content_hash = canonical_hash([item.model_dump(mode="json") for item in files])
        return cls(
            skill_id=skill_id,
            version=version,
            content_hash=content_hash,
            files=files,
        )

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        paths = [item.relative_path for item in self.files]
        if paths[0] != "SKILL.md" or paths.count("SKILL.md") != 1:
            raise ValueError("skill content must begin with exactly one SKILL.md")
        if len(paths) != len(set(paths)):
            raise ValueError("skill content paths must be unique")
        if paths[1:] != sorted(paths[1:]):
            raise ValueError("skill reference files must use canonical path order")
        serialized_files = [item.model_dump(mode="json") for item in self.files]
        if len(canonical_json_bytes(serialized_files)) > MAX_SKILL_CONTENT_BYTES:
            raise ValueError("skill content exceeds the byte limit")
        if self.content_hash != canonical_hash(serialized_files):
            raise ValueError("skill content hash mismatch")
        return self


class DurableSkillLoadReceiptV1(_DurableLoopModel):
    """Content-free receipt binding one exact loaded skill to its content ref."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id: _OpaqueId
    operation_key: _Sha256
    stable_step_id: _OpaqueId
    skill_id: _OpaqueId
    provider_id: _OpaqueId
    version: Annotated[str, Field(min_length=1, max_length=128)]
    catalog_revision: Annotated[str, Field(min_length=1, max_length=128)]
    catalog_hash: _Sha256
    content_hash: _Sha256
    content_ref: ContentRefV1
    loaded_at: datetime
    already_loaded: bool = False

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        _as_utc(self.loaded_at, "loaded_at")
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
    chat_ui: Literal[True] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
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


class CheckpointStateV2(CheckpointStateV1):
    """V2 replay state with frozen skill catalog and load receipts."""

    schema_version: SchemaVersion2 = "2"  # type: ignore[assignment]
    identity: DurableRunIdentityV2
    skill_catalog_ref: ContentRefV1
    skill_catalog_hash: _Sha256
    completed_skill_searches: _NonNegativeInt = 0
    completed_skill_loads: _NonNegativeInt = 0
    loaded_skill_receipts: Annotated[
        tuple[DurableSkillLoadReceiptV1, ...],
        Field(max_length=MAX_LOADED_SKILLS),
    ] = ()
    loaded_skill_bytes: _NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_skill_state(self) -> Self:
        if self.skill_catalog_hash != self.identity.skill_catalog_hash:
            raise ValueError("checkpoint skill catalog hash does not match identity")
        if self.completed_skill_searches > self.identity.budget.max_skill_searches:
            raise ValueError("checkpoint skill searches exceed the frozen budget")
        if self.completed_skill_loads > self.identity.budget.max_skill_loads:
            raise ValueError("checkpoint skill loads exceed the frozen budget")
        if self.completed_skill_loads < len(self.loaded_skill_receipts):
            raise ValueError("skill load counter cannot trail loaded skill receipts")
        loaded = [(item.skill_id, item.version) for item in self.loaded_skill_receipts]
        if len(loaded) != len(set(loaded)):
            raise ValueError("checkpoint loaded skill receipts must be unique")
        if any(
            item.run_id != self.identity.run_id
            or item.catalog_hash != self.skill_catalog_hash
            for item in self.loaded_skill_receipts
        ):
            raise ValueError("loaded skill receipts must match the run and catalog")
        if self.loaded_skill_bytes > self.identity.budget.max_loaded_skill_bytes:
            raise ValueError("checkpoint loaded skill bytes exceed the frozen budget")
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
    ui: DurableChatRunOptionsV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_plan_json(self) -> Self:
        assert_json_value(self.model_settings)
        assert_json_value(self.settings)
        if len(canonical_json_bytes(self.model_settings)) > MAX_DURABLE_ENVELOPE_BYTES:
            raise ValueError("model settings exceed the byte limit")
        if len(canonical_json_bytes(self.settings)) > MAX_DURABLE_ENVELOPE_BYTES:
            raise ValueError("durable-loop settings exceed the byte limit")
        return self


class DurableLoopPlanDocumentV2(DurableLoopPlanDocumentV1):
    """V2 external plan with immutable skill, retention, and response bindings."""

    schema_version: SchemaVersion2 = "2"  # type: ignore[assignment]
    skill_catalog_ref: ContentRefV1
    skill_catalog_hash: _Sha256
    initial_skill_metadata: Annotated[
        tuple[DurableSkillMetadataV1, ...],
        Field(max_length=MAX_INITIAL_SKILL_METADATA_RECORDS),
    ] = ()
    retention_policy: DurableRetentionPolicyV1
    allow_human_input: bool = True
    response_schema_ref: ContentRefV1 | None = None
    response_schema_hash: _Sha256 | None = None

    @model_validator(mode="after")
    def validate_v2_plan(self) -> Self:
        if (
            len(
                canonical_json_bytes(
                    [item.model_dump(mode="json") for item in self.initial_skill_metadata]
                )
            )
            > MAX_INITIAL_SKILL_METADATA_BYTES
        ):
            raise ValueError("initial skill metadata exceeds the byte limit")
        metadata_ids = [item.skill_id for item in self.initial_skill_metadata]
        if len(metadata_ids) != len(set(metadata_ids)):
            raise ValueError("initial skill metadata IDs must be unique")
        if (self.response_schema_ref is None) != (self.response_schema_hash is None):
            raise ValueError("response schema reference and validator hash must appear together")
        return self


class DurableRunDocumentV1(_DurableLoopModel):
    """External content document never copied into Durable history."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    plan: DurableLoopPlanDocumentV1
    checkpoint: CheckpointStateV1
    pending_decision: ModelDecisionEnvelopeV1 | None = None


class DurableRunDocumentV2(_DurableLoopModel):
    """V2 external content document never copied into Durable history."""

    schema_version: SchemaVersion2 = "2"
    plan: DurableLoopPlanDocumentV2
    checkpoint: CheckpointStateV2
    pending_decision: ModelDecisionEnvelopeV1 | None = None

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        if self.plan.skill_catalog_hash != self.checkpoint.skill_catalog_hash:
            raise ValueError("run document skill catalog bindings do not match")
        if self.plan.retention_policy != self.checkpoint.identity.retention_policy:
            raise ValueError("run document retention policies do not match")
        return self


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


class DurableOrchestrationInputV2(DurableOrchestrationInputV1):
    """Refs-only V2 orchestration state safe for replay and continue-as-new."""

    schema_version: SchemaVersion2 = "2"  # type: ignore[assignment]
    identity: DurableRunIdentityV2
    skill_catalog_ref: ContentRefV1
    skill_catalog_hash: _Sha256
    completed_skill_searches: _NonNegativeInt = 0
    completed_skill_loads: _NonNegativeInt = 0
    loaded_skill_receipt_refs: Annotated[
        tuple[ContentRefV1, ...],
        Field(max_length=MAX_LOADED_SKILLS),
    ] = ()
    loaded_skill_bytes: _NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_v2_envelope(self) -> Self:
        if self.skill_catalog_hash != self.identity.skill_catalog_hash:
            raise ValueError("orchestration skill catalog hash does not match identity")
        if self.completed_skill_searches > self.identity.budget.max_skill_searches:
            raise ValueError("orchestration skill searches exceed the frozen budget")
        if self.completed_skill_loads > self.identity.budget.max_skill_loads:
            raise ValueError("orchestration skill loads exceed the frozen budget")
        if self.completed_skill_loads < len(self.loaded_skill_receipt_refs):
            raise ValueError("skill load counter cannot trail receipt references")
        if self.loaded_skill_bytes > self.identity.budget.max_loaded_skill_bytes:
            raise ValueError("loaded skill bytes exceed the frozen budget")
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


class SkillSearchActivityResultV1(_DurableLoopModel):
    """Refs-only result of one runtime-owned skill metadata search."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    operation_key: _Sha256
    catalog_hash: _Sha256
    page_ref: ContentRefV1
    result_bytes: Annotated[
        int,
        Field(ge=0, le=MAX_SKILL_SEARCH_RESULT_BYTES),
    ]
    written_bytes: _NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_envelope_size(self) -> Self:
        _assert_envelope_size(self)
        return self


class SkillLoadActivityResultV1(_DurableLoopModel):
    """Refs-only result of one runtime-owned immutable skill load."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    operation_key: _Sha256
    catalog_hash: _Sha256
    skill_id: _OpaqueId
    version: Annotated[str, Field(min_length=1, max_length=128)]
    content_hash: _Sha256
    content_ref: ContentRefV1
    receipt_ref: ContentRefV1
    already_loaded: bool = False
    written_bytes: _NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_envelope_size(self) -> Self:
        _assert_envelope_size(self)
        return self


class DurableResourceExpiryV1(_DurableLoopModel):
    """Explicit public and recovery deadlines for one run resource."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    events_expires_at: datetime
    result_expires_at: datetime
    human_content_expires_at: datetime
    receipts_expire_at: datetime
    skills_expire_at: datetime
    tombstone_expires_at: datetime
    idempotency_expires_at: datetime

    @model_validator(mode="after")
    def validate_expiry_order(self) -> Self:
        values = {
            name: _as_utc(value, name)
            for name, value in (
                ("events_expires_at", self.events_expires_at),
                ("result_expires_at", self.result_expires_at),
                ("human_content_expires_at", self.human_content_expires_at),
                ("receipts_expire_at", self.receipts_expire_at),
                ("skills_expire_at", self.skills_expire_at),
                ("tombstone_expires_at", self.tombstone_expires_at),
                ("idempotency_expires_at", self.idempotency_expires_at),
            )
        }
        public_expiry = max(
            values["events_expires_at"],
            values["result_expires_at"],
        )
        if values["human_content_expires_at"] > values["result_expires_at"]:
            raise ValueError("human content expiry must not exceed result expiry")
        if values["receipts_expire_at"] < public_expiry:
            raise ValueError("receipt expiry must cover public content expiry")
        if values["skills_expire_at"] < public_expiry:
            raise ValueError("skill expiry must cover content-bearing resources")
        if values["tombstone_expires_at"] < values["receipts_expire_at"]:
            raise ValueError("tombstone expiry must cover receipt expiry")
        if values["idempotency_expires_at"] != values["tombstone_expires_at"]:
            raise ValueError("idempotency expiry must match tombstone replay expiry")
        return self


class DurableRunResourceHeaderV1(_DurableLoopModel):
    """Create-once owner/policy root and bounded terminal run projection."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id: _OpaqueId
    session_id: _OpaqueId
    owner_hash: _Sha256
    access_namespace_hash: _Sha256
    request_id_hash: _Sha256
    status: DurableLoopRunStatus
    retention_policy: DurableRetentionPolicyV1
    created_at: datetime
    updated_at: datetime
    active_deadline: datetime
    terminal_at: datetime | None = None
    terminal_projection_ref: ContentRefV1 | None = None
    artifact_page_head_hash: _Sha256 | None = None
    artifact_page_count: _NonNegativeInt = 0
    expiry: DurableResourceExpiryV1 | None = None
    record_version: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_resource_header(self) -> Self:
        created = _as_utc(self.created_at, "created_at")
        updated = _as_utc(self.updated_at, "updated_at")
        active_deadline = _as_utc(self.active_deadline, "active_deadline")
        if updated < created or active_deadline <= created:
            raise ValueError("run resource dates must be monotonic")
        if (self.artifact_page_head_hash is None) != (self.artifact_page_count == 0):
            raise ValueError("artifact page head must match the page count")
        terminal = self.status in {
            DurableLoopRunStatus.COMPLETED,
            DurableLoopRunStatus.FAILED,
            DurableLoopRunStatus.CANCELLED,
        }
        if terminal != (self.terminal_at is not None):
            raise ValueError("terminal status must match terminal timestamp")
        if terminal != (self.expiry is not None):
            raise ValueError("terminal status must match explicit resource expiry")
        if self.terminal_at is not None:
            terminal_at = _as_utc(self.terminal_at, "terminal_at")
            if terminal_at < created:
                raise ValueError("terminal timestamp must not precede creation")
        return self


class DurableRunTombstoneV1(_DurableLoopModel):
    """Minimal content-free owner-bound proof of an expired run."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id_hash: _Sha256
    session_id_hash: _Sha256
    owner_hash: _Sha256
    access_namespace_hash: _Sha256
    terminal_status: DurableLoopRunStatus
    expiry_class: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    terminal_at: datetime
    resource_expired_at: datetime
    tombstone_expires_at: datetime

    @model_validator(mode="after")
    def validate_tombstone_dates(self) -> Self:
        if self.terminal_status not in {
            DurableLoopRunStatus.COMPLETED,
            DurableLoopRunStatus.FAILED,
            DurableLoopRunStatus.CANCELLED,
        }:
            raise ValueError("run tombstone requires a terminal status")
        terminal = _as_utc(self.terminal_at, "terminal_at")
        resource_expired = _as_utc(
            self.resource_expired_at,
            "resource_expired_at",
        )
        tombstone_expires = _as_utc(
            self.tombstone_expires_at,
            "tombstone_expires_at",
        )
        if not terminal <= resource_expired < tombstone_expires:
            raise ValueError("run tombstone dates must be monotonic")
        return self


class DurableRunArtifactV1(_DurableLoopModel):
    """One exact artifact or keyed document retained for a run."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    kind: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    retention_class: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    content_ref: ContentRefV1 | None = None
    keyed_document_name: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    expires_at: datetime

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        if (self.content_ref is None) == (self.keyed_document_name is None):
            raise ValueError("run artifact must identify one content ref or keyed document")
        _as_utc(self.expires_at, "expires_at")
        return self


class DurableRunArtifactPageV1(_DurableLoopModel):
    """One immutable bounded page in a run's exact artifact index."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id: _OpaqueId
    page_index: _NonNegativeInt
    artifacts: Annotated[
        tuple[DurableRunArtifactV1, ...],
        Field(min_length=1, max_length=MAX_RUN_ARTIFACTS_PER_PAGE),
    ]
    previous_page_hash: _Sha256 | None = None
    page_hash: _Sha256

    @model_validator(mode="after")
    def validate_page_hash(self) -> Self:
        expected = canonical_hash(
            {
                "artifacts": [artifact.model_dump(mode="json") for artifact in self.artifacts],
                "page_index": self.page_index,
                "previous_page_hash": self.previous_page_hash,
                "run_id": self.run_id,
            }
        )
        if self.page_hash != expected:
            raise ValueError("run artifact page hash mismatch")
        return self


class DurableObjectRunReferenceV1(_DurableLoopModel):
    """One live or pending run reference to canonical retained content."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id_hash: _Sha256
    retention_class: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    deadline: datetime
    pending: bool = False

    @model_validator(mode="after")
    def validate_deadline(self) -> Self:
        _as_utc(self.deadline, "deadline")
        return self


class DurableObjectReferenceV1(_DurableLoopModel):
    """Bounded CAS root/shard state for one canonical content object."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    object_kind: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    object_digest: _Sha256
    state: DurableObjectReferenceState
    references: Annotated[
        tuple[DurableObjectRunReferenceV1, ...],
        Field(max_length=MAX_RUN_ARTIFACTS_PER_PAGE),
    ] = ()
    deletion_fence: _Sha256 | None = None
    record_version: Annotated[int, Field(ge=1)]
    updated_at: datetime

    @model_validator(mode="after")
    def validate_reference_state(self) -> Self:
        _as_utc(self.updated_at, "updated_at")
        deleting = self.state is DurableObjectReferenceState.DELETING
        if deleting != (self.deletion_fence is not None):
            raise ValueError("deleting object state must match its deletion fence")
        reference_keys = [(item.run_id_hash, item.retention_class) for item in self.references]
        if len(reference_keys) != len(set(reference_keys)):
            raise ValueError("object run references must be unique")
        return self


class DurableSessionResourceV1(_DurableLoopModel):
    """Owner-bound retained session root fenced against active admission."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    session_id_hash: _Sha256
    owner_hash: _Sha256
    access_namespace_hash: _Sha256
    committed_generation: _NonNegativeInt
    active_run_id_hash: _Sha256 | None = None
    context_ref: ContentRefV1
    renewed_at: datetime
    expires_at: datetime
    record_version: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_session_dates(self) -> Self:
        renewed = _as_utc(self.renewed_at, "renewed_at")
        expires = _as_utc(self.expires_at, "expires_at")
        if expires <= renewed:
            raise ValueError("session expiry must follow its renewal")
        return self


class DurableSessionTombstoneV1(_DurableLoopModel):
    """Content-free proof that one owner-bound session expired."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    session_id_hash: _Sha256
    owner_hash: _Sha256
    access_namespace_hash: _Sha256
    expired_at: datetime
    tombstone_expires_at: datetime

    @model_validator(mode="after")
    def validate_tombstone_dates(self) -> Self:
        expired = _as_utc(self.expired_at, "expired_at")
        tombstone_expires = _as_utc(
            self.tombstone_expires_at,
            "tombstone_expires_at",
        )
        if tombstone_expires <= expired:
            raise ValueError("session tombstone expiry must follow session expiry")
        return self


class DurablePublicLinksV1(_DurableLoopModel):
    """Absolute-path link relations returned for one public run."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    status: Annotated[str, Field(pattern=_PUBLIC_PATH_PATTERN.pattern)]
    events: Annotated[str, Field(pattern=_PUBLIC_PATH_PATTERN.pattern)]
    result: Annotated[str, Field(pattern=_PUBLIC_PATH_PATTERN.pattern)]
    cancel: Annotated[str, Field(pattern=_PUBLIC_PATH_PATTERN.pattern)]
    human_input: Annotated[
        tuple[Annotated[str, Field(pattern=_PUBLIC_PATH_PATTERN.pattern)], ...],
        Field(max_length=64),
    ] = ()


class DurablePublicErrorV1(_DurableLoopModel):
    """Stable public error document retaining the legacy error string."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    code: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    error: _ShortText
    status: Annotated[int, Field(ge=400, le=599)]
    possibly_committed: bool = False
    resource_expiry: DurableResourceExpiryV1 | None = None


class DurablePublicStatusV1(_DurableLoopModel):
    """Content-free application-neutral public lifecycle projection."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    run_id: _OpaqueId
    session_id: _OpaqueId
    status: DurableLoopRunStatus
    phase: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    created_at: datetime
    updated_at: datetime
    completed_model_steps: _NonNegativeInt
    completed_tool_calls: _NonNegativeInt
    completed_skill_searches: _NonNegativeInt = 0
    completed_skill_loads: _NonNegativeInt = 0
    human_waits: _NonNegativeInt
    result_available: bool = False
    links: DurablePublicLinksV1
    resource_expiry: DurableResourceExpiryV1 | None = None
    error: DurablePublicErrorV1 | None = None

    @model_validator(mode="after")
    def validate_status_dates(self) -> Self:
        created = _as_utc(self.created_at, "created_at")
        updated = _as_utc(self.updated_at, "updated_at")
        if updated < created:
            raise ValueError("public status update must not precede creation")
        return self


class DurableRunStartedPayloadV1(_DurableLoopModel):
    """Public payload for initial run admission."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    session_id: _OpaqueId
    status: DurableLoopRunStatus


class DurableSkillSearchPayloadV1(_DurableLoopModel):
    """Content-free public summary of a completed skill search."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    operation_key: _Sha256
    result_count: Annotated[int, Field(ge=0, le=256)]
    has_more: bool


class DurableSkillLoadPayloadV1(_DurableLoopModel):
    """Public summary of one exact skill load without provider identity."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    operation_key: _Sha256
    skill_id: _OpaqueId
    version: Annotated[str, Field(min_length=1, max_length=128)]
    already_loaded: bool = False


class DurableModelProgressPayloadV1(_DurableLoopModel):
    """Bounded public model progress."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    step_index: _NonNegativeInt
    phase: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]


class DurableToolEventPayloadV1(_DurableLoopModel):
    """Content-free public tool progress without arguments or results."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    call_key: _Sha256
    tool_name: _ToolName
    status: ToolResultStatus | None = None


class DurableHumanInputRequiredPayloadV1(_DurableLoopModel):
    """Public pointer to one authorized human-input resource."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    request_id: _OpaqueId
    expires_at: datetime
    input_link: Annotated[str, Field(pattern=_PUBLIC_PATH_PATTERN.pattern)]


class DurableAssistantDeltaPayloadV1(_DurableLoopModel):
    """One bounded UTF-8 assistant text delta."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    text: Annotated[str, Field(min_length=1, max_length=16 * 1024)]

    @model_validator(mode="after")
    def validate_delta_bytes(self) -> Self:
        if len(self.text.encode("utf-8")) > 16 * 1024:
            raise ValueError("assistant delta exceeds the byte limit")
        return self


class DurableMessageCommittedPayloadV1(_DurableLoopModel):
    """Public content-free confirmation of a committed assistant message."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    committed_generation: _NonNegativeInt
    response_hash: _Sha256


class DurableTerminalEventPayloadV1(_DurableLoopModel):
    """Public terminal disposition without result content."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    status: DurableLoopRunStatus
    result_available: bool = False
    error_code: (
        Annotated[
            str,
            Field(pattern=_ERROR_CODE_PATTERN.pattern),
        ]
        | None
    ) = None
    possibly_committed: bool = False

    @model_validator(mode="after")
    def validate_terminal_status(self) -> Self:
        if self.status not in {
            DurableLoopRunStatus.COMPLETED,
            DurableLoopRunStatus.FAILED,
            DurableLoopRunStatus.CANCELLED,
        }:
            raise ValueError("terminal event requires a terminal run status")
        if self.status is DurableLoopRunStatus.COMPLETED and self.error_code is not None:
            raise ValueError("completed terminal event cannot include an error code")
        return self


class DurableObservationDegradedPayloadV1(_DurableLoopModel):
    """Public fallback links when observation publication degrades."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    code: Annotated[str, Field(pattern=_ERROR_CODE_PATTERN.pattern)]
    status_link: Annotated[str, Field(pattern=_PUBLIC_PATH_PATTERN.pattern)]
    result_link: Annotated[str, Field(pattern=_PUBLIC_PATH_PATTERN.pattern)]


DurablePublicEventPayload = (
    DurableRunStartedPayloadV1
    | DurableSkillSearchPayloadV1
    | DurableSkillLoadPayloadV1
    | DurableModelProgressPayloadV1
    | DurableToolEventPayloadV1
    | DurableHumanInputRequiredPayloadV1
    | DurableAssistantDeltaPayloadV1
    | DurableMessageCommittedPayloadV1
    | DurableTerminalEventPayloadV1
    | DurableObservationDegradedPayloadV1
)


class DurablePublicEventV2(_DurableLoopModel):
    """One strict generic schema-v2 public event frame."""

    schema_version: SchemaVersion2 = "2"
    run_id: _OpaqueId
    sequence: Annotated[int, Field(ge=1)]
    timestamp: datetime
    type: DurablePublicEventType
    payload: DurablePublicEventPayload

    @model_validator(mode="after")
    def validate_event_payload(self) -> Self:
        expected_types: dict[
            DurablePublicEventType,
            type[_DurableLoopModel],
        ] = {
            DurablePublicEventType.RUN_STARTED: DurableRunStartedPayloadV1,
            DurablePublicEventType.SKILL_SEARCH_COMPLETED: DurableSkillSearchPayloadV1,
            DurablePublicEventType.SKILL_LOAD_STARTED: DurableSkillLoadPayloadV1,
            DurablePublicEventType.SKILL_LOAD_COMPLETED: DurableSkillLoadPayloadV1,
            DurablePublicEventType.MODEL_PROGRESS: DurableModelProgressPayloadV1,
            DurablePublicEventType.TOOL_STARTED: DurableToolEventPayloadV1,
            DurablePublicEventType.TOOL_COMPLETED: DurableToolEventPayloadV1,
            DurablePublicEventType.HUMAN_INPUT_REQUIRED: (DurableHumanInputRequiredPayloadV1),
            DurablePublicEventType.ASSISTANT_DELTA: DurableAssistantDeltaPayloadV1,
            DurablePublicEventType.MESSAGE_COMMITTED: (DurableMessageCommittedPayloadV1),
            DurablePublicEventType.RUN_COMPLETED: DurableTerminalEventPayloadV1,
            DurablePublicEventType.RUN_FAILED: DurableTerminalEventPayloadV1,
            DurablePublicEventType.RUN_CANCELLED: DurableTerminalEventPayloadV1,
            DurablePublicEventType.OBSERVATION_DEGRADED: (DurableObservationDegradedPayloadV1),
        }
        if not isinstance(self.payload, expected_types[self.type]):
            raise ValueError("public event type does not match its payload")
        if isinstance(self.payload, DurableTerminalEventPayloadV1):
            expected_status = {
                DurablePublicEventType.RUN_COMPLETED: DurableLoopRunStatus.COMPLETED,
                DurablePublicEventType.RUN_FAILED: DurableLoopRunStatus.FAILED,
                DurablePublicEventType.RUN_CANCELLED: DurableLoopRunStatus.CANCELLED,
            }.get(self.type)
            if expected_status is not None and self.payload.status is not expected_status:
                raise ValueError("terminal public event type does not match its status")
        if isinstance(self.payload, DurableToolEventPayloadV1) and (
            self.type is DurablePublicEventType.TOOL_STARTED
        ) != (self.payload.status is None):
            raise ValueError("tool event type does not match its status")
        _as_utc(self.timestamp, "timestamp")
        _assert_envelope_size(self)
        return self


class DurableTriggerBindingPrincipalV1(_DurableLoopModel):
    """Stable durable-only initiator identity for a background trigger."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    app_identity_hash: _Sha256
    agent_slug: _ToolName
    trigger_registration: _OpaqueId
    connection_identity_hash: _Sha256 | None = None
    principal_hash: _Sha256

    @model_validator(mode="after")
    def validate_principal_hash(self) -> Self:
        expected = canonical_hash(
            {
                "agent_slug": self.agent_slug,
                "app_identity_hash": self.app_identity_hash,
                "connection_identity_hash": self.connection_identity_hash,
                "kind": "trigger_binding",
                "schema_version": DURABLE_LOOP_SCHEMA_VERSION,
                "trigger_registration": self.trigger_registration,
            }
        )
        if self.principal_hash != expected:
            raise ValueError("trigger binding principal hash mismatch")
        return self


class DurableTriggerAdmissionRecordV1(_DurableLoopModel):
    """Authoritative refs-only staged trigger admission request."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    record_key: _Sha256
    owner_hash: _Sha256
    access_namespace_hash: _Sha256
    initiator_hash: _Sha256
    agent_slug: _ToolName
    trigger_type: DurableTriggerType
    trigger_registration: _OpaqueId
    stable_event_id_hash: _Sha256
    normalized_body_hash: _Sha256
    request_hash: _Sha256
    session_id: _OpaqueId
    run_id: _OpaqueId
    payload_ref: ContentRefV1
    prompt_ref: ContentRefV1
    state: DurableTriggerAdmissionState = DurableTriggerAdmissionState.PENDING
    staged_at: datetime
    admission_deadline: datetime
    admitted_at: datetime | None = None
    terminal_reason_code: (
        Annotated[
            str,
            Field(pattern=_ERROR_CODE_PATTERN.pattern),
        ]
        | None
    ) = None
    attempts: _NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_trigger_record(self) -> Self:
        staged = _as_utc(self.staged_at, "staged_at")
        deadline = _as_utc(self.admission_deadline, "admission_deadline")
        if deadline <= staged:
            raise ValueError("trigger admission deadline must follow staging")
        expected = deterministic_trigger_record_key(
            owner_hash=self.owner_hash,
            trigger_registration=self.trigger_registration,
            stable_event_id_hash=self.stable_event_id_hash,
            normalized_body_hash=self.normalized_body_hash,
        )
        if self.record_key != expected:
            raise ValueError("trigger admission record key mismatch")
        admitted = self.state is DurableTriggerAdmissionState.ADMITTED
        if admitted != (self.admitted_at is not None):
            raise ValueError("admitted trigger state must match admitted timestamp")
        terminal = self.state in {
            DurableTriggerAdmissionState.REJECTED,
            DurableTriggerAdmissionState.EXPIRED,
            DurableTriggerAdmissionState.CANCELLED,
        }
        if terminal != (self.terminal_reason_code is not None):
            raise ValueError("terminal trigger state must match a reason code")
        return self


class DurableTriggerPendingLedgerV1(_DurableLoopModel):
    """One immutable bounded CAS page of staged trigger records."""

    schema_version: SchemaVersion = DURABLE_LOOP_SCHEMA_VERSION
    shard: Annotated[int, Field(ge=0, le=255)]
    page_index: _NonNegativeInt
    records: Annotated[
        tuple[DurableTriggerAdmissionRecordV1, ...],
        Field(max_length=MAX_TRIGGER_LEDGER_RECORDS_PER_PAGE),
    ] = ()
    previous_page_hash: _Sha256 | None = None
    page_hash: _Sha256

    @model_validator(mode="after")
    def validate_ledger_page(self) -> Self:
        keys = [record.record_key for record in self.records]
        if len(keys) != len(set(keys)):
            raise ValueError("trigger ledger record keys must be unique")
        expected = canonical_hash(
            {
                "page_index": self.page_index,
                "previous_page_hash": self.previous_page_hash,
                "records": [record.model_dump(mode="json") for record in self.records],
                "shard": self.shard,
            }
        )
        if self.page_hash != expected:
            raise ValueError("trigger pending ledger page hash mismatch")
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


def deterministic_skill_operation_key(
    *,
    run_id: str,
    step_index: int,
    call_ordinal: int,
    plan_version: Literal["2"],
    catalog_hash: str,
    operation_kind: Literal["search_skills", "load_skill"],
    arguments: dict[str, object],
    expected_version: str | None = None,
    expected_content_hash: str | None = None,
) -> str:
    """Derive a V2 skill operation key without changing legacy call keys."""
    assert_json_value(arguments)
    if len(canonical_json_bytes(arguments)) > MAX_TOOL_ARGUMENT_BYTES:
        raise ValueError("skill operation arguments exceed the byte limit")
    return canonical_hash(
        {
            "arguments": arguments,
            "call_ordinal": call_ordinal,
            "catalog_hash": catalog_hash,
            "expected_content_hash": expected_content_hash,
            "expected_version": expected_version,
            "operation_kind": operation_kind,
            "plan_version": plan_version,
            "run_id": run_id,
            "step_index": step_index,
        }
    )


def deterministic_trigger_record_key(
    *,
    owner_hash: str,
    trigger_registration: str,
    stable_event_id_hash: str,
    normalized_body_hash: str,
) -> str:
    """Derive the authoritative key for one staged trigger delivery."""
    return canonical_hash(
        {
            "kind": "trigger_admission",
            "normalized_body_hash": normalized_body_hash,
            "owner_hash": owner_hash,
            "schema_version": DURABLE_LOOP_SCHEMA_VERSION,
            "stable_event_id_hash": stable_event_id_hash,
            "trigger_registration": trigger_registration,
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
