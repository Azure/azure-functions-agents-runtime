"""Strict live-manifest binding verification for ACA session routing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .transport_models import (
    ProvisionedSandboxIdentity,
    SandboxGroupBindingError,
    SandboxProvisioningError,
    normalize_sandbox_group_resource_id,
)

type _ManifestText = Annotated[str, StringConstraints(min_length=1)]
type _ManifestCount = Annotated[int, Field(ge=0)]

# Wire contract: the harness writes its manifest at exactly this path inside
# every sandbox, so both sides must agree on this literal string.
SESSION_MANIFEST_PATH = "/var/lib/azurefunctions-agents-runtime/session/manifest.json"


class SandboxManifestMismatchError(Exception):
    """A redacted mismatch between authoritative state and a live sandbox manifest."""

    def __init__(self, fields: frozenset[str]) -> None:
        self.fields = fields
        rendered_fields = ", ".join(sorted(fields))
        super().__init__(f"Sandbox manifest binding mismatch: {rendered_fields}.")


@dataclass(frozen=True, slots=True)
class ExpectedSandboxManifestBinding:
    """Opaque, authoritative controller inputs that this layer only compares.

    Already-typed controller state; ``create`` normalizes only the Sandbox
    Group resource ID so it compares correctly against the live manifest.
    """

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

    @classmethod
    def create(
        cls,
        *,
        manifest_version: int,
        protocol_version: str,
        session_id: str,
        owner_hash_version: str,
        owner_hash: str,
        app_hash: str,
        sandbox_group_resource_id: str,
        sandbox_id: str,
        generation: int,
        digest_kind: str,
        digest: str,
    ) -> ExpectedSandboxManifestBinding:
        return cls(
            manifest_version=manifest_version,
            protocol_version=protocol_version,
            session_id=session_id,
            owner_hash_version=owner_hash_version,
            owner_hash=owner_hash,
            app_hash=app_hash,
            sandbox_group_resource_id=normalize_sandbox_group_resource_id(
                sandbox_group_resource_id
            ),
            sandbox_id=sandbox_id,
            generation=generation,
            digest_kind=digest_kind,
            digest=digest,
        )


class ObservedSandboxManifestBinding(BaseModel):
    """A strictly parsed, untrusted binding read from the sandbox data plane.

    ``strict`` blocks type coercion so a manifest cannot pass a string where an
    int is required; ``extra="ignore"`` lets the harness own additional manifest
    sections without this layer claiming their schema.
    """

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    manifest_version: _ManifestCount
    protocol_version: _ManifestText
    session_id: _ManifestText
    owner_hash_version: _ManifestText
    owner_hash: _ManifestText
    app_hash: _ManifestText
    sandbox_group_resource_id: _ManifestText
    sandbox_id: _ManifestText
    generation: _ManifestCount
    digest_kind: _ManifestText
    digest: _ManifestText


def parse_sandbox_manifest_binding(payload: bytes | str) -> ObservedSandboxManifestBinding:
    """Parse only the binding fields this layer requires, not the whole manifest.

    ``payload`` is untrusted data read from inside the sandbox — the
    forgery/repointing detection surface. Duplicate keys are rejected before
    validation because both ``json.loads`` and Pydantic's own JSON parser
    silently keep the last value, which lets one document mean two things.
    Every failure collapses to one redacted :class:`SandboxManifestMismatchError`.
    """

    try:
        raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        decoded = json.loads(raw, object_pairs_hook=_manifest_object)
        observed = ObservedSandboxManifestBinding.model_validate(decoded)
        return observed.model_copy(
            update={
                "sandbox_group_resource_id": normalize_sandbox_group_resource_id(
                    observed.sandbox_group_resource_id
                )
            }
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
        _DuplicateManifestKeyError,
        SandboxGroupBindingError,
        SandboxProvisioningError,
    ):
        raise SandboxManifestMismatchError(frozenset({"manifest"})) from None


def verify_sandbox_manifest(
    expected: ExpectedSandboxManifestBinding,
    observed: ObservedSandboxManifestBinding,
    live_identity: ProvisionedSandboxIdentity,
) -> None:
    """Verify all routing-critical bindings without disclosing their values."""

    mismatches: set[str] = set()
    if expected.manifest_version != observed.manifest_version:
        mismatches.add("manifest_version")
    if expected.protocol_version != observed.protocol_version:
        mismatches.add("protocol_version")
    if expected.session_id != observed.session_id:
        mismatches.add("session_id")
    if expected.owner_hash_version != observed.owner_hash_version:
        mismatches.add("owner_hash_version")
    if expected.owner_hash != observed.owner_hash:
        mismatches.add("owner_hash")
    if expected.app_hash != observed.app_hash:
        mismatches.add("app_hash")
    if expected.generation != observed.generation:
        mismatches.add("generation")
    if expected.digest_kind != observed.digest_kind:
        mismatches.add("digest_kind")
    if expected.digest != observed.digest:
        mismatches.add("digest")

    if (
        expected.sandbox_group_resource_id != observed.sandbox_group_resource_id
        or expected.sandbox_group_resource_id != live_identity.group_resource_id
    ):
        mismatches.add("sandbox_group_resource_id")
    if expected.sandbox_id != observed.sandbox_id or expected.sandbox_id != live_identity.sandbox_id:
        mismatches.add("sandbox_id")

    if mismatches:
        raise SandboxManifestMismatchError(frozenset(mismatches))


class _DuplicateManifestKeyError(ValueError):
    """Raised internally when untrusted manifest JSON repeats a key."""


def _manifest_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateManifestKeyError
        result[key] = value
    return result
