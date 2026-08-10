from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from azure_functions_agents._credential import build_async_credential
from azure_functions_agents.client_manager import (
    _DEFAULT_FOUNDRY_MODEL,
    _DEFAULT_OPENAI_MODEL,
    ClientManager,
    InferenceTarget,
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


@pytest.mark.parametrize(
    ("provider", "builder", "endpoint_name", "endpoint"),
    [
        ("openai", "_build_openai", None, None),
        (
            "azure_openai",
            "_build_azure_openai",
            "AZURE_OPENAI_ENDPOINT",
            "https://account.openai.azure.com/openai/deployments/private?api-version=secret",
        ),
        (
            "foundry",
            "_build_foundry",
            "FOUNDRY_PROJECT_ENDPOINT",
            "https://user:password@project.services.ai.azure.com:443/api/projects/private",
        ),
    ],
)
def test_build_chat_client_with_target_matches_client_branch(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    builder: str,
    endpoint_name: str | None,
    endpoint: str | None,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider)
    if endpoint_name and endpoint:
        monkeypatch.setenv(endpoint_name, endpoint)
    client = object()

    with patch.object(MAFClientManager, builder, return_value=client) as build:
        built_client, target = MAFClientManager().build_chat_client_with_target("model-one")

    assert built_client is client
    build.assert_called_once_with("model-one")
    assert target == InferenceTarget(provider, "model-one")
    assert not hasattr(target, "inference_host")


def test_maf_target_uses_one_provider_and_model_resolution_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    client = object()

    with (
        patch.object(
            MAFClientManager, "_provider", return_value=MAFProvider.FOUNDRY
        ) as provider,
        patch.object(
            MAFClientManager, "_resolve_model", return_value="resolved-model"
        ) as resolve,
        patch.object(MAFClientManager, "_build_foundry", return_value=client),
    ):
        built_client, target = MAFClientManager().build_chat_client_with_target("requested-model")

    assert built_client is client
    assert target.provider == "foundry"
    assert target.model == "resolved-model"
    provider.assert_called_once_with()
    resolve.assert_called_once_with("requested-model", MAFProvider.FOUNDRY)


def test_maf_target_preserves_custom_model_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    provider_client = object()

    class CustomModelManager(MAFClientManager):
        def resolve_model(self, requested: str | None) -> str:
            assert requested is None
            return "custom-deployment"

    with patch.object(
        MAFClientManager,
        "_build_openai",
        return_value=provider_client,
    ) as build:
        client, target = CustomModelManager().build_chat_client_with_target(None)

    assert client is provider_client
    build.assert_called_once_with("custom-deployment")
    assert target == InferenceTarget("openai", "custom-deployment")


def test_custom_manager_target_fallback_builds_client_once() -> None:
    class CustomManager(ClientManager):
        calls = 0

        def resolve_model(self, requested: str | None) -> str:
            return requested or "custom-model"

        def build_chat_client(self, model: str | None) -> Any:
            self.calls += 1
            return object()

    manager = CustomManager()

    client, target = manager.build_chat_client_with_target("custom-model")

    assert client is not None
    assert manager.calls == 1
    assert target == InferenceTarget()


def test_maf_subclass_build_chat_client_override_keeps_virtual_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    provider_client = object()

    class WrappingMAFClientManager(MAFClientManager):
        calls = 0

        def build_chat_client(self, model: str | None) -> Any:
            self.calls += 1
            return ("wrapped", super().build_chat_client(model))

    manager = WrappingMAFClientManager()

    with patch.object(MAFClientManager, "_build_openai", return_value=provider_client) as build:
        client, target = manager.build_chat_client_with_target("model-one")

    assert client == ("wrapped", provider_client)
    assert manager.calls == 1
    build.assert_called_once_with("model-one")
    assert target == InferenceTarget()


def test_anthropic_api_key_does_not_select_direct_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")

    with pytest.raises(RuntimeError, match="No MAF provider configured"):
        MAFClientManager().build_chat_client_with_target(None)


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
