from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from azure.storage.blob import aio

from azure_functions_agents import _credential
from azure_functions_agents.harness import _session_storage as storage


@pytest.fixture
def storage_environment(monkeypatch):
    for name in (
        "AzureWebJobsStorage",
        "AzureWebJobsStorage__blobServiceUri",
        "AzureWebJobsStorage__clientId",
        "AZURE_CLIENT_ID",
        "AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER",
    ):
        monkeypatch.delenv(name, raising=False)


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
    storage_environment, monkeypatch, storage_client, app_client, expected
):
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", "https://fixture.invalid")
    if storage_client is not None:
        monkeypatch.setenv("AzureWebJobsStorage__clientId", storage_client)
    if app_client is not None:
        monkeypatch.setenv("AZURE_CLIENT_ID", app_client)
    settings = storage.blob_storage_from_environment()
    assert settings is not None
    assert settings.client_id == expected


@pytest.mark.asyncio
async def test_owned_blob_construction_uses_only_its_frozen_identity(
    storage_environment, monkeypatch
):
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
    settings = storage.blob_storage_from_environment()
    assert settings is not None
    monkeypatch.setenv("AzureWebJobsStorage__clientId", "changed")
    owned = await storage.open_blob_service(settings)
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
        await storage.open_blob_service(
            storage.BlobStorageSettings(blob_service_url="https://fixture.invalid")
        )
    credential.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_owned_blob_connection_string_uses_no_identity_builder(monkeypatch):
    service = SimpleNamespace(close=AsyncMock())
    client = SimpleNamespace(from_connection_string=lambda connection: service)
    monkeypatch.setattr(aio, "BlobServiceClient", client)

    def denied(_client_id):
        raise AssertionError("Connection strings must not construct an identity")

    monkeypatch.setattr(_credential, "build_async_credential_with_client_id", denied)
    owned = await storage.open_blob_service(storage.BlobStorageSettings(
        connection_string="fixture-connection", blob_service_url="https://ignored.invalid"
    ))
    assert owned.service is service and owned.credential is None
    await owned.close()
    service.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("container_override", "environment_container", "expected"),
    [
        ("explicit", "environment", "explicit"),
        (" explicit ", "environment", " explicit "),
        ("", " environment ", "environment"),
        (None, " \t ", storage.DEFAULT_CONTAINER_NAME),
    ],
)
def test_explicit_container_retains_existing_override_and_blank_semantics(
    monkeypatch, container_override, environment_container, expected
):
    monkeypatch.setenv("AzureWebJobsStorage", "fixture")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER", environment_container)
    settings = storage.blob_storage_from_environment(container_name=container_override)
    assert settings is not None
    assert settings.container_name == expected


@pytest.mark.asyncio
async def test_owned_service_failure_still_closes_credential():
    failure = OSError("fixture service close failure")
    service = SimpleNamespace(close=AsyncMock(side_effect=failure))
    credential = SimpleNamespace(close=AsyncMock())
    owned = storage.OwnedBlobService(service, credential)

    with pytest.raises(OSError) as caught:
        await owned.close()

    assert caught.value is failure
    service.close.assert_awaited_once()
    credential.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_blob_settings_fail_before_resource_construction(monkeypatch):
    service = Mock()
    credential = Mock()
    monkeypatch.setattr(aio, "BlobServiceClient", service)
    monkeypatch.setattr(_credential, "build_async_credential_with_client_id", credential)

    with pytest.raises(ValueError, match="connection string or service URI"):
        await storage.open_blob_service(storage.BlobStorageSettings())

    service.assert_not_called()
    credential.assert_not_called()


@pytest.mark.asyncio
async def test_configured_connection_failure_does_not_fall_back_to_identity(monkeypatch):
    failure = ValueError("fixture invalid connection")
    service = Mock()
    service.from_connection_string.side_effect = failure
    credential = Mock()
    monkeypatch.setattr(aio, "BlobServiceClient", service)
    monkeypatch.setattr(_credential, "build_async_credential_with_client_id", credential)
    settings = storage.BlobStorageSettings(
        connection_string="fixture-invalid",
        blob_service_url="https://ignored.invalid",
    )

    with pytest.raises(ValueError) as caught:
        await storage.open_blob_service(settings)

    assert caught.value is failure
    service.from_connection_string.assert_called_once_with("fixture-invalid")
    service.assert_not_called()
    credential.assert_not_called()


def test_storage_error_keeps_http_status_and_oserror_contract():
    error = storage.SessionStorageError(5, "fixture sanitized failure")
    assert isinstance(error, OSError)
    assert error.errno == 5
    assert error.status_code == 503
