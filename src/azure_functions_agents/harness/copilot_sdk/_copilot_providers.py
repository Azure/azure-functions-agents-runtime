"""Frozen Copilot SDK provider configuration for the local preview."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Final, Literal, Protocol, Self
from urllib.parse import urlsplit

from ...config.env import EnvVar, runtime_env_value
from .._harness_binding import UnsupportedCapabilityError
from .._provider_config import ProviderKind

if TYPE_CHECKING:
    from copilot.session import ProviderConfig, ProviderTokenArgs

type BearerTokenProvider = Callable[[ProviderTokenArgs], Awaitable[str]]
type ProviderWireType = Literal["openai", "azure"]
type WireApi = Literal["responses"]

_AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
_FOUNDRY_SCOPE = "https://ai.azure.com/.default"
_PROVIDER_WIRE_OPENAI: Final[ProviderWireType] = "openai"
_PROVIDER_WIRE_AZURE: Final[ProviderWireType] = "azure"
_WIRE_API_RESPONSES: Final[WireApi] = "responses"
_OPENAI_BASE_URL: Final = "https://api.openai.com/v1"
_FOUNDRY_OPENAI_V1_SUFFIX: Final = "/openai/v1"


class ProviderTokenSource(Protocol):
    def bearer_token_provider(self, scope: str, diagnostic: str) -> BearerTokenProvider:
        """Return a request-scoped bearer-token callback."""


def _validated_https_endpoint(
    env_name: EnvVar,
    *,
    host_check: Callable[[str], bool],
    path_check: Callable[[str], bool],
    allowed_ports: frozenset[int | None] | None,
    invalid_url_diagnostic: str,
    invalid_shape_diagnostic: str,
) -> str:
    endpoint = runtime_env_value(env_name).rstrip("/")
    try:
        url = urlsplit(endpoint)
        port = url.port
    except ValueError:
        raise UnsupportedCapabilityError(invalid_url_diagnostic) from None
    if (
        url.scheme != "https"
        or not host_check(url.hostname or "")
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or not path_check(url.path)
        or (allowed_ports is not None and port not in allowed_ports)
        or (allowed_ports is None and port == 0)
    ):
        raise UnsupportedCapabilityError(invalid_shape_diagnostic)
    return endpoint


def _azure_openai_endpoint() -> str:
    diagnostic = (
        f"Copilot Azure OpenAI requires a valid host-only {EnvVar.AZURE_OPENAI_ENDPOINT}."
    )
    return _validated_https_endpoint(
        EnvVar.AZURE_OPENAI_ENDPOINT,
        host_check=lambda host: bool(host) and not any(character.isspace() for character in host),
        path_check=lambda path: path in {"", "/"},
        allowed_ports=None,
        invalid_url_diagnostic=diagnostic,
        invalid_shape_diagnostic=diagnostic,
    )


def _foundry_endpoint() -> str:
    return _validated_https_endpoint(
        EnvVar.FOUNDRY_PROJECT_ENDPOINT,
        host_check=lambda host: host.endswith(".services.ai.azure.com"),
        path_check=lambda path: bool(re.fullmatch(r"/api/projects/[A-Za-z0-9_-]+", path)),
        allowed_ports=frozenset({None, 443}),
        invalid_url_diagnostic="Copilot Foundry preview requires a valid HTTPS project endpoint.",
        invalid_shape_diagnostic=(
            f"Copilot Foundry preview requires {EnvVar.FOUNDRY_PROJECT_ENDPOINT} in the form "
            "https://<resource>.services.ai.azure.com/api/projects/<project>."
        ),
    )


def _azure_openai_api_version() -> str | None:
    value = runtime_env_value(EnvVar.AZURE_OPENAI_API_VERSION)
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise UnsupportedCapabilityError(
            f"Copilot Azure OpenAI requires {EnvVar.AZURE_OPENAI_API_VERSION} "
            "to be a valid API-version token."
        )
    return value


class CopilotProvider(ABC):
    kind: ClassVar[ProviderKind]

    @classmethod
    @abstractmethod
    def from_environment(cls) -> Self:
        """Freeze provider settings selected from the current runtime environment."""

    @property
    @abstractmethod
    def auth_label(self) -> str:
        """Safe authentication label for diagnostics."""

    @abstractmethod
    def setup_diagnostic(self) -> str:
        """Return the safe provider-setup diagnostic."""

    @abstractmethod
    def sdk_config(self, model: str, tokens: ProviderTokenSource) -> ProviderConfig:
        """Build the SDK ProviderConfig lazily."""


@dataclass(frozen=True)
class OpenAIProvider(CopilotProvider):
    api_key: str = field(repr=False)

    kind: ClassVar[ProviderKind] = ProviderKind.OPENAI

    @classmethod
    def from_environment(cls) -> Self:
        api_key = runtime_env_value(EnvVar.OPENAI_API_KEY)
        if not api_key:
            raise UnsupportedCapabilityError(f"Copilot OpenAI requires {EnvVar.OPENAI_API_KEY}.")
        return cls(api_key)

    @property
    def auth_label(self) -> str:
        return EnvVar.OPENAI_API_KEY

    def setup_diagnostic(self) -> str:
        return (
            "Copilot OpenAI provider setup failed. Check the model and "
            f"{EnvVar.OPENAI_API_KEY} configuration."
        )

    def sdk_config(self, model: str, _tokens: ProviderTokenSource) -> ProviderConfig:
        from copilot.session import ProviderConfig

        async def token(_args: ProviderTokenArgs) -> str:
            return self.api_key

        return ProviderConfig(
            type=_PROVIDER_WIRE_OPENAI,
            wire_api=_WIRE_API_RESPONSES,
            base_url=_OPENAI_BASE_URL,
            model_id=model,
            wire_model=model,
            bearer_token_provider=token,
        )


@dataclass(frozen=True)
class AzureOpenAIProvider(CopilotProvider):
    endpoint: str = field(repr=False)
    api_version: str | None = None
    api_key: str | None = field(default=None, repr=False)

    kind: ClassVar[ProviderKind] = ProviderKind.AZURE_OPENAI

    @classmethod
    def from_environment(cls) -> Self:
        return cls(
            endpoint=_azure_openai_endpoint(),
            api_version=_azure_openai_api_version(),
            api_key=runtime_env_value(EnvVar.AZURE_OPENAI_API_KEY) or None,
        )

    @property
    def auth_label(self) -> str:
        return EnvVar.AZURE_OPENAI_API_KEY if self.api_key else "Azure credential"

    def setup_diagnostic(self) -> str:
        return (
            "Copilot Azure OpenAI provider setup failed. Check "
            f"{EnvVar.AZURE_OPENAI_ENDPOINT}, {EnvVar.AZURE_OPENAI_API_VERSION}, "
            "the deployment, and the approved "
            f"{self.auth_label} configuration."
        )

    def sdk_config(self, model: str, tokens: ProviderTokenSource) -> ProviderConfig:
        from copilot.session import ProviderConfig

        provider = ProviderConfig(
            type=_PROVIDER_WIRE_AZURE,
            wire_api=_WIRE_API_RESPONSES,
            base_url=self.endpoint,
            model_id=model,
            wire_model=model,
        )
        if self.api_version is not None:
            provider["azure"] = {"api_version": self.api_version}
        if self.api_key:
            provider["api_key"] = self.api_key
        else:
            provider["bearer_token_provider"] = tokens.bearer_token_provider(
                _AZURE_OPENAI_SCOPE,
                "Copilot Azure OpenAI could not acquire an Entra token. "
                "Check the approved Azure credential configuration and deployment access.",
            )
        return provider


@dataclass(frozen=True)
class FoundryProvider(CopilotProvider):
    endpoint: str = field(repr=False)

    kind: ClassVar[ProviderKind] = ProviderKind.FOUNDRY

    @classmethod
    def from_environment(cls) -> Self:
        return cls(_foundry_endpoint())

    @property
    def auth_label(self) -> str:
        return "Azure credential"

    def setup_diagnostic(self) -> str:
        return (
            f"Copilot Foundry provider setup failed. Check {EnvVar.FOUNDRY_PROJECT_ENDPOINT}, "
            "the deployment, and the approved Azure credential configuration."
        )

    def sdk_config(self, model: str, tokens: ProviderTokenSource) -> ProviderConfig:
        from copilot.session import ProviderConfig

        return ProviderConfig(
            type=_PROVIDER_WIRE_OPENAI,
            wire_api=_WIRE_API_RESPONSES,
            base_url=f"{self.endpoint}{_FOUNDRY_OPENAI_V1_SUFFIX}",
            model_id=model,
            wire_model=model,
            bearer_token_provider=tokens.bearer_token_provider(
                _FOUNDRY_SCOPE,
                "Copilot Foundry preview could not acquire an Entra token. "
                "Check the approved Azure credential configuration and project access.",
            ),
        )


_PROVIDERS: dict[ProviderKind, type[CopilotProvider]] = {
    provider.kind: provider
    for provider in (OpenAIProvider, AzureOpenAIProvider, FoundryProvider)
}
