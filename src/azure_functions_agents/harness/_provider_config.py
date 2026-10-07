"""SDK-neutral provider and model target resolution shared by selected harnesses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..config.env import EnvVar, runtime_env_value


class ProviderKind(StrEnum):
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    FOUNDRY = "foundry"


@dataclass(frozen=True)
class InferenceTarget:
    """Provider and model selected for one inference request."""

    provider: str | None = None
    model: str | None = None


_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_DEFAULT_FOUNDRY_MODEL = "gpt-4o-mini"


def _configured_value(name: EnvVar) -> str:
    return runtime_env_value(name).strip()


def resolve_inference_target(requested: str | None) -> InferenceTarget:
    """Resolve provider/model settings without constructing a harness client."""
    provider = _configured_value(EnvVar.PROVIDER).lower()
    if not provider:
        if _configured_value(EnvVar.AZURE_OPENAI_ENDPOINT):
            provider = ProviderKind.AZURE_OPENAI
        elif _configured_value(EnvVar.FOUNDRY_PROJECT_ENDPOINT):
            provider = ProviderKind.FOUNDRY
        elif _configured_value(EnvVar.OPENAI_API_KEY):
            provider = ProviderKind.OPENAI
        else:
            raise RuntimeError(
                "No inference provider configured. Set one of: "
                "OPENAI_API_KEY (OpenAI), "
                "AZURE_OPENAI_ENDPOINT (+ AZURE_OPENAI_API_KEY or managed identity) "
                "for Azure OpenAI, or FOUNDRY_PROJECT_ENDPOINT for Microsoft Foundry. "
                "You can also set AZURE_FUNCTIONS_AGENTS_PROVIDER="
                "openai|azure_openai|foundry to override."
            )
    runtime_model = _configured_value(EnvVar.MODEL)
    if requested:
        model = requested
    elif provider == ProviderKind.AZURE_OPENAI:
        model = (
            _configured_value(EnvVar.AZURE_OPENAI_DEPLOYMENT)
            or runtime_model
            or _DEFAULT_OPENAI_MODEL
        )
    elif provider == ProviderKind.FOUNDRY:
        model = _configured_value(EnvVar.FOUNDRY_MODEL) or runtime_model or _DEFAULT_FOUNDRY_MODEL
    else:
        model = runtime_model or _DEFAULT_OPENAI_MODEL
    return InferenceTarget(provider=str(provider), model=model)
