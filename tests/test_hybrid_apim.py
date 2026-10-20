import pytest

from azure_functions_agents.experimental.hybrid_apim import HybridApimClientManager
from azure_functions_agents.experimental.hybrid_config import HYBRID_APIM_MODEL_ENV


def test_hybrid_apim_client_uses_custom_subscription_header() -> None:
    manager = HybridApimClientManager(
        base_url="https://example.test/openai/v1",
        audience=None,
        subscription_key="private",
        environment={"AZURE_FUNCTIONS_AGENTS_MODEL": "deployment"},
    )

    client, target = manager.build_chat_client_with_target(None)

    assert target.provider == "azure_openai_apim"
    assert target.model == "deployment"
    assert client is not None


def test_hybrid_apim_foundry_responses_route_adds_required_api_version() -> None:
    manager = HybridApimClientManager(
        base_url="https://example.test/project/openai",
        audience=None,
        subscription_key="private",
    )

    client, target = manager.build_chat_client_with_target("deployment")

    assert client.client.default_query == {"api-version": "2025-03-01-preview"}
    assert target.api_version == "2025-03-01-preview"


def test_hybrid_apim_openai_v1_route_keeps_openai_compatible_shape() -> None:
    manager = HybridApimClientManager(
        base_url="https://example.test/project/openai/v1",
        audience=None,
        subscription_key="private",
    )

    client, target = manager.build_chat_client_with_target("deployment")

    assert client.client.default_query == {}
    assert target.api_version == "responses-v1"


@pytest.mark.asyncio
async def test_hybrid_apim_manager_close_is_idempotent() -> None:
    manager = HybridApimClientManager(
        base_url="https://example.test/openai/v1",
        audience=None,
        subscription_key="private",
    )

    await manager.close()
    await manager.close()


@pytest.mark.asyncio
async def test_hybrid_apim_managed_identity_header_uses_token() -> None:
    manager = HybridApimClientManager(
        base_url="https://example.test/openai/v1",
        audience="api://gateway",
        subscription_key=None,
    )

    async def token() -> str:
        return "token-value"

    manager._token_provider = lambda: token  # type: ignore[method-assign]

    headers = await manager.request_headers()

    assert headers["Authorization"] == "Bearer token-value"


def test_hybrid_apim_production_environment_uses_general_model_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(HYBRID_APIM_MODEL_ENV, raising=False)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "production-deployment")
    manager = HybridApimClientManager(
        base_url="https://example.test/openai/v1",
        audience=None,
        subscription_key="private",
        environment=None,
    )

    assert manager.resolve_model(None) == "production-deployment"
