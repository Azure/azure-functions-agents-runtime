"""Inbound authentication enforcement for built-in endpoints.

This module is the only place that reasons about *who* may call an agent's
built-in HTTP endpoints. It maps the authoring-level ``builtin_endpoints.auth``
policy onto an Azure Functions ``AuthLevel`` (for native function/system-key
"API key" auth) and, for Entra ID, checks the caller's identity before the
runner is ever invoked.

Entra ID enforcement is delegated entirely to **App Service Authentication
(Easy Auth)**. The platform validates the Entra-issued token (bearer or cookie),
and injects a validated ``X-MS-CLIENT-PRINCIPAL`` header. The runtime trusts that
header and applies the configured tenant/audience/client-id allow-lists as
defense-in-depth. The runtime never parses or validates a JWT itself; a request
in ``entra`` mode without a validated principal is rejected (fail closed).
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
    rejected (fail closed).
    """
    if auth.mode != "entra":
        return None
    entra = auth.entra

    principal_header = get_header(_EASY_AUTH_PRINCIPAL_HEADER)
    if not principal_header:
        return AuthError(401, "Entra authentication required (App Service Authentication).")

    principal = _decode_easy_auth_principal(principal_header)
    if principal is None:
        return AuthError(401, "Invalid client principal header.")

    auth_typ = principal.get("auth_typ")
    if not isinstance(auth_typ, str) or auth_typ.lower() not in {"aad", "azureactivedirectory"}:
        return AuthError(401, "Entra authentication required.")

    return _check_allowlists(_flatten_claims(principal), entra)
