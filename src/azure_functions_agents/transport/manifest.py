"""Strict live-manifest binding verification for ACA session routing."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from ..journal_paths import SESSION_MANIFEST_PATH as _JOURNAL_SESSION_MANIFEST_PATH
from ..strict_json import DuplicateJsonKeyError, decode_json_object
from .transport_models import (
    ProvisionedSandboxIdentity,
    SandboxGroupBindingError,
    SandboxProvisioningError,
    normalize_sandbox_group_resource_id,
)

type _ManifestText = Annotated[str, StringConstraints(min_length=1)]
type _ManifestCount = Annotated[int, Field(ge=0)]
SESSION_MANIFEST_PATH = _JOURNAL_SESSION_MANIFEST_PATH

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
    ``state_store_fingerprint`` is the non-secret ``s1-<52 base32>`` value the
    caller chooses to stamp -- this layer never derives or freshness-checks it.
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
    state_store_fingerprint: str

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
        state_store_fingerprint: str,
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
            state_store_fingerprint=state_store_fingerprint,
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
    state_store_fingerprint: _ManifestText


def parse_sandbox_manifest_binding(payload: bytes | str) -> ObservedSandboxManifestBinding:
    """Parse only the binding fields this layer requires, not the whole manifest.

    ``payload`` is untrusted data read from inside the sandbox — the
    forgery/repointing detection surface. Duplicate keys are rejected before
    validation because both ``json.loads`` and Pydantic's own JSON parser
    silently keep the last value, which lets one document mean two things.
    Every failure collapses to one redacted :class:`SandboxManifestMismatchError`.
    """

    try:
        decoded = decode_json_object(payload)
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
        DuplicateJsonKeyError,
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
    comparisons = (
        ("manifest_version", expected.manifest_version != observed.manifest_version),
        ("protocol_version", expected.protocol_version != observed.protocol_version),
        ("session_id", expected.session_id != observed.session_id),
        ("owner_hash_version", expected.owner_hash_version != observed.owner_hash_version),
        ("owner_hash", expected.owner_hash != observed.owner_hash),
        ("app_hash", expected.app_hash != observed.app_hash),
        ("generation", expected.generation != observed.generation),
        ("digest_kind", expected.digest_kind != observed.digest_kind),
        ("digest", expected.digest != observed.digest),
        ("state_store_fingerprint", expected.state_store_fingerprint != observed.state_store_fingerprint),
        (
            "sandbox_group_resource_id",
            expected.sandbox_group_resource_id != observed.sandbox_group_resource_id
            or expected.sandbox_group_resource_id != live_identity.group_resource_id,
        ),
        (
            "sandbox_id",
            expected.sandbox_id != observed.sandbox_id
            or expected.sandbox_id != live_identity.sandbox_id,
        ),
    )
    mismatches = frozenset(field for field, differs in comparisons if differs)

    if mismatches:
        raise SandboxManifestMismatchError(mismatches)


def render_sandbox_manifest_binding(expected: ExpectedSandboxManifestBinding) -> bytes:
    """Render the canonical seed/live payload for one expected binding.

    One dataclass drives both the controller-authored seed and the field set
    the harness later copies verbatim into the live manifest, so the two
    renderings can never independently drift. Output is UTF-8 JSON with
    sorted keys, compact separators, no NaN, and a single trailing newline.
    """

    text = json.dumps(asdict(expected), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{text}\n".encode()
