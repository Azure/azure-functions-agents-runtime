from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from azure_functions_agents._credential import build_async_credential
from azure_functions_agents.client_manager import (
    _DEFAULT_FOUNDRY_MODEL,
    _DEFAULT_OPENAI_MODEL,
    ClientManager,
    InferenceTarget,
    MAFClientManager,
    get_client_manager,
    set_client_manager,
    shutdown_client_manager,
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
        patch.object(MAFClientManager, "_provider", return_value="foundry") as provider,
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
    resolve.assert_called_once_with("requested-model", "foundry")


@pytest.mark.asyncio
async def test_maf_manager_reuses_provider_client_on_one_worker_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    manager = MAFClientManager()
    client = object()

    with patch.object(MAFClientManager, "_build_foundry", return_value=client) as build:
        first, first_target = manager.build_chat_client_with_target("shared-model")
        await asyncio.sleep(0)
        second, second_target = manager.build_chat_client_with_target("shared-model")

    assert first is client
    assert second is first
    assert first_target == second_target == InferenceTarget("foundry", "shared-model")
    build.assert_called_once_with("shared-model")


def test_maf_manager_partitions_cached_clients_by_resolved_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    manager = MAFClientManager()
    clients = [object(), object()]

    with patch.object(MAFClientManager, "_build_foundry", side_effect=clients) as build:
        first, _ = manager.build_chat_client_with_target("model-one")
        second, _ = manager.build_chat_client_with_target("model-two")

    assert first is clients[0]
    assert second is clients[1]
    assert first is not second
    assert build.call_count == 2


def test_maf_manager_partitions_cached_clients_by_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project-one.example")
    manager = MAFClientManager()
    clients = [object(), object()]

    with patch.object(MAFClientManager, "_build_foundry", side_effect=clients) as build:
        first, _ = manager.build_chat_client_with_target("shared-model")
        monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project-two.example")
        second, _ = manager.build_chat_client_with_target("shared-model")

    assert first is clients[0]
    assert second is clients[1]
    assert build.call_count == 2


def test_maf_manager_partitions_azure_openai_clients_by_api_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://account.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "version-one")
    manager = MAFClientManager()
    clients = [object(), object()]

    with patch.object(MAFClientManager, "_build_azure_openai", side_effect=clients) as build:
        first, _ = manager.build_chat_client_with_target("shared-model")
        monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "version-two")
        second, _ = manager.build_chat_client_with_target("shared-model")

    assert first is clients[0]
    assert second is clients[1]
    assert build.call_count == 2


def test_maf_manager_publishes_one_client_during_concurrent_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    manager = MAFClientManager()
    client = object()

    def _build(_model: str) -> object:
        time.sleep(0.02)
        return client

    with (
        patch.object(MAFClientManager, "_build_foundry", side_effect=_build) as build,
        ThreadPoolExecutor(max_workers=8) as executor,
    ):
        futures = [
            executor.submit(manager.build_chat_client_with_target, "shared-model")
            for _ in range(8)
        ]
        built_clients = [future.result()[0] for future in futures]

    assert all(built is client for built in built_clients)
    build.assert_called_once_with("shared-model")


@pytest.mark.asyncio
async def test_maf_manager_closes_foundry_transports_and_credential_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    manager = MAFClientManager()
    openai_client = SimpleNamespace(close=AsyncMock())
    project_client = SimpleNamespace(close=AsyncMock())
    chat_client = SimpleNamespace(client=openai_client, project_client=project_client)
    credential = SimpleNamespace(close=AsyncMock())

    with (
        patch(
            "agent_framework.foundry.FoundryChatClient",
            return_value=chat_client,
        ),
        patch(
            "azure_functions_agents.client_manager.build_async_credential",
            return_value=credential,
        ),
    ):
        built, _ = manager.build_chat_client_with_target("shared-model")
        await manager.close()
        await manager.close()

    assert built is chat_client
    openai_client.close.assert_awaited_once_with()
    project_client.close.assert_awaited_once_with()
    credential.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_maf_manager_closes_openai_transport_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    manager = MAFClientManager()
    transport = SimpleNamespace(close=AsyncMock())
    chat_client = SimpleNamespace(client=transport)

    with patch.object(MAFClientManager, "_build_openai", return_value=chat_client):
        manager.build_chat_client_with_target("shared-model")
        await manager.close()

    transport.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_maf_manager_shares_credential_across_foundry_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    manager = MAFClientManager()
    credential = SimpleNamespace(close=AsyncMock())
    chat_clients = [
        SimpleNamespace(
            client=SimpleNamespace(close=AsyncMock()),
            project_client=SimpleNamespace(close=AsyncMock()),
        ),
        SimpleNamespace(
            client=SimpleNamespace(close=AsyncMock()),
            project_client=SimpleNamespace(close=AsyncMock()),
        ),
    ]

    with (
        patch(
            "agent_framework.foundry.FoundryChatClient",
            side_effect=chat_clients,
        ) as client_ctor,
        patch(
            "azure_functions_agents.client_manager.build_async_credential",
            return_value=credential,
        ) as credential_builder,
    ):
        manager.build_chat_client_with_target("model-one")
        manager.build_chat_client_with_target("model-two")
        await manager.close()

    credential_builder.assert_called_once_with()
    assert [item.kwargs["credential"] for item in client_ctor.call_args_list] == [
        credential,
        credential,
    ]


@pytest.mark.asyncio
async def test_foundry_client_exposes_pinned_transport_cleanup_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://example.services.ai.azure.com/api/projects/test",
    )
    manager = MAFClientManager()
    credential = SimpleNamespace(close=AsyncMock(), get_token=AsyncMock())

    with patch(
        "azure_functions_agents.client_manager.build_async_credential",
        return_value=credential,
    ):
        client, _ = manager.build_chat_client_with_target("test-model")
        reused, _ = manager.build_chat_client_with_target("test-model")

        assert reused is client
        assert callable(client.client.close)
        assert callable(client.project_client.close)
        await manager.close()

    credential.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_maf_manager_attempts_all_cleanup_after_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    manager = MAFClientManager()
    openai_client = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("httpx close")))
    project_client = SimpleNamespace(close=AsyncMock())
    chat_client = SimpleNamespace(client=openai_client, project_client=project_client)
    credential = SimpleNamespace(close=AsyncMock())

    with (
        patch("agent_framework.foundry.FoundryChatClient", return_value=chat_client),
        patch(
            "azure_functions_agents.client_manager.build_async_credential",
            return_value=credential,
        ),
    ):
        manager.build_chat_client_with_target("shared-model")
        with pytest.raises(ExceptionGroup, match="Failed to close"):
            await manager.close()

    openai_client.close.assert_awaited_once_with()
    project_client.close.assert_awaited_once_with()
    credential.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_maf_manager_rejects_build_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    manager = MAFClientManager()

    await manager.close()

    with (
        patch.object(MAFClientManager, "_build_openai", return_value=object()),
        pytest.raises(RuntimeError, match="closed"),
    ):
        manager.build_chat_client_with_target("model-one")


@pytest.mark.asyncio
async def test_maf_manager_cleanup_survives_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    manager = MAFClientManager()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def _slow_close() -> None:
        close_started.set()
        await allow_close.wait()

    openai_client = SimpleNamespace(close=AsyncMock(side_effect=_slow_close))
    project_client = SimpleNamespace(close=AsyncMock())
    chat_client = SimpleNamespace(client=openai_client, project_client=project_client)
    credential = SimpleNamespace(close=AsyncMock())

    with (
        patch("agent_framework.foundry.FoundryChatClient", return_value=chat_client),
        patch(
            "azure_functions_agents.client_manager.build_async_credential",
            return_value=credential,
        ),
    ):
        manager.build_chat_client_with_target("shared-model")
        first_close = asyncio.create_task(manager.close())
        await close_started.wait()
        first_close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_close

        allow_close.set()
        await manager.close()

    openai_client.close.assert_awaited_once_with()
    project_client.close.assert_awaited_once_with()
    credential.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_shutdown_does_not_clear_manager_installed_while_old_manager_closes() -> None:
    get_client_manager()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class BlockingManager(ClientManager):
        def resolve_model(self, requested: str | None) -> str:
            return requested or "blocking-model"

        def build_chat_client(self, model: str | None) -> Any:
            return object()

        async def close(self) -> None:
            close_started.set()
            await allow_close.wait()

    blocking = BlockingManager()
    replacement = BlockingManager()
    set_client_manager(blocking)
    shutdown = asyncio.create_task(shutdown_client_manager())
    await close_started.wait()

    set_client_manager(replacement)
    allow_close.set()
    await shutdown

    assert get_client_manager() is replacement
    set_client_manager(MAFClientManager())


@pytest.mark.asyncio
async def test_set_client_manager_rejects_abandoning_owned_provider_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    get_client_manager()
    manager = MAFClientManager()
    chat_client = SimpleNamespace(client=SimpleNamespace(close=AsyncMock()))

    with patch.object(MAFClientManager, "_build_openai", return_value=chat_client):
        manager.build_chat_client_with_target("shared-model")
    set_client_manager(manager)

    with pytest.raises(RuntimeError, match="owns provider resources"):
        set_client_manager(MAFClientManager())

    await shutdown_client_manager()
    set_client_manager(MAFClientManager())


def test_set_client_manager_retires_displaced_unused_maf_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    displaced = MAFClientManager()
    replacement = MAFClientManager()
    set_client_manager(displaced)

    set_client_manager(replacement)

    with (
        patch.object(MAFClientManager, "_build_openai", return_value=object()),
        pytest.raises(RuntimeError, match="closed"),
    ):
        displaced.build_chat_client_with_target("model-one")


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
