"""Pure models for durable ACA session control records."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from .._session_id import SESSION_ID_PATTERN

TABLE_NAME = "AzureFunctionsAgentsSessions"
ROW_SCHEMA_VERSION = 1
MAX_SNAPSHOT_IDS = 64
MAX_SNAPSHOT_IDS_SERIALIZED_BYTES = 8192

type OwnerKind = Literal["entra_user", "function_app", "trigger_binding"]
type SessionStatus = Literal[
    "creating",
    "ready",
    "running",
    "canceling",
    "suspending",
    "suspended",
    "resuming",
    "failed",
    "quarantined",
    "tombstoned",
    "deleting",
    "deleted",
]
type DurableRunStatus = Literal[
    "accepted",
    "running",
    "succeeded",
    "failed",
    "canceled",
    "timed_out",
    "abandoned",
]
type TableEntityValue = str | int | bool | datetime
type TableEntity = dict[str, TableEntityValue]

_OWNER_KINDS: frozenset[str] = frozenset(
    {"entra_user", "function_app", "trigger_binding"}
)
_SESSION_STATUSES: frozenset[str] = frozenset(
    {
        "creating",
        "ready",
        "running",
        "canceling",
        "suspending",
        "suspended",
        "resuming",
        "failed",
        "quarantined",
        "tombstoned",
        "deleting",
        "deleted",
    }
)
_RUN_STATUSES: frozenset[str] = frozenset(
    {
        "accepted",
        "running",
        "succeeded",
        "failed",
        "canceled",
        "timed_out",
        "abandoned",
    }
)
_HASH_PATTERN = re.compile(r"^[ao][1-9][0-9]*-[0-9a-f]{64}$")
_OWNER_VERSION_PATTERN = re.compile(r"^o[1-9][0-9]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STATE_STORE_FINGERPRINT_PATTERN = re.compile(r"^s1-[0-9a-f]{64}$")
_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_REGION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")


class SessionStateContractError(ValueError):
    """Raised when identity or durable-row data violates the P3a contract."""


def _normalize_guid(value: str, field_name: str) -> str:
    try:
        return str(UUID(value.strip()))
    except (AttributeError, ValueError) as exc:
        raise SessionStateContractError(f"{field_name} must be a valid UUID") from exc


def _normalize_arm_segment(value: str, field_name: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip()).lower()
    if not normalized:
        raise SessionStateContractError(f"{field_name} must be non-empty")
    if any(character in normalized for character in ("/", "\\")):
        raise SessionStateContractError(f"{field_name} must be one ARM resource-id segment")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise SessionStateContractError(f"{field_name} must not contain control characters")
    return normalized


def _normalize_agent_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or len(normalized.encode("utf-8")) > 128:
        raise SessionStateContractError("agent_slug must be 1-128 UTF-8 bytes")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise SessionStateContractError("agent_slug must not contain control characters")
    return normalized


def _validate_opaque_id(value: str, field_name: str) -> str:
    if SESSION_ID_PATTERN.fullmatch(value) is None:
        raise SessionStateContractError(
            f"{field_name} must match {SESSION_ID_PATTERN.pattern}"
        )
    return value


def _validate_hash(value: str, field_name: str) -> str:
    if _HASH_PATTERN.fullmatch(value) is None:
        raise SessionStateContractError(
            f"{field_name} must be a versioned lower-case SHA-256 hash"
        )
    return value


def _validate_sha256(value: str, field_name: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise SessionStateContractError(f"{field_name} must be a lower-case SHA-256 digest")
    return value


def _bounded_text(
    value: str,
    field_name: str,
    *,
    max_bytes: int,
    allow_empty: bool = False,
) -> str:
    normalized = unicodedata.normalize("NFC", value)
    encoded = normalized.encode("utf-8")
    if (not allow_empty and not normalized) or len(encoded) > max_bytes:
        qualifier = "0" if allow_empty else "1"
        raise SessionStateContractError(
            f"{field_name} must be {qualifier}-{max_bytes} UTF-8 bytes"
        )
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise SessionStateContractError(f"{field_name} must not contain control characters")
    return normalized


def _optional_bounded_text(
    value: str | None,
    field_name: str,
    *,
    max_bytes: int,
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name, max_bytes=max_bytes)


def _validate_reason(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if _REASON_PATTERN.fullmatch(value) is None:
        raise SessionStateContractError(
            f"{field_name} must be a lower-case bounded reason code"
        )
    return value


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SessionStateContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def validate_generation(generation: int) -> int:
    """Validate a forward-only session-backing generation."""
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise SessionStateContractError("generation must be an integer >= 1")
    return generation


def validate_generation_transition(
    previous: int,
    candidate: int,
    *,
    backing_rebind: bool,
) -> None:
    """Validate that normal writes preserve generation and rebinds advance it."""
    validate_generation(previous)
    validate_generation(candidate)
    if backing_rebind:
        if candidate <= previous:
            raise SessionStateContractError(
                "a state-preserving backing rebind must strictly increase generation"
            )
    elif candidate != previous:
        raise SessionStateContractError("normal session mutations must preserve generation")


@dataclass(frozen=True, slots=True, repr=False)
class AppIdentity:
    """Stable Function App/slot identity derived from platform inputs."""

    subscription_id: str
    resource_group: str
    site_name: str
    slot_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _normalize_guid(self.subscription_id, "subscription_id"),
        )
        object.__setattr__(
            self,
            "resource_group",
            _normalize_arm_segment(self.resource_group, "resource_group"),
        )
        object.__setattr__(
            self,
            "site_name",
            _normalize_arm_segment(self.site_name, "site_name"),
        )
        if self.slot_name is not None:
            normalized_slot = self.slot_name.strip()
            if not normalized_slot or normalized_slot.lower() == "production":
                object.__setattr__(self, "slot_name", None)
            else:
                object.__setattr__(
                    self,
                    "slot_name",
                    _normalize_arm_segment(normalized_slot, "slot_name"),
                )

    @property
    def resource_id(self) -> str:
        """Return the canonical slot-aware ARM resource ID."""
        site_id = (
            f"/subscriptions/{self.subscription_id}"
            f"/resourcegroups/{self.resource_group}"
            f"/providers/microsoft.web/sites/{self.site_name}"
        )
        if self.slot_name is None:
            return site_id
        return f"{site_id}/slots/{self.slot_name}"

    def __repr__(self) -> str:
        return "AppIdentity(resource_id=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class EntraPrincipal:
    """Stable identity fields extracted from a platform-validated Easy Auth principal."""

    tenant_id: str
    object_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _normalize_guid(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "object_id", _normalize_guid(self.object_id, "object_id"))

    def __repr__(self) -> str:
        return "EntraPrincipal(<redacted>)"


@dataclass(frozen=True, slots=True)
class FunctionAppPrincipal:
    """Marker proving the Functions host applied function/admin-key authentication."""


@dataclass(frozen=True, slots=True)
class TriggerBindingPrincipal:
    """Reserved owner input for FRD 0009; unsupported in P3a."""


type OwnerPrincipal = EntraPrincipal | FunctionAppPrincipal | TriggerBindingPrincipal


@dataclass(frozen=True, slots=True, repr=False)
class EntraUserOwnerContext:
    """Per-user owner context resolved from immutable Entra claims."""

    app_identity: AppIdentity
    agent_slug: str
    tenant_id: str
    object_id: str
    kind: Literal["entra_user"] = field(default="entra_user", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_slug", _normalize_agent_slug(self.agent_slug))
        object.__setattr__(self, "tenant_id", _normalize_guid(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "object_id", _normalize_guid(self.object_id, "object_id"))

    def __repr__(self) -> str:
        return (
            "EntraUserOwnerContext("
            f"kind={self.kind!r}, agent_slug={self.agent_slug!r}, claims=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class FunctionAppOwnerContext:
    """App-scoped owner context shared by all valid function/admin-key callers."""

    app_identity: AppIdentity
    agent_slug: str
    kind: Literal["function_app"] = field(default="function_app", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_slug", _normalize_agent_slug(self.agent_slug))

    def __repr__(self) -> str:
        return (
            "FunctionAppOwnerContext("
            f"kind={self.kind!r}, agent_slug={self.agent_slug!r}, app=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TriggerBindingOwnerContext:
    """Reserved owner-context discriminator; it cannot be resolved or hashed in P3a."""

    app_identity: AppIdentity
    agent_slug: str
    kind: Literal["trigger_binding"] = field(default="trigger_binding", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_slug", _normalize_agent_slug(self.agent_slug))

    def __repr__(self) -> str:
        return (
            "TriggerBindingOwnerContext("
            f"kind={self.kind!r}, agent_slug={self.agent_slug!r}, reserved=True)"
        )


type OwnerContext = (
    EntraUserOwnerContext | FunctionAppOwnerContext | TriggerBindingOwnerContext
)


@dataclass(frozen=True, slots=True)
class OwnerPartition:
    """Hashed owner/app partition shared by one admission's durable rows."""

    owner_hash_version: str
    app_hash: str
    owner_kind: OwnerKind
    owner_hash: str

    def __post_init__(self) -> None:
        if _OWNER_VERSION_PATTERN.fullmatch(self.owner_hash_version) is None:
            raise SessionStateContractError("owner_hash_version must match o<version>")
        _validate_hash(self.app_hash, "app_hash")
        _validate_hash(self.owner_hash, "owner_hash")
        if not self.app_hash.startswith("a"):
            raise SessionStateContractError("app_hash must use an app canonicalizer prefix")
        if self.owner_hash != (
            f"{self.owner_hash_version}-{self.owner_hash.rsplit('-', 1)[-1]}"
        ):
            raise SessionStateContractError(
                "owner_hash prefix must match owner_hash_version"
            )
        if self.owner_kind not in _OWNER_KINDS:
            raise SessionStateContractError("unsupported owner_kind")

    @property
    def partition_key(self) -> str:
        return (
            f"{self.owner_hash_version}:{self.app_hash}:"
            f"{self.owner_kind}:{self.owner_hash}"
        )

    @classmethod
    def parse(cls, value: str) -> OwnerPartition:
        parts = value.split(":")
        if len(parts) != 4:
            raise SessionStateContractError("invalid owner partition key")
        owner_hash_version, app_hash, owner_kind_value, owner_hash = parts
        if owner_kind_value not in _OWNER_KINDS:
            raise SessionStateContractError("unsupported owner_kind")
        return cls(
            owner_hash_version=owner_hash_version,
            app_hash=app_hash,
            owner_kind=cast(OwnerKind, owner_kind_value),
            owner_hash=owner_hash,
        )


@dataclass(frozen=True, slots=True)
class SessionRowKey:
    session_id: str

    def __post_init__(self) -> None:
        _validate_opaque_id(self.session_id, "session_id")

    def __str__(self) -> str:
        return f"session:{self.session_id}"


@dataclass(frozen=True, slots=True)
class RunRowKey:
    session_id: str
    run_id: str

    def __post_init__(self) -> None:
        _validate_opaque_id(self.session_id, "session_id")
        _validate_opaque_id(self.run_id, "run_id")

    def __str__(self) -> str:
        return f"run:{self.session_id}:{self.run_id}"


@dataclass(frozen=True, slots=True)
class IdempotencyRowKey:
    session_id: str
    idempotency_hash: str

    def __post_init__(self) -> None:
        _validate_opaque_id(self.session_id, "session_id")
        _validate_sha256(self.idempotency_hash, "idempotency_hash")

    def __str__(self) -> str:
        return f"idem:{self.session_id}:{self.idempotency_hash}"


type DurableRowKey = SessionRowKey | RunRowKey | IdempotencyRowKey


def parse_row_key(value: str) -> DurableRowKey:
    """Parse a P3a durable row key without accepting ambiguous extra components."""
    parts = value.split(":")
    if len(parts) == 2 and parts[0] == "session":
        return SessionRowKey(parts[1])
    if len(parts) == 3 and parts[0] == "run":
        return RunRowKey(parts[1], parts[2])
    if len(parts) == 3 and parts[0] == "idem":
        return IdempotencyRowKey(parts[1], parts[2])
    raise SessionStateContractError("invalid durable row key")


def _validate_snapshot_id(value: str) -> str:
    return _bounded_text(value, "snapshot_id", max_bytes=256)


def encode_snapshot_ids(snapshot_ids: Sequence[str]) -> str:
    """Encode snapshot IDs as deterministic, bounded row-schema-v1 JSON."""
    values = tuple(_validate_snapshot_id(value) for value in snapshot_ids)
    if len(values) > MAX_SNAPSHOT_IDS:
        raise SessionStateContractError(
            f"snapshot_ids exceeds the {MAX_SNAPSHOT_IDS}-item limit"
        )
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_IDS_SERIALIZED_BYTES:
        raise SessionStateContractError(
            "snapshot_ids exceeds the serialized UTF-8 byte limit"
        )
    return encoded


def decode_snapshot_ids(value: str) -> tuple[str, ...]:
    """Decode only the canonical bounded JSON representation written by P3a."""
    try:
        decoded: object = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SessionStateContractError("snapshot_ids is not valid JSON") from exc
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) for item in decoded
    ):
        raise SessionStateContractError("snapshot_ids must be a JSON array of strings")
    values = tuple(cast(str, item) for item in decoded)
    if encode_snapshot_ids(values) != value:
        raise SessionStateContractError("snapshot_ids is not canonically encoded")
    return values


def _base_entity(
    partition: OwnerPartition,
    row_key: DurableRowKey,
) -> TableEntity:
    return {
        "PartitionKey": partition.partition_key,
        "RowKey": str(row_key),
        "schema_version": ROW_SCHEMA_VERSION,
        "owner_hash_version": partition.owner_hash_version,
    }


@dataclass(frozen=True, slots=True)
class DurableSessionRecord:
    """Version-one durable session entity, independent of an Azure SDK."""

    owner_partition: OwnerPartition
    session_id: str
    sandbox_id: str | None
    generation: int
    digest_kind: str
    digest: str
    protocol: str
    status: SessionStatus
    last_activity_at: datetime
    expires_at: datetime
    idle_policy_armed: bool
    active_run_id: str | None
    snapshot_ids: tuple[str, ...]
    region: str
    state_store_fingerprint: str
    quarantine_reason: str | None
    tombstone_reason: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _validate_opaque_id(self.session_id, "session_id")
        object.__setattr__(
            self,
            "sandbox_id",
            _optional_bounded_text(
                self.sandbox_id,
                "sandbox_id",
                max_bytes=256,
            ),
        )
        validate_generation(self.generation)
        object.__setattr__(
            self,
            "digest_kind",
            _bounded_text(self.digest_kind, "digest_kind", max_bytes=64),
        )
        object.__setattr__(
            self,
            "digest",
            _bounded_text(self.digest, "digest", max_bytes=256),
        )
        object.__setattr__(
            self,
            "protocol",
            _bounded_text(self.protocol, "protocol", max_bytes=64),
        )
        if self.status not in _SESSION_STATUSES:
            raise SessionStateContractError("unsupported session status")
        object.__setattr__(
            self,
            "last_activity_at",
            _utc_datetime(self.last_activity_at, "last_activity_at"),
        )
        object.__setattr__(self, "expires_at", _utc_datetime(self.expires_at, "expires_at"))
        if not isinstance(self.idle_policy_armed, bool):
            raise SessionStateContractError("idle_policy_armed must be a boolean")
        if self.active_run_id is not None:
            _validate_opaque_id(self.active_run_id, "active_run_id")
        normalized_snapshots = tuple(self.snapshot_ids)
        encode_snapshot_ids(normalized_snapshots)
        object.__setattr__(self, "snapshot_ids", normalized_snapshots)
        normalized_region = self.region.strip().lower()
        if _REGION_PATTERN.fullmatch(normalized_region) is None:
            raise SessionStateContractError("region must be a normalized Azure region")
        object.__setattr__(self, "region", normalized_region)
        if _STATE_STORE_FINGERPRINT_PATTERN.fullmatch(
            self.state_store_fingerprint
        ) is None:
            raise SessionStateContractError(
                "state_store_fingerprint must match s1-<lower-case sha256>"
            )
        object.__setattr__(
            self,
            "quarantine_reason",
            _validate_reason(self.quarantine_reason, "quarantine_reason"),
        )
        object.__setattr__(
            self,
            "tombstone_reason",
            _validate_reason(self.tombstone_reason, "tombstone_reason"),
        )
        if self.status == "quarantined" and self.quarantine_reason is None:
            raise SessionStateContractError(
                "quarantined sessions require quarantine_reason"
            )
        if self.status == "tombstoned" and self.tombstone_reason is None:
            raise SessionStateContractError("tombstoned sessions require tombstone_reason")
        object.__setattr__(self, "created_at", _utc_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _utc_datetime(self.updated_at, "updated_at"))
        if self.updated_at < self.created_at:
            raise SessionStateContractError("updated_at must not precede created_at")

    @property
    def row_key(self) -> SessionRowKey:
        return SessionRowKey(self.session_id)

    def to_table_entity(self) -> TableEntity:
        entity = _base_entity(self.owner_partition, self.row_key)
        entity.update(
            {
                "sandbox_id": self.sandbox_id or "",
                "generation": self.generation,
                "digest_kind": self.digest_kind,
                "digest": self.digest,
                "protocol": self.protocol,
                "status": self.status,
                "last_activity_at": self.last_activity_at,
                "expires_at": self.expires_at,
                "idle_policy_armed": self.idle_policy_armed,
                "active_run_id": self.active_run_id or "",
                "snapshot_ids": encode_snapshot_ids(self.snapshot_ids),
                "region": self.region,
                "state_store_fingerprint": self.state_store_fingerprint,
                "quarantine_reason": self.quarantine_reason or "",
                "tombstone_reason": self.tombstone_reason or "",
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }
        )
        return entity

    @classmethod
    def from_table_entity(cls, entity: Mapping[str, object]) -> DurableSessionRecord:
        partition = _read_partition(entity)
        row_key = parse_row_key(_require_str(entity, "RowKey"))
        if not isinstance(row_key, SessionRowKey):
            raise SessionStateContractError("entity RowKey is not a session row")
        _validate_entity_header(entity, partition)
        status_value = _require_str(entity, "status")
        if status_value not in _SESSION_STATUSES:
            raise SessionStateContractError("unsupported session status")
        return cls(
            owner_partition=partition,
            session_id=row_key.session_id,
            sandbox_id=_optional_entity_str(entity, "sandbox_id"),
            generation=_require_int(entity, "generation"),
            digest_kind=_require_str(entity, "digest_kind"),
            digest=_require_str(entity, "digest"),
            protocol=_require_str(entity, "protocol"),
            status=cast(SessionStatus, status_value),
            last_activity_at=_require_datetime(entity, "last_activity_at"),
            expires_at=_require_datetime(entity, "expires_at"),
            idle_policy_armed=_require_bool(entity, "idle_policy_armed"),
            active_run_id=_optional_entity_str(entity, "active_run_id"),
            snapshot_ids=decode_snapshot_ids(_require_str(entity, "snapshot_ids")),
            region=_require_str(entity, "region"),
            state_store_fingerprint=_require_str(entity, "state_store_fingerprint"),
            quarantine_reason=_optional_entity_str(entity, "quarantine_reason"),
            tombstone_reason=_optional_entity_str(entity, "tombstone_reason"),
            created_at=_require_datetime(entity, "created_at"),
            updated_at=_require_datetime(entity, "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class DurableRunRecord:
    """Version-one durable top-level run entity."""

    owner_partition: OwnerPartition
    session_id: str
    run_id: str
    generation: int
    status: DurableRunStatus
    result_available: bool
    status_reason: str | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _validate_opaque_id(self.session_id, "session_id")
        _validate_opaque_id(self.run_id, "run_id")
        validate_generation(self.generation)
        if self.status not in _RUN_STATUSES:
            raise SessionStateContractError("unsupported run status")
        if not isinstance(self.result_available, bool):
            raise SessionStateContractError("result_available must be a boolean")
        if self.result_available and self.status != "succeeded":
            raise SessionStateContractError(
                "result_available may be true only for a succeeded run"
            )
        object.__setattr__(
            self,
            "status_reason",
            _validate_reason(self.status_reason, "status_reason"),
        )
        object.__setattr__(self, "expires_at", _utc_datetime(self.expires_at, "expires_at"))
        object.__setattr__(self, "created_at", _utc_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _utc_datetime(self.updated_at, "updated_at"))
        if self.updated_at < self.created_at:
            raise SessionStateContractError("updated_at must not precede created_at")

    @property
    def row_key(self) -> RunRowKey:
        return RunRowKey(self.session_id, self.run_id)

    def to_table_entity(self) -> TableEntity:
        entity = _base_entity(self.owner_partition, self.row_key)
        entity.update(
            {
                "generation": self.generation,
                "status": self.status,
                "result_available": self.result_available,
                "status_reason": self.status_reason or "",
                "expires_at": self.expires_at,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }
        )
        return entity

    @classmethod
    def from_table_entity(cls, entity: Mapping[str, object]) -> DurableRunRecord:
        partition = _read_partition(entity)
        row_key = parse_row_key(_require_str(entity, "RowKey"))
        if not isinstance(row_key, RunRowKey):
            raise SessionStateContractError("entity RowKey is not a run row")
        _validate_entity_header(entity, partition)
        status_value = _require_str(entity, "status")
        if status_value not in _RUN_STATUSES:
            raise SessionStateContractError("unsupported run status")
        return cls(
            owner_partition=partition,
            session_id=row_key.session_id,
            run_id=row_key.run_id,
            generation=_require_int(entity, "generation"),
            status=cast(DurableRunStatus, status_value),
            result_available=_require_bool(entity, "result_available"),
            status_reason=_optional_entity_str(entity, "status_reason"),
            expires_at=_require_datetime(entity, "expires_at"),
            created_at=_require_datetime(entity, "created_at"),
            updated_at=_require_datetime(entity, "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class DurableIdempotencyRecord:
    """Hashed idempotency locator; the raw caller key is never retained."""

    owner_partition: OwnerPartition
    session_id: str
    idempotency_hash: str
    request_hash: str
    run_id: str
    expires_at: datetime
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_opaque_id(self.session_id, "session_id")
        _validate_sha256(self.idempotency_hash, "idempotency_hash")
        _validate_sha256(self.request_hash, "request_hash")
        _validate_opaque_id(self.run_id, "run_id")
        object.__setattr__(self, "expires_at", _utc_datetime(self.expires_at, "expires_at"))
        object.__setattr__(self, "created_at", _utc_datetime(self.created_at, "created_at"))

    @property
    def row_key(self) -> IdempotencyRowKey:
        return IdempotencyRowKey(self.session_id, self.idempotency_hash)

    def to_table_entity(self) -> TableEntity:
        entity = _base_entity(self.owner_partition, self.row_key)
        entity.update(
            {
                "request_hash": self.request_hash,
                "run_id": self.run_id,
                "expires_at": self.expires_at,
                "created_at": self.created_at,
            }
        )
        return entity

    @classmethod
    def from_table_entity(
        cls,
        entity: Mapping[str, object],
    ) -> DurableIdempotencyRecord:
        partition = _read_partition(entity)
        row_key = parse_row_key(_require_str(entity, "RowKey"))
        if not isinstance(row_key, IdempotencyRowKey):
            raise SessionStateContractError("entity RowKey is not an idempotency row")
        _validate_entity_header(entity, partition)
        return cls(
            owner_partition=partition,
            session_id=row_key.session_id,
            idempotency_hash=row_key.idempotency_hash,
            request_hash=_require_str(entity, "request_hash"),
            run_id=_require_str(entity, "run_id"),
            expires_at=_require_datetime(entity, "expires_at"),
            created_at=_require_datetime(entity, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class AdmissionRecords:
    """Rows that a future P3b admission writes in one owner-partition EGT."""

    session: DurableSessionRecord
    run: DurableRunRecord
    idempotency: DurableIdempotencyRecord | None = None

    def __post_init__(self) -> None:
        partition_key = self.session.owner_partition.partition_key
        if self.run.owner_partition.partition_key != partition_key:
            raise SessionStateContractError(
                "session and run admission rows must share one owner partition"
            )
        if self.run.session_id != self.session.session_id:
            raise SessionStateContractError(
                "session and run admission rows must share session_id"
            )
        if self.run.generation != self.session.generation:
            raise SessionStateContractError(
                "session and run admission rows must share generation"
            )
        if self.session.active_run_id != self.run.run_id:
            raise SessionStateContractError(
                "admitted session active_run_id must identify the admitted run"
            )
        if self.idempotency is None:
            return
        if self.idempotency.owner_partition.partition_key != partition_key:
            raise SessionStateContractError(
                "idempotency admission row must share the owner partition"
            )
        if self.idempotency.session_id != self.session.session_id:
            raise SessionStateContractError(
                "idempotency admission row must share session_id"
            )
        if self.idempotency.run_id != self.run.run_id:
            raise SessionStateContractError(
                "idempotency admission row must identify the admitted run"
            )


def _read_partition(entity: Mapping[str, object]) -> OwnerPartition:
    return OwnerPartition.parse(_require_str(entity, "PartitionKey"))


def _validate_entity_header(
    entity: Mapping[str, object],
    partition: OwnerPartition,
) -> None:
    if _require_int(entity, "schema_version") != ROW_SCHEMA_VERSION:
        raise SessionStateContractError("unsupported durable row schema_version")
    if _require_str(entity, "owner_hash_version") != partition.owner_hash_version:
        raise SessionStateContractError(
            "owner_hash_version does not match the owner partition"
        )


def _require_str(entity: Mapping[str, object], field_name: str) -> str:
    value = entity.get(field_name)
    if not isinstance(value, str):
        raise SessionStateContractError(f"{field_name} must be a string")
    return value


def _optional_entity_str(
    entity: Mapping[str, object],
    field_name: str,
) -> str | None:
    value = _require_str(entity, field_name)
    return value or None


def _require_int(entity: Mapping[str, object], field_name: str) -> int:
    value = entity.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SessionStateContractError(f"{field_name} must be an integer")
    return value


def _require_bool(entity: Mapping[str, object], field_name: str) -> bool:
    value = entity.get(field_name)
    if not isinstance(value, bool):
        raise SessionStateContractError(f"{field_name} must be a boolean")
    return value


def _require_datetime(entity: Mapping[str, object], field_name: str) -> datetime:
    value = entity.get(field_name)
    if not isinstance(value, datetime):
        raise SessionStateContractError(f"{field_name} must be a datetime")
    return value
