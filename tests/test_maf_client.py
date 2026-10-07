from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from azure_functions_agents._credential import build_async_credential
from azure_functions_agents.harness._provider_config import InferenceTarget
from azure_functions_agents.harness.agent_framework._maf_client import build_chat_client


@pytest.fixture(autouse=True)
def clear_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AZURE_FUNCTIONS_AGENTS_PROVIDER",
        "AZURE_FUNCTIONS_AGENTS_MODEL",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT",
        "FOUNDRY_PROJECT_ENDPOINT",
        "FOUNDRY_MODEL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_build_openai_client_with_resolved_target(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_framework.openai as openai_module

    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    client = object()
    constructor = Mock(return_value=client)
    monkeypatch.setattr(openai_module, "OpenAIChatClient", constructor)

    built, target = build_chat_client("custom-model")

    assert built is client
    assert target == InferenceTarget("openai", "custom-model")
    constructor.assert_called_once_with(model="custom-model", api_key=None)


def test_build_azure_openai_client_preserves_optional_auth_and_api_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_framework.openai as openai_module

    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://account.example")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "offline")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "preview")
    client = object()
    constructor = Mock(return_value=client)
    monkeypatch.setattr(openai_module, "OpenAIChatClient", constructor)

    built, target = build_chat_client("deployment")

    assert built is client
    assert target == InferenceTarget("azure_openai", "deployment")
    constructor.assert_called_once_with(
        model="deployment",
        azure_endpoint="https://account.example",
        api_version="preview",
        api_key="offline",
    )


def test_build_foundry_client_uses_managed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_framework.foundry as foundry_module

    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    client = object()
    constructor = Mock(return_value=client)
    credential = object()
    monkeypatch.setattr(foundry_module, "FoundryChatClient", constructor)
    monkeypatch.setattr(
        "azure_functions_agents.harness.agent_framework._maf_client.build_async_credential",
        Mock(return_value=credential),
    )

    built, target = build_chat_client("deployment")

    assert built is client
    assert target == InferenceTarget("foundry", "deployment")
    constructor.assert_called_once_with(
        project_endpoint="https://project.example",
        model="deployment",
        credential=credential,
    )


def test_build_managed_identity_credential_passes_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-id-123")
    with patch("azure.identity.aio.DefaultAzureCredential") as credential_ctor:
        credential = object()
        credential_ctor.return_value = credential

        assert build_async_credential() is credential

    credential_ctor.assert_called_once_with(managed_identity_client_id="client-id-123")


@pytest.mark.asyncio
async def test_foundry_stateless_request_does_not_include_encrypted_content() -> None:
    from agent_framework.foundry import FoundryChatClient

    client = object.__new__(FoundryChatClient)
    client._prepare_tools_for_openai = lambda _tools: None
    client._prepare_messages_for_openai = lambda _messages, **_kwargs: [
        {"role": "user", "content": "ping"}
    ]
    client._prepare_response_and_text_format = lambda **_kwargs: (None, None)
    client._check_model_presence = lambda options: options.setdefault("model", "test-model")

    request_options = await client._prepare_options([], {})

    assert "reasoning.encrypted_content" not in request_options.get("include", [])
