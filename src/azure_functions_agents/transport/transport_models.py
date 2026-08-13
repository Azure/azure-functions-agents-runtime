"""Runtime-owned models for controller-to-sandbox transport."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, get_args

_MAX_PROVIDER_LABEL_VALUE_LENGTH = 63
SANDBOX_GROUP_AUTHORIZATION_ERROR_CODE = "sandbox_group_authorization_failed"
SANDBOX_GROUP_AUTHORIZATION_MESSAGE = (
    "Sandbox Group data-plane authorization failed. Grant the controller identity "
    "'Container Apps SandboxGroup Data Owner' on the configured Sandbox Group."
)


class SandboxTransportError(Exception):
    """Base error for the runtime-owned sandbox transport boundary."""


class SandboxProvisioningError(SandboxTransportError):
    """Raised when a sandbox provisioning request is unsafe or malformed."""


class SandboxCapacityError(SandboxProvisioningError):
    """Raised when the Sandbox Group cannot currently admit another sandbox."""


class SandboxGroupAuthorizationError(SandboxProvisioningError):
    """Raised when the controller lacks Sandbox Group data-plane authorization."""

    def __init__(self) -> None:
        super().__init__(SANDBOX_GROUP_AUTHORIZATION_MESSAGE)


class SandboxCreateOutcomeUnknownError(SandboxProvisioningError):
    """A create was accepted but its durable outcome cannot yet be reconciled."""


class SandboxGroupBindingError(SandboxTransportError):
    """Raised when a configured, persisted, ARM, or live group binding disagrees."""


class AcaSandboxDependencyError(SandboxTransportError):
    """Raised when the optional ACA Sandbox SDK extra is unavailable."""


class SandboxFileNotFoundError(SandboxTransportError):
    """A ``SandboxFileTransport`` read/stat/delete found no entry at the given path."""


class SandboxFileOperationError(SandboxTransportError):
    """A ``SandboxFileTransport`` operation failed for an operational (non-missing) reason.

    ``status_code`` is the provider HTTP status when known (``None`` for a
    network-level failure with no response), so callers can still make a
    narrow, typed retry decision without catching a provider SDK exception.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class SandboxFileEntry:
    """A directory entry projected out of a provider file response."""

    name: str
    path: str
    size: int | None
    is_directory: bool
    modified_at: str | None = None
    mode: str | int | None = None


@dataclass(frozen=True, slots=True)
class SandboxFileStat:
    """A file or directory metadata projection."""

    path: str
    size: int | None
    is_directory: bool
    modified_at: str | None = None
    mode: str | int | None = None


@dataclass(frozen=True, slots=True)
class SandboxExecResult:
    """A process-execution result projected out of a provider response."""

    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class SandboxLifecyclePolicy:
    """Complete per-sandbox lifecycle policy projected without provider SDK types."""

    auto_suspend_seconds: int | None
    auto_suspend_mode: SandboxAutoSuspendMode = "Disk"
    auto_delete_seconds: int = 1

    @classmethod
    def create(
        cls,
        *,
        auto_suspend_seconds: int | None,
        auto_delete_seconds: int,
        auto_suspend_mode: SandboxAutoSuspendMode = "Disk",
    ) -> SandboxLifecyclePolicy:
        if auto_suspend_seconds is not None and auto_suspend_seconds < 60:
            raise SandboxProvisioningError(
                "Sandbox auto_suspend_seconds must be at least 60 when enabled."
            )
        if auto_delete_seconds <= 0:
            raise SandboxProvisioningError("Sandbox auto_delete_seconds must be positive.")
        if auto_suspend_mode not in {"Memory", "Disk"}:
            raise SandboxProvisioningError("Sandbox auto_suspend_mode must be Memory or Disk.")
        return cls(
            auto_suspend_seconds=auto_suspend_seconds,
            auto_suspend_mode=auto_suspend_mode,
            auto_delete_seconds=auto_delete_seconds,
        )


@dataclass(frozen=True, slots=True)
class SandboxSummary:
    """A label-filterable platform inventory projection for one sandbox."""

    sandbox_id: str
    labels: Mapping[str, str]
    state: str | None = None
    created_at: str | None = None
    modified_at: str | None = None

    @classmethod
    def create(
        cls,
        *,
        sandbox_id: str,
        labels: Mapping[str, str],
        state: str | None = None,
        created_at: str | None = None,
        modified_at: str | None = None,
    ) -> SandboxSummary:
        return cls(
            sandbox_id=_require_nonempty_string(sandbox_id, "sandbox_id"),
            labels=MappingProxyType(_validate_labels(labels)),
            state=_optional_bounded_text(state, "state", max_bytes=32),
            created_at=_optional_timestamp(created_at, "created_at"),
            modified_at=_optional_timestamp(modified_at, "modified_at"),
        )


@dataclass(frozen=True, slots=True)
class SandboxSnapshot:
    """A provider-neutral projection for a retained sandbox snapshot."""

    snapshot_id: str
    sandbox_id: str | None
    created_at: str | None = None

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        sandbox_id: str | None,
        created_at: str | None = None,
    ) -> SandboxSnapshot:
        return cls(
            snapshot_id=_require_nonempty_string(snapshot_id, "snapshot_id"),
            sandbox_id=(
                None
                if sandbox_id is None
                else _require_nonempty_string(sandbox_id, "sandbox_id")
            ),
            created_at=_optional_timestamp(created_at, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class SandboxGroupResourceId:
    """The normalized, controller-configured ARM identity of a Sandbox Group."""

    resource_id: str
    subscription_id: str
    resource_group: str
    group_name: str


@dataclass(frozen=True, slots=True)
class SandboxGroupIdentity:
    """A resolved customer-owned Sandbox Group and its immutable placement."""

    resource_id: str
    subscription_id: str
    resource_group: str
    group_name: str
    region: str


@dataclass(frozen=True, slots=True)
class SandboxGroupBinding:
    """The persisted group and region binding for one session.

    Construct with :meth:`create` so values are normalized once.
    """

    resource_id: str
    region: str

    @classmethod
    def create(cls, resource_id: str, region: str) -> SandboxGroupBinding:
        return cls(
            resource_id=normalize_sandbox_group_resource_id(resource_id),
            region=_normalize_region(region),
        )


@dataclass(frozen=True, slots=True)
class PersistedSandboxBinding:
    """The persisted location of a session sandbox after a Functions recycle.

    Construct with :meth:`create` so values are validated once.
    """

    sandbox_id: str
    group: SandboxGroupBinding

    @classmethod
    def create(cls, sandbox_id: str, group: SandboxGroupBinding) -> PersistedSandboxBinding:
        return cls(sandbox_id=_require_nonempty_string(sandbox_id, "sandbox_id"), group=group)


@dataclass(frozen=True, slots=True)
class ProvisionedSandboxIdentity:
    """Live sandbox identity returned by a provider handle.

    Construct with :meth:`create` so values are validated and normalized once.
    """

    sandbox_id: str
    group_resource_id: str
    region: str

    @classmethod
    def create(
        cls, sandbox_id: str, group_resource_id: str, region: str
    ) -> ProvisionedSandboxIdentity:
        return cls(
            sandbox_id=_require_nonempty_string(sandbox_id, "sandbox_id"),
            group_resource_id=normalize_sandbox_group_resource_id(group_resource_id),
            region=_normalize_region(region),
        )


@dataclass(frozen=True, slots=True)
class DiskSource:
    """Use one explicit public disk image name.

    Construct with :meth:`create` so values are validated once.
    """

    disk: str

    @classmethod
    def create(cls, disk: str) -> DiskSource:
        return cls(disk=_require_nonempty_string(disk, "disk"))


@dataclass(frozen=True, slots=True)
class DiskIdSource:
    """Use one explicit customer-managed disk image identifier.

    Construct with :meth:`create` so values are validated once.
    """

    disk_id: str

    @classmethod
    def create(cls, disk_id: str) -> DiskIdSource:
        return cls(disk_id=_require_nonempty_string(disk_id, "disk_id"))


@dataclass(frozen=True, slots=True)
class PresetSource:
    """Use one explicit provider preset.

    Construct with :meth:`create` so values are validated once.
    """

    preset: str

    @classmethod
    def create(cls, preset: str) -> PresetSource:
        return cls(preset=_require_nonempty_string(preset, "preset"))


type SandboxCreateSource = DiskSource | DiskIdSource | PresetSource
type SandboxEgressInspection = Literal["Full"]
type SandboxAutoSuspendMode = Literal["Memory", "Disk"]
type SandboxEgressHostAction = Literal["Allow", "Deny"]
type SandboxEgressRuleActionType = Literal["Allow", "Deny", "Transform", "Rewrite"]
type SandboxEgressHeaderOperation = Literal["Set", "Insert", "Remove"]


@dataclass(frozen=True, slots=True)
class SandboxProvisioningLabels:
    """The only controller labels that may reach a session sandbox.

    Values are opaque inputs from the controller. This model deliberately does
    not derive or canonicalize the owner or app fingerprints. Construct with
    :meth:`create` so values are validated once.
    """

    owner_hash_version: str
    owner_kind: str
    owner_hash: str
    app_hash: str
    session_id: str
    operation_label: str | None = None

    @classmethod
    def create(
        cls,
        owner_hash_version: str,
        owner_hash: str,
        app_hash: str,
        session_id: str,
        *,
        owner_kind: str = "function_app",
        operation_label: str | None = None,
    ) -> SandboxProvisioningLabels:
        return cls(
            owner_hash_version=_require_provider_label_value(
                owner_hash_version, "owner_hash_version"
            ),
            owner_kind=_require_provider_label_value(owner_kind, "owner_kind"),
            owner_hash=_require_provider_label_value(owner_hash, "owner_hash"),
            app_hash=_require_provider_label_value(app_hash, "app_hash"),
            session_id=_require_provider_label_value(session_id, "session_id"),
            operation_label=(
                None
                if operation_label is None
                else _require_provider_label_value(operation_label, "operation_label")
            ),
        )

    def to_provider_labels(self) -> dict[str, str]:
        """Return only safe, versioned fingerprint labels for provisioning."""

        labels = {
            "owner_hash_version": self.owner_hash_version,
            "owner_kind": self.owner_kind,
            "owner_hash": self.owner_hash,
            "app_hash": self.app_hash,
            "session_id": self.session_id,
        }
        if self.operation_label is not None:
            labels["operation_label"] = self.operation_label
        return labels


@dataclass(frozen=True, slots=True)
class SandboxEgressHostRule:
    """One host-only Allow or Deny egress rule."""

    host: str
    action: SandboxEgressHostAction

    @classmethod
    def create(cls, *, host: str, action: SandboxEgressHostAction) -> SandboxEgressHostRule:
        if action not in {"Allow", "Deny"}:
            raise SandboxProvisioningError("Sandbox egress host rules support only Allow or Deny.")
        return cls(host=_normalize_egress_host(host), action=action)


@dataclass(frozen=True, slots=True)
class SandboxEgressSecretRef:
    """A customer-provisioned secret reference for an egress header."""

    secret_id: str = field(repr=False)
    secret_key: str = field(repr=False)
    format: str = field(repr=False)

    @classmethod
    def create(
        cls, *, secret_id: str, secret_key: str, format: str = "{value}"
    ) -> SandboxEgressSecretRef:
        _require_nonempty_string(secret_id, "egress secret_id")
        _require_nonempty_string(secret_key, "egress secret_key")
        if not isinstance(format, str) or not format or "{value}" not in format:
            raise SandboxProvisioningError(
                "Sandbox egress secret format must be non-empty and contain {value}."
            )
        return cls(secret_id=secret_id, secret_key=secret_key, format=format)


@dataclass(frozen=True, slots=True)
class SandboxEgressHeader:
    """One static or secret-backed header transformation."""

    operation: SandboxEgressHeaderOperation
    name: str
    value: str | None = field(default=None, repr=False)
    secret_ref: SandboxEgressSecretRef | None = field(default=None, repr=False)

    @classmethod
    def create(
        cls,
        *,
        operation: SandboxEgressHeaderOperation,
        name: str,
        value: str | None = None,
        secret_ref: SandboxEgressSecretRef | None = None,
    ) -> SandboxEgressHeader:
        if operation not in get_args(SandboxEgressHeaderOperation.__value__):
            raise SandboxProvisioningError("Sandbox egress header operation is invalid.")
        _require_nonempty_string(name, "egress header name")
        if operation == "Remove":
            if value is not None or secret_ref is not None:
                raise SandboxProvisioningError(
                    "Sandbox Remove header operations must not include a value."
                )
            return cls(operation=operation, name=name)
        if (value is None) == (secret_ref is None):
            raise SandboxProvisioningError(
                "Sandbox egress header operations require exactly one value source."
            )
        if value is not None and not isinstance(value, str):
            raise SandboxProvisioningError("Sandbox egress header values must be strings.")
        return cls(operation=operation, name=name, value=value, secret_ref=secret_ref)


@dataclass(frozen=True, slots=True)
class SandboxEgressRuleMatch:
    """The provider-neutral HTTP request shape matched by an ordered rule."""

    host: str
    path: str | None = None
    methods: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        host: str,
        path: str | None = None,
        methods: Iterable[str] = (),
    ) -> SandboxEgressRuleMatch:
        return cls(
            host=_normalize_egress_host(host),
            path=_normalize_egress_path(path),
            methods=_normalize_egress_methods(methods),
        )


@dataclass(frozen=True, slots=True)
class SandboxEgressRuleAction:
    """The operation applied after an ordered rule matches."""

    type: SandboxEgressRuleActionType
    host: str | None = None
    path: str | None = None
    scheme: str | None = None
    headers: tuple[SandboxEgressHeader, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        type: SandboxEgressRuleActionType,
        host: str | None = None,
        path: str | None = None,
        scheme: str | None = None,
        headers: Iterable[SandboxEgressHeader] = (),
    ) -> SandboxEgressRuleAction:
        if type not in get_args(SandboxEgressRuleActionType.__value__):
            raise SandboxProvisioningError("Sandbox egress rule action is invalid.")
        normalized_headers = tuple(headers)
        if not all(isinstance(header, SandboxEgressHeader) for header in normalized_headers):
            raise SandboxProvisioningError("Sandbox egress rule headers must be typed values.")
        normalized_host = _normalize_egress_host(host) if host is not None else None
        normalized_path = _normalize_egress_path(path)
        normalized_scheme = _normalize_egress_scheme(scheme)
        changes_request = any(
            value is not None
            for value in (normalized_host, normalized_path, normalized_scheme)
        ) or bool(normalized_headers)
        if type in {"Allow", "Deny"} and changes_request:
            raise SandboxProvisioningError(
                "Sandbox Allow and Deny rules must not include transforms."
            )
        if type in {"Transform", "Rewrite"} and not changes_request:
            raise SandboxProvisioningError(
                "Sandbox Transform and Rewrite rules must change a request."
            )
        return cls(
            type=type,
            host=normalized_host,
            path=normalized_path,
            scheme=normalized_scheme,
            headers=normalized_headers,
        )


@dataclass(frozen=True, slots=True)
class SandboxEgressRule:
    """One ordered full-inspection egress rule."""

    name: str
    match: SandboxEgressRuleMatch
    action: SandboxEgressRuleAction

    @classmethod
    def create(
        cls,
        *,
        name: str,
        match: SandboxEgressRuleMatch,
        action: SandboxEgressRuleAction,
    ) -> SandboxEgressRule:
        _require_nonempty_string(name, "egress rule name")
        if not isinstance(match, SandboxEgressRuleMatch):
            raise SandboxProvisioningError("Sandbox egress rule match must be typed.")
        if not isinstance(action, SandboxEgressRuleAction):
            raise SandboxProvisioningError("Sandbox egress rule action must be typed.")
        return cls(name=name, match=match, action=action)


@dataclass(frozen=True, slots=True)
class SandboxEgressPolicy:
    """A safe egress-policy request accepted by the sandbox adapter."""

    default_action: Literal["Deny"] = "Deny"
    traffic_inspection: SandboxEgressInspection = "Full"
    host_rules: tuple[SandboxEgressHostRule, ...] = ()
    rules: tuple[SandboxEgressRule, ...] = ()

    @classmethod
    def create(
        cls,
        default_action: Literal["Deny"] = "Deny",
        traffic_inspection: SandboxEgressInspection = "Full",
        host_rules: Iterable[SandboxEgressHostRule] = (),
        rules: Iterable[SandboxEgressRule] = (),
    ) -> SandboxEgressPolicy:
        if default_action != "Deny":
            raise SandboxProvisioningError("Sandbox egress default_action must be Deny.")
        if traffic_inspection != "Full":
            raise SandboxProvisioningError("Sandbox egress traffic_inspection must be Full.")
        normalized_host_rules = tuple(host_rules)
        normalized_rules = tuple(rules)
        if not all(isinstance(rule, SandboxEgressHostRule) for rule in normalized_host_rules):
            raise SandboxProvisioningError("Sandbox egress host rules must be typed values.")
        if not all(isinstance(rule, SandboxEgressRule) for rule in normalized_rules):
            raise SandboxProvisioningError("Sandbox egress rules must be typed values.")
        _validate_host_rule_order(normalized_host_rules)
        return cls(
            default_action=default_action,
            traffic_inspection=traffic_inspection,
            host_rules=normalized_host_rules,
            rules=normalized_rules,
        )


@dataclass(frozen=True, slots=True)
class SandboxCreateRequest:
    """A fail-closed request to create one individual session sandbox.

    Construct with :meth:`create` so values are validated once.
    """

    source: SandboxCreateSource
    labels: SandboxProvisioningLabels
    remaining_setup_budget_seconds: float
    cpu: str = "1000m"
    memory: str = "2048Mi"
    auto_suspend_seconds: int = 300
    auto_suspend_mode: SandboxAutoSuspendMode = "Disk"
    environment: Mapping[str, str] = field(default_factory=dict)
    entrypoint: tuple[str, ...] = ()
    cmd: tuple[str, ...] = ()
    egress_policy: SandboxEgressPolicy = field(default_factory=SandboxEgressPolicy.create)
    ports: tuple[object, ...] = ()
    skip_egress_proxy: bool = False
    polling_interval_seconds: int = 3
    reconcile_only: bool = False

    @classmethod
    def create(
        cls,
        source: SandboxCreateSource,
        labels: SandboxProvisioningLabels,
        remaining_setup_budget_seconds: float,
        cpu: str = "1000m",
        memory: str = "2048Mi",
        auto_suspend_seconds: int = 300,
        auto_suspend_mode: SandboxAutoSuspendMode = "Disk",
        environment: Mapping[str, str] | None = None,
        entrypoint: tuple[str, ...] = (),
        cmd: tuple[str, ...] = (),
        egress_policy: SandboxEgressPolicy | None = None,
        ports: tuple[object, ...] = (),
        skip_egress_proxy: bool = False,
        polling_interval_seconds: int = 3,
        reconcile_only: bool = False,
    ) -> SandboxCreateRequest:
        if not isinstance(source, (DiskSource, DiskIdSource, PresetSource)):
            raise SandboxProvisioningError(
                "Sandbox source must be exactly one of disk, disk_id, or preset."
            )
        _require_nonempty_string(cpu, "cpu")
        _require_nonempty_string(memory, "memory")
        if auto_suspend_seconds <= 0:
            raise SandboxProvisioningError("Sandbox auto_suspend_seconds must be positive.")
        if auto_suspend_mode not in {"Memory", "Disk"}:
            raise SandboxProvisioningError("Sandbox auto_suspend_mode must be Memory or Disk.")
        if ports:
            raise SandboxProvisioningError("Session sandboxes must not expose inbound ports.")
        if skip_egress_proxy is not False:
            raise SandboxProvisioningError("Session sandboxes must not bypass the egress proxy.")
        if (
            not math.isfinite(remaining_setup_budget_seconds)
            or remaining_setup_budget_seconds <= 0
        ):
            raise SandboxProvisioningError(
                "Sandbox remaining_setup_budget_seconds must be positive and finite."
            )
        if polling_interval_seconds <= 0:
            raise SandboxProvisioningError("Sandbox polling_interval_seconds must be positive.")
        if not isinstance(reconcile_only, bool):
            raise SandboxProvisioningError("Sandbox reconcile_only must be a boolean.")
        for command_part in (*entrypoint, *cmd):
            _require_nonempty_string(command_part, "entrypoint or cmd item")

        return cls(
            source=source,
            labels=labels,
            remaining_setup_budget_seconds=remaining_setup_budget_seconds,
            cpu=cpu,
            memory=memory,
            auto_suspend_seconds=auto_suspend_seconds,
            auto_suspend_mode=auto_suspend_mode,
            environment=_validate_create_environment(environment or {}),
            entrypoint=entrypoint,
            cmd=cmd,
            egress_policy=egress_policy if egress_policy is not None else SandboxEgressPolicy.create(),
            ports=ports,
            skip_egress_proxy=skip_egress_proxy,
            polling_interval_seconds=polling_interval_seconds,
            reconcile_only=reconcile_only,
        )

    @property
    def provisioning_timeout_seconds(self) -> float:
        """Return the explicit bounded SDK polling timeout for this request."""

        return self.remaining_setup_budget_seconds


def parse_sandbox_group_resource_id(value: str) -> SandboxGroupResourceId:
    """Strictly parse the only ARM resource identity this layer may target."""

    if not isinstance(value, str):
        raise SandboxGroupBindingError("Sandbox Group resource ID must be a string.")
    segments = [segment for segment in value.strip().split("/") if segment]
    if len(segments) != 8:
        raise SandboxGroupBindingError("Sandbox Group resource ID has an invalid shape.")

    expected_segments = (
        "subscriptions",
        "resourcegroups",
        "providers",
        "microsoft.app",
        "sandboxgroups",
    )
    actual_segments = (
        segments[0].casefold(),
        segments[2].casefold(),
        segments[4].casefold(),
        segments[5].casefold(),
        segments[6].casefold(),
    )
    if actual_segments != expected_segments:
        raise SandboxGroupBindingError("Sandbox Group resource ID has an invalid provider type.")

    subscription_id, resource_group, group_name = segments[1], segments[3], segments[7]
    _require_nonempty_string(subscription_id, "subscription_id")
    _require_nonempty_string(resource_group, "resource_group")
    _require_nonempty_string(group_name, "sandbox_group")
    resource_id = (
        f"/subscriptions/{subscription_id.casefold()}"
        f"/resourceGroups/{resource_group.casefold()}"
        f"/providers/Microsoft.App/sandboxGroups/{group_name.casefold()}"
    )
    return SandboxGroupResourceId(
        resource_id=resource_id,
        subscription_id=subscription_id,
        resource_group=resource_group,
        group_name=group_name,
    )


def normalize_sandbox_group_resource_id(value: str) -> str:
    """Return the canonical comparison representation for a Sandbox Group ARM ID."""

    return parse_sandbox_group_resource_id(value).resource_id


def source_to_provider_kwargs(source: SandboxCreateSource) -> dict[str, str]:
    """Project exactly one runtime-owned source into provider-neutral keyword data."""

    if isinstance(source, DiskSource):
        return {"disk": source.disk}
    if isinstance(source, DiskIdSource):
        return {"disk_id": source.disk_id}
    if isinstance(source, PresetSource):
        return {"preset": source.preset}
    raise SandboxProvisioningError(
        "Sandbox source must be exactly one of disk, disk_id, or preset."
    )


def _normalize_region(value: str) -> str:
    _require_nonempty_string(value, "region")
    return value.strip().casefold()


def _require_nonempty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SandboxProvisioningError(f"Sandbox {field_name} must be a non-empty string.")
    return value


def _require_provider_label_value(value: object, field_name: str) -> str:
    label_value = _require_nonempty_string(value, field_name)
    if len(label_value) > _MAX_PROVIDER_LABEL_VALUE_LENGTH:
        raise SandboxProvisioningError(
            f"Sandbox {field_name} must not exceed "
            f"{_MAX_PROVIDER_LABEL_VALUE_LENGTH} characters."
        )
    return label_value


def _validate_create_environment(environment: Mapping[str, str]) -> Mapping[str, str]:
    """Validate string environment values and return an immutable copy."""

    validated: dict[str, str] = {}
    for key, value in environment.items():
        _require_nonempty_string(key, "environment key")
        if not isinstance(value, str):
            raise SandboxProvisioningError("Sandbox environment values must be strings.")
        validated[key] = value
    return MappingProxyType(validated)


def _normalize_egress_host(value: object) -> str:
    host = _require_nonempty_string(value, "egress host").strip().casefold()
    if (
        host.startswith(".")
        or "/" in host
        or "://" in host
        or any(character.isspace() for character in host)
    ):
        raise SandboxProvisioningError("Sandbox egress host has an invalid shape.")
    return host


def _normalize_egress_path(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise SandboxProvisioningError("Sandbox egress path must start with /.")
    return value


def _normalize_egress_methods(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise SandboxProvisioningError("Sandbox egress methods must be non-empty strings.")
        method = value.strip().upper()
        if method not in normalized:
            normalized.append(method)
    return tuple(normalized)


def _normalize_egress_scheme(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.casefold() not in {"http", "https"}:
        raise SandboxProvisioningError("Sandbox egress scheme must be http or https.")
    return value.casefold()


def _validate_host_rule_order(rules: tuple[SandboxEgressHostRule, ...]) -> None:
    for index, earlier in enumerate(rules):
        if earlier.action != "Allow":
            continue
        for later in rules[index + 1 :]:
            if later.action == "Deny" and _host_rule_covers(earlier.host, later.host):
                raise SandboxProvisioningError(
                    "A broad sandbox egress Allow host rule must not shadow a Deny rule."
                )


def _host_rule_covers(broader: str, narrower: str) -> bool:
    return (
        broader == "*"
        or broader == narrower
        or (broader.startswith("*.") and narrower.endswith(broader[1:]))
    )


def _validate_labels(labels: Mapping[str, str]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for key, value in labels.items():
        validated[_require_nonempty_string(key, "label key")] = _require_nonempty_string(
            value,
            "label value",
        )
    return validated


def _optional_timestamp(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_nonempty_string(value, field_name)


def _optional_bounded_text(value: str | None, field_name: str, *, max_bytes: int) -> str | None:
    if value is None:
        return None
    normalized = _require_nonempty_string(value, field_name)
    if len(normalized.encode("utf-8")) > max_bytes:
        raise SandboxProvisioningError(f"{field_name} exceeds {max_bytes} UTF-8 bytes.")
    return normalized
