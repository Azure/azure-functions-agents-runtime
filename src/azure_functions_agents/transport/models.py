"""Runtime-owned models for controller-to-sandbox transport."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal


class SandboxTransportError(Exception):
    """Base error for the runtime-owned sandbox transport boundary."""


class SandboxProvisioningError(SandboxTransportError):
    """Raised when a sandbox provisioning request is unsafe or malformed."""


class SandboxGroupBindingError(SandboxTransportError):
    """Raised when a configured, persisted, ARM, or live group binding disagrees."""


class AcaSandboxDependencyError(SandboxTransportError):
    """Raised when the optional ACA Sandbox SDK extra is unavailable."""


@dataclass(frozen=True, slots=True)
class SandboxFileEntry:
    """A directory entry projected out of a provider file response."""

    name: str
    path: str
    size: int | None
    is_directory: bool
    modified_at: str | None = None
    mode: str | None = None


@dataclass(frozen=True, slots=True)
class SandboxFileStat:
    """A file or directory metadata projection."""

    path: str
    size: int | None
    is_directory: bool
    modified_at: str | None = None
    mode: str | None = None


@dataclass(frozen=True, slots=True)
class SandboxExecResult:
    """A process-execution result projected out of a provider response."""

    exit_code: int
    stdout: str
    stderr: str


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
    """The persisted group and region binding for one session."""

    resource_id: str
    region: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_id", normalize_sandbox_group_resource_id(self.resource_id))
        object.__setattr__(self, "region", _normalize_region(self.region))


@dataclass(frozen=True, slots=True)
class PersistedSandboxBinding:
    """The persisted location of a session sandbox after a Functions recycle."""

    sandbox_id: str
    group: SandboxGroupBinding

    def __post_init__(self) -> None:
        _require_nonempty_string(self.sandbox_id, "sandbox_id")


@dataclass(frozen=True, slots=True)
class ProvisionedSandboxIdentity:
    """Live sandbox identity returned by a provider handle."""

    sandbox_id: str
    group_resource_id: str
    region: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.sandbox_id, "sandbox_id")
        object.__setattr__(
            self,
            "group_resource_id",
            normalize_sandbox_group_resource_id(self.group_resource_id),
        )
        object.__setattr__(self, "region", _normalize_region(self.region))


@dataclass(frozen=True, slots=True)
class DiskSource:
    """Use one explicit public disk image name."""

    disk: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.disk, "disk")


@dataclass(frozen=True, slots=True)
class DiskIdSource:
    """Use one explicit customer-managed disk image identifier."""

    disk_id: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.disk_id, "disk_id")


@dataclass(frozen=True, slots=True)
class PresetSource:
    """Use one explicit provider preset."""

    preset: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.preset, "preset")


type SandboxCreateSource = DiskSource | DiskIdSource | PresetSource
type SandboxEgressInspection = Literal["Full", "Partial"]
type SandboxAutoSuspendMode = Literal["Memory", "Disk"]


@dataclass(frozen=True, slots=True)
class SandboxProvisioningLabels:
    """The only controller labels that may reach a session sandbox.

    Values are opaque inputs from the controller. This model deliberately does
    not derive or canonicalize the owner or app fingerprints.
    """

    owner_hash_version: str
    owner_hash: str
    app_hash: str
    session_id: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.owner_hash_version, "owner_hash_version")
        _require_nonempty_string(self.owner_hash, "owner_hash")
        _require_nonempty_string(self.app_hash, "app_hash")
        _require_nonempty_string(self.session_id, "session_id")

    def to_provider_labels(self) -> dict[str, str]:
        """Return only safe, versioned fingerprint labels for provisioning."""

        return {
            "owner_hash_version": self.owner_hash_version,
            "owner_hash": self.owner_hash,
            "app_hash": self.app_hash,
            "session_id": self.session_id,
        }


@dataclass(frozen=True, slots=True)
class SandboxEgressPolicy:
    """A safe egress-policy request accepted by the P4a adapter."""

    default_action: Literal["Deny"] = "Deny"
    traffic_inspection: SandboxEgressInspection = "Full"

    def __post_init__(self) -> None:
        if self.default_action != "Deny":
            raise SandboxProvisioningError("Sandbox egress default_action must be Deny.")
        if self.traffic_inspection not in {"Full", "Partial"}:
            raise SandboxProvisioningError(
                "Sandbox egress traffic_inspection must be Full or Partial."
            )


@dataclass(frozen=True, slots=True)
class SandboxCreateRequest:
    """A fail-closed request to create one individual session sandbox."""

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
    egress_policy: SandboxEgressPolicy = field(default_factory=SandboxEgressPolicy)
    ports: tuple[object, ...] = ()
    skip_egress_proxy: bool = False
    polling_interval_seconds: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.source, (DiskSource, DiskIdSource, PresetSource)):
            raise SandboxProvisioningError(
                "Sandbox source must be exactly one of disk, disk_id, or preset."
            )
        _require_nonempty_string(self.cpu, "cpu")
        _require_nonempty_string(self.memory, "memory")
        if self.auto_suspend_seconds <= 0:
            raise SandboxProvisioningError("Sandbox auto_suspend_seconds must be positive.")
        if self.auto_suspend_mode not in {"Memory", "Disk"}:
            raise SandboxProvisioningError(
                "Sandbox auto_suspend_mode must be Memory or Disk."
            )
        if self.ports:
            raise SandboxProvisioningError("Session sandboxes must not expose inbound ports.")
        if self.skip_egress_proxy is not False:
            raise SandboxProvisioningError("Session sandboxes must not bypass the egress proxy.")
        if (
            not math.isfinite(self.remaining_setup_budget_seconds)
            or self.remaining_setup_budget_seconds <= 0
        ):
            raise SandboxProvisioningError(
                "Sandbox remaining_setup_budget_seconds must be positive and finite."
            )
        if self.polling_interval_seconds <= 0:
            raise SandboxProvisioningError("Sandbox polling_interval_seconds must be positive.")

        environment = dict(self.environment)
        for key, value in environment.items():
            _require_nonempty_string(key, "environment key")
            if not isinstance(value, str):
                raise SandboxProvisioningError("Sandbox environment values must be strings.")
            if _is_controller_credential_key(key):
                raise SandboxProvisioningError(
                    "Sandbox environment must not contain Azure or state credentials."
                )
        object.__setattr__(self, "environment", MappingProxyType(environment))

        for command_part in (*self.entrypoint, *self.cmd):
            _require_nonempty_string(command_part, "entrypoint or cmd item")

    @property
    def provisioning_timeout_seconds(self) -> float:
        """Return the explicit bounded SDK polling timeout for this request."""

        return min(self.remaining_setup_budget_seconds, 30.0)


def parse_sandbox_group_resource_id(value: str) -> SandboxGroupResourceId:
    """Strictly parse the only ARM resource identity P4a is allowed to target."""

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


def _require_nonempty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SandboxProvisioningError(f"Sandbox {field_name} must be a non-empty string.")


def _is_controller_credential_key(key: str) -> bool:
    normalized = key.upper()
    return (
        normalized.startswith(("AZURE_", "AZUREWEBJOBS", "IDENTITY_", "MSI_"))
        or "CONNECTION_STRING" in normalized
        or normalized.endswith(("_ACCOUNT_KEY", "_SAS_TOKEN"))
    )
