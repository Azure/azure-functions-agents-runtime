from __future__ import annotations

from typing import Any

import pytest

from azure_functions_agents.client_manager import MAFClientManager


def test_build_chat_client_uses_configured_provider_endpoint_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_build_foundry(
        cls: type[MAFClientManager],
        model: str,
        *,
        endpoint: str | None = None,
    ) -> object:
        captured["model"] = model
        captured["endpoint"] = endpoint
        return object()

    monkeypatch.setattr(MAFClientManager, "_build_foundry", classmethod(fake_build_foundry))

    MAFClientManager().build_chat_client(
        "gpt-4.1",
        provider="foundry",
        endpoint="https://foundry.example.test",
    )

    assert captured == {
        "model": "gpt-4.1",
        "endpoint": "https://foundry.example.test",
    }


def test_build_chat_client_normalizes_hyphenated_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_build_azure_openai(
        cls: type[MAFClientManager],
        model: str,
        *,
        endpoint: str | None = None,
    ) -> object:
        captured["model"] = model
        captured["endpoint"] = endpoint
        return object()

    monkeypatch.setattr(MAFClientManager, "_build_azure_openai", classmethod(fake_build_azure_openai))

    MAFClientManager().build_chat_client(
        "gpt-4.1",
        provider="azure-openai",
        endpoint="https://azure-openai.example.test",
    )

    assert captured == {
        "model": "gpt-4.1",
        "endpoint": "https://azure-openai.example.test",
    }
