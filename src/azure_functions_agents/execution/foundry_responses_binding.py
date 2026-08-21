"""Deployment-published Foundry Responses runtime binding resolution."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from ..foundry_responses.fha_runtime_projection import (
    FhaRuntimeProjection,
    compute_fha_wrapper_digest,
)
from ..session_state._label_encoding import encode_label_safe_digest
from ..session_state.identity import compute_app_hash, frame_canonical_components
from ..session_state.session_models import AppIdentity
from .foundry_application_content import (
    MAX_APPLICATION_CONTENT_MANIFEST_BYTES,
    ApplicationContentManifest,
    ApplicationContentManifestError,
    compute_application_content_digest,
    parse_application_content_manifest,
    serialize_application_content_manifest,
    validate_sha256_digest,
)

FHA_PROJECT_ENDPOINT_ENV = "AZURE_FUNCTIONS_AGENTS_FHA_PROJECT_ENDPOINT"
FHA_PROJECT_RESOURCE_ID_ENV = "AZURE_FUNCTIONS_AGENTS_FHA_PROJECT_RESOURCE_ID"
FHA_MANAGED_AGENT_NAME_ENV = "AZURE_FUNCTIONS_AGENTS_FHA_MANAGED_AGENT_NAME"
FHA_MANAGED_AGENT_VERSION_ENV = "AZURE_FUNCTIONS_AGENTS_FHA_MANAGED_AGENT_VERSION"
FHA_APPLICATION_CONTENT_MANIFEST_ENV = "AZURE_FUNCTIONS_AGENTS_FHA_APPLICATION_CONTENT_MANIFEST"
FHA_APPLICATION_CONTENT_DIGEST_ENV = "AZURE_FUNCTIONS_AGENTS_FHA_APPLICATION_CONTENT_DIGEST"
FHA_WRAPPER_DIGEST_ENV = "AZURE_FUNCTIONS_AGENTS_FHA_WRAPPER_DIGEST"
FHA_BINDING_FINGERPRINT_ENV = "AZURE_FUNCTIONS_AGENTS_FHA_BINDING_FINGERPRINT"

FHA_BINDING_ENV_NAMES = (
    FHA_PROJECT_ENDPOINT_ENV,
    FHA_PROJECT_RESOURCE_ID_ENV,
    FHA_MANAGED_AGENT_NAME_ENV,
    FHA_MANAGED_AGENT_VERSION_ENV,
    FHA_APPLICATION_CONTENT_MANIFEST_ENV,
    FHA_APPLICATION_CONTENT_DIGEST_ENV,
    FHA_WRAPPER_DIGEST_ENV,
    FHA_BINDING_FINGERPRINT_ENV,
)
FHA_BINDING_FINGERPRINT_VERSION = "fha1"
MAX_FHA_BINDING_ENV_VALUE_BYTES = MAX_APPLICATION_CONTENT_MANIFEST_BYTES

_MANAGED_AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MANAGED_AGENT_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BINDING_FINGERPRINT_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}-[a-z2-7]{52}$")


class FoundryResponsesBindingError(ValueError):
    """The deployment-published Foundry Responses binding is unsafe or incomplete."""

    def __init__(self, fields: frozenset[str]) -> None:
        self.fields = fields
        super().__init__(
            f"Foundry Responses binding validation failed: {', '.join(sorted(fields))}."
        )


class FoundryResponsesBindingState(StrEnum):
    """The explicit startup disposition of the Foundry Responses binding."""

    DISABLED = "disabled"
    ENABLED = "enabled"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class FoundryResponsesRuntimeBinding:
    """The immutable, deployment-owned configuration for one Foundry Responses runtime."""

    project_endpoint: str
    project_resource_id: str
    managed_agent_name: str
    managed_agent_version: str
    application_content_manifest: ApplicationContentManifest
    application_content_digest: str
    wrapper_digest: str
    binding_fingerprint: str

    @classmethod
    def create(
        cls,
        *,
        project_endpoint: str,
        project_resource_id: str,
        managed_agent_name: str,
        managed_agent_version: str,
        application_content_manifest: ApplicationContentManifest | bytes | str,
        application_content_digest: str,
        wrapper_digest: str,
        binding_fingerprint: str,
    ) -> FoundryResponsesRuntimeBinding:
        endpoint = _normalize_project_endpoint(
            _require_bounded_text(project_endpoint, FHA_PROJECT_ENDPOINT_ENV)
        )
        resource_id = _validate_project_resource_id(
            _require_bounded_text(project_resource_id, FHA_PROJECT_RESOURCE_ID_ENV)
        )
        agent_name = _validate_agent_name(
            _require_bounded_text(managed_agent_name, FHA_MANAGED_AGENT_NAME_ENV)
        )
        agent_version = _validate_agent_version(
            _require_bounded_text(managed_agent_version, FHA_MANAGED_AGENT_VERSION_ENV)
        )
        manifest = _parse_manifest(application_content_manifest)
        digest = _validate_digest(
            _require_bounded_text(
                application_content_digest,
                FHA_APPLICATION_CONTENT_DIGEST_ENV,
            ),
            FHA_APPLICATION_CONTENT_DIGEST_ENV,
        )
        checked_wrapper_digest = _validate_digest(
            _require_bounded_text(wrapper_digest, FHA_WRAPPER_DIGEST_ENV),
            FHA_WRAPPER_DIGEST_ENV,
        )
        fingerprint = _validate_binding_fingerprint(
            _require_bounded_text(binding_fingerprint, FHA_BINDING_FINGERPRINT_ENV)
        )
        return cls(
            project_endpoint=endpoint,
            project_resource_id=resource_id,
            managed_agent_name=agent_name,
            managed_agent_version=agent_version,
            application_content_manifest=manifest,
            application_content_digest=digest,
            wrapper_digest=checked_wrapper_digest,
            binding_fingerprint=fingerprint,
        )

    @property
    def application_content_manifest_json(self) -> str:
        """Return the exact canonical manifest text expected in the app setting."""
        return serialize_application_content_manifest(self.application_content_manifest)

    def validate_application_content(self, application_root: Path) -> None:
        """Fail closed unless the resolved application root matches this binding's digest."""
        try:
            observed_digest = compute_application_content_digest(
                application_root,
                self.application_content_manifest,
            )
        except ApplicationContentManifestError:
            raise FoundryResponsesBindingError(
                frozenset({FHA_APPLICATION_CONTENT_MANIFEST_ENV})
            ) from None
        if not hmac.compare_digest(observed_digest, self.application_content_digest):
            raise FoundryResponsesBindingError(frozenset({FHA_APPLICATION_CONTENT_DIGEST_ENV}))

    def validate_runtime_projection(self, projection: FhaRuntimeProjection) -> None:
        """Fail closed unless the compiled projection matches the generated wrapper."""
        try:
            from ..foundry_responses.fha_resilient_responses_entrypoint import (
                render_fha_hosted_responses_entrypoint,
            )

            observed_digest = compute_fha_wrapper_digest(
                projection,
                render_fha_hosted_responses_entrypoint(),
            )
        except (AttributeError, ImportError, TypeError, ValueError):
            raise FoundryResponsesBindingError(frozenset({FHA_WRAPPER_DIGEST_ENV})) from None
        if not hmac.compare_digest(observed_digest, self.wrapper_digest):
            raise FoundryResponsesBindingError(frozenset({FHA_WRAPPER_DIGEST_ENV}))

    def validate_fingerprint(self, app_identity: AppIdentity) -> None:
        """Fail closed unless this binding belongs to the current Function App environment."""
        expected = compute_foundry_responses_binding_fingerprint(
            app_identity=app_identity,
            project_endpoint=self.project_endpoint,
            project_resource_id=self.project_resource_id,
            managed_agent_name=self.managed_agent_name,
            managed_agent_version=self.managed_agent_version,
            application_content_manifest=self.application_content_manifest,
            application_content_digest=self.application_content_digest,
            wrapper_digest=self.wrapper_digest,
        )
        if not hmac.compare_digest(expected, self.binding_fingerprint):
            raise FoundryResponsesBindingError(frozenset({FHA_BINDING_FINGERPRINT_ENV}))


@dataclass(frozen=True, slots=True)
class FoundryResponsesBindingResolution:
    """One explicit disabled, enabled, or fail-closed binding-resolution result."""

    state: FoundryResponsesBindingState
    binding: FoundryResponsesRuntimeBinding | None
    error: FoundryResponsesBindingError | None = None

    @property
    def is_enabled(self) -> bool:
        """Return whether a complete valid binding selected Foundry Responses."""
        return self.state is FoundryResponsesBindingState.ENABLED

    def require_binding(self) -> FoundryResponsesRuntimeBinding | None:
        """Return the binding or raise the captured fail-closed validation error."""
        if self.state is FoundryResponsesBindingState.INVALID:
            if self.error is None:
                raise RuntimeError("Invalid Foundry Responses binding has no validation error.")
            raise self.error
        return self.binding


def inspect_foundry_responses_runtime_binding(
    environment: Mapping[str, object] | None = None,
    *,
    aca_sandbox_configured: bool = False,
) -> FoundryResponsesBindingResolution:
    """Inspect the eight app settings without converting an invalid set into a fallback."""
    source = os.environ if environment is None else environment
    present = frozenset(name for name in FHA_BINDING_ENV_NAMES if name in source)
    if not present:
        return FoundryResponsesBindingResolution(
            state=FoundryResponsesBindingState.DISABLED,
            binding=None,
        )
    if len(present) != len(FHA_BINDING_ENV_NAMES):
        return _invalid_resolution(frozenset(FHA_BINDING_ENV_NAMES).difference(present))

    try:
        binding = FoundryResponsesRuntimeBinding.create(
            project_endpoint=_required_environment_value(source, FHA_PROJECT_ENDPOINT_ENV),
            project_resource_id=_required_environment_value(source, FHA_PROJECT_RESOURCE_ID_ENV),
            managed_agent_name=_required_environment_value(source, FHA_MANAGED_AGENT_NAME_ENV),
            managed_agent_version=_required_environment_value(
                source,
                FHA_MANAGED_AGENT_VERSION_ENV,
            ),
            application_content_manifest=_required_environment_value(
                source,
                FHA_APPLICATION_CONTENT_MANIFEST_ENV,
            ),
            application_content_digest=_required_environment_value(
                source,
                FHA_APPLICATION_CONTENT_DIGEST_ENV,
            ),
            wrapper_digest=_required_environment_value(source, FHA_WRAPPER_DIGEST_ENV),
            binding_fingerprint=_required_environment_value(source, FHA_BINDING_FINGERPRINT_ENV),
        )
    except FoundryResponsesBindingError as error:
        return _invalid_resolution(error.fields, error)
    if aca_sandbox_configured:
        return _invalid_resolution(frozenset({"session_runtime.aca_sandbox"}))

    return FoundryResponsesBindingResolution(
        state=FoundryResponsesBindingState.ENABLED,
        binding=binding,
    )


def resolve_foundry_responses_runtime_binding(
    environment: Mapping[str, object] | None = None,
    *,
    aca_sandbox_configured: bool = False,
) -> FoundryResponsesRuntimeBinding | None:
    """Return a complete binding, no binding, or raise rather than silently falling back."""
    return inspect_foundry_responses_runtime_binding(
        environment,
        aca_sandbox_configured=aca_sandbox_configured,
    ).require_binding()


def compute_foundry_responses_binding_fingerprint(
    *,
    app_identity: AppIdentity,
    project_endpoint: str,
    project_resource_id: str,
    managed_agent_name: str,
    managed_agent_version: str,
    application_content_manifest: ApplicationContentManifest | bytes | str,
    application_content_digest: str,
    wrapper_digest: str,
) -> str:
    """Compute the non-secret binding fingerprint shared by bootstrap and startup."""
    normalized = FoundryResponsesRuntimeBinding.create(
        project_endpoint=project_endpoint,
        project_resource_id=project_resource_id,
        managed_agent_name=managed_agent_name,
        managed_agent_version=managed_agent_version,
        application_content_manifest=application_content_manifest,
        application_content_digest=application_content_digest,
        wrapper_digest=wrapper_digest,
        binding_fingerprint=f"{FHA_BINDING_FINGERPRINT_VERSION}-{'a' * 52}",
    )
    components = (
        "foundry_responses_binding",
        FHA_BINDING_FINGERPRINT_VERSION,
        compute_app_hash(app_identity),
        normalized.project_endpoint,
        normalized.project_resource_id,
        normalized.managed_agent_name,
        normalized.managed_agent_version,
        normalized.application_content_manifest_json,
        normalized.application_content_digest,
        normalized.wrapper_digest,
    )
    digest = hashlib.sha256(frame_canonical_components(components)).digest()
    return f"{FHA_BINDING_FINGERPRINT_VERSION}-{encode_label_safe_digest(digest)}"


def _invalid_resolution(
    fields: frozenset[str],
    error: FoundryResponsesBindingError | None = None,
) -> FoundryResponsesBindingResolution:
    validation_error = error or FoundryResponsesBindingError(fields)
    return FoundryResponsesBindingResolution(
        state=FoundryResponsesBindingState.INVALID,
        binding=None,
        error=validation_error,
    )


def _required_environment_value(source: Mapping[str, object], name: str) -> str:
    value = source.get(name)
    return _require_bounded_text(value, name)


def _require_bounded_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FoundryResponsesBindingError(frozenset({name}))
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise FoundryResponsesBindingError(frozenset({name})) from None
    if encoded_length > MAX_FHA_BINDING_ENV_VALUE_BYTES:
        raise FoundryResponsesBindingError(frozenset({name}))
    return value


def _parse_manifest(
    value: ApplicationContentManifest | bytes | str,
) -> ApplicationContentManifest:
    try:
        if isinstance(value, ApplicationContentManifest):
            serialized = serialize_application_content_manifest(value)
            _require_bounded_text(serialized, FHA_APPLICATION_CONTENT_MANIFEST_ENV)
            return parse_application_content_manifest(serialized)
        if isinstance(value, bytes):
            if len(value) > MAX_FHA_BINDING_ENV_VALUE_BYTES:
                raise FoundryResponsesBindingError(
                    frozenset({FHA_APPLICATION_CONTENT_MANIFEST_ENV})
                )
            return parse_application_content_manifest(value)
        return parse_application_content_manifest(
            _require_bounded_text(value, FHA_APPLICATION_CONTENT_MANIFEST_ENV)
        )
    except ApplicationContentManifestError:
        raise FoundryResponsesBindingError(
            frozenset({FHA_APPLICATION_CONTENT_MANIFEST_ENV})
        ) from None


def _validate_digest(value: str, name: str) -> str:
    try:
        return validate_sha256_digest(value)
    except ApplicationContentManifestError:
        raise FoundryResponsesBindingError(frozenset({name})) from None


def _normalize_project_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise FoundryResponsesBindingError(frozenset({FHA_PROJECT_ENDPOINT_ENV})) from None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.query
        or parsed.fragment
    ):
        raise FoundryResponsesBindingError(frozenset({FHA_PROJECT_ENDPOINT_ENV}))
    path = parsed.path.rstrip("/")
    path_parts = path.split("/")
    if not path or any(part in {"", ".", ".."} for part in path_parts[1:]):
        raise FoundryResponsesBindingError(frozenset({FHA_PROJECT_ENDPOINT_ENV}))
    hostname = parsed.hostname.casefold()
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    return urlunsplit(("https", netloc, path, "", ""))


def _validate_project_resource_id(value: str) -> str:
    if not value.startswith("/") or "?" in value or "#" in value:
        raise FoundryResponsesBindingError(frozenset({FHA_PROJECT_RESOURCE_ID_ENV}))
    parts = value.split("/")[1:]
    if (
        len(parts) < 8
        or any(not part for part in parts)
        or parts[0].casefold() != "subscriptions"
        or parts[2].casefold() != "resourcegroups"
        or parts[4].casefold() != "providers"
        or len(parts[6:]) % 2 != 0
        or any(part in {".", ".."} or "\\" in part for part in parts)
    ):
        raise FoundryResponsesBindingError(frozenset({FHA_PROJECT_RESOURCE_ID_ENV}))
    try:
        UUID(parts[1])
    except ValueError:
        raise FoundryResponsesBindingError(frozenset({FHA_PROJECT_RESOURCE_ID_ENV})) from None
    if any(any(character.isspace() for character in part) for part in parts):
        raise FoundryResponsesBindingError(frozenset({FHA_PROJECT_RESOURCE_ID_ENV}))
    return value


def _validate_agent_name(value: str) -> str:
    if _MANAGED_AGENT_NAME_PATTERN.fullmatch(value) is None:
        raise FoundryResponsesBindingError(frozenset({FHA_MANAGED_AGENT_NAME_ENV}))
    return value


def _validate_agent_version(value: str) -> str:
    if _MANAGED_AGENT_VERSION_PATTERN.fullmatch(value) is None:
        raise FoundryResponsesBindingError(frozenset({FHA_MANAGED_AGENT_VERSION_ENV}))
    return value


def _validate_binding_fingerprint(value: str) -> str:
    if _BINDING_FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise FoundryResponsesBindingError(frozenset({FHA_BINDING_FINGERPRINT_ENV}))
    return value
