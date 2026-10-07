from __future__ import annotations

import pytest

from azure_functions_agents.harness._provider_config import (
    InferenceTarget,
    ProviderKind,
    resolve_inference_target,
)


@pytest.fixture(autouse=True)
def clear_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AZURE_FUNCTIONS_AGENTS_PROVIDER",
        "AZURE_FUNCTIONS_AGENTS_MODEL",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
        "FOUNDRY_PROJECT_ENDPOINT",
        "FOUNDRY_MODEL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("provider", "specific_env", "specific_model", "default_model"),
    [
        ("openai", None, None, "gpt-4o-mini"),
        ("azure_openai", "AZURE_OPENAI_DEPLOYMENT", "deployment", "gpt-4o-mini"),
        ("foundry", "FOUNDRY_MODEL", "deployment", "gpt-4o-mini"),
    ],
)
def test_resolve_inference_target_precedence(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    specific_env: str | None,
    specific_model: str | None,
    default_model: str,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", provider)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "runtime-model")
    if specific_env and specific_model:
        monkeypatch.setenv(specific_env, specific_model)

    expected = specific_model or "runtime-model"
    assert resolve_inference_target(None) == InferenceTarget(provider, expected or default_model)
    assert resolve_inference_target("requested") == InferenceTarget(provider, "requested")


@pytest.mark.parametrize(
    ("environment", "expected_provider"),
    [
        ({"AZURE_OPENAI_ENDPOINT": "endpoint"}, ProviderKind.AZURE_OPENAI),
        ({"FOUNDRY_PROJECT_ENDPOINT": "endpoint"}, ProviderKind.FOUNDRY),
        ({"OPENAI_API_KEY": "offline"}, ProviderKind.OPENAI),
    ],
)
def test_provider_auto_detection(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    expected_provider: ProviderKind,
) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert resolve_inference_target("model").provider == expected_provider


def test_explicit_provider_wins_over_environment_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "endpoint")

    assert resolve_inference_target("model").provider == ProviderKind.OPENAI


def test_no_provider_is_an_actionable_error() -> None:
    with pytest.raises(RuntimeError, match="No inference provider configured"):
        resolve_inference_target(None)
