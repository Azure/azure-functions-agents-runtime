"""Tests for built-in endpoint authentication enforcement.

Entra enforcement is delegated to App Service Authentication (Easy Auth); the
runtime only trusts the platform-injected ``X-MS-CLIENT-PRINCIPAL`` header and
applies the configured allow-lists. There is no in-app token validation.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import azure.functions as func
import pytest

from azure_functions_agents.config.schema import EndpointAuthConfig, EntraAuthConfig
from azure_functions_agents.registration._auth import (
    authorize_entra_request,
    resolve_endpoint_auth_level,
)

_ENV_KEYS = (
    "AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH",
    "WEBSITE_AUTH_ENABLED",
)


@pytest.fixture(autouse=True)
def _entra_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Default posture for these tests: Easy Auth is enforced in front of the app,
    # so the injected principal header is trusted. Tests covering the fail-closed
    # gate clear this explicitly.
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")


def _header_getter(headers: dict[str, str]) -> Any:
    lowered = {k.lower(): v for k, v in headers.items()}
    return lambda name: lowered.get(name.lower())


def _principal_header(claims: list[dict[str, str]], *, auth_typ: str = "aad") -> str:
    payload = json.dumps({"auth_typ": auth_typ, "claims": claims})
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


# --- resolve_endpoint_auth_level --------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("function", func.AuthLevel.FUNCTION),
        ("admin", func.AuthLevel.ADMIN),
        ("anonymous", func.AuthLevel.ANONYMOUS),
        ("entra", func.AuthLevel.ANONYMOUS),
    ],
)
def test_resolve_endpoint_auth_level(mode: str, expected: func.AuthLevel) -> None:
    assert resolve_endpoint_auth_level(EndpointAuthConfig(mode=mode)) == expected  # type: ignore[arg-type]


# --- non-entra modes skip in-app enforcement --------------------------------


@pytest.mark.parametrize("mode", ["function", "admin", "anonymous"])
def test_non_entra_modes_are_not_enforced_in_app(mode: str) -> None:
    auth = EndpointAuthConfig(mode=mode)  # type: ignore[arg-type]
    assert authorize_entra_request(_header_getter({}), auth) is None


# --- entra: fail closed without a validated principal -----------------------


def test_entra_missing_principal_is_unauthorized() -> None:
    auth = EndpointAuthConfig(mode="entra")
    error = authorize_entra_request(_header_getter({}), auth)
    assert error is not None
    assert error.status_code == 401


def test_entra_bearer_token_alone_is_unauthorized() -> None:
    # A raw bearer token is not trusted: only Easy Auth-validated principals are.
    auth = EndpointAuthConfig(mode="entra")
    headers = {"Authorization": "Bearer some.jwt.token"}
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 401


# --- entra: refuse to trust the principal header without Easy Auth -----------


def test_entra_without_easy_auth_rejects_spoofed_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When Easy Auth is not enforced, X-MS-CLIENT-PRINCIPAL is caller-controlled
    # and must not be trusted even if it is a well-formed aad principal.
    monkeypatch.delenv("WEBSITE_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH", raising=False)
    auth = EndpointAuthConfig(mode="entra")
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal_header([{"typ": "tid", "val": "t-1"}])}
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 401


def test_entra_explicit_easy_auth_assertion_enables_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The platform signal is absent, but the operator asserts Easy Auth via the
    # app setting, so the injected principal is trusted.
    monkeypatch.delenv("WEBSITE_AUTH_ENABLED", raising=False)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH", "true")
    auth = EndpointAuthConfig(mode="entra")
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal_header([{"typ": "tid", "val": "t-1"}])}
    assert authorize_entra_request(_header_getter(headers), auth) is None


# --- entra: Easy Auth principal ---------------------------------------------


def test_entra_easy_auth_principal_authorized() -> None:
    auth = EndpointAuthConfig(mode="entra")
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal_header([{"typ": "tid", "val": "t-1"}])}
    assert authorize_entra_request(_header_getter(headers), auth) is None


def test_entra_easy_auth_invalid_principal_header_is_unauthorized() -> None:
    auth = EndpointAuthConfig(mode="entra")
    headers = {"X-MS-CLIENT-PRINCIPAL": "not-base64-json!!!"}
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 401


def test_entra_easy_auth_non_aad_principal_is_unauthorized() -> None:
    auth = EndpointAuthConfig(mode="entra")
    headers = {
        "X-MS-CLIENT-PRINCIPAL": _principal_header(
            [{"typ": "tid", "val": "t-1"}], auth_typ="google"
        )
    }
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 401


def test_entra_easy_auth_tenant_allowlist_match() -> None:
    auth = EndpointAuthConfig(mode="entra", entra=EntraAuthConfig(tenant_id="t-1"))
    claims = [
        {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": "t-1"},
    ]
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal_header(claims)}
    assert authorize_entra_request(_header_getter(headers), auth) is None


def test_entra_easy_auth_tenant_allowlist_mismatch_is_forbidden() -> None:
    auth = EndpointAuthConfig(mode="entra", entra=EntraAuthConfig(tenant_id="t-1"))
    claims = [{"typ": "tid", "val": "other-tenant"}]
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal_header(claims)}
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 403


def test_entra_easy_auth_audience_allowlist_mismatch_is_forbidden() -> None:
    auth = EndpointAuthConfig(mode="entra", entra=EntraAuthConfig(allowed_audiences=["api://app"]))
    claims = [{"typ": "aud", "val": "api://other"}]
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal_header(claims)}
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 403


def test_entra_easy_auth_client_id_allowlist_mismatch_is_forbidden() -> None:
    auth = EndpointAuthConfig(mode="entra", entra=EntraAuthConfig(allowed_client_ids=["app-a"]))
    claims = [{"typ": "appid", "val": "app-b"}]
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal_header(claims)}
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 403


def test_entra_allowlists_come_only_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # The runtime no longer reads dedicated AZURE_FUNCTIONS_AGENTS_ENTRA_* fallback
    # variables — allow-lists are sourced solely from the authored config (authors
    # keep secrets out of source via $VAR/%VAR% frontmatter substitution). Setting
    # the old env var must NOT constrain a request whose config declares no tenant.
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENTRA_TENANT_ID", "t-env")
    auth = EndpointAuthConfig(mode="entra")
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal_header([{"typ": "tid", "val": "t-other"}])}
    assert authorize_entra_request(_header_getter(headers), auth) is None


def test_entra_config_tenant_is_enforced_regardless_of_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A configured allow-list is enforced from config alone; the legacy env var is
    # ignored and does not widen or override it.
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENTRA_TENANT_ID", "t-env")
    auth = EndpointAuthConfig(mode="entra", entra=EntraAuthConfig(tenant_id="t-config"))
    ok = {"X-MS-CLIENT-PRINCIPAL": _principal_header([{"typ": "tid", "val": "t-config"}])}
    assert authorize_entra_request(_header_getter(ok), auth) is None
    bad = {"X-MS-CLIENT-PRINCIPAL": _principal_header([{"typ": "tid", "val": "t-env"}])}
    error = authorize_entra_request(_header_getter(bad), auth)
    assert error is not None
    assert error.status_code == 403

