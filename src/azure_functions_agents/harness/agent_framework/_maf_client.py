"""Microsoft Agent Framework provider and chat-client construction."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypedDict

from ..._credential import build_async_credential
from ..._logger import logger
from ...config.env import EnvVar, runtime_env_value
from .._provider_config import (
    InferenceTarget,
    ProviderKind,
    provider_rule,
    resolve_inference_target,
)
from ._maf_warnings import suppress_experimental_warnings

if TYPE_CHECKING:
    from agent_framework.foundry import FoundryChatClient
    from agent_framework.openai import OpenAIChatClient
    from azure.identity.aio import DefaultAzureCredential

type MAFChatClient = OpenAIChatClient | FoundryChatClient
type _ClientBuilder = Callable[[str], MAFChatClient]


class _AzureOpenAIClientOptions(TypedDict, total=False):
    model: str
    azure_endpoint: str
    api_version: str
    api_key: str
    credential: DefaultAzureCredential


def _env(name: EnvVar) -> str:
    return runtime_env_value(name).strip()


def _required_env(provider: ProviderKind) -> str:
    rule = provider_rule(provider)
    endpoint = _env(rule.autodetect_env)
    if endpoint:
        return endpoint
    raise RuntimeError(
        f"AZURE_FUNCTIONS_AGENTS_PROVIDER={provider.value} requires "
        f"{rule.autodetect_env} to be set."
    )


def _build_openai_client(model: str) -> MAFChatClient:
    from agent_framework.openai import OpenAIChatClient

    return OpenAIChatClient(model=model, api_key=_env(EnvVar.OPENAI_API_KEY) or None)


def _build_azure_openai_client(model: str) -> MAFChatClient:
    from agent_framework.openai import OpenAIChatClient

    options: _AzureOpenAIClientOptions = {
        "model": model,
        "azure_endpoint": _required_env(ProviderKind.AZURE_OPENAI),
    }
    api_version = _env(EnvVar.AZURE_OPENAI_API_VERSION)
    if api_version:
        options["api_version"] = api_version
    api_key = _env(EnvVar.AZURE_OPENAI_API_KEY)
    if api_key:
        options["api_key"] = api_key
    else:
        options["credential"] = build_async_credential()
    return OpenAIChatClient(**options)


def _build_foundry_client(model: str) -> MAFChatClient:
    from agent_framework.foundry import FoundryChatClient

    return FoundryChatClient(
        project_endpoint=_required_env(ProviderKind.FOUNDRY),
        model=model,
        credential=build_async_credential(),
    )


_CLIENT_BUILDERS: dict[ProviderKind, _ClientBuilder] = {
    ProviderKind.OPENAI: _build_openai_client,
    ProviderKind.AZURE_OPENAI: _build_azure_openai_client,
    ProviderKind.FOUNDRY: _build_foundry_client,
}


def build_chat_client(model: str | None) -> tuple[MAFChatClient, InferenceTarget]:
    """Build the selected MAF client and return its resolved target."""
    target = resolve_inference_target(model)
    provider, resolved_model = target.require()
    logger.info("MAF provider=%s model=%s", provider, resolved_model)
    with suppress_experimental_warnings():
        return _CLIENT_BUILDERS[provider](resolved_model), target
