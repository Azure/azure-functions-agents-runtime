from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from azure.storage.blob import aio

from azure_functions_agents import _credential
from azure_functions_agents._native_session_identity import (
    StorageMode,
    resolve_route,
    session_prefix,
)
from azure_functions_agents._session_storage import BlobStorageSettings, open_blob_service

STORAGE_ENV = (
    "AzureWebJobsStorage",
    "AzureWebJobsStorage__blobServiceUri",
    "AzureWebJobsStorage__clientId",
    "AZURE_CLIENT_ID",
    "AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER",
    "WEBSITE_OWNER_NAME",
    "WEBSITE_DEPLOYMENT_ID",
    "WEBSITE_SITE_NAME",
    "WEBSITE_INSTANCE_ID",
    "AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE",
)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    for name in STORAGE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "sessions"))
    return tmp_path


def test_no_storage_configuration_selects_local_even_on_a_worker(configured, monkeypatch):
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "worker")
    route = resolve_route(configured)
    assert route.mode is StorageMode.LOCAL
    assert route.blob is None
    assert route.local_dir == configured / "sessions"
    assert session_prefix(route, "agent", "session") == "copilot-native/local/agent/session"


@pytest.mark.parametrize("name", ["AzureWebJobsStorage", "AzureWebJobsStorage__blobServiceUri"])
def test_existing_storage_setting_selects_blob_without_a_new_selector(
    configured, monkeypatch, name
):
    monkeypatch.setenv(name, "configured")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE", "local")
    route = resolve_route(configured)
    assert route.mode is StorageMode.BLOB
    assert route.blob is not None
    assert route.blob.container_name == "azure-functions-agents"


def test_storage_precedence_and_values_are_frozen_without_secret_reprs(configured, monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage", "  AccountKey=fixture-secret  ")
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", "https://ignored.invalid")
    monkeypatch.setenv("AzureWebJobsStorage__clientId", " storage-identity ")
    monkeypatch.setenv("AZURE_CLIENT_ID", "app-identity")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER", " session-container ")
    route = resolve_route(configured)
    assert route.blob.connection_string == "AccountKey=fixture-secret"
    assert route.blob.blob_service_url is None
    assert route.blob.client_id == "storage-identity"
    assert route.blob.container_name == "session-container"
    assert "fixture-secret" not in repr(route)
    assert "fixture-secret" not in repr(route.blob)
    monkeypatch.delenv("AzureWebJobsStorage")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "changed")
    assert route.mode is StorageMode.BLOB
    assert session_prefix(route, "agent", "session") == "copilot-native/local/agent/session"
    with pytest.raises(FrozenInstanceError):
        route.correlation_key = "changed"


@pytest.mark.parametrize(
    ("storage_client", "app_client", "expected"),
    [
        (None, None, ""),
        (None, " app ", "app"),
        ("", " app ", "app"),
        ("  ", " app ", ""),
        (" storage ", " app ", "storage"),
    ],
)
def test_storage_specific_client_id_preserves_existing_precedence(
    configured, monkeypatch, storage_client, app_client, expected
):
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", "https://fixture.invalid")
    if storage_client is not None:
        monkeypatch.setenv("AzureWebJobsStorage__clientId", storage_client)
    if app_client is not None:
        monkeypatch.setenv("AZURE_CLIENT_ID", app_client)
    assert resolve_route(configured).blob.client_id == expected


@pytest.mark.parametrize(
    ("owner", "deployment", "site", "expected"),
    [
        (None, None, None, "local"),
        (" Owner+RG ", " Deployment ", "ignored", "owner+rg/deployment"),
        (" Owner+RG ", None, " Site ", "owner+rg/site"),
        (" Owner+RG ", None, None, "owner+rg"),
        (None, " Deployment ", "ignored", "deployment"),
        (None, None, " Site ", "site"),
        ("  ", "\t", " Site ", "site"),
    ],
)
def test_native_paths_reuse_shared_readable_identity_once(
    configured, monkeypatch, owner, deployment, site, expected
):
    for name, value in (
        ("WEBSITE_OWNER_NAME", owner),
        ("WEBSITE_DEPLOYMENT_ID", deployment),
        ("WEBSITE_SITE_NAME", site),
    ):
        if value is not None:
            monkeypatch.setenv(name, value)
    route = resolve_route(configured)
    assert session_prefix(route, "agent_one", "one.test") == (
        f"copilot-native/{expected}/agent_one/one.test"
    )


@pytest.mark.parametrize("session_id", [None, ".", "..", "../outside", "/absolute", "", "x" * 129])
def test_session_identity_is_validated_before_becoming_a_path(configured, session_id):
    with pytest.raises(ValueError, match="session"):
        session_prefix(resolve_route(configured), "agent", session_id)


@pytest.mark.parametrize("slug", ["", "../agent", "with-hyphens", "with/slashes", "agent\n"])
def test_agent_slug_is_validated_before_becoming_a_path(configured, slug):
    with pytest.raises(ValueError, match="agent_slug"):
        session_prefix(resolve_route(configured), slug, "session")


@pytest.mark.parametrize("site", ["../escape", "/absolute", "path\\escape", "path//escape"])
def test_unsafe_shared_correlation_segments_are_not_adopted(configured, monkeypatch, site):
    monkeypatch.setenv("WEBSITE_SITE_NAME", site)
    with pytest.raises(ValueError, match="identity"):
        session_prefix(resolve_route(configured), "agent", "session")


def test_blank_storage_settings_remain_unconfigured(configured, monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage", " ")
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", "\t")
    assert resolve_route(configured).mode is StorageMode.LOCAL


@pytest.mark.asyncio
async def test_owned_blob_construction_uses_only_its_frozen_identity(configured, monkeypatch):
    calls = []
    credential = SimpleNamespace(close=AsyncMock())
    service = SimpleNamespace(close=AsyncMock())

    def build_credential(client_id):
        calls.append(("credential", client_id))
        return credential

    def build_service(**kwargs):
        calls.append(("service", kwargs))
        return service

    monkeypatch.setattr(_credential, "build_async_credential_with_client_id", build_credential)
    monkeypatch.setattr(aio, "BlobServiceClient", build_service)
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", " https://fixture.invalid ")
    monkeypatch.setenv("AzureWebJobsStorage__clientId", " frozen-identity ")
    route = resolve_route(configured)
    monkeypatch.setenv("AzureWebJobsStorage__clientId", "changed")
    owned = await open_blob_service(route.blob)
    assert calls == [
        ("credential", "frozen-identity"),
        ("service", {"account_url": "https://fixture.invalid", "credential": credential}),
    ]
    await owned.close()
    service.close.assert_awaited_once()
    credential.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_owned_blob_constructor_failure_closes_its_credential(monkeypatch):
    credential = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(
        _credential, "build_async_credential_with_client_id", lambda _client_id: credential
    )

    def fail(**_kwargs):
        raise ValueError("fixture configuration failure")

    monkeypatch.setattr(aio, "BlobServiceClient", fail)
    with pytest.raises(ValueError):
        await open_blob_service(BlobStorageSettings(blob_service_url="https://fixture.invalid"))
    credential.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_owned_blob_connection_string_uses_no_identity_builder(monkeypatch):
    service = SimpleNamespace(close=AsyncMock())
    client = SimpleNamespace(from_connection_string=lambda connection: service)
    monkeypatch.setattr(aio, "BlobServiceClient", client)

    def denied(_client_id):
        raise AssertionError("Connection strings must not construct an identity")

    monkeypatch.setattr(_credential, "build_async_credential_with_client_id", denied)
    owned = await open_blob_service(BlobStorageSettings(
        connection_string="fixture-connection", blob_service_url="https://ignored.invalid"
    ))
    assert owned.service is service and owned.credential is None
    await owned.close()
    service.close.assert_awaited_once()
