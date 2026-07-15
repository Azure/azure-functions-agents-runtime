"""Tests for built-in endpoint authentication enforcement."""

from __future__ import annotations

import base64
import datetime
import json
from typing import Any

import azure.functions as func
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from azure_functions_agents.config.schema import EndpointAuthConfig, EntraAuthConfig
from azure_functions_agents.registration import _auth
from azure_functions_agents.registration._auth import (
    authorize_entra_request,
    resolve_endpoint_auth_level,
)

_ENV_KEYS = (
    "AZURE_FUNCTIONS_AGENTS_ENTRA_TENANT_ID",
    "AZURE_FUNCTIONS_AGENTS_ENTRA_AUDIENCES",
    "AZURE_FUNCTIONS_AGENTS_ENTRA_CLIENT_IDS",
)


@pytest.fixture(autouse=True)
def _clear_entra_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _header_getter(headers: dict[str, str]) -> Any:
    lowered = {k.lower(): v for k, v in headers.items()}
    return lambda name: lowered.get(name.lower())


def _principal_header(claims: list[dict[str, str]]) -> str:
    payload = json.dumps({"auth_typ": "aad", "claims": claims})
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


class _RsaKeys:
    def __init__(self) -> None:
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = self._private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self.public_pem = self._private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def token(self, **claims: Any) -> str:
        return jwt.encode(claims, self.private_pem, algorithm="RS256")


@pytest.fixture
def rsa_keys() -> _RsaKeys:
    return _RsaKeys()


@pytest.fixture
def stub_signing_key(monkeypatch: pytest.MonkeyPatch, rsa_keys: _RsaKeys) -> _RsaKeys:
    monkeypatch.setattr(_auth, "_get_signing_key", lambda token, tenant_id: rsa_keys.public_pem)
    return rsa_keys


def _future_exp(minutes: int = 10) -> int:
    now = datetime.datetime.now(tz=datetime.UTC)
    return int((now + datetime.timedelta(minutes=minutes)).timestamp())


def _past_exp(minutes: int = 10) -> int:
    now = datetime.datetime.now(tz=datetime.UTC)
    return int((now - datetime.timedelta(minutes=minutes)).timestamp())


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


# --- entra: missing credentials ---------------------------------------------


def test_entra_missing_credentials_is_unauthorized() -> None:
    auth = EndpointAuthConfig(mode="entra")
    error = authorize_entra_request(_header_getter({}), auth)
    assert error is not None
    assert error.status_code == 401


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


def test_entra_easy_auth_client_id_allowlist_mismatch_is_forbidden() -> None:
    auth = EndpointAuthConfig(mode="entra", entra=EntraAuthConfig(allowed_client_ids=["app-a"]))
    claims = [{"typ": "appid", "val": "app-b"}]
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal_header(claims)}
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 403


def test_entra_allowlists_fall_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENTRA_TENANT_ID", "t-env")
    auth = EndpointAuthConfig(mode="entra")
    ok = {"X-MS-CLIENT-PRINCIPAL": _principal_header([{"typ": "tid", "val": "t-env"}])}
    assert authorize_entra_request(_header_getter(ok), auth) is None
    bad = {"X-MS-CLIENT-PRINCIPAL": _principal_header([{"typ": "tid", "val": "t-other"}])}
    error = authorize_entra_request(_header_getter(bad), auth)
    assert error is not None
    assert error.status_code == 403


# --- entra: bearer token ----------------------------------------------------


def test_entra_bearer_token_authorized(stub_signing_key: _RsaKeys) -> None:
    auth = EndpointAuthConfig(mode="entra")
    token = stub_signing_key.token(tid="t-1", exp=_future_exp())
    headers = {"Authorization": f"Bearer {token}"}
    assert authorize_entra_request(_header_getter(headers), auth) is None


def test_entra_bearer_token_tenant_mismatch_is_forbidden(stub_signing_key: _RsaKeys) -> None:
    auth = EndpointAuthConfig(mode="entra", entra=EntraAuthConfig(tenant_id="t-1"))
    token = stub_signing_key.token(tid="t-2", exp=_future_exp())
    headers = {"Authorization": f"Bearer {token}"}
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 403


def test_entra_bearer_token_audience_match(stub_signing_key: _RsaKeys) -> None:
    auth = EndpointAuthConfig(
        mode="entra", entra=EntraAuthConfig(allowed_audiences=["api://app"])
    )
    token = stub_signing_key.token(aud="api://app", exp=_future_exp())
    headers = {"Authorization": f"Bearer {token}"}
    assert authorize_entra_request(_header_getter(headers), auth) is None


def test_entra_bearer_token_audience_mismatch_is_unauthorized(stub_signing_key: _RsaKeys) -> None:
    auth = EndpointAuthConfig(
        mode="entra", entra=EntraAuthConfig(allowed_audiences=["api://app"])
    )
    token = stub_signing_key.token(aud="api://other", exp=_future_exp())
    headers = {"Authorization": f"Bearer {token}"}
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 401


def test_entra_bearer_token_expired_is_unauthorized(stub_signing_key: _RsaKeys) -> None:
    auth = EndpointAuthConfig(mode="entra")
    token = stub_signing_key.token(tid="t-1", exp=_past_exp())
    headers = {"Authorization": f"Bearer {token}"}
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 401


def test_entra_bearer_token_wrong_signature_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch, rsa_keys: _RsaKeys
) -> None:
    other = _RsaKeys()
    monkeypatch.setattr(_auth, "_get_signing_key", lambda token, tenant_id: other.public_pem)
    auth = EndpointAuthConfig(mode="entra")
    token = rsa_keys.token(tid="t-1", exp=_future_exp())
    headers = {"Authorization": f"Bearer {token}"}
    error = authorize_entra_request(_header_getter(headers), auth)
    assert error is not None
    assert error.status_code == 401
