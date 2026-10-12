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

    provider: ProviderKind | None = None
    model: str | None = None

    def require(self) -> tuple[ProviderKind, str]:
        """Return the resolved provider/model or fail fast if either is missing."""
        if self.provider is None or self.model is None:
            raise RuntimeError("Inference provider/model resolution was incomplete.")
        return self.provider, self.model


@dataclass(frozen=True)
class ProviderRule:
    """Provider-specific environment and default-model rules."""

    kind: ProviderKind
    autodetect_env: EnvVar
    model_env: EnvVar | None
    default_model: str


_PROVIDER_RULES: tuple[ProviderRule, ...] = (
    ProviderRule(
        kind=ProviderKind.AZURE_OPENAI,
        autodetect_env=EnvVar.AZURE_OPENAI_ENDPOINT,
        model_env=EnvVar.AZURE_OPENAI_DEPLOYMENT,
        default_model="gpt-4o-mini",
    ),
    ProviderRule(
        kind=ProviderKind.FOUNDRY,
        autodetect_env=EnvVar.FOUNDRY_PROJECT_ENDPOINT,
        model_env=EnvVar.FOUNDRY_MODEL,
        default_model="gpt-4o-mini",
    ),
    ProviderRule(
        kind=ProviderKind.OPENAI,
        autodetect_env=EnvVar.OPENAI_API_KEY,
        model_env=None,
        default_model="gpt-4o-mini",
    ),
)
_PROVIDER_RULES_BY_KIND = {rule.kind: rule for rule in _PROVIDER_RULES}


def _configured_value(name: EnvVar) -> str:
    return runtime_env_value(name).strip()


def provider_rule(kind: ProviderKind) -> ProviderRule:
    """Return the shared configuration rule for one inference provider."""
    return _PROVIDER_RULES_BY_KIND[kind]


def _resolve_provider_override() -> ProviderKind | None:
    configured = _configured_value(EnvVar.PROVIDER).lower()
    if not configured:
        return None
    try:
        return ProviderKind(configured)
    except ValueError:
        raise RuntimeError(
            f"Unknown AZURE_FUNCTIONS_AGENTS_PROVIDER '{configured}'. "
            "Use one of: openai, azure_openai, foundry."
        ) from None


def _autodetected_provider() -> ProviderKind | None:
    for rule in _PROVIDER_RULES:
        if _configured_value(rule.autodetect_env):
            return rule.kind
    return None


def resolve_inference_target(requested: str | None) -> InferenceTarget:
    """Resolve provider/model settings without constructing a harness client."""
    provider = _resolve_provider_override() or _autodetected_provider()
    if provider is None:
        raise RuntimeError(
            "No inference provider configured. Set one of: "
            "OPENAI_API_KEY (OpenAI), "
            "AZURE_OPENAI_ENDPOINT (+ AZURE_OPENAI_API_KEY or managed identity) "
            "for Azure OpenAI, or FOUNDRY_PROJECT_ENDPOINT for Microsoft Foundry. "
            "You can also set AZURE_FUNCTIONS_AGENTS_PROVIDER="
            "openai|azure_openai|foundry to override."
        )

    rule = provider_rule(provider)
    runtime_model = _configured_value(EnvVar.MODEL)
    if requested:
        model = requested
    else:
        provider_model = _configured_value(rule.model_env) if rule.model_env is not None else ""
        model = provider_model or runtime_model or rule.default_model
    return InferenceTarget(provider=provider, model=model)
