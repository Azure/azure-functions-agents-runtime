"""Versioned identity hashing and durable key construction for session state."""

from __future__ import annotations

import hashlib
import os
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from uuid import UUID, uuid4

from .._session_id import SESSION_ID_PATTERN
from ._label_encoding import encode_label_safe_digest
from .session_models import (
    AppIdentity,
    EntraPrincipal,
    EntraUserOwnerContext,
    FunctionAppOwnerContext,
    FunctionAppPrincipal,
    IdempotencyRowKey,
    OwnerContext,
    OwnerPartition,
    OwnerPrincipal,
    RunRowKey,
    SessionRowKey,
    SessionStateContractError,
    TriggerBindingOwnerContext,
    TriggerBindingPrincipal,
)

_APP_HASH_V1 = "a1"
_OWNER_HASH_V1 = "o1"
APP_HASH_VERSION = _APP_HASH_V1
OWNER_HASH_VERSION = _OWNER_HASH_V1
MAX_IDEMPOTENCY_KEY_BYTES = 1024

type EnvironmentReader = Callable[[str], str | None]
type UuidFactory = Callable[[], UUID]
type AppCanonicalizer = Callable[[AppIdentity], bytes]
type OwnerCanonicalizer = Callable[[OwnerContext], bytes]


class AppIdentityResolutionError(SessionStateContractError):
    """Raised when stable Function App/slot platform identity is unavailable."""


class OwnerResolutionError(SessionStateContractError):
    """Raised when an authenticated request cannot produce a supported owner."""


class CanonicalizerVersionError(SessionStateContractError):
    """Raised when a durable hash refers to an unavailable canonicalizer."""


def frame_canonical_components(components: Sequence[str]) -> bytes:
    """Frame NFC UTF-8 components with exact big-endian count/length prefixes."""
    if len(components) >= 2**32:
        raise SessionStateContractError("too many canonical identity components")
    framed = bytearray(len(components).to_bytes(4, "big"))
    for component in components:
        encoded = unicodedata.normalize("NFC", component).encode("utf-8")
        if len(encoded) >= 2**32:
            raise SessionStateContractError("canonical identity component is too large")
        framed.extend(len(encoded).to_bytes(4, "big"))
        framed.extend(encoded)
    return bytes(framed)


def _canonicalize_app_a1(app_identity: AppIdentity) -> bytes:
    # Portable across Flex/Premium/Standard: no resource-group component and no
    # SKU conditionals. Function-key auth still means app-owned sessions; this
    # only identifies which app/slot the process is.
    return frame_canonical_components(
        (
            "app",
            _APP_HASH_V1,
            app_identity.subscription_id,
            app_identity.site_name,
            app_identity.slot_name or "",
        )
    )


def _canonicalize_owner_o1(owner: OwnerContext) -> bytes:
    app_hash = compute_app_hash(owner.app_identity, _APP_HASH_V1)
    if isinstance(owner, EntraUserOwnerContext):
        return frame_canonical_components(
            (
                owner.kind,
                _OWNER_HASH_V1,
                app_hash,
                owner.agent_slug,
                owner.tenant_id,
                owner.object_id,
            )
        )
    if isinstance(owner, FunctionAppOwnerContext):
        return frame_canonical_components(
            (
                owner.kind,
                _OWNER_HASH_V1,
                app_hash,
                owner.agent_slug,
            )
        )
    if isinstance(owner, TriggerBindingOwnerContext):
        raise OwnerResolutionError(
            "trigger_binding owner contexts are reserved for FRD 0009"
        )
    raise OwnerResolutionError("unsupported owner context")


APP_CANONICALIZERS: Mapping[str, AppCanonicalizer] = MappingProxyType(
    {_APP_HASH_V1: _canonicalize_app_a1}
)
OWNER_CANONICALIZERS: Mapping[str, OwnerCanonicalizer] = MappingProxyType(
    {_OWNER_HASH_V1: _canonicalize_owner_o1}
)


def _canonicalizer_for_app(
    version: str,
    canonicalizers: Mapping[str, AppCanonicalizer],
) -> AppCanonicalizer:
    try:
        return canonicalizers[version]
    except KeyError as exc:
        raise CanonicalizerVersionError(
            f"unsupported app hash canonicalizer version: {version}"
        ) from exc


def _canonicalizer_for_owner(
    version: str,
    canonicalizers: Mapping[str, OwnerCanonicalizer],
) -> OwnerCanonicalizer:
    try:
        return canonicalizers[version]
    except KeyError as exc:
        raise CanonicalizerVersionError(
            f"unsupported owner hash canonicalizer version: {version}"
        ) from exc


def compute_app_hash(
    app_identity: AppIdentity,
    version: str = APP_HASH_VERSION,
    *,
    canonicalizers: Mapping[str, AppCanonicalizer] = APP_CANONICALIZERS,
) -> str:
    canonical = _canonicalizer_for_app(version, canonicalizers)(app_identity)
    digest = hashlib.sha256(canonical).digest()
    return f"{version}-{encode_label_safe_digest(digest)}"


def compute_owner_hash(
    owner: OwnerContext,
    version: str = OWNER_HASH_VERSION,
    *,
    canonicalizers: Mapping[str, OwnerCanonicalizer] = OWNER_CANONICALIZERS,
) -> str:
    canonical = _canonicalizer_for_owner(version, canonicalizers)(owner)
    digest = hashlib.sha256(canonical).digest()
    return f"{version}-{encode_label_safe_digest(digest)}"


def verify_app_hash(
    app_identity: AppIdentity,
    expected_hash: str,
    stored_version: str,
    *,
    canonicalizers: Mapping[str, AppCanonicalizer] = APP_CANONICALIZERS,
) -> bool:
    """Verify under the row's stored version without selecting or migrating to latest."""
    return (
        expected_hash.startswith(f"{stored_version}-")
        and compute_app_hash(
            app_identity,
            stored_version,
            canonicalizers=canonicalizers,
        )
        == expected_hash
    )


def verify_owner_hash(
    owner: OwnerContext,
    expected_hash: str,
    stored_version: str,
    *,
    canonicalizers: Mapping[str, OwnerCanonicalizer] = OWNER_CANONICALIZERS,
) -> bool:
    """Verify under the row's stored version without selecting or migrating to latest."""
    return (
        expected_hash.startswith(f"{stored_version}-")
        and compute_owner_hash(
            owner,
            stored_version,
            canonicalizers=canonicalizers,
        )
        == expected_hash
    )


def resolve_function_app_identity(
    get_environment: EnvironmentReader = os.getenv,
) -> AppIdentity:
    """Resolve stable app/slot identity from platform-provided environment values."""

    def require(name: str) -> str:
        value = get_environment(name)
        if value is None or not value.strip():
            raise AppIdentityResolutionError(
                f"stable Function App identity requires {name}"
            )
        return value.strip()

    owner_name = require("WEBSITE_OWNER_NAME")
    subscription_id, separator, _webspace = owner_name.partition("+")
    if not separator or not subscription_id:
        raise AppIdentityResolutionError(
            "WEBSITE_OWNER_NAME does not contain a stable subscription prefix"
        )
    site_name = require("WEBSITE_SITE_NAME")
    slot_value = get_environment("WEBSITE_SLOT_NAME")
    slot_name = slot_value.strip() if slot_value is not None else None
    try:
        return AppIdentity.create(
            subscription_id=subscription_id,
            site_name=site_name,
            slot_name=slot_name,
        )
    except SessionStateContractError as exc:
        raise AppIdentityResolutionError(
            "stable Function App identity inputs are invalid"
        ) from exc


def resolve_owner_context(
    app_identity: AppIdentity,
    agent_slug: str,
    principal: OwnerPrincipal | None,
) -> OwnerContext:
    """Resolve only explicitly authenticated, currently supported owner kinds."""
    if isinstance(principal, EntraPrincipal):
        return EntraUserOwnerContext.create(
            app_identity=app_identity,
            agent_slug=agent_slug,
            tenant_id=principal.tenant_id,
            object_id=principal.object_id,
        )
    if isinstance(principal, FunctionAppPrincipal):
        return FunctionAppOwnerContext.create(
            app_identity=app_identity,
            agent_slug=agent_slug,
        )
    if isinstance(principal, TriggerBindingPrincipal):
        raise OwnerResolutionError(
            "trigger_binding owner resolution is reserved for FRD 0009"
        )
    raise OwnerResolutionError("authenticated owner principal could not be resolved")


def owner_partition(
    owner: OwnerContext,
    *,
    app_version: str = APP_HASH_VERSION,
    owner_version: str = OWNER_HASH_VERSION,
) -> OwnerPartition:
    return OwnerPartition.create(
        owner_hash_version=owner_version,
        app_hash=compute_app_hash(owner.app_identity, app_version),
        owner_kind=owner.kind,
        owner_hash=compute_owner_hash(owner, owner_version),
    )


def validate_session_id(value: str) -> str:
    if SESSION_ID_PATTERN.fullmatch(value) is None:
        raise SessionStateContractError(
            f"session_id must match {SESSION_ID_PATTERN.pattern}"
        )
    return value


def validate_run_id(value: str) -> str:
    if SESSION_ID_PATTERN.fullmatch(value) is None:
        raise SessionStateContractError(f"run_id must match {SESSION_ID_PATTERN.pattern}")
    return value


def mint_session_id(uuid_factory: UuidFactory = uuid4) -> str:
    return validate_session_id(uuid_factory().hex)


def mint_run_id(uuid_factory: UuidFactory = uuid4) -> str:
    return validate_run_id(uuid_factory().hex)


def session_row_key(session_id: str) -> SessionRowKey:
    return SessionRowKey.create(validate_session_id(session_id))


def run_row_key(session_id: str, run_id: str) -> RunRowKey:
    return RunRowKey.create(validate_session_id(session_id), validate_run_id(run_id))


def hash_idempotency_key(idempotency_key: str) -> str:
    encoded = idempotency_key.encode("utf-8")
    if not encoded or len(encoded) > MAX_IDEMPOTENCY_KEY_BYTES:
        raise SessionStateContractError(
            "idempotency_key must be 1-1024 UTF-8 bytes"
        )
    return hashlib.sha256(encoded).hexdigest()


def idempotency_row_key(
    session_id: str,
    idempotency_key: str,
) -> IdempotencyRowKey:
    return IdempotencyRowKey.create(
        validate_session_id(session_id),
        hash_idempotency_key(idempotency_key),
    )
