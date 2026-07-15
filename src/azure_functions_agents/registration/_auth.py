"""Inbound authentication enforcement for built-in endpoints.

This module is the only place that reasons about *who* may call an agent's
built-in HTTP endpoints. It maps the authoring-level ``builtin_endpoints.auth``
policy onto an Azure Functions ``AuthLevel`` (for native function/system-key
"API key" auth) and, for Entra ID, validates the caller's identity before the
runner is ever invoked.

Two proofs of an Entra identity are accepted, in order:

1. **Easy Auth** — an ``X-MS-CLIENT-PRINCIPAL`` header injected by App Service
   Authentication after the platform has already validated the token.
2. **Bearer token** — an ``Authorization: Bearer <jwt>`` validated in-app against
   Entra ID (JWKS signature + ``exp`` + optional audience), then filtered by the
   configured tenant/audience/client-id allow-lists.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import azure.functions as func

from ..config import EndpointAuthConfig, EntraAuthConfig
from ..config.env import runtime_env_value

_EASY_AUTH_PRINCIPAL_HEADER = "x-ms-client-principal"
_AUTHORIZATION_HEADER = "authorization"
_BEARER_PREFIX = "bearer "

_AUTH_LEVEL_BY_MODE: dict[str, func.AuthLevel] = {
    "function": func.AuthLevel.FUNCTION,
    "admin": func.AuthLevel.ADMIN,
    "anonymous": func.AuthLevel.ANONYMOUS,
    # entra replaces the function-key gate with the runtime's identity check,
    # so the Functions level is anonymous and enforcement happens in-app.
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


def _env_list(name: str) -> list[str]:
    raw = runtime_env_value(name)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _resolved_tenant_id(entra: EntraAuthConfig | None) -> str | None:
    if entra and entra.tenant_id:
        return entra.tenant_id
    return runtime_env_value("AZURE_FUNCTIONS_AGENTS_ENTRA_TENANT_ID") or None


def _resolved_audiences(entra: EntraAuthConfig | None) -> list[str]:
    if entra and entra.allowed_audiences:
        return list(entra.allowed_audiences)
    return _env_list("AZURE_FUNCTIONS_AGENTS_ENTRA_AUDIENCES")


def _resolved_client_ids(entra: EntraAuthConfig | None) -> list[str]:
    if entra and entra.allowed_client_ids:
        return list(entra.allowed_client_ids)
    return _env_list("AZURE_FUNCTIONS_AGENTS_ENTRA_CLIENT_IDS")


def _short_claim_name(claim_type: str) -> str:
    if claim_type in _CLAIM_ALIASES:
        return _CLAIM_ALIASES[claim_type]
    # Fall back to the last path segment of a URI-style claim type.
    return claim_type.rsplit("/", 1)[-1]


def _flatten_claims(principal: dict[str, Any]) -> dict[str, list[str]]:
    """Normalize an Easy Auth principal or decoded JWT into short-name -> values."""
    flat: dict[str, list[str]] = {}
    claims = principal.get("claims")
    if isinstance(claims, list):
        # Easy Auth shape: a list of {"typ": ..., "val": ...} entries.
        for entry in claims:
            if not isinstance(entry, dict):
                continue
            typ = entry.get("typ")
            val = entry.get("val")
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
    tenant_id = _resolved_tenant_id(entra)
    if tenant_id and tenant_id not in flat.get("tid", []):
        return AuthError(403, "Token tenant is not allowed.")

    audiences = _resolved_audiences(entra)
    if audiences and not (set(audiences) & set(flat.get("aud", []))):
        return AuthError(403, "Token audience is not allowed.")

    client_ids = _resolved_client_ids(entra)
    if client_ids:
        caller = set(flat.get("appid", [])) | set(flat.get("azp", []))
        if not (set(client_ids) & caller):
            return AuthError(403, "Caller application is not allowed.")
    return None


def _decode_easy_auth_principal(header_value: str) -> dict[str, Any] | None:
    try:
        raw = base64.b64decode(header_value, validate=True)
        data = json.loads(raw)
    except (binascii.Error, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _jwks_uri(tenant_id: str | None) -> str:
    tenant = tenant_id or "common"
    return f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"


_jwks_clients: dict[str, Any] = {}


def _get_signing_key(token: str, tenant_id: str | None) -> Any:
    """Resolve the Entra signing key for ``token`` (cached per JWKS URI).

    Isolated so tests can patch it and avoid any network access.
    """
    import jwt

    uri = _jwks_uri(tenant_id)
    client = _jwks_clients.get(uri)
    if client is None:
        client = jwt.PyJWKClient(uri)
        _jwks_clients[uri] = client
    return client.get_signing_key_from_jwt(token).key


def _validate_bearer_token(
    token: str, entra: EntraAuthConfig | None
) -> tuple[dict[str, Any] | None, AuthError | None]:
    try:
        import jwt
    except ImportError:  # pragma: no cover - dependency is declared in pyproject
        return None, AuthError(500, "Bearer token validation requires PyJWT.")

    tenant_id = _resolved_tenant_id(entra)
    audiences = _resolved_audiences(entra)
    options: dict[str, Any] = {"require": ["exp"]}
    decode_kwargs: dict[str, Any] = {"algorithms": ["RS256"], "options": options}
    if audiences:
        decode_kwargs["audience"] = audiences
    else:
        options["verify_aud"] = False

    try:
        signing_key = _get_signing_key(token, tenant_id)
        claims = jwt.decode(token, signing_key, **decode_kwargs)
    except Exception:
        return None, AuthError(401, "Invalid bearer token.")

    if not isinstance(claims, dict):
        return None, AuthError(401, "Invalid bearer token payload.")
    return claims, None


def authorize_entra_request(
    get_header: HeaderGetter, auth: EndpointAuthConfig
) -> AuthError | None:
    """Authorize an inbound request against an endpoint auth policy.

    Returns ``None`` when the request is authorized (including for the
    non-``entra`` modes, whose enforcement is handled by the Functions host key
    check), or an :class:`AuthError` describing why it was rejected.
    """
    if auth.mode != "entra":
        return None
    entra = auth.entra

    principal_header = get_header(_EASY_AUTH_PRINCIPAL_HEADER)
    if principal_header:
        principal = _decode_easy_auth_principal(principal_header)
        if principal is None:
            return AuthError(401, "Invalid client principal header.")
        auth_typ = principal.get("auth_typ")
        if not isinstance(auth_typ, str) or auth_typ.lower() not in {"aad", "azureactivedirectory"}:
            return AuthError(401, "Entra authentication required.")
        return _check_allowlists(_flatten_claims(principal), entra)

    authorization = get_header(_AUTHORIZATION_HEADER)
    if authorization and authorization.lower().startswith(_BEARER_PREFIX):
        token = authorization[len(_BEARER_PREFIX) :].strip()
        claims, error = _validate_bearer_token(token, entra)
        if error is not None:
            return error
        assert claims is not None
        return _check_allowlists(_flatten_claims(claims), entra)

    return AuthError(401, "Authentication required.")
