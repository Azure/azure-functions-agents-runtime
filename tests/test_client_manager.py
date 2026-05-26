from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch, sentinel

import pytest
from agent_framework.openai import OpenAIChatClient
from pydantic import ValidationError

from azure_functions_agents.client_manager import ClientFactoryError, build_chat_client
from azure_functions_agents.client_manager.providers import (
    AzureOpenAIConfig,
    FoundryConfig,
    OpenAIConfig,
    ProviderSpec,
    UnknownProviderError,
    _build_foundry_client,
)
from azure_functions_agents.config.schema import AgentConfiguration


def _agent_configuration(
    provider: str,
    provider_config: dict[str, Any],
    *,
    model: str,
) -> AgentConfiguration:
    return AgentConfiguration.model_validate(
        {
            "provider": provider,
            "model": model,
            provider: provider_config,
        }
    )


def _auth_mode_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "MAF auth provider=" in r.getMessage()]


def test_get_chat_client_dispatches_to_provider_factory_and_forwards_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return "client"

    monkeypatch.setattr(
        "azure_functions_agents.client_manager.get_provider",
        lambda provider: ProviderSpec(provider, OpenAIConfig, fake_factory),
    )

    cfg = AgentConfiguration.model_validate(
        {
            "provider": "openai",
            "model": "gpt-4.1",
            "timeout": 42,
            "openai": {
                "base_url": "https://openai.example.test",
                "organization": "contoso",
            },
        }
    )

    client = build_chat_client(cfg)

    assert client == "client"
    assert captured == {
        "model": "gpt-4.1",
        "base_url": "https://openai.example.test",
        "organization": "contoso",
        "timeout": 42,
    }


def test_get_chat_client_filters_none_api_key_to_allow_maf_env_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return "client"

    monkeypatch.setattr(
        "azure_functions_agents.client_manager.get_provider",
        lambda provider: ProviderSpec(provider, OpenAIConfig, fake_factory),
    )

    cfg = AgentConfiguration.model_validate(
        {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "openai": {
                "api_key": None,
            },
        }
    )

    build_chat_client(cfg)

    assert captured == {"model": "gpt-4.1-mini"}
    assert "api_key" not in captured


def test_build_chat_client_injects_top_level_model_into_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return "client"

    monkeypatch.setattr(
        "azure_functions_agents.client_manager.get_provider",
        lambda provider: ProviderSpec(provider, OpenAIConfig, fake_factory),
    )

    cfg = AgentConfiguration.model_validate(
        {
            "provider": "openai",
            "model": "gpt-4o",
            "openai": {},
        }
    )

    build_chat_client(cfg)

    assert captured["model"] == "gpt-4o"


def test_build_chat_client_omits_api_key_after_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return "client"

    monkeypatch.setattr(
        "azure_functions_agents.client_manager.get_provider",
        lambda provider: ProviderSpec(provider, OpenAIConfig, fake_factory),
    )

    cfg = AgentConfiguration.model_validate(
        {
            "provider": "azure_openai",
            "model": "gpt-4o",
            "azure_openai": {
                "azure_endpoint": "https://example.invalid/",
                "api_version": "2024-10-21",
                "api_key": None,
            },
        }
    )

    build_chat_client(cfg)

    assert captured["model"] == "gpt-4o"
    assert "api_key" not in captured


def test_get_chat_client_unknown_provider_raises() -> None:
    cfg = SimpleNamespace(
        provider="bogus",
        model=None,
        provider_config=SimpleNamespace(model_dump=lambda **_: {}),
        timeout=None,
    )

    with pytest.raises(UnknownProviderError, match="Unknown provider 'bogus'"):
        # AgentConfiguration's schema rejects unknown providers at parse time,
        # so exercising build_chat_client's UnknownProviderError path requires
        # a duck-typed stub.
        build_chat_client(cfg)  # type: ignore[arg-type]


def test_get_chat_client_wraps_type_error_with_diagnostic_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_factory(**kwargs: Any) -> object:
        raise TypeError("unexpected keyword argument 'organization'")

    monkeypatch.setattr(
        "azure_functions_agents.client_manager.get_provider",
        lambda provider: ProviderSpec(provider, OpenAIConfig, fake_factory),
    )

    cfg = AgentConfiguration.model_validate(
        {
            "provider": "openai",
            "model": "gpt-4.1",
            "timeout": 30,
            "openai": {
                "organization": "contoso",
            },
        }
    )

    with pytest.raises(ClientFactoryError) as exc_info:
        build_chat_client(cfg)

    message = str(exc_info.value)
    assert "Failed to construct MAF client for provider 'openai'" in message
    assert "unexpected keyword argument 'organization'" in message
    assert "Offending kwargs=['organization', 'model', 'timeout']" in message
    assert "Check your agent_configuration.openai sub-block." in message


def test_get_chat_client_azure_openai_api_key_only_uses_api_key_auth(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    caplog.set_level(logging.DEBUG, logger="azure.functions.AgentRuntime")
    async_credential = Mock(name="aio_default_credential")
    sync_credential = Mock(side_effect=AssertionError("sync DefaultAzureCredential used"))
    openai_client = object()

    with (
        patch(
            "azure_functions_agents.client_manager.providers.azure_identity_aio.DefaultAzureCredential",
            async_credential,
        ),
        patch("azure.identity.DefaultAzureCredential", sync_credential),
        patch("agent_framework.openai.OpenAIChatClient", return_value=openai_client) as openai_ctor,
    ):
        client = build_chat_client(
            _agent_configuration(
                "azure_openai",
                {
                    "azure_endpoint": "https://example.invalid/",
                    "api_version": "2024-10-21",
                    "api_key": "K",
                },
                model="gpt-4.1-mini",
            )
        )

    assert client is openai_client
    async_credential.assert_not_called()
    sync_credential.assert_not_called()
    forwarded_kwargs = openai_ctor.call_args.kwargs
    assert forwarded_kwargs == {
        "model": "gpt-4.1-mini",
        "azure_endpoint": "https://example.invalid/",
        "api_version": "2024-10-21",
        "api_key": "K",
    }
    auth_records = _auth_mode_records(caplog)
    assert len(auth_records) == 1
    assert (
        auth_records[0].getMessage()
        == "MAF auth provider=azure_openai mode=api_key mi_client_id_set=False"
    )


def test_get_chat_client_azure_openai_managed_identity_only_uses_yaml_client_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_CLIENT_ID", "ENV-GUID")
    caplog.set_level(logging.DEBUG, logger="azure.functions.AgentRuntime")
    async_credential = Mock(name="aio_default_credential", return_value=sentinel.async_credential)
    sync_credential = Mock(side_effect=AssertionError("sync DefaultAzureCredential used"))
    openai_client = object()

    with (
        patch(
            "azure_functions_agents.client_manager.providers.azure_identity_aio.DefaultAzureCredential",
            async_credential,
        ),
        patch("azure.identity.DefaultAzureCredential", sync_credential),
        patch("agent_framework.openai.OpenAIChatClient", return_value=openai_client) as openai_ctor,
    ):
        client = build_chat_client(
            _agent_configuration(
                "azure_openai",
                {
                    "azure_endpoint": "https://example.invalid/",
                    "api_version": "2024-10-21",
                    "managed_identity_client_id": "YAML-GUID",
                },
                model="gpt-4.1-mini",
            )
        )

    assert client is openai_client
    async_credential.assert_called_once_with(managed_identity_client_id="YAML-GUID")
    sync_credential.assert_not_called()
    forwarded_kwargs = openai_ctor.call_args.kwargs
    assert forwarded_kwargs == {
        "model": "gpt-4.1-mini",
        "azure_endpoint": "https://example.invalid/",
        "api_version": "2024-10-21",
        "credential": sentinel.async_credential,
    }
    assert "managed_identity_client_id" not in forwarded_kwargs
    auth_records = _auth_mode_records(caplog)
    assert len(auth_records) == 1
    assert (
        auth_records[0].getMessage()
        == "MAF auth provider=azure_openai mode=managed_identity_user_assigned mi_client_id_set=True"
    )


def test_azure_openai_config_rejects_api_key_and_managed_identity_together() -> None:
    with pytest.raises(ValidationError, match="Cannot set both 'api_key'"):
        AzureOpenAIConfig(
            azure_endpoint="https://example.invalid/",
            api_version="2024-10-21",
            api_key="K",
            managed_identity_client_id="GUID",
        )


def test_get_chat_client_azure_openai_env_api_key_fallback_skips_credential(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "envkey")
    caplog.set_level(logging.DEBUG, logger="azure.functions.AgentRuntime")
    async_credential = Mock(name="aio_default_credential")
    sync_credential = Mock(side_effect=AssertionError("sync DefaultAzureCredential used"))
    openai_client = object()

    with (
        patch(
            "azure_functions_agents.client_manager.providers.azure_identity_aio.DefaultAzureCredential",
            async_credential,
        ),
        patch("azure.identity.DefaultAzureCredential", sync_credential),
        patch("agent_framework.openai.OpenAIChatClient", return_value=openai_client) as openai_ctor,
    ):
        client = build_chat_client(
            _agent_configuration(
                "azure_openai",
                {
                    "azure_endpoint": "https://example.invalid/",
                    "api_version": "2024-10-21",
                },
                model="gpt-4.1-mini",
            )
        )

    assert client is openai_client
    async_credential.assert_not_called()
    sync_credential.assert_not_called()
    forwarded_kwargs = openai_ctor.call_args.kwargs
    assert forwarded_kwargs == {
        "model": "gpt-4.1-mini",
        "azure_endpoint": "https://example.invalid/",
        "api_version": "2024-10-21",
    }
    auth_records = _auth_mode_records(caplog)
    assert len(auth_records) == 1
    assert (
        auth_records[0].getMessage()
        == "MAF auth provider=azure_openai mode=api_key_env_fallback mi_client_id_set=False"
    )


def test_get_chat_client_azure_openai_system_assigned_managed_identity(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    caplog.set_level(logging.DEBUG, logger="azure.functions.AgentRuntime")
    async_credential = Mock(name="aio_default_credential", return_value=sentinel.async_credential)
    sync_credential = Mock(side_effect=AssertionError("sync DefaultAzureCredential used"))
    openai_client = object()

    with (
        patch(
            "azure_functions_agents.client_manager.providers.azure_identity_aio.DefaultAzureCredential",
            async_credential,
        ),
        patch("azure.identity.DefaultAzureCredential", sync_credential),
        patch("agent_framework.openai.OpenAIChatClient", return_value=openai_client) as openai_ctor,
    ):
        client = build_chat_client(
            _agent_configuration(
                "azure_openai",
                {
                    "azure_endpoint": "https://example.invalid/",
                    "api_version": "2024-10-21",
                },
                model="gpt-4.1-mini",
            )
        )

    assert client is openai_client
    async_credential.assert_called_once_with()
    sync_credential.assert_not_called()
    forwarded_kwargs = openai_ctor.call_args.kwargs
    assert forwarded_kwargs == {
        "model": "gpt-4.1-mini",
        "azure_endpoint": "https://example.invalid/",
        "api_version": "2024-10-21",
        "credential": sentinel.async_credential,
    }
    auth_records = _auth_mode_records(caplog)
    assert len(auth_records) == 1
    assert (
        auth_records[0].getMessage()
        == "MAF auth provider=azure_openai mode=managed_identity_system_assigned mi_client_id_set=False"
    )


def test_get_chat_client_foundry_managed_identity_client_id_uses_async_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="azure.functions.AgentRuntime")
    async_credential = Mock(name="aio_default_credential", return_value=sentinel.async_credential)
    sync_credential = Mock(side_effect=AssertionError("sync DefaultAzureCredential used"))
    foundry_client = object()

    with (
        patch(
            "azure_functions_agents.client_manager.providers.azure_identity_aio.DefaultAzureCredential",
            async_credential,
        ),
        patch("azure.identity.DefaultAzureCredential", sync_credential),
        patch("agent_framework.foundry.FoundryChatClient", return_value=foundry_client) as foundry_ctor,
    ):
        client = build_chat_client(
            _agent_configuration(
                "foundry",
                {
                    "project_endpoint": "https://example.invalid/",
                    "managed_identity_client_id": "GUID",
                },
                model="gpt-4.1-mini",
            )
        )

    assert client is foundry_client
    async_credential.assert_called_once_with(managed_identity_client_id="GUID")
    sync_credential.assert_not_called()
    forwarded_kwargs = foundry_ctor.call_args.kwargs
    assert forwarded_kwargs == {
        "model": "gpt-4.1-mini",
        "project_endpoint": "https://example.invalid/",
        "credential": sentinel.async_credential,
    }
    assert "managed_identity_client_id" not in forwarded_kwargs
    auth_records = _auth_mode_records(caplog)
    assert len(auth_records) == 1
    assert (
        auth_records[0].getMessage()
        == "MAF auth provider=foundry mode=managed_identity_user_assigned mi_client_id_set=True"
    )


def test_get_chat_client_foundry_system_assigned_managed_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="azure.functions.AgentRuntime")
    async_credential = Mock(name="aio_default_credential", return_value=sentinel.async_credential)
    sync_credential = Mock(side_effect=AssertionError("sync DefaultAzureCredential used"))
    foundry_client = object()

    with (
        patch(
            "azure_functions_agents.client_manager.providers.azure_identity_aio.DefaultAzureCredential",
            async_credential,
        ),
        patch("azure.identity.DefaultAzureCredential", sync_credential),
        patch("agent_framework.foundry.FoundryChatClient", return_value=foundry_client) as foundry_ctor,
    ):
        client = build_chat_client(
            _agent_configuration(
                "foundry",
                {
                    "project_endpoint": "https://example.invalid/",
                },
                model="gpt-4.1-mini",
            )
        )

    assert client is foundry_client
    async_credential.assert_called_once_with()
    sync_credential.assert_not_called()
    assert foundry_ctor.call_args.kwargs == {
        "model": "gpt-4.1-mini",
        "project_endpoint": "https://example.invalid/",
        "credential": sentinel.async_credential,
    }
    auth_records = _auth_mode_records(caplog)
    assert len(auth_records) == 1
    assert (
        auth_records[0].getMessage()
        == "MAF auth provider=foundry mode=managed_identity_system_assigned mi_client_id_set=False"
    )


def test_azure_openai_config_rejects_credential_field_from_yaml() -> None:
    with pytest.raises(ValidationError, match="credential"):
        AzureOpenAIConfig.model_validate(
            {
                "azure_endpoint": "https://example.invalid/",
                "api_version": "2024-10-21",
                "credential": "some-string",
            }
        )


def test_foundry_config_rejects_credential_field_from_yaml() -> None:
    with pytest.raises(ValidationError, match="credential"):
        FoundryConfig.model_validate(
            {
                "project_endpoint": "https://example.invalid/",
                "credential": "...",
            }
        )


def test_get_chat_client_never_logs_secrets_for_azure_openai_auth_paths(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    caplog.set_level(logging.DEBUG, logger="azure.functions.AgentRuntime")
    async_credential = Mock(name="aio_default_credential", return_value=sentinel.async_credential)

    with (
        patch(
            "azure_functions_agents.client_manager.providers.azure_identity_aio.DefaultAzureCredential",
            async_credential,
        ),
        patch("agent_framework.openai.OpenAIChatClient", side_effect=[object(), object()]),
    ):
        build_chat_client(
            _agent_configuration(
                "azure_openai",
                {
                    "azure_endpoint": "https://example.invalid/",
                    "api_version": "2024-10-21",
                    "api_key": "VERY_SECRET_KEY_REDACTED",
                },
                model="gpt-4.1-mini",
            )
        )
        build_chat_client(
            _agent_configuration(
                "azure_openai",
                {
                    "azure_endpoint": "https://example.invalid/",
                    "api_version": "2024-10-21",
                    "managed_identity_client_id": "00000000-0000-0000-0000-000000000000",
                },
                model="gpt-4.1-mini",
            )
        )

    for record in caplog.records:
        msg = record.getMessage()
        assert "VERY_SECRET_KEY_REDACTED" not in msg
        assert "00000000-0000-0000-0000-000000000000" not in msg
        for arg in record.args or ():
            assert "VERY_SECRET_KEY_REDACTED" not in str(arg)
            assert "00000000-0000-0000-0000-000000000000" not in str(arg)


def test_build_foundry_client_injects_default_credential_only_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    class FakeCredential:
        pass

    injected_credential = FakeCredential()
    explicit_credential = FakeCredential()

    monkeypatch.setattr(
        "azure.identity.aio.DefaultAzureCredential",
        lambda: injected_credential,
    )
    monkeypatch.setattr(
        "agent_framework.foundry.FoundryChatClient",
        lambda **kwargs: captured.append(kwargs) or kwargs,
    )

    result_without_credential = _build_foundry_client(
        model="gpt-4.1",
        project_endpoint="https://foundry.example.test",
    )
    result_with_credential = _build_foundry_client(
        model="gpt-4.1",
        project_endpoint="https://foundry.example.test",
        credential=explicit_credential,
    )

    assert captured[0]["credential"] is injected_credential
    assert captured[1]["credential"] is explicit_credential
    assert captured[0]["model"] == "gpt-4.1"
    assert captured[1]["model"] == "gpt-4.1"
    assert result_without_credential["credential"] is injected_credential
    assert result_with_credential["credential"] is explicit_credential


def test_get_chat_client_uses_foundry_provider_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return "foundry-client"

    monkeypatch.setattr(
        "azure_functions_agents.client_manager.get_provider",
        lambda provider: ProviderSpec(provider, FoundryConfig, fake_factory),
    )

    cfg = AgentConfiguration.model_validate(
        {
            "provider": "foundry",
            "model": "gpt-4.1",
            "foundry": {
                "project_endpoint": "https://foundry.example.test",
                "audience": "agents",
            },
        }
    )

    client = build_chat_client(cfg)

    assert client == "foundry-client"
    assert captured == {
        "model": "gpt-4.1",
        "project_endpoint": "https://foundry.example.test",
        "audience": "agents",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "openai": {
                "api_key": "fake",
                "base_url": "https://example.invalid/",
            },
        },
        {
            "provider": "azure_openai",
            "model": "gpt-4.1-mini",
            "azure_openai": {
                "api_key": "fake",
                "azure_endpoint": "https://example.invalid/",
                "api_version": "2024-10-21",
            },
        },
    ],
)
def test_get_chat_client_constructs_real_openai_chat_client_without_network(
    payload: dict[str, Any],
) -> None:
    cfg = AgentConfiguration.model_validate(payload)

    client = build_chat_client(cfg)

    assert isinstance(client, OpenAIChatClient)


def test_get_chat_client_constructs_foundry_client_with_renamed_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    credential = object()

    class RecordingFoundryChatClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        "azure.identity.aio.DefaultAzureCredential",
        lambda: credential,
    )
    monkeypatch.setattr(
        "agent_framework.foundry.FoundryChatClient",
        RecordingFoundryChatClient,
    )

    cfg = AgentConfiguration.model_validate(
        {
            "provider": "foundry",
            "model": "gpt-4.1-mini",
            "foundry": {
                "project_endpoint": "https://example.invalid/",
            },
        }
    )

    client = build_chat_client(cfg)

    assert isinstance(client, RecordingFoundryChatClient)
    assert captured == {
        "model": "gpt-4.1-mini",
        "project_endpoint": "https://example.invalid/",
        "credential": credential,
    }
