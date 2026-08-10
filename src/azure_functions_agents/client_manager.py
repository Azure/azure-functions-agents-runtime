"""Pluggable chat-client providers.

The runtime uses an abstract :class:`ClientManager` so that different
backends (today: Microsoft Agent Framework via Azure OpenAI / OpenAI / Foundry;
in the future: other agent frameworks) can be plugged in without touching the
agent registration or HTTP/streaming layers.

Only one implementation ships today: :class:`MAFClientManager`. It is selected
automatically by :func:`get_client_manager` and lives behind a process-wide
singleton because building a provider client (and the underlying credential
caches it owns) is cheap to share across requests.

ABC surface
-----------

* :meth:`ClientManager.resolve_model` — pick the actual model/deployment to
  use given an optional per-call request.
* :meth:`ClientManager.build_chat_client` — return a fresh ``ChatClient``
  bound to a specific model.
* :meth:`ClientManager.close` — release any resources held by the manager
  (called from the application's shutdown hook).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from ._credential import build_async_credential
from ._logger import logger
from .config.env import runtime_backend_env_value, runtime_backend_env_value_from

# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class ClientManager(ABC):
    """Provider-agnostic interface for building chat clients."""

    name: str = "abstract"

    @abstractmethod
    def resolve_model(self, requested: str | None) -> str:
        """Return the model/deployment id to use for this turn.

        ``requested`` is the per-call value (e.g. from the agent's frontmatter
        or from an explicit override). Implementations should fall back to
        environment variables and finally a sensible default.
        """

    @abstractmethod
    def build_chat_client(self, model: str | None) -> Any:
        """Construct and return a chat client for the given model.

        ``model`` may be ``None``, in which case the implementation MUST call
        :meth:`resolve_model` itself. The return type is intentionally
        ``Any`` so different framework SDKs can be plugged in.
        """

    async def close(self) -> None:
        """Release any resources held by the manager. Default: no-op."""
        return None


# ---------------------------------------------------------------------------
# MAF implementation
# ---------------------------------------------------------------------------


_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_DEFAULT_FOUNDRY_MODEL = "gpt-4o-mini"


class MAFProvider(StrEnum):
    """The supported MAF provider wire values."""

    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    FOUNDRY = "foundry"


def resolve_maf_provider(
    environment: Mapping[str, str],
    *,
    required: bool = True,
) -> MAFProvider | None:
    """Resolve MAF provider precedence from an explicit environment mapping."""

    explicit = runtime_backend_env_value_from(
        environment,
        "AZURE_FUNCTIONS_AGENTS_PROVIDER",
    ).lower()
    if explicit:
        try:
            return MAFProvider(explicit)
        except ValueError:
            accepted = ", ".join(provider.value for provider in MAFProvider)
            raise RuntimeError(
                f"Unknown AZURE_FUNCTIONS_AGENTS_PROVIDER '{explicit}'. "
                f"Use one of: {accepted}."
            ) from None
    if runtime_backend_env_value_from(environment, "AZURE_OPENAI_ENDPOINT"):
        return MAFProvider.AZURE_OPENAI
    if runtime_backend_env_value_from(environment, "FOUNDRY_PROJECT_ENDPOINT"):
        return MAFProvider.FOUNDRY
    if runtime_backend_env_value_from(environment, "OPENAI_API_KEY"):
        return MAFProvider.OPENAI
    if not required:
        return None
    raise RuntimeError(
        "No MAF provider configured. Set one of: "
        "OPENAI_API_KEY (OpenAI), "
        "AZURE_OPENAI_ENDPOINT (+ AZURE_OPENAI_API_KEY or managed identity) for Azure OpenAI, "
        "or FOUNDRY_PROJECT_ENDPOINT for Microsoft Foundry. "
        "You can also set AZURE_FUNCTIONS_AGENTS_PROVIDER="
        f"{'|'.join(provider.value for provider in MAFProvider)} to override."
    )


class MAFClientManager(ClientManager):
    """Build Microsoft Agent Framework chat clients.

    Selects a provider from environment variables — an explicit
    ``AZURE_FUNCTIONS_AGENTS_PROVIDER`` value wins. Otherwise:

    1. ``AZURE_OPENAI_ENDPOINT``      → Azure OpenAI
    2. ``FOUNDRY_PROJECT_ENDPOINT``   → Microsoft Foundry
    3. ``OPENAI_API_KEY``             → vanilla OpenAI
    """

    name = "maf"

    def resolve_model(self, requested: str | None) -> str:
        """Resolve model as requested > provider-specific env > runtime env > default."""
        if requested:
            return requested
        provider = self._provider()
        runtime_model = runtime_backend_env_value("AZURE_FUNCTIONS_AGENTS_MODEL")
        if provider is MAFProvider.AZURE_OPENAI:
            return self._env("AZURE_OPENAI_DEPLOYMENT") or runtime_model or _DEFAULT_OPENAI_MODEL
        if provider is MAFProvider.FOUNDRY:
            return self._env("FOUNDRY_MODEL") or runtime_model or _DEFAULT_FOUNDRY_MODEL
        if provider is MAFProvider.OPENAI:
            return runtime_model or _DEFAULT_OPENAI_MODEL
        raise AssertionError("Resolved MAF provider is unsupported.")

    def build_chat_client(self, model: str | None) -> Any:
        provider = self._provider()
        resolved = self.resolve_model(model)
        logger.info("MAF provider=%s model=%s", provider, resolved)
        if provider is MAFProvider.OPENAI:
            return self._build_openai(resolved)
        if provider is MAFProvider.AZURE_OPENAI:
            return self._build_azure_openai(resolved)
        if provider is MAFProvider.FOUNDRY:
            return self._build_foundry(resolved)
        raise AssertionError("Resolved MAF provider is unsupported.")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _env(name: str) -> str:
        """Return ``$name`` stripped, or ``""`` if missing/blank.

        Empty-string env vars are common in local.settings.json templates and
        ``azd env set X ""`` workflows. We treat them as if the variable were
        unset so auto-detection does not pick them up.
        """
        return runtime_backend_env_value(name)

    @classmethod
    def _provider(cls) -> MAFProvider:
        provider = resolve_maf_provider(os.environ)
        assert provider is not None
        return provider

    @classmethod
    def _build_openai(cls, model: str) -> Any:
        from agent_framework.openai import OpenAIChatClient

        return OpenAIChatClient(
            model=model,
            api_key=cls._env("OPENAI_API_KEY") or None,
        )

    @classmethod
    def _build_azure_openai(cls, model: str) -> Any:
        from agent_framework.openai import OpenAIChatClient

        endpoint = cls._env("AZURE_OPENAI_ENDPOINT")
        if not endpoint:
            raise RuntimeError(
                f"AZURE_FUNCTIONS_AGENTS_PROVIDER={MAFProvider.AZURE_OPENAI.value} requires "
                "AZURE_OPENAI_ENDPOINT to be set."
            )
        kwargs: dict[str, Any] = {
            "model": model,
            "azure_endpoint": endpoint,
        }
        # Only forward api_version when the user explicitly sets it. MAF defaults
        # to the Responses API ("preview") which rejects Chat Completions GA
        # versions like "2024-10-21" with "API version not supported".
        api_version = cls._env("AZURE_OPENAI_API_VERSION")
        if api_version:
            kwargs["api_version"] = api_version
        api_key = cls._env("AZURE_OPENAI_API_KEY")
        if api_key:
            kwargs["api_key"] = api_key
        else:
            kwargs["credential"] = build_async_credential()
        return OpenAIChatClient(**kwargs)

    @classmethod
    def _build_foundry(cls, model: str) -> Any:
        from agent_framework.foundry import FoundryChatClient

        endpoint = cls._env("FOUNDRY_PROJECT_ENDPOINT")
        if not endpoint:
            raise RuntimeError(
                f"AZURE_FUNCTIONS_AGENTS_PROVIDER={MAFProvider.FOUNDRY.value} requires "
                "FOUNDRY_PROJECT_ENDPOINT to be set."
            )
        return FoundryChatClient(
            project_endpoint=endpoint,
            model=model,
            credential=build_async_credential(),
        )


# ---------------------------------------------------------------------------
# Process-wide singleton selection
# ---------------------------------------------------------------------------

_INSTANCE: ClientManager | None = None


def get_client_manager() -> ClientManager:
    """Return the process-wide :class:`ClientManager` instance.

    Today this always returns :class:`MAFClientManager`. Future versions may
    switch on an env var (e.g. ``AZURE_FUNCTIONS_AGENTS_PROVIDER``) to pick
    between alternative implementations.
    """
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = MAFClientManager()
        logger.info("ClientManager initialized: %s", _INSTANCE.name)
    return _INSTANCE


def set_client_manager(manager: ClientManager) -> None:
    """Override the process-wide :class:`ClientManager`.

    Intended for tests and for advanced apps that want to plug in a custom
    backend.
    """
    global _INSTANCE
    _INSTANCE = manager


async def shutdown_client_manager() -> None:
    """Close the active manager (if any). Idempotent."""
    global _INSTANCE
    if _INSTANCE is not None:
        try:
            await _INSTANCE.close()
        finally:
            _INSTANCE = None
