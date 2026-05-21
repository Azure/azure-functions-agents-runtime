from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from azure.identity import aio as azure_identity_aio
from pydantic import BaseModel, ConfigDict, model_validator

from azure_functions_agents._logger import logger

if TYPE_CHECKING:
    class ChatClientProtocol(Protocol):
        """Minimal protocol placeholder for provider factory typing."""

else:
    ChatClientProtocol = Any


class UnknownProviderError(ValueError):
    """Raised when an unrecognized provider name is requested."""


class ProviderConfigBase(BaseModel):
    """Base for per-provider config models. Unknown keys flow through as kwargs."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class OpenAIConfig(ProviderConfigBase):
    model: str
    base_url: str | None = None
    api_key: str | None = None


class AzureOpenAIConfig(ProviderConfigBase):
    model: str
    azure_endpoint: str
    api_version: str
    api_key: str | None = None
    managed_identity_client_id: str | None = None

    @model_validator(mode="after")
    def validate_auth_fields(self) -> AzureOpenAIConfig:
        if self.api_key is not None and self.managed_identity_client_id is not None:
            raise ValueError(
                "Cannot set both 'api_key' and 'managed_identity_client_id' on "
                "azure_openai. Use one or the other."
            )

        if self.model_extra and "credential" in self.model_extra:
            raise ValueError(
                "YAML configuration cannot accept a 'credential:' field — "
                "TokenCredential objects cannot be materialized from YAML. Use "
                "'api_key' or 'managed_identity_client_id' instead."
            )

        return self


class FoundryConfig(ProviderConfigBase):
    model: str
    project_endpoint: str
    managed_identity_client_id: str | None = None

    @model_validator(mode="after")
    def validate_credential_field(self) -> FoundryConfig:
        if self.model_extra and "credential" in self.model_extra:
            raise ValueError(
                "YAML configuration cannot accept a 'credential:' field — "
                "TokenCredential objects cannot be materialized from YAML. Use "
                "'managed_identity_client_id' instead."
            )

        return self


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    config_model: type[ProviderConfigBase]
    client_factory: Callable[..., ChatClientProtocol]


def _build_openai_client(**kwargs: Any) -> ChatClientProtocol:
    from agent_framework.openai import OpenAIChatClient

    return cast(ChatClientProtocol, OpenAIChatClient(**kwargs))


def _build_azure_openai_client(**kwargs: Any) -> ChatClientProtocol:
    # Per design decision: azure_openai stays on OpenAIChatClient (MAF tolerates the extra kwargs)
    from agent_framework.openai import OpenAIChatClient

    api_key = kwargs.get("api_key")
    explicit_credential = kwargs.get("credential")
    managed_identity_client_id = kwargs.pop("managed_identity_client_id", None)
    if api_key:
        auth_mode = "api_key"
    elif explicit_credential is not None:
        auth_mode = "credential_explicit"
    elif managed_identity_client_id:
        kwargs["credential"] = azure_identity_aio.DefaultAzureCredential(
            managed_identity_client_id=managed_identity_client_id
        )
        auth_mode = "managed_identity_user_assigned"
    elif os.environ.get("AZURE_OPENAI_API_KEY"):
        auth_mode = "api_key_env_fallback"
    else:
        kwargs["credential"] = azure_identity_aio.DefaultAzureCredential()
        auth_mode = "managed_identity_system_assigned"
    # AUDIT: never log kwarg values, never log raw config, never log a credential object.
    logger.info(
        "MAF auth provider=%s mode=%s mi_client_id_set=%s",
        "azure_openai",
        auth_mode,
        bool(managed_identity_client_id),
    )
    return cast(ChatClientProtocol, OpenAIChatClient(**kwargs))


def _build_foundry_client(**kwargs: Any) -> ChatClientProtocol:
    # This repo currently uses FoundryChatClient with the async DefaultAzureCredential.
    from agent_framework.foundry import FoundryChatClient

    explicit_credential = kwargs.get("credential")
    managed_identity_client_id = kwargs.pop("managed_identity_client_id", None)
    if explicit_credential is not None:
        auth_mode = "credential_explicit"
    elif managed_identity_client_id:
        kwargs["credential"] = azure_identity_aio.DefaultAzureCredential(
            managed_identity_client_id=managed_identity_client_id
        )
        auth_mode = "managed_identity_user_assigned"
    else:
        kwargs["credential"] = azure_identity_aio.DefaultAzureCredential()
        auth_mode = "managed_identity_system_assigned"
    # AUDIT: never log kwarg values, never log raw config, never log a credential object.
    logger.info(
        "MAF auth provider=%s mode=%s mi_client_id_set=%s",
        "foundry",
        auth_mode,
        bool(managed_identity_client_id),
    )
    return cast(ChatClientProtocol, FoundryChatClient(**kwargs))


PROVIDER_REGISTRY: dict[str, ProviderSpec] = {
    "openai": ProviderSpec("openai", OpenAIConfig, _build_openai_client),
    "azure_openai": ProviderSpec(
        "azure_openai",
        AzureOpenAIConfig,
        _build_azure_openai_client,
    ),
    "foundry": ProviderSpec("foundry", FoundryConfig, _build_foundry_client),
}


def get_provider(name: str) -> ProviderSpec:
    try:
        return PROVIDER_REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(PROVIDER_REGISTRY))
        raise UnknownProviderError(
            f"Unknown provider {name!r}; known providers are: {known}"
        ) from exc
