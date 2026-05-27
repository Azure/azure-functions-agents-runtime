from __future__ import annotations

from unittest.mock import patch

import pytest

from azure_functions_agents._credential import build_async_credential
from azure_functions_agents.client_manager import (
    _DEFAULT_FOUNDRY_MODEL,
    _DEFAULT_OPENAI_MODEL,
    MAFClientManager,
)


@pytest.mark.parametrize(
    ("provider", "provider_env", "provider_model"),
    [
        ("azure_openai", "AZURE_OPENAI_DEPLOYMENT", "azure-provider-model"),
        ("foundry", "FOUNDRY_MODEL", "foundry-provider-model"),
    ],
)
def test_resolve_model_requested_wins(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    provider_env: str,
    provider_model: str,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider)
    monkeypatch.setenv(provider_env, provider_model)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "fallback-model")

    assert MAFClientManager().resolve_model("requested-model") == "requested-model"


@pytest.mark.parametrize(
    ("provider", "provider_env", "provider_model"),
    [
        ("azure_openai", "AZURE_OPENAI_DEPLOYMENT", "azure-provider-model"),
        ("foundry", "FOUNDRY_MODEL", "foundry-provider-model"),
    ],
)
def test_resolve_model_prefers_provider_specific_env(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    provider_env: str,
    provider_model: str,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider)
    monkeypatch.setenv(provider_env, provider_model)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "fallback-model")

    assert MAFClientManager().resolve_model(None) == provider_model


@pytest.mark.parametrize(
    ("provider", "provider_env"),
    [
        ("azure_openai", "AZURE_OPENAI_DEPLOYMENT"),
        ("foundry", "FOUNDRY_MODEL"),
        ("openai", None),
    ],
)
def test_resolve_model_uses_runtime_model_as_fallback(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    provider_env: str | None,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "fallback-model")
    if provider_env:
        monkeypatch.delenv(provider_env, raising=False)

    assert MAFClientManager().resolve_model(None) == "fallback-model"


def test_resolve_model_accepts_legacy_maf_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_MODEL", raising=False)
    monkeypatch.setenv("MAF_PROVIDER", "openai")
    monkeypatch.setenv("MAF_MODEL", "legacy-fallback-model")

    assert MAFClientManager().resolve_model(None) == "legacy-fallback-model"


@pytest.mark.parametrize(
    ("provider", "default_model"),
    [
        ("openai", _DEFAULT_OPENAI_MODEL),
        ("azure_openai", _DEFAULT_OPENAI_MODEL),
        ("foundry", _DEFAULT_FOUNDRY_MODEL),
    ],
)
def test_resolve_model_uses_default_when_no_override_exists(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    default_model: str,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_MODEL", raising=False)
    monkeypatch.delenv("MAF_MODEL", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("FOUNDRY_MODEL", raising=False)

    assert MAFClientManager().resolve_model(None) == default_model


def test_build_managed_identity_credential_passes_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-id-123")

    with patch("azure.identity.aio.DefaultAzureCredential") as credential_ctor:
        credential = object()
        credential_ctor.return_value = credential

        assert build_async_credential() is credential

    credential_ctor.assert_called_once_with(managed_identity_client_id="client-id-123")


def test_build_managed_identity_credential_without_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)

    with patch("azure.identity.aio.DefaultAzureCredential") as credential_ctor:
        credential = object()
        credential_ctor.return_value = credential

        assert build_async_credential() is credential

    credential_ctor.assert_called_once_with()
