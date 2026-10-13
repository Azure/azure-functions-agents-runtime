from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from azure.core.credentials import AccessToken

from azure_functions_agents.harness import (
    _harness_binding as _harness,
)
from azure_functions_agents.harness import (
    _harness_lifecycle as lifecycle,
)
from azure_functions_agents.harness._harness_binding import (
    AppHarness,
    HarnessKind,
    UnsupportedCapabilityError,
)
from azure_functions_agents.harness._provider_config import ProviderKind
from azure_functions_agents.harness.agent_framework import _maf_client
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_execution as execution,
)
from azure_functions_agents.harness.copilot_sdk import (
    _copilot_runtime as runtime,
)
from azure_functions_agents.harness.copilot_sdk._copilot_preview import CopilotPreviewError
from azure_functions_agents.harness.copilot_sdk._copilot_providers import (
    _PROVIDERS,
    AzureOpenAIProvider,
    FoundryProvider,
    OpenAIProvider,
)
from azure_functions_agents.harness.copilot_sdk._copilot_session_identity import resolve_route
from tests.test_copilot_preview import preview as preview


@pytest.fixture
def binding(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle, "_SHUTDOWN_CALLBACKS", set())
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri", raising=False)
    return AppHarness(
        HarnessKind.COPILOT, tmp_path, tmp_path / "state", "gpt-4.1-mini",
        OpenAIProvider("not-a-credential"), session_storage=resolve_route(tmp_path),
    )


@pytest.mark.parametrize("endpoint", [
    "", "http://fixture.services.ai.azure.com/api/projects/test",
    "https://fixture.services.ai.azure.com/api/projects/test?api-key=do-not-log",
    "https://user:do-not-log@fixture.services.ai.azure.com/api/projects/test",
    "https://fixture.services.ai.azure.com:do-not-log/api/projects/test",
    "https://fixture.example/api/projects/test",
])
def test_invalid_foundry_endpoint_never_echoes_credentials(preview, monkeypatch, endpoint):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", endpoint)
    with pytest.raises(UnsupportedCapabilityError) as error:
        _harness.get_harness()
    assert "do-not-log" not in str(error.value)


@pytest.mark.parametrize(
    ("provider", "settings", "expected_model"),
    [
        ("openai", {"OPENAI_API_KEY": "sentinel-not-a-secret"}, "runtime-model"),
        ("azure_openai", {
            "AZURE_OPENAI_ENDPOINT": "https://fixture.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT": "azure-deployment",
        }, "azure-deployment"),
        ("foundry", {
            "FOUNDRY_PROJECT_ENDPOINT": "https://fixture.services.ai.azure.com/api/projects/test",
            "FOUNDRY_MODEL": "foundry-deployment",
        }, "foundry-deployment"),
    ],
)
def test_provider_target_is_resolved_without_maf_client_construction(
    preview, monkeypatch, provider, settings, expected_model,
):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "runtime-model")
    for name in (
        "AZURE_FUNCTIONS_AGENTS_PROVIDER", "AZURE_OPENAI_ENDPOINT",
        "FOUNDRY_PROJECT_ENDPOINT", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    maf = Mock(side_effect=AssertionError("MAF client construction must not run"))
    monkeypatch.setattr(_maf_client, "build_chat_client", maf)
    selected = _harness.get_harness()
    assert selected.provider is not None
    assert selected.provider.kind == provider
    assert selected.default_model == expected_model
    maf.assert_not_called()


@pytest.mark.parametrize(
    ("provider", "settings", "diagnostic"),
    [
        ("openai", {}, "OPENAI_API_KEY"),
        ("azure_openai", {"AZURE_OPENAI_ENDPOINT": "https://fixture.openai.azure.com/path"}, "host-only"),
        ("azure_openai", {
            "AZURE_OPENAI_ENDPOINT": "https://fixture.openai.azure.com",
            "AZURE_OPENAI_API_VERSION": "2024-10-21?api-key=do-not-log",
        }, "API_VERSION"),
        ("unsupported", {}, "openai.*azure_openai.*foundry"),
    ],
)
def test_invalid_provider_settings_are_sanitized(preview, monkeypatch, provider, settings, diagnostic):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(UnsupportedCapabilityError, match=diagnostic) as error:
        _harness.get_harness()
    assert "do-not-log" not in str(error.value)


def test_copilot_provider_registry_freezes_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fixture.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-secret")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://fixture.services.ai.azure.com/api/projects/test")
    openai = _PROVIDERS[ProviderKind.OPENAI].from_environment()
    azure = _PROVIDERS[ProviderKind.AZURE_OPENAI].from_environment()
    foundry = _PROVIDERS[ProviderKind.FOUNDRY].from_environment()
    assert isinstance(openai, OpenAIProvider)
    assert isinstance(azure, AzureOpenAIProvider)
    assert isinstance(foundry, FoundryProvider)
    assert azure.api_version == "2024-10-21"
    assert azure.auth_label == "AZURE_OPENAI_API_KEY"
    for provider in (openai, azure, foundry):
        assert "secret" not in repr(provider)
        assert "fixture" not in repr(provider)


def test_azure_provider_auth_mode_is_frozen(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fixture.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "frozen-secret")
    provider = AzureOpenAIProvider.from_environment()
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    assert provider.auth_label == "AZURE_OPENAI_API_KEY"
    assert provider.api_key == "frozen-secret"


def test_foundry_configuration_is_frozen_without_authentication(preview, monkeypatch):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://fixture.services.ai.azure.com/api/projects/test")
    monkeypatch.setenv("FOUNDRY_MODEL", "deployed-model")
    selected = _harness.get_harness()
    assert selected.provider is not None
    assert selected.provider.kind == "foundry"
    assert selected.default_model == "deployed-model"
    assert "fixture.services.ai.azure.com" not in repr(selected)
    assert not selected.storage_root.exists()


@pytest.mark.asyncio
async def test_openai_provider_callback_uses_frozen_startup_key(binding, monkeypatch):
    owner = runtime.get_runtime(binding)
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-one")
    provider = execution._provider(binding, owner, "per-agent-model")
    assert provider == {
        "type": "openai",
        "wire_api": "responses",
        "base_url": "https://api.openai.com/v1",
        "model_id": "per-agent-model",
        "wire_model": "per-agent-model",
        "bearer_token_provider": provider["bearer_token_provider"],
    }
    callback = provider["bearer_token_provider"]
    assert await callback(SimpleNamespace()) == "not-a-credential"
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-two")
    assert await callback(SimpleNamespace()) == "not-a-credential"


def test_azure_api_key_provider_avoids_credential_construction(binding, monkeypatch):
    selected = replace(binding, provider=AzureOpenAIProvider(
        "https://fixture.openai.azure.com", "2024-10-21", "sentinel-not-a-secret",
    ))
    monkeypatch.setattr(
        runtime, "build_async_credential",
        Mock(side_effect=AssertionError("Azure API key must not build a credential")),
    )
    provider = execution._provider(selected, runtime.get_runtime(selected), "azure-deployment")
    assert provider == {
        "type": "azure",
        "wire_api": "responses",
        "base_url": "https://fixture.openai.azure.com",
        "model_id": "azure-deployment",
        "wire_model": "azure-deployment",
        "azure": {"api_version": "2024-10-21"},
        "api_key": "sentinel-not-a-secret",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "endpoint", "scope"),
    [
        (AzureOpenAIProvider, "https://fixture.openai.azure.com", "https://cognitiveservices.azure.com/.default"),
        (FoundryProvider, "https://fixture.services.ai.azure.com/api/projects/test", "https://ai.azure.com/.default"),
    ],
)
async def test_entra_callbacks_refresh_overlap_and_close(binding, monkeypatch, provider, endpoint, scope):
    selected = replace(binding, provider=provider(endpoint))
    issued = 0

    async def get_token(received_scope):
        nonlocal issued
        assert received_scope == scope
        issued += 1
        current = issued
        await asyncio.sleep(0)
        return AccessToken(f"sentinel-token-{current}", 9999999999)

    credential = SimpleNamespace(get_token=AsyncMock(side_effect=get_token), close=AsyncMock())
    build = Mock(return_value=credential)
    monkeypatch.setattr(runtime, "build_async_credential", build)
    owner = runtime.get_runtime(selected)
    config = execution._provider(selected, owner, "deployment")
    callback = config["bearer_token_provider"]
    first = await callback(SimpleNamespace())
    second = await callback(SimpleNamespace())
    overlapped = await asyncio.gather(callback(SimpleNamespace()), callback(SimpleNamespace()))
    await owner.close()
    assert first == "sentinel-token-1"
    assert second == "sentinel-token-2"
    assert set(overlapped) == {"sentinel-token-3", "sentinel-token-4"}
    assert credential.get_token.await_count == 4
    build.assert_called_once_with()
    credential.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_entra_callback_failure_is_sanitized(binding, monkeypatch):
    selected = replace(binding, provider=AzureOpenAIProvider("https://fixture.openai.azure.com"))
    credential = SimpleNamespace(
        get_token=AsyncMock(side_effect=RuntimeError("sentinel-private-credential-detail")),
        close=AsyncMock(),
    )
    monkeypatch.setattr(runtime, "build_async_credential", Mock(return_value=credential))
    owner = runtime.get_runtime(selected)
    callback = execution._provider(selected, owner, "deployment")["bearer_token_provider"]
    try:
        with pytest.raises(CopilotPreviewError, match=r"Azure OpenAI.*Entra") as error:
            await callback(SimpleNamespace())
        assert "sentinel-private-credential-detail" not in str(error.value)
    finally:
        await owner.close()
