"""Microsoft Agent Framework provider and chat-client construction."""

from __future__ import annotations

from typing import Any, cast

from ..._credential import build_async_credential
from ..._logger import logger
from ...config.env import EnvVar, runtime_env_value
from .._provider_config import InferenceTarget, ProviderKind, resolve_inference_target
from ._maf_warnings import suppress_experimental_warnings


def _env(name: EnvVar) -> str:
    return runtime_env_value(name).strip()


def build_chat_client(model: str | None) -> tuple[Any, InferenceTarget]:
    """Build the selected MAF client and return its resolved target."""
    target = resolve_inference_target(model)
    provider = cast(str, target.provider)
    resolved_model = cast(str, target.model)
    logger.info("MAF provider=%s model=%s", provider, resolved_model)
    with suppress_experimental_warnings():
        if provider == ProviderKind.OPENAI:
            from agent_framework.openai import OpenAIChatClient

            return OpenAIChatClient(model=resolved_model, api_key=_env(EnvVar.OPENAI_API_KEY) or None), target
        if provider == ProviderKind.AZURE_OPENAI:
            from agent_framework.openai import OpenAIChatClient

            endpoint = _env(EnvVar.AZURE_OPENAI_ENDPOINT)
            if not endpoint:
                raise RuntimeError(
                    "AZURE_FUNCTIONS_AGENTS_PROVIDER=azure_openai requires "
                    "AZURE_OPENAI_ENDPOINT to be set."
                )
            kwargs: dict[str, Any] = {"model": resolved_model, "azure_endpoint": endpoint}
            api_version = _env(EnvVar.AZURE_OPENAI_API_VERSION)
            if api_version:
                kwargs["api_version"] = api_version
            api_key = _env(EnvVar.AZURE_OPENAI_API_KEY)
            if api_key:
                kwargs["api_key"] = api_key
            else:
                kwargs["credential"] = build_async_credential()
            return OpenAIChatClient(**kwargs), target
        if provider == ProviderKind.FOUNDRY:
            from agent_framework.foundry import FoundryChatClient

            endpoint = _env(EnvVar.FOUNDRY_PROJECT_ENDPOINT)
            if not endpoint:
                raise RuntimeError(
                    "AZURE_FUNCTIONS_AGENTS_PROVIDER=foundry requires "
                    "FOUNDRY_PROJECT_ENDPOINT to be set."
                )
            return (
                FoundryChatClient(
                    project_endpoint=endpoint,
                    model=resolved_model,
                    credential=build_async_credential(),
                ),
                target,
            )
    raise RuntimeError(
        f"Unknown AZURE_FUNCTIONS_AGENTS_PROVIDER '{provider}'. "
        "Use one of: openai, azure_openai, foundry."
    )
