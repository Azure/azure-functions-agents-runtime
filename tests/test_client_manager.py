from __future__ import annotations

from unittest.mock import patch

import pytest

from azure_functions_agents._credential import build_async_credential
from azure_functions_agents.client_manager import (
    _DEFAULT_FOUNDRY_MODEL,
    _DEFAULT_OPENAI_MODEL,
    MAFClientManager,
    MAFProvider,
    resolve_maf_provider,
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
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("FOUNDRY_MODEL", raising=False)

    assert MAFClientManager().resolve_model(None) == default_model


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        (
            {
                "AZURE_FUNCTIONS_AGENTS_PROVIDER": "foundry",
                "AZURE_OPENAI_ENDPOINT": "https://azure.example",
                "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.example",
                "OPENAI_API_KEY": "openai-key",
            },
            MAFProvider.FOUNDRY,
        ),
        (
            {
                "AZURE_OPENAI_ENDPOINT": "https://azure.example",
                "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.example",
                "OPENAI_API_KEY": "openai-key",
            },
            MAFProvider.AZURE_OPENAI,
        ),
        (
            {
                "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.example",
                "OPENAI_API_KEY": "openai-key",
            },
            MAFProvider.FOUNDRY,
        ),
        (
            {
                "AZURE_FUNCTIONS_AGENTS_SANDBOXENV_AZURE_OPENAI_ENDPOINT": "https://azure.example",
                "OPENAI_API_KEY": "openai-key",
            },
            MAFProvider.AZURE_OPENAI,
        ),
        ({"OPENAI_API_KEY": "openai-key"}, MAFProvider.OPENAI),
    ],
)
def test_resolve_maf_provider_matches_manager_precedence(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    expected: MAFProvider,
) -> None:
    names = (
        "AZURE_FUNCTIONS_AGENTS_PROVIDER",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_FUNCTIONS_AGENTS_SANDBOXENV_AZURE_OPENAI_ENDPOINT",
        "FOUNDRY_PROJECT_ENDPOINT",
        "OPENAI_API_KEY",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert resolve_maf_provider(environment) == expected
    assert MAFClientManager()._provider() == expected


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("openai", MAFProvider.OPENAI),
        ("AZURE_OPENAI", MAFProvider.AZURE_OPENAI),
        ("Foundry", MAFProvider.FOUNDRY),
    ],
)
def test_resolve_maf_provider_parses_explicit_values(
    configured: str,
    expected: MAFProvider,
) -> None:
    assert resolve_maf_provider({"AZURE_FUNCTIONS_AGENTS_PROVIDER": configured}) is expected


def test_resolve_maf_provider_rejects_unknown_explicit_value() -> None:
    with pytest.raises(
        RuntimeError,
        match=(
            r"Unknown AZURE_FUNCTIONS_AGENTS_PROVIDER 'other'\. "
            r"Use one of: openai, azure_openai, foundry\."
        ),
    ):
        resolve_maf_provider({"AZURE_FUNCTIONS_AGENTS_PROVIDER": "other"})


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
