from __future__ import annotations

from unittest import mock

import pytest

from azure_functions_agents._credential import build_credential


@pytest.mark.parametrize("client_id", ["test-client-id", "00000000-0000-0000-0000-000000000000"])
def test_build_credential_passes_managed_identity_client_id_when_env_set(
    monkeypatch: pytest.MonkeyPatch, client_id: str
) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", client_id)

    credential = object()
    with mock.patch("azure.identity.DefaultAzureCredential", return_value=credential) as factory:
        assert build_credential() is credential

    factory.assert_called_once_with(managed_identity_client_id=client_id)


@pytest.mark.parametrize("client_id", [None, ""])
def test_build_credential_returns_bare_default_credential_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, client_id: str | None
) -> None:
    if client_id is None:
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    else:
        monkeypatch.setenv("AZURE_CLIENT_ID", client_id)

    credential = object()
    with mock.patch("azure.identity.DefaultAzureCredential", return_value=credential) as factory:
        assert build_credential() is credential

    factory.assert_called_once_with()
