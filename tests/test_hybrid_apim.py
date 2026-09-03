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


@pytest.mark.asyncio
async def test_hybrid_apim_manager_close_is_idempotent() -> None:
    manager = HybridApimClientManager(
        base_url="https://example.test/openai/v1",
        audience=None,
        subscription_key="private",
    )

    await manager.close()
    await manager.close()


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
