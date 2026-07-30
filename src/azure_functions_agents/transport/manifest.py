"""Strict live-manifest binding verification for ACA session routing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from .models import (
    ProvisionedSandboxIdentity,
    SandboxGroupBindingError,
    SandboxProvisioningError,
    normalize_sandbox_group_resource_id,
)

SESSION_MANIFEST_PATH = "/var/lib/azure-functions-agents/session/manifest.json"


class SandboxManifestMismatchError(Exception):
    """A redacted mismatch between authoritative state and a live sandbox manifest."""

    def __init__(self, fields: frozenset[str]) -> None:
        self.fields = fields
        rendered_fields = ", ".join(sorted(fields))
        super().__init__(f"Sandbox manifest binding mismatch: {rendered_fields}.")


@dataclass(frozen=True, slots=True)
class ExpectedSandboxManifestBinding:
    """Opaque, authoritative P3/P4b inputs that P4a must only compare."""

    manifest_version: int
    protocol_version: str
    session_id: str
    owner_hash_version: str
    owner_hash: str
    app_hash: str
    sandbox_group_resource_id: str
    sandbox_id: str
    generation: int
    digest_kind: str
    digest: str

    def __post_init__(self) -> None:
        _validate_integer(self.manifest_version, "manifest_version")
        _validate_integer(self.generation, "generation")
        for field_name in (
            "protocol_version",
            "session_id",
            "owner_hash_version",
            "owner_hash",
            "app_hash",
            "sandbox_id",
            "digest_kind",
            "digest",
        ):
            _validate_string(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "sandbox_group_resource_id",
            normalize_sandbox_group_resource_id(self.sandbox_group_resource_id),
        )


@dataclass(frozen=True, slots=True)
class ObservedSandboxManifestBinding:
    """A strictly parsed, untrusted binding read from the sandbox data plane."""

    manifest_version: int
    protocol_version: str
    session_id: str
    owner_hash_version: str
    owner_hash: str
    app_hash: str
    sandbox_group_resource_id: str
    sandbox_id: str
    generation: int
    digest_kind: str
    digest: str


_REQUIRED_MANIFEST_KEYS = frozenset(
    {
        "manifest_version",
        "protocol_version",
        "session_id",
        "owner_hash_version",
        "owner_hash",
        "app_hash",
        "sandbox_group_resource_id",
        "sandbox_id",
        "generation",
        "digest_kind",
        "digest",
    }
)


def parse_sandbox_manifest_binding(payload: bytes | str) -> ObservedSandboxManifestBinding:
    """Parse P4a's required binding fields without owning the full harness manifest."""

    try:
        raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        decoded = json.loads(raw, object_pairs_hook=_manifest_object)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        _DuplicateManifestKeyError,
    ):
        raise SandboxManifestMismatchError(frozenset({"manifest"})) from None

    if not isinstance(decoded, dict) or not _REQUIRED_MANIFEST_KEYS.issubset(decoded):
        raise SandboxManifestMismatchError(frozenset({"manifest"}))

    try:
        manifest_version = _parsed_integer(decoded, "manifest_version")
        generation = _parsed_integer(decoded, "generation")
        observed = ObservedSandboxManifestBinding(
            manifest_version=manifest_version,
            protocol_version=_parsed_string(decoded, "protocol_version"),
            session_id=_parsed_string(decoded, "session_id"),
            owner_hash_version=_parsed_string(decoded, "owner_hash_version"),
            owner_hash=_parsed_string(decoded, "owner_hash"),
            app_hash=_parsed_string(decoded, "app_hash"),
            sandbox_group_resource_id=normalize_sandbox_group_resource_id(
                _parsed_string(decoded, "sandbox_group_resource_id")
            ),
            sandbox_id=_parsed_string(decoded, "sandbox_id"),
            generation=generation,
            digest_kind=_parsed_string(decoded, "digest_kind"),
            digest=_parsed_string(decoded, "digest"),
        )
    except (TypeError, ValueError, SandboxGroupBindingError, SandboxProvisioningError):
        raise SandboxManifestMismatchError(frozenset({"manifest"})) from None
    return observed


def verify_sandbox_manifest(
    expected: ExpectedSandboxManifestBinding,
    observed: ObservedSandboxManifestBinding,
    live_identity: ProvisionedSandboxIdentity,
) -> None:
    """Verify all routing-critical bindings without disclosing their values."""

    mismatches: set[str] = set()
    for field_name in (
        "manifest_version",
        "protocol_version",
        "session_id",
        "owner_hash_version",
        "owner_hash",
        "app_hash",
        "generation",
        "digest_kind",
        "digest",
    ):
        if getattr(expected, field_name) != getattr(observed, field_name):
            mismatches.add(field_name)

    if (
        expected.sandbox_group_resource_id != observed.sandbox_group_resource_id
        or expected.sandbox_group_resource_id != live_identity.group_resource_id
    ):
        mismatches.add("sandbox_group_resource_id")
    if expected.sandbox_id != observed.sandbox_id or expected.sandbox_id != live_identity.sandbox_id:
        mismatches.add("sandbox_id")

    if mismatches:
        raise SandboxManifestMismatchError(frozenset(mismatches))


def _parsed_integer(payload: dict[str, Any], field_name: str) -> int:
    value = payload[field_name]
    _validate_integer(value, field_name)
    return cast(int, value)


def _parsed_string(payload: dict[str, Any], field_name: str) -> str:
    value = payload[field_name]
    _validate_string(value, field_name)
    return cast(str, value)


def _validate_integer(value: object, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


def _validate_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string.")


class _DuplicateManifestKeyError(ValueError):
    """Raised internally when untrusted manifest JSON repeats a key."""


def _manifest_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateManifestKeyError
        result[key] = value
    return result
