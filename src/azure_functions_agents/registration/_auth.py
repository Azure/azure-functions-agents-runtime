"""Inbound authentication enforcement for built-in endpoints.

This module is the only place that reasons about *who* may call an agent's
built-in HTTP endpoints. It maps the authoring-level ``builtin_endpoints.http_auth``
policy onto an Azure Functions ``AuthLevel`` (for native function/system-key
"API key" auth) and, for Entra ID, checks the caller's identity before the
runner is ever invoked.

Entra ID enforcement is delegated entirely to **App Service Authentication
(Easy Auth)**. The platform validates the Entra-issued token (bearer or cookie),
and injects a validated ``X-MS-CLIENT-PRINCIPAL`` header. The runtime trusts that
header and applies the configured tenant/audience/client-id allow-lists as
defense-in-depth. The runtime never parses or validates a JWT itself; a request
in ``entra`` mode without a validated principal is rejected (fail closed).

Because ``entra`` routes are registered anonymous at the Functions key layer, the
injected principal header is only trustworthy when Easy Auth is guaranteed to sit
in front of the app (Easy Auth strips any client-supplied ``X-MS-CLIENT-PRINCIPAL``
header before injecting its own). If Easy Auth is disabled, that header is just
caller-controlled input, which would be an authentication bypass. The runtime
therefore refuses to trust the header unless it has positive, non-spoofable
evidence that Easy Auth is enforced -- the platform-injected
``WEBSITE_AUTH_ENABLED`` environment variable, or an explicit operator assertion
via ``AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH`` -- and fails closed otherwise.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import azure.functions as func

from ..config import EndpointAuthConfig, EntraAuthConfig
from ..config.env import runtime_env_value
from ..session_state.models import (
    EntraPrincipal,
    FunctionAppPrincipal,
    OwnerPrincipal,
    SessionStateContractError,
)

_EASY_AUTH_PRINCIPAL_HEADER = "x-ms-client-principal"

# Non-spoofable signals that App Service Authentication (Easy Auth) is enforced in
# front of the app. Both are process environment variables (not request headers),
# so a caller cannot forge them. ``WEBSITE_AUTH_ENABLED`` is injected by the App
# Service platform when Easy Auth is enabled; the ``..._ENTRA_EASY_AUTH`` app
# setting lets operators assert enforcement where the platform signal is absent.
_PLATFORM_EASY_AUTH_ENV = "WEBSITE_AUTH_ENABLED"
_EASY_AUTH_ASSERTION_ENV = "AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH"

_TRUTHY_VALUES = frozenset({"true", "1", "yes", "y"})

_AUTH_LEVEL_BY_MODE: dict[str, func.AuthLevel] = {
    "function": func.AuthLevel.FUNCTION,
    "admin": func.AuthLevel.ADMIN,
    "anonymous": func.AuthLevel.ANONYMOUS,
    # entra replaces the function-key gate with an Easy Auth identity check, so
    # the Functions level is anonymous and the platform-injected principal is
    # validated in-app against the configured allow-lists.
    "entra": func.AuthLevel.ANONYMOUS,
}

# Map common long-form (Easy Auth / WS-Fed) claim types to their short JWT names.
_CLAIM_ALIASES: dict[str, str] = {
    "http://schemas.microsoft.com/identity/claims/tenantid": "tid",
    "http://schemas.microsoft.com/identity/claims/objectidentifier": "oid",
}

type HeaderGetter = Callable[[str], str | None]


@dataclass(frozen=True)
class AuthError:
    """A failed authorization outcome to surface to the caller."""

    status_code: int
    message: str


def resolve_endpoint_auth_level(auth: EndpointAuthConfig) -> func.AuthLevel:
    """Map an endpoint auth policy to the Azure Functions route ``AuthLevel``."""
    return _AUTH_LEVEL_BY_MODE.get(auth.mode, func.AuthLevel.FUNCTION)


def _easy_auth_enforced() -> bool:
    """Return True only with positive evidence that Easy Auth is enforced.

    Either the platform-injected ``WEBSITE_AUTH_ENABLED`` variable or the explicit
    ``AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH`` operator assertion is sufficient.
    Both are environment variables, so they cannot be spoofed by a caller. When
    neither is truthy the injected principal header must not be trusted.
    """
    return (
        runtime_env_value(_PLATFORM_EASY_AUTH_ENV).lower() in _TRUTHY_VALUES
        or runtime_env_value(_EASY_AUTH_ASSERTION_ENV).lower() in _TRUTHY_VALUES
    )


def _short_claim_name(claim_type: str) -> str:
    if claim_type in _CLAIM_ALIASES:
        return _CLAIM_ALIASES[claim_type]
    # Fall back to the last path segment of a URI-style claim type.
    return claim_type.rsplit("/", 1)[-1]


def _as_string_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _flatten_claims(principal: Mapping[str, object]) -> dict[str, list[str]]:
    """Normalize an Easy Auth principal or decoded JWT into short-name -> values."""
    flat: dict[str, list[str]] = {}
    claims = principal.get("claims")
    if isinstance(claims, list):
        # Easy Auth shape: a list of {"typ": ..., "val": ...} entries.
        for entry in claims:
            mapped_entry = _as_string_mapping(entry)
            if mapped_entry is None:
                continue
            typ = mapped_entry.get("typ")
            val = mapped_entry.get("val")
            if isinstance(typ, str) and isinstance(val, str):
                flat.setdefault(_short_claim_name(typ), []).append(val)
        return flat
    # Decoded JWT / flat dict of claims.
    for key, value in principal.items():
        short = _short_claim_name(key)
        if isinstance(value, str):
            flat.setdefault(short, []).append(value)
        elif isinstance(value, list):
            flat.setdefault(short, []).extend(str(item) for item in value)
    return flat


def _check_allowlists(
    flat: dict[str, list[str]], entra: EntraAuthConfig | None
) -> AuthError | None:
    if entra is None:
        return None

    if entra.tenant_id and entra.tenant_id not in flat.get("tid", []):
        return AuthError(403, "Token tenant is not allowed.")

    if entra.allowed_audiences and not (
        set(entra.allowed_audiences) & set(flat.get("aud", []))
    ):
        return AuthError(403, "Token audience is not allowed.")

    if entra.allowed_client_ids:
        caller = set(flat.get("appid", [])) | set(flat.get("azp", []))
        if not (set(entra.allowed_client_ids) & caller):
            return AuthError(403, "Caller application is not allowed.")
    return None


def _decode_easy_auth_principal(header_value: str) -> dict[str, object] | None:
    try:
        raw = base64.b64decode(header_value, validate=True)
        data: object = json.loads(raw)
    except (binascii.Error, ValueError):
        return None
    return _as_string_mapping(data)


def _authorized_entra_claims(
    get_header: HeaderGetter,
    auth: EndpointAuthConfig,
) -> tuple[
    dict[str, object] | None,
    dict[str, list[str]] | None,
    AuthError | None,
]:
    if not _easy_auth_enforced():
        return None, None, AuthError(
            401,
            "Entra authentication requires App Service Authentication (Easy Auth) "
            "to be enabled in front of this app.",
        )

    principal_header = get_header(_EASY_AUTH_PRINCIPAL_HEADER)
    if not principal_header:
        return None, None, AuthError(
            401,
            "Entra authentication required (App Service Authentication).",
        )

    principal = _decode_easy_auth_principal(principal_header)
    if principal is None:
        return None, None, AuthError(401, "Invalid client principal header.")

    auth_typ = principal.get("auth_typ")
    if not isinstance(auth_typ, str) or auth_typ.lower() not in {
        "aad",
        "azureactivedirectory",
    }:
        return None, None, AuthError(401, "Entra authentication required.")

    flat = _flatten_claims(principal)
    allowlist_error = _check_allowlists(flat, auth.entra)
    if allowlist_error is not None:
        return None, None, allowlist_error
    return principal, flat, None


def _single_claim(claims: Mapping[str, list[str]], name: str) -> str | None:
    normalized = {value.strip().lower() for value in claims.get(name, []) if value.strip()}
    if len(normalized) != 1:
        return None
    return next(iter(normalized))


def _owner_identity_claims(principal: Mapping[str, object]) -> dict[str, list[str]]:
    """Extract only exact Entra identity claim names and supported standard aliases."""
    result: dict[str, list[str]] = {}
    claims = principal.get("claims")
    if isinstance(claims, list):
        for entry in claims:
            mapped_entry = _as_string_mapping(entry)
            if mapped_entry is None:
                continue
            claim_type = mapped_entry.get("typ")
            claim_value = mapped_entry.get("val")
            if not isinstance(claim_type, str) or not isinstance(claim_value, str):
                continue
            short_name = (
                claim_type
                if claim_type in {"tid", "oid"}
                else _CLAIM_ALIASES.get(claim_type)
            )
            if short_name in {"tid", "oid"}:
                result.setdefault(short_name, []).append(claim_value)
        return result

    for claim_type, claim_value in principal.items():
        short_name = (
            claim_type if claim_type in {"tid", "oid"} else _CLAIM_ALIASES.get(claim_type)
        )
        if short_name not in {"tid", "oid"}:
            continue
        if isinstance(claim_value, str):
            result.setdefault(short_name, []).append(claim_value)
        elif isinstance(claim_value, list):
            result.setdefault(short_name, []).extend(
                value for value in claim_value if isinstance(value, str)
            )
    return result


def resolve_owner_principal(
    get_header: HeaderGetter,
    auth: EndpointAuthConfig,
) -> OwnerPrincipal | AuthError:
    """Resolve a typed owner input without changing existing endpoint enforcement.

    This seam is intentionally not wired into request execution in P3a. Functions
    host key modes resolve to an app marker without reading a key or key name.
    Entra mode reuses the already-authorized Easy Auth principal but additionally
    requires stable ``tid`` and immutable ``oid`` claims for durable ownership.
    """
    if auth.mode in {"function", "admin"}:
        return FunctionAppPrincipal()
    if auth.mode != "entra":
        return AuthError(401, "Persistent sessions require authenticated endpoint auth.")

    principal, _claims, error = _authorized_entra_claims(get_header, auth)
    if error is not None:
        return error
    if principal is None:
        return AuthError(401, "Stable Entra owner identity is required.")

    identity_claims = _owner_identity_claims(principal)
    tenant_id = _single_claim(identity_claims, "tid")
    object_id = _single_claim(identity_claims, "oid")
    if tenant_id is None or object_id is None:
        return AuthError(401, "Stable Entra owner identity is required.")
    try:
        return EntraPrincipal(tenant_id=tenant_id, object_id=object_id)
    except SessionStateContractError:
        return AuthError(401, "Stable Entra owner identity is required.")


def authorize_entra_request(
    get_header: HeaderGetter, auth: EndpointAuthConfig
) -> AuthError | None:
    """Authorize an inbound request against an endpoint auth policy.

    Returns ``None`` when the request is authorized (including for the
    non-``entra`` modes, whose enforcement is handled by the Functions host key
    check), or an :class:`AuthError` describing why it was rejected.

    In ``entra`` mode the request must carry a validated App Service
    Authentication (Easy Auth) ``X-MS-CLIENT-PRINCIPAL`` header. The runtime does
    not validate tokens itself; a request without a validated Entra principal is
    rejected (fail closed). The header is only trusted when Easy Auth is
    verifiably enforced (see :func:`_easy_auth_enforced`); otherwise the request
    is rejected rather than trusting a potentially caller-supplied header.
    """
    if auth.mode != "entra":
        return None
    _principal, _claims, error = _authorized_entra_claims(get_header, auth)
    return error
