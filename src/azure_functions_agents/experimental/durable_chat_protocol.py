"""Shared external contracts for the private durable-chat UI."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self, runtime_checkable
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..strict_json import (
    DuplicateJsonKeyError,
    canonical_json_bytes,
    decode_json_object,
)
from .durable_loop_protocol import (
    ContentRefV1,
    DurableChatModelMode,
    DurableChatRunOptionsV1,
    DurableChatStartOptionsV1,
    DurableFaultProfile,
    DurableLoopRunStatus,
    HumanInputState,
    SandboxExecutionProfile,
    ToolProvenance,
    canonical_hash,
)

DURABLE_CHAT_SCHEMA_VERSION: Literal["1"] = "1"
MAX_DURABLE_CHAT_DOCUMENT_BYTES = 1024 * 1024
MAX_DURABLE_CHAT_EVENT_BATCH = 64
MAX_DURABLE_CHAT_REPLAY_EVENTS = 128
MAX_DURABLE_CHAT_TOTAL_RESERVED_OBSERVATION_BYTES = 4 * 1024 * 1024
MAX_DURABLE_CHAT_PENDING_OBSERVATIONS = 256
MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS = 16
MAX_DURABLE_CHAT_CAS_ATTEMPTS = 8
DURABLE_CHAT_PUBLISHER_DRAIN_DEADLINE_SECONDS = 10.0
MAX_DURABLE_CHAT_EVENT_BYTES = 16 * 1024
MAX_DURABLE_CHAT_BATCH_BYTES = 512 * 1024
MAX_DURABLE_CHAT_SNAPSHOT_BYTES = 512 * 1024
MAX_DURABLE_CHAT_DRAFT_BYTES = 256 * 1024
MAX_DURABLE_CHAT_TEXT_DELTA_BYTES = 8 * 1024
MAX_DURABLE_CHAT_SANDBOX_OBSERVATIONS = 2048
MAX_DURABLE_CHAT_TOOL_PROGRESS = 2048
MAX_DURABLE_CHAT_PRODUCER_EPOCHS = 256

_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_AGENT_SLUG_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PHASE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/[0-9A-Fa-f-]{36}/resourceGroups/"
    r"[A-Za-z0-9._()-]{1,90}/providers/[A-Za-z0-9.]+"
    r"(?:/[A-Za-z0-9._()-]{1,128})+$"
)
_ROUTE_PARAMETER_PATTERN = re.compile(r"\{([a-z_]+)\}")
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "accountkey",
        "apikey",
        "authorization",
        "code",
        "credential",
        "key",
        "password",
        "sas",
        "secret",
        "sharedaccesskey",
        "sharedaccesssignature",
        "sig",
        "signature",
        "token",
        "x-functions-key",
    }
)
_SENSITIVE_URL_VALUE_MARKERS = (
    "accountkey=",
    "sharedaccesssignature=",
    "sharedaccesskey=",
    "sig=",
    "token=",
    "x-functions-key=",
)
_SENSITIVE_TEXT_MARKER = re.compile(
    r"(?:access_token|accountkey|apikey|authorization|credential|password|secret|"
    r"sharedaccesssignature)\s*[:=]",
    re.IGNORECASE,
)

_OpaqueId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_OPAQUE_ID_PATTERN.pattern),
]
_AgentSlug = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_AGENT_SLUG_PATTERN.pattern),
]
_Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN.pattern)]
_Phase = Annotated[str, Field(pattern=_PHASE_PATTERN.pattern)]
_NonNegativeInt = Annotated[int, Field(ge=0)]
_PositiveInt = Annotated[int, Field(ge=1)]
_RoutePath = Annotated[str, Field(min_length=1, max_length=512)]
_SanitizedText = Annotated[str, Field(min_length=1, max_length=512)]
_ExternalUrl = Annotated[str, Field(min_length=1, max_length=2048)]
_ResourceId = Annotated[
    str,
    Field(min_length=1, max_length=1024, pattern=_RESOURCE_ID_PATTERN.pattern),
]


class DurableChatProtocolDocumentError(ValueError):
    """An untrusted durable-chat document is invalid."""


class DurableChatRouteName(StrEnum):
    """The fixed same-origin route descriptors returned by bootstrap."""

    START_RUN = "start_run"
    STATUS = "status"
    RESULT = "result"
    CANCEL = "cancel"
    HUMAN_INPUT_DETAIL = "human_input_detail"
    HUMAN_INPUT_SUBMIT = "human_input_submit"
    EVENTS = "events"
    DIAGNOSTICS = "diagnostics"


class DurableChatHttpMethod(StrEnum):
    """HTTP methods used by the durable-chat route contract."""

    GET = "GET"
    POST = "POST"


class DurableChatIntegrationKind(StrEnum):
    """The non-secret request-diagnostics integrations."""

    DURABLE_TASK_SCHEDULER = "durable_task_scheduler"
    APPLICATION_INSIGHTS = "application_insights"


class DurableChatEventType(StrEnum):
    """The complete durable-chat SSE vocabulary."""

    SNAPSHOT = "snapshot"
    RUN_STATUS = "run_status"
    PROGRESS = "progress"
    MODEL_ATTEMPT = "model_attempt"
    ASSISTANT_TEXT = "assistant_text"
    ASSISTANT_DRAFT_REPLACED = "assistant_draft_replaced"
    TOOL = "tool"
    SANDBOX = "sandbox"
    HUMAN_INPUT = "human_input"
    TERMINAL = "terminal"
    DEGRADED = "degraded"


class DurableChatModelAttemptState(StrEnum):
    """The observation-only lifecycle of a foreground model attempt."""

    STARTED = "started"
    STREAMING = "streaming"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class DurableChatToolState(StrEnum):
    """The content-free lifecycle of one dispatched tool call."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    AMBIGUOUS = "ambiguous"


class DurableChatSandboxState(StrEnum):
    """The last observed sandbox lifecycle state for one local tool call."""

    NOT_ALLOCATED = "not_allocated"
    REMOTE_NO_SANDBOX = "remote_no_sandbox"
    EXECUTING = "executing"
    RETAINED_IDLE = "retained_idle"
    DELETE_REQUESTED = "delete_requested"
    CONFIRMED_DELETED = "confirmed_deleted"
    REPLACEMENT_INSTANCE = "replacement_instance"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class DurableChatObservationDegradationReason(StrEnum):
    """Why optional chat observations became incomplete or status-only."""

    QUEUE_FULL = "queue_full"
    OBSERVATION_BYTE_LIMIT = "observation_byte_limit"
    PRODUCER_ATTEMPTS_EXHAUSTED = "producer_attempts_exhausted"
    PUBLISH_TIMEOUT = "publish_timeout"
    CAS_ATTEMPTS_EXHAUSTED = "cas_attempts_exhausted"
    STORAGE_UNAVAILABLE = "storage_unavailable"


class DurableChatReplayDisposition(StrEnum):
    """How a replay reader should apply one journal response."""

    DELTAS = "deltas"
    SNAPSHOT_REQUIRED = "snapshot_required"
    CURSOR_AHEAD = "cursor_ahead"


class DurableChatPublicationDisposition(StrEnum):
    """The bounded publisher result, independent from model/tool outcomes."""

    PUBLISHED = "published"
    DEGRADED = "degraded"
    STALE_PRODUCER = "stale_producer"


class DurableChatEnqueueDisposition(StrEnum):
    """The immediate result of a non-blocking execution-side observation."""

    ENQUEUED = "enqueued"
    DROPPED = "dropped"


class _DurableChatModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class DurableChatJournalLimitsV1(_DurableChatModel):
    """Fixed observation limits that are separate from execution budgets."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    max_events_per_batch: Annotated[
        int,
        Field(ge=MAX_DURABLE_CHAT_EVENT_BATCH, le=MAX_DURABLE_CHAT_EVENT_BATCH),
    ] = MAX_DURABLE_CHAT_EVENT_BATCH
    max_total_reserved_observation_bytes: Annotated[
        int,
        Field(
            ge=MAX_DURABLE_CHAT_TOTAL_RESERVED_OBSERVATION_BYTES,
            le=MAX_DURABLE_CHAT_TOTAL_RESERVED_OBSERVATION_BYTES,
        ),
    ] = MAX_DURABLE_CHAT_TOTAL_RESERVED_OBSERVATION_BYTES
    max_pending_observations: Annotated[
        int,
        Field(
            ge=MAX_DURABLE_CHAT_PENDING_OBSERVATIONS,
            le=MAX_DURABLE_CHAT_PENDING_OBSERVATIONS,
        ),
    ] = MAX_DURABLE_CHAT_PENDING_OBSERVATIONS
    max_producer_attempts: Annotated[
        int,
        Field(
            ge=MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS,
            le=MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS,
        ),
    ] = MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS
    max_cas_attempts: Annotated[
        int,
        Field(ge=MAX_DURABLE_CHAT_CAS_ATTEMPTS, le=MAX_DURABLE_CHAT_CAS_ATTEMPTS),
    ] = MAX_DURABLE_CHAT_CAS_ATTEMPTS
    publisher_drain_deadline_seconds: Annotated[
        float,
        Field(
            ge=DURABLE_CHAT_PUBLISHER_DRAIN_DEADLINE_SECONDS,
            le=DURABLE_CHAT_PUBLISHER_DRAIN_DEADLINE_SECONDS,
        ),
    ] = DURABLE_CHAT_PUBLISHER_DRAIN_DEADLINE_SECONDS


DURABLE_CHAT_JOURNAL_LIMITS = DurableChatJournalLimitsV1()


class DurableChatRouteDescriptorV1(_DurableChatModel):
    """One safe same-origin route template supplied by authenticated bootstrap."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    name: DurableChatRouteName
    method: DurableChatHttpMethod
    path_template: _RoutePath

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        _validate_same_origin_path(self.path_template)
        parameters = frozenset(_ROUTE_PARAMETER_PATTERN.findall(self.path_template))
        expected_parameters, expected_method = _ROUTE_REQUIREMENTS[self.name]
        if parameters != expected_parameters:
            raise ValueError("durable-chat route parameters do not match its route")
        if self.method is not expected_method:
            raise ValueError("durable-chat route method does not match its route")
        return self


_ROUTE_REQUIREMENTS: dict[
    DurableChatRouteName,
    tuple[frozenset[str], DurableChatHttpMethod],
] = {
    DurableChatRouteName.START_RUN: (frozenset(), DurableChatHttpMethod.POST),
    DurableChatRouteName.STATUS: (frozenset({"run_id"}), DurableChatHttpMethod.GET),
    DurableChatRouteName.RESULT: (frozenset({"run_id"}), DurableChatHttpMethod.GET),
    DurableChatRouteName.CANCEL: (frozenset({"run_id"}), DurableChatHttpMethod.POST),
    DurableChatRouteName.HUMAN_INPUT_DETAIL: (
        frozenset({"run_id", "request_id"}),
        DurableChatHttpMethod.GET,
    ),
    DurableChatRouteName.HUMAN_INPUT_SUBMIT: (
        frozenset({"run_id", "request_id"}),
        DurableChatHttpMethod.POST,
    ),
    DurableChatRouteName.EVENTS: (frozenset({"run_id"}), DurableChatHttpMethod.GET),
    DurableChatRouteName.DIAGNOSTICS: (
        frozenset({"run_id"}),
        DurableChatHttpMethod.GET,
    ),
}


class DurableChatAgentIdentityV1(_DurableChatModel):
    """The public agent identity shown by the chat shell."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    slug: _AgentSlug
    display_name: Annotated[str, Field(min_length=1, max_length=160)]

    @model_validator(mode="after")
    def validate_display_name(self) -> Self:
        if not self.display_name.strip():
            raise ValueError("agent display name must not be blank")
        return self


class DurableChatIntegrationAvailabilityV1(_DurableChatModel):
    """One credential-free capability indication from bootstrap."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    configured: bool
    unavailable_reason: _SanitizedText | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.configured and self.unavailable_reason is not None:
            raise ValueError("configured integration cannot have an unavailable reason")
        if not self.configured and self.unavailable_reason is None:
            raise ValueError("unconfigured integration requires an unavailable reason")
        if self.unavailable_reason is not None:
            _validate_sanitized_text(self.unavailable_reason)
        return self


class DurableChatIntegrationMetadataV1(_DurableChatModel):
    """Optional sanitized metadata; links themselves are per-request diagnostics."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    durable_task_scheduler: DurableChatIntegrationAvailabilityV1 | None = None
    application_insights: DurableChatIntegrationAvailabilityV1 | None = None


class DurableChatFrozenDiagnosticsV1(_DurableChatModel):
    """Credential-free diagnostics metadata frozen for one admitted run."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    sandbox_group_resource_id: _ResourceId | None = None
    durable_task_dashboard_url: _ExternalUrl | None = None
    durable_task_unavailable_reason: _SanitizedText | None = None
    application_insights_resource_id: _ResourceId | None = None
    application_insights_tracing_enabled: bool = False
    application_insights_unavailable_reason: _SanitizedText | None = None
    request_started_at: datetime
    request_ends_at: datetime

    @model_validator(mode="after")
    def validate_frozen_diagnostics(self) -> Self:
        _as_utc(self.request_started_at, "request_started_at")
        if _as_utc(self.request_ends_at, "request_ends_at") < _as_utc(
            self.request_started_at,
            "request_started_at",
        ):
            raise ValueError("durable-chat diagnostics time range is invalid")
        if (self.durable_task_dashboard_url is None) == (
            self.durable_task_unavailable_reason is None
        ):
            raise ValueError("durable-chat DTS diagnostics must be available or disabled")
        if self.durable_task_dashboard_url is not None:
            _validate_external_url(self.durable_task_dashboard_url)
        if self.durable_task_unavailable_reason is not None:
            _validate_sanitized_text(self.durable_task_unavailable_reason)
        application_insights_available = (
            self.application_insights_resource_id is not None
            and self.application_insights_tracing_enabled
        )
        if application_insights_available != (
            self.application_insights_unavailable_reason is None
        ):
            raise ValueError(
                "durable-chat Application Insights diagnostics availability is invalid"
            )
        if self.application_insights_unavailable_reason is not None:
            _validate_sanitized_text(self.application_insights_unavailable_reason)
        return self


class DurableChatBootstrapV1(_DurableChatModel):
    """Owner-safe bootstrap data for one same-origin durable-chat page."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    agent: DurableChatAgentIdentityV1
    routes: Annotated[
        tuple[DurableChatRouteDescriptorV1, ...],
        Field(min_length=len(DurableChatRouteName), max_length=len(DurableChatRouteName)),
    ]
    supported_sandbox_profiles: Annotated[
        tuple[SandboxExecutionProfile, ...],
        Field(min_length=1, max_length=len(SandboxExecutionProfile)),
    ]
    default_sandbox_profile: SandboxExecutionProfile
    foreground_streaming_available: bool
    sandbox_group_resource_id: _ResourceId | None = None
    integrations: DurableChatIntegrationMetadataV1 | None = None
    history_namespace: _Sha256

    @model_validator(mode="after")
    def validate_bootstrap(self) -> Self:
        route_names = {route.name for route in self.routes}
        if route_names != set(DurableChatRouteName):
            raise ValueError("durable-chat bootstrap must supply each route exactly once")
        if len(route_names) != len(self.routes):
            raise ValueError("durable-chat bootstrap routes must be unique")
        if len(set(self.supported_sandbox_profiles)) != len(self.supported_sandbox_profiles):
            raise ValueError("durable-chat sandbox profiles must be unique")
        if self.default_sandbox_profile not in self.supported_sandbox_profiles:
            raise ValueError("durable-chat default sandbox profile must be supported")
        return self


class DurableChatRunInitializationV1(_DurableChatModel):
    """Authoritative create-once metadata for an admitted chat observation run."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    run_id: _OpaqueId
    session_id: _OpaqueId
    owner_hash: _Sha256
    request_id_hash: _Sha256
    request_hash: _Sha256
    plan_ref: ContentRefV1
    input_ref: ContentRefV1
    expires_at: datetime
    committed_generation: _NonNegativeInt
    ui: DurableChatRunOptionsV1
    created_at: datetime
    diagnostics: DurableChatFrozenDiagnosticsV1 | None = None

    @model_validator(mode="after")
    def validate_initialization(self) -> Self:
        if _as_utc(self.expires_at, "expires_at") <= _as_utc(
            self.created_at,
            "created_at",
        ):
            raise ValueError("durable-chat initialization expiry must be after creation")
        return self


class DurableChatModelProducerV1(_DurableChatModel):
    """An external observation identity for one model attempt."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    producer_type: Literal["model"] = "model"
    step_index: _NonNegativeInt
    observation_epoch: Annotated[
        int,
        Field(ge=1, le=MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS),
    ]


class DurableChatToolProducerV1(_DurableChatModel):
    """The existing deterministic tool call key as an observation identity."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    producer_type: Literal["tool"] = "tool"
    call_key: _Sha256


type DurableChatProducerV1 = Annotated[
    DurableChatModelProducerV1 | DurableChatToolProducerV1,
    Field(discriminator="producer_type"),
]


class DurableChatProducerEpochV1(_DurableChatModel):
    """The latest externally allocated epoch for one model step."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    step_index: _NonNegativeInt
    observation_epoch: Annotated[
        int,
        Field(ge=1, le=MAX_DURABLE_CHAT_PRODUCER_ATTEMPTS),
    ]


class DurableChatProgressV1(_DurableChatModel):
    """Content-free progress from real durable execution boundaries."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    status: DurableLoopRunStatus
    phase: _Phase
    model_steps: _NonNegativeInt
    tool_calls: _NonNegativeInt
    human_waits: _NonNegativeInt
    step_index: _NonNegativeInt
    result_available: bool = False
    updated_at: datetime

    @model_validator(mode="after")
    def validate_updated_at(self) -> Self:
        _as_utc(self.updated_at, "updated_at")
        return self


class DurableChatAssistantDraftV1(_DurableChatModel):
    """The current non-terminal assistant text projection."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    producer: DurableChatModelProducerV1
    text: Annotated[str, Field(max_length=MAX_DURABLE_CHAT_DRAFT_BYTES)]
    updated_at: datetime

    @model_validator(mode="after")
    def validate_draft(self) -> Self:
        _validate_utf8_limit(
            self.text,
            MAX_DURABLE_CHAT_DRAFT_BYTES,
            "durable-chat draft",
        )
        _as_utc(self.updated_at, "updated_at")
        return self


class DurableChatToolProgressV1(_DurableChatModel):
    """A content-free current projection for one deterministic tool call."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    producer: DurableChatToolProducerV1
    step_index: _NonNegativeInt
    tool_name: _AgentSlug
    provenance: ToolProvenance
    state: DurableChatToolState
    updated_at: datetime

    @model_validator(mode="after")
    def validate_updated_at(self) -> Self:
        _as_utc(self.updated_at, "updated_at")
        return self


class DurableChatSandboxObservationV1(_DurableChatModel):
    """Historical actual sandbox identity and last-observed state for one tool."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    producer: DurableChatToolProducerV1
    step_index: _NonNegativeInt
    tool_name: _AgentSlug
    provenance: ToolProvenance
    sandbox_profile: SandboxExecutionProfile
    sandbox_group_resource_id: _ResourceId | None = None
    sandbox_id: _OpaqueId | None = None
    sandbox_generation: _PositiveInt | None = None
    state: DurableChatSandboxState
    replaced_sandbox_id: _OpaqueId | None = None
    observed_at: datetime

    @model_validator(mode="after")
    def validate_sandbox_observation(self) -> Self:
        _as_utc(self.observed_at, "observed_at")
        if self.provenance is not ToolProvenance.LOCAL:
            if (
                self.state is not DurableChatSandboxState.REMOTE_NO_SANDBOX
                or self.sandbox_group_resource_id is not None
                or self.sandbox_id is not None
                or self.sandbox_generation is not None
                or self.replaced_sandbox_id is not None
            ):
                raise ValueError("non-local tools must report remote_no_sandbox only")
            return self
        if self.sandbox_group_resource_id is None:
            raise ValueError("local tool sandbox observations require a sandbox group")
        if self.state is DurableChatSandboxState.REMOTE_NO_SANDBOX:
            raise ValueError("local tools cannot report remote_no_sandbox")
        sandbox_required = self.state not in {
            DurableChatSandboxState.NOT_ALLOCATED,
            DurableChatSandboxState.UNAVAILABLE,
        }
        if sandbox_required and self.sandbox_id is None:
            raise ValueError("sandbox identity does not match the observed lifecycle state")
        if (
            self.state is DurableChatSandboxState.NOT_ALLOCATED
            and self.sandbox_id is not None
        ):
            raise ValueError("not-allocated sandbox observations cannot name a sandbox")
        if self.state is DurableChatSandboxState.REPLACEMENT_INSTANCE:
            if self.replaced_sandbox_id is None:
                raise ValueError("replacement sandbox observation requires its prior sandbox")
        elif self.replaced_sandbox_id is not None:
            raise ValueError("only replacement observations may name a prior sandbox")
        if (
            self.state is DurableChatSandboxState.RETAINED_IDLE
            and self.sandbox_profile is not SandboxExecutionProfile.RETAINED_SESSION
        ):
            raise ValueError("retained idle state requires the retained-session profile")
        return self


class DurableChatObservationHealthV1(_DurableChatModel):
    """The bounded health projection for optional chat observations."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    degraded: bool = False
    reasons: Annotated[
        tuple[DurableChatObservationDegradationReason, ...],
        Field(max_length=len(DurableChatObservationDegradationReason)),
    ] = ()
    dropped_observations: _NonNegativeInt = 0
    reserved_observation_bytes: Annotated[
        int,
        Field(ge=0, le=MAX_DURABLE_CHAT_TOTAL_RESERVED_OBSERVATION_BYTES),
    ] = 0
    last_error_code: _Phase | None = None

    @model_validator(mode="after")
    def validate_health(self) -> Self:
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("durable-chat degradation reasons must be unique")
        if self.degraded != bool(self.reasons):
            raise ValueError("durable-chat degradation must match its reasons")
        if self.dropped_observations and not self.degraded:
            raise ValueError("dropped observations require a degraded health projection")
        return self


class DurableChatRunProjectionV1(_DurableChatModel):
    """One atomically published browser-safe run projection."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    run_id: _OpaqueId
    session_id: _OpaqueId
    published_revision: _PositiveInt
    through_sequence: _NonNegativeInt
    draft: DurableChatAssistantDraftV1 | None = None
    progress: DurableChatProgressV1
    producer_epochs: Annotated[
        tuple[DurableChatProducerEpochV1, ...],
        Field(max_length=MAX_DURABLE_CHAT_PRODUCER_EPOCHS),
    ] = ()
    tool_progress: Annotated[
        tuple[DurableChatToolProgressV1, ...],
        Field(max_length=MAX_DURABLE_CHAT_TOOL_PROGRESS),
    ] = ()
    sandbox_observations: Annotated[
        tuple[DurableChatSandboxObservationV1, ...],
        Field(max_length=MAX_DURABLE_CHAT_SANDBOX_OBSERVATIONS),
    ] = ()
    observation_health: DurableChatObservationHealthV1 = DurableChatObservationHealthV1()

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        steps = [epoch.step_index for epoch in self.producer_epochs]
        if len(steps) != len(set(steps)):
            raise ValueError("durable-chat producer epochs must have unique steps")
        call_keys = [progress.producer.call_key for progress in self.tool_progress]
        if len(call_keys) != len(set(call_keys)):
            raise ValueError("durable-chat tool progress must have unique call keys")
        if self.draft is not None and not any(
            epoch.step_index == self.draft.producer.step_index
            and epoch.observation_epoch == self.draft.producer.observation_epoch
            for epoch in self.producer_epochs
        ):
            raise ValueError("durable-chat draft producer must be in the epoch projection")
        if len(canonical_json_bytes(self)) > MAX_DURABLE_CHAT_SNAPSHOT_BYTES:
            raise ValueError("durable-chat projection exceeds the snapshot byte limit")
        return self


class DurableChatSnapshotFrameV1(_DurableChatModel):
    """A snapshot SSE frame replacing a browser projection through one watermark."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    event_type: Literal[DurableChatEventType.SNAPSHOT] = DurableChatEventType.SNAPSHOT
    captured_at: datetime
    projection: DurableChatRunProjectionV1

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        _as_utc(self.captured_at, "captured_at")
        if len(canonical_json_bytes(self)) > MAX_DURABLE_CHAT_SNAPSHOT_BYTES:
            raise ValueError("durable-chat snapshot exceeds the byte limit")
        return self


class _DurableChatObservationBaseV1(_DurableChatModel):
    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    run_id: _OpaqueId
    session_id: _OpaqueId
    observed_at: datetime

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        _as_utc(self.observed_at, "observed_at")
        if len(canonical_json_bytes(self)) > MAX_DURABLE_CHAT_EVENT_BYTES:
            raise ValueError("durable-chat observation exceeds the byte limit")
        return self


class DurableChatRunStatusObservationV1(_DurableChatObservationBaseV1):
    """A compact authoritative-status transition."""

    event_type: Literal[DurableChatEventType.RUN_STATUS] = DurableChatEventType.RUN_STATUS
    status: DurableLoopRunStatus
    phase: _Phase
    result_available: bool = False


class DurableChatProgressObservationV1(_DurableChatObservationBaseV1):
    """A content-free progress update from a real execution boundary."""

    event_type: Literal[DurableChatEventType.PROGRESS] = DurableChatEventType.PROGRESS
    progress: DurableChatProgressV1


class DurableChatModelAttemptObservationV1(_DurableChatObservationBaseV1):
    """A lifecycle update for an external model-attempt epoch."""

    event_type: Literal[DurableChatEventType.MODEL_ATTEMPT] = DurableChatEventType.MODEL_ATTEMPT
    producer: DurableChatModelProducerV1
    state: DurableChatModelAttemptState


class DurableChatAssistantTextObservationV1(_DurableChatObservationBaseV1):
    """One genuine assistant-visible foreground text delta."""

    event_type: Literal[DurableChatEventType.ASSISTANT_TEXT] = DurableChatEventType.ASSISTANT_TEXT
    producer: DurableChatModelProducerV1
    delta: Annotated[str, Field(min_length=1, max_length=MAX_DURABLE_CHAT_TEXT_DELTA_BYTES)]

    @model_validator(mode="after")
    def validate_delta(self) -> Self:
        _validate_utf8_limit(
            self.delta,
            MAX_DURABLE_CHAT_TEXT_DELTA_BYTES,
            "durable-chat text delta",
        )
        return self


class DurableChatAssistantDraftReplacedObservationV1(_DurableChatObservationBaseV1):
    """Fence an older model draft before a retry begins writing a replacement."""

    event_type: Literal[DurableChatEventType.ASSISTANT_DRAFT_REPLACED] = (
        DurableChatEventType.ASSISTANT_DRAFT_REPLACED
    )
    previous_producer: DurableChatModelProducerV1
    producer: DurableChatModelProducerV1

    @model_validator(mode="after")
    def validate_replacement(self) -> Self:
        if (
            self.previous_producer.step_index != self.producer.step_index
            or self.producer.observation_epoch <= self.previous_producer.observation_epoch
        ):
            raise ValueError("draft replacement must advance one model observation epoch")
        return self


class DurableChatToolObservationV1(_DurableChatObservationBaseV1):
    """A content-free tool start or terminal outcome."""

    event_type: Literal[DurableChatEventType.TOOL] = DurableChatEventType.TOOL
    progress: DurableChatToolProgressV1


class DurableChatSandboxObservationEventV1(_DurableChatObservationBaseV1):
    """A historical local-sandbox identity observation."""

    event_type: Literal[DurableChatEventType.SANDBOX] = DurableChatEventType.SANDBOX
    observation: DurableChatSandboxObservationV1


class DurableChatHumanInputObservationV1(_DurableChatObservationBaseV1):
    """A content-free human-input state update."""

    event_type: Literal[DurableChatEventType.HUMAN_INPUT] = DurableChatEventType.HUMAN_INPUT
    request_id: _OpaqueId
    state: HumanInputState
    expires_at: datetime

    @model_validator(mode="after")
    def validate_human_input(self) -> Self:
        _as_utc(self.expires_at, "expires_at")
        return self


class DurableChatTerminalObservationV1(_DurableChatObservationBaseV1):
    """A terminal state marker; final response text remains on the result route."""

    event_type: Literal[DurableChatEventType.TERMINAL] = DurableChatEventType.TERMINAL
    status: DurableLoopRunStatus
    result_available: bool
    committed_generation: _NonNegativeInt | None = None
    error_code: _Phase | None = None

    @model_validator(mode="after")
    def validate_terminal(self) -> Self:
        if self.status not in {
            DurableLoopRunStatus.COMPLETED,
            DurableLoopRunStatus.FAILED,
            DurableLoopRunStatus.CANCELLED,
        }:
            raise ValueError("durable-chat terminal observation must be terminal")
        if self.status is DurableLoopRunStatus.COMPLETED:
            if not self.result_available or self.error_code is not None:
                raise ValueError("completed durable-chat terminal must have only a result")
        elif self.result_available:
            raise ValueError("non-completed durable-chat terminal cannot have a result")
        return self


class DurableChatDegradedObservationV1(_DurableChatObservationBaseV1):
    """A visible notification that optional observations are incomplete."""

    event_type: Literal[DurableChatEventType.DEGRADED] = DurableChatEventType.DEGRADED
    health: DurableChatObservationHealthV1

    @model_validator(mode="after")
    def validate_degraded(self) -> Self:
        if not self.health.degraded:
            raise ValueError("degraded observation requires a degraded health projection")
        return self


type DurableChatObservationV1 = Annotated[
    DurableChatRunStatusObservationV1
    | DurableChatProgressObservationV1
    | DurableChatModelAttemptObservationV1
    | DurableChatAssistantTextObservationV1
    | DurableChatAssistantDraftReplacedObservationV1
    | DurableChatToolObservationV1
    | DurableChatSandboxObservationEventV1
    | DurableChatHumanInputObservationV1
    | DurableChatTerminalObservationV1
    | DurableChatDegradedObservationV1,
    Field(discriminator="event_type"),
]


class DurableChatEventFrameV1(_DurableChatModel):
    """One replayable SSE delta assigned a run-scoped sequence by the journal."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    sequence: _PositiveInt
    published_revision: _PositiveInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    event: DurableChatObservationV1

    @model_validator(mode="after")
    def validate_frame(self) -> Self:
        if len(canonical_json_bytes(self)) > MAX_DURABLE_CHAT_EVENT_BYTES:
            raise ValueError("durable-chat event frame exceeds the byte limit")
        return self


type DurableChatStreamFrameV1 = DurableChatSnapshotFrameV1 | DurableChatEventFrameV1


class DurableChatObservationBatchV1(_DurableChatModel):
    """A bounded unsequenced batch passed from the observer drainer to storage."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    run_id: _OpaqueId
    observations: Annotated[
        tuple[DurableChatObservationV1, ...],
        Field(min_length=1, max_length=MAX_DURABLE_CHAT_EVENT_BATCH),
    ]

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        if any(observation.run_id != self.run_id for observation in self.observations):
            raise ValueError("durable-chat batch observations must match the run")
        if len(canonical_json_bytes(self)) > MAX_DURABLE_CHAT_BATCH_BYTES:
            raise ValueError("durable-chat observation batch exceeds the byte limit")
        return self

    def reserved_bytes(self) -> int:
        """Return the storage allowance to reserve before publication."""
        return len(canonical_json_bytes(self))


class DurableChatPublicationResultV1(_DurableChatModel):
    """One bounded journal-publish result that never changes execution authority."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    run_id: _OpaqueId
    disposition: DurableChatPublicationDisposition
    published_revision: _NonNegativeInt
    through_sequence: _NonNegativeInt
    health: DurableChatObservationHealthV1

    @model_validator(mode="after")
    def validate_publication(self) -> Self:
        if (
            self.disposition is DurableChatPublicationDisposition.PUBLISHED
            and self.published_revision == 0
        ):
            raise ValueError("published durable-chat result requires a revision")
        if (
            self.disposition is DurableChatPublicationDisposition.DEGRADED
            and not self.health.degraded
        ):
            raise ValueError("degraded durable-chat result requires degraded health")
        return self


class DurableChatReplayPageV1(_DurableChatModel):
    """A replay response with either contiguous deltas or a replacement snapshot."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    run_id: _OpaqueId
    requested_after_sequence: _NonNegativeInt
    disposition: DurableChatReplayDisposition
    through_sequence: _NonNegativeInt
    snapshot: DurableChatSnapshotFrameV1 | None = None
    events: Annotated[
        tuple[DurableChatEventFrameV1, ...],
        Field(max_length=MAX_DURABLE_CHAT_REPLAY_EVENTS),
    ] = ()

    @model_validator(mode="after")
    def validate_replay(self) -> Self:  # noqa: PLR0912
        sequences = [event.sequence for event in self.events]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("durable-chat replay event sequences must be strictly increasing")
        if any(event.event.run_id != self.run_id for event in self.events):
            raise ValueError("durable-chat replay events must match the requested run")
        sessions = {event.event.session_id for event in self.events}
        if len(sessions) > 1:
            raise ValueError("durable-chat replay events must match one session")
        if any(sequence > self.through_sequence for sequence in sequences):
            raise ValueError("durable-chat replay event exceeds its published watermark")
        if self.disposition is DurableChatReplayDisposition.CURSOR_AHEAD:
            if self.snapshot is not None or self.events:
                raise ValueError("cursor-ahead replay cannot contain snapshot or events")
            return self
        if self.disposition is DurableChatReplayDisposition.DELTAS:
            if self.snapshot is not None:
                raise ValueError("delta replay cannot contain a snapshot")
            if any(sequence <= self.requested_after_sequence for sequence in sequences):
                raise ValueError("delta replay must follow the requested cursor")
            expected = self.requested_after_sequence + 1
            if sequences != list(range(expected, expected + len(sequences))):
                raise ValueError("durable-chat replay deltas must be contiguous")
            return self
        if self.snapshot is None:
            raise ValueError("snapshot-required replay requires a snapshot")
        projection = self.snapshot.projection
        if projection.run_id != self.run_id:
            raise ValueError("durable-chat snapshot run does not match replay run")
        if sessions and sessions != {projection.session_id}:
            raise ValueError("durable-chat replay events do not match the snapshot session")
        if projection.through_sequence > self.through_sequence:
            raise ValueError("durable-chat snapshot exceeds the replay watermark")
        if any(sequence <= projection.through_sequence for sequence in sequences):
            raise ValueError("replay deltas must follow the snapshot watermark")
        expected = projection.through_sequence + 1
        if sequences != list(range(expected, expected + len(sequences))):
            raise ValueError("durable-chat snapshot deltas must be contiguous")
        return self


class DurableChatDiagnosticLinkV1(_DurableChatModel):
    """One secret-free DTS or Application Insights request diagnostic action."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    kind: DurableChatIntegrationKind
    available: bool
    href: _ExternalUrl | None = None
    unavailable_reason: _SanitizedText | None = None

    @model_validator(mode="after")
    def validate_link(self) -> Self:
        if self.available:
            if self.href is None or self.unavailable_reason is not None:
                raise ValueError("available diagnostic link requires only an href")
            _validate_external_url(self.href)
        elif self.href is not None or self.unavailable_reason is None:
            raise ValueError("unavailable diagnostic link requires only a reason")
        elif self.unavailable_reason is not None:
            _validate_sanitized_text(self.unavailable_reason)
        return self


class DurableChatDiagnosticsV1(_DurableChatModel):
    """Owner-authorized, browser-safe run diagnostics without provider payloads."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    run_id: _OpaqueId
    session_id: _OpaqueId
    model_mode: DurableChatModelMode
    status: DurableLoopRunStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    committed_generation: _NonNegativeInt | None = None
    configured_sandbox_group_resource_id: _ResourceId | None = None
    sandbox_observations: Annotated[
        tuple[DurableChatSandboxObservationV1, ...],
        Field(max_length=MAX_DURABLE_CHAT_SANDBOX_OBSERVATIONS),
    ] = ()
    observation_health: DurableChatObservationHealthV1
    links: Annotated[
        tuple[DurableChatDiagnosticLinkV1, ...],
        Field(max_length=len(DurableChatIntegrationKind)),
    ] = ()

    @model_validator(mode="after")
    def validate_diagnostics(self) -> Self:
        created_at = _as_utc(self.created_at, "created_at")
        updated_at = _as_utc(self.updated_at, "updated_at")
        expires_at = _as_utc(self.expires_at, "expires_at")
        if not created_at <= updated_at <= expires_at:
            raise ValueError("durable-chat diagnostic times must be ordered")
        kinds = [link.kind for link in self.links]
        if len(kinds) != len(set(kinds)):
            raise ValueError("durable-chat diagnostic links must be unique by kind")
        return self


class DurableChatBrowserCursorV1(_DurableChatModel):
    """The exclusive journal cursor persisted with a browser run projection."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    run_id: _OpaqueId
    after_sequence: _NonNegativeInt
    published_revision: _PositiveInt


class DurableChatBrowserRunStateV1(_DurableChatModel):
    """The complete browser record committed atomically with its replay cursor."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    history_namespace: _Sha256
    session_id: _OpaqueId
    run_id: _OpaqueId
    projection: DurableChatRunProjectionV1
    cursor: DurableChatBrowserCursorV1
    persisted_at: datetime

    @model_validator(mode="after")
    def validate_browser_run_state(self) -> Self:
        _as_utc(self.persisted_at, "persisted_at")
        if (
            self.projection.run_id != self.run_id
            or self.cursor.run_id != self.run_id
            or self.projection.session_id != self.session_id
        ):
            raise ValueError("browser run state must bind one session and one run")
        if (
            self.cursor.after_sequence != self.projection.through_sequence
            or self.cursor.published_revision != self.projection.published_revision
        ):
            raise ValueError("browser cursor must match its atomically stored projection")
        return self


class DurableChatBrowserSessionV1(_DurableChatModel):
    """One browser-local retained chat session with no cross-device semantics."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    history_namespace: _Sha256
    session_id: _OpaqueId
    display_name: Annotated[str, Field(min_length=1, max_length=160)]
    created_at: datetime
    updated_at: datetime
    latest_run_id: _OpaqueId | None = None

    @model_validator(mode="after")
    def validate_browser_session(self) -> Self:
        if not self.display_name.strip():
            raise ValueError("browser session display name must not be blank")
        if _as_utc(self.updated_at, "updated_at") < _as_utc(
            self.created_at,
            "created_at",
        ):
            raise ValueError("browser session update cannot precede creation")
        return self


class DurableChatBrowserPreferencesV1(_DurableChatModel):
    """The browser-local inspector visibility preference for one history namespace."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    history_namespace: _Sha256
    details_visible: bool = True


class DurableChatStartBodyV1(_DurableChatModel):
    """The normalized body retained locally to reconcile one chat submission."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    prompt: Annotated[str, Field(min_length=1, max_length=256 * 1024)]
    session_id: _OpaqueId
    request_id: _OpaqueId | None = Field(default=None, exclude_if=lambda value: value is None)
    sandbox_profile: SandboxExecutionProfile = SandboxExecutionProfile.PER_CALL
    fault_profile: DurableFaultProfile = DurableFaultProfile.NONE
    ui: DurableChatStartOptionsV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_start_body(self) -> Self:
        if not self.prompt.strip():
            raise ValueError("durable-chat prompt must not be blank")
        _validate_utf8_limit(self.prompt, 256 * 1024, "durable-chat prompt")
        return self


class DurableChatBrowserSubmissionV1(_DurableChatModel):
    """One locally persisted idempotent start body, before its HTTP submission."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    history_namespace: _Sha256
    idempotency_key: _OpaqueId
    body: DurableChatStartBodyV1
    persisted_at: datetime

    @model_validator(mode="after")
    def validate_submission(self) -> Self:
        _as_utc(self.persisted_at, "persisted_at")
        return self


class DurableChatEnqueueResultV1(_DurableChatModel):
    """A synchronous observation enqueue outcome, never a tool/model result."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    disposition: DurableChatEnqueueDisposition
    pending_observations: Annotated[
        int,
        Field(ge=0, le=MAX_DURABLE_CHAT_PENDING_OBSERVATIONS),
    ]
    degradation_reason: DurableChatObservationDegradationReason | None = None

    @model_validator(mode="after")
    def validate_enqueue_result(self) -> Self:
        if (self.disposition is DurableChatEnqueueDisposition.ENQUEUED) != (
            self.degradation_reason is None
        ):
            raise ValueError("enqueue degradation reason must match the disposition")
        return self


class DurableChatDrainResultV1(_DurableChatModel):
    """The drainer's bounded best-effort result after a separate deadline."""

    schema_version: Literal["1"] = DURABLE_CHAT_SCHEMA_VERSION
    published_observations: _NonNegativeInt
    dropped_observations: _NonNegativeInt
    health: DurableChatObservationHealthV1


@runtime_checkable
class DurableChatRunInitializationPort(Protocol):
    """Authoritative create-once store; it is not the best-effort journal."""

    async def load_run_initialization(
        self,
        *,
        run_id: str,
    ) -> DurableChatRunInitializationV1 | None:
        """Load the winning admitted-run initialization, if it exists."""

    async def create_run_initialization_once(
        self,
        *,
        initialization: DurableChatRunInitializationV1,
    ) -> DurableChatRunInitializationV1:
        """Create only if absent and return the winner for competing starters."""


@runtime_checkable
class DurableChatJournalPort(Protocol):
    """Async storage boundary used only by the observer publisher and readers."""

    async def publish(
        self,
        *,
        run_id: str,
        expected_published_revision: int,
        batch: DurableChatObservationBatchV1,
        deadline: datetime,
    ) -> DurableChatPublicationResultV1:
        """Publish one content-first batch with bounded CAS and storage work."""

    async def reserve_model_producers(
        self,
        *,
        run_id: str,
        step_index: int,
        count: int,
        deadline: datetime,
    ) -> tuple[DurableChatModelProducerV1, ...]:
        """Reserve monotonic external model observation epochs for one step."""

    async def replay(
        self,
        *,
        run_id: str,
        after_sequence: int,
        limit: int,
    ) -> DurableChatReplayPageV1:
        """Read deltas after an exclusive cursor or return a replacement snapshot."""

    async def read_diagnostics(
        self,
        *,
        run_id: str,
    ) -> DurableChatDiagnosticsV1 | None:
        """Read browser-safe diagnostics after the caller has authorized ownership."""


@runtime_checkable
class DurableChatObservationSink(Protocol):
    """Non-blocking execution-side enqueue boundary; it must not await storage."""

    def try_enqueue(
        self,
        *,
        observation: DurableChatObservationV1,
    ) -> DurableChatEnqueueResultV1:
        """Queue one observation or return a visible degraded/drop result immediately."""


@runtime_checkable
class DurableChatObservationDrainerPort(Protocol):
    """Best-effort async publisher invoked outside tool and provider retry scopes."""

    async def drain(self, *, deadline: datetime) -> DurableChatDrainResultV1:
        """Drain queued observations within the independent publisher deadline."""


def parse_durable_chat_document[ModelT: BaseModel](
    payload: bytes | str,
    model: type[ModelT],
    *,
    maximum_bytes: int = MAX_DURABLE_CHAT_DOCUMENT_BYTES,
) -> ModelT:
    """Strictly parse one bounded durable-chat JSON object."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > maximum_bytes:
        raise DurableChatProtocolDocumentError("durable-chat document exceeds the byte limit")
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
        raise DurableChatProtocolDocumentError("invalid durable-chat protocol document") from exc


def durable_chat_run_correlation(run_id: str) -> str:
    """Return the stable non-provider correlation used by chat diagnostics."""
    if _OPAQUE_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("durable-chat run ID is invalid")
    return canonical_hash(
        {
            "durable_chat_schema_version": DURABLE_CHAT_SCHEMA_VERSION,
            "run_id": run_id,
        }
    )


def _as_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_utf8_limit(value: str, maximum_bytes: int, name: str) -> None:
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{name} exceeds the byte limit")


def _validate_same_origin_path(path: str) -> None:
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or "?" in path
        or "#" in path
        or "://" in path
        or any(character.isspace() or ord(character) < 32 for character in path)
    ):
        raise ValueError("durable-chat route must be a safe same-origin path")
    decoded = unquote(path)
    if (
        "\\" in decoded
        or "//" in decoded
        or "://" in decoded
        or any(segment == ".." for segment in decoded.split("/"))
    ):
        raise ValueError("durable-chat route path traversal is not allowed")
    for parameter in _ROUTE_PARAMETER_PATTERN.findall(path):
        if parameter not in {"run_id", "request_id"}:
            raise ValueError("durable-chat route uses an unknown path parameter")
    remaining = _ROUTE_PARAMETER_PATTERN.sub("", path)
    if "{" in remaining or "}" in remaining:
        raise ValueError("durable-chat route has an invalid path parameter")


def _validate_external_url(value: str) -> None:
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("diagnostic URL contains invalid whitespace")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("diagnostic URL is invalid") from exc
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("diagnostic URL must be a credential-free HTTP URL")
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("diagnostic URL must use HTTPS outside local development")
    fragment_query = parsed.fragment.partition("?")[2] or parsed.fragment
    for component in (parsed.query, fragment_query):
        for key, query_value in parse_qsl(component, keep_blank_values=True):
            if unquote(key).casefold() in _SENSITIVE_QUERY_KEYS:
                raise ValueError("diagnostic URL must not contain authorization material")
            lowered_value = unquote(query_value).casefold()
            if any(marker in lowered_value for marker in _SENSITIVE_URL_VALUE_MARKERS):
                raise ValueError("diagnostic URL must not contain authorization material")


def _validate_sanitized_text(value: str) -> None:
    lowered = value.casefold()
    if (
        any(character.isspace() and character not in {" ", "\t"} for character in value)
        or "://" in lowered
        or any(marker in lowered for marker in _SENSITIVE_URL_VALUE_MARKERS)
        or _SENSITIVE_TEXT_MARKER.search(value) is not None
    ):
        raise ValueError("durable-chat text must not contain authorization material")
