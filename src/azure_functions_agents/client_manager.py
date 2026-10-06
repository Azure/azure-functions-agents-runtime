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
* :meth:`ClientManager.build_chat_client_with_target` — return a fresh client
    with authoritative inference-target metadata when available.
* :meth:`ClientManager.close` — release any resources held by the manager
  (called from the application's shutdown hook).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from ._credential import build_async_credential
from ._logger import logger
from .config.env import runtime_env_value

# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InferenceTarget:
    """Construction-time metadata for the model endpoint used by a chat client."""

    provider: str | None = None
    model: str | None = None


class ProviderKind(StrEnum):
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    FOUNDRY = "foundry"


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

    def build_chat_client_with_target(
        self, model: str | None
    ) -> tuple[Any, InferenceTarget]:
        """Construct a client and return any authoritative target metadata."""
        return self.build_chat_client(model), InferenceTarget()

    def validate_provider_settings(self, model: str | None) -> None:
        """Validate provider settings without constructing execution resources."""
        return None

    async def close(self) -> None:
        """Release any resources held by the manager. Default: no-op."""
        return None


# ---------------------------------------------------------------------------
# MAF implementation
# ---------------------------------------------------------------------------


_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_DEFAULT_FOUNDRY_MODEL = "gpt-4o-mini"


class MAFClientManager(ClientManager):
    """Build Microsoft Agent Framework chat clients.

    Selects a provider from environment variables — explicit
    ``AZURE_FUNCTIONS_AGENTS_PROVIDER=openai|azure_openai|foundry`` wins. Otherwise:

    1. ``AZURE_OPENAI_ENDPOINT``      → Azure OpenAI
    2. ``FOUNDRY_PROJECT_ENDPOINT``   → Microsoft Foundry
    3. ``OPENAI_API_KEY``             → vanilla OpenAI
    """

    name = "maf"

    def resolve_model(self, requested: str | None) -> str:
        """Resolve model as requested > provider-specific env > runtime env > default."""
        return self._resolve_model(requested, self._provider())

    def validate_provider_settings(self, model: str | None) -> None:
        """Validate built-in MAF provider settings without constructing a client."""
        provider = self._provider()
        self._resolve_model(model, provider)
        if provider == ProviderKind.OPENAI and not self._env("OPENAI_API_KEY"):
            raise RuntimeError(
                "AZURE_FUNCTIONS_AGENTS_PROVIDER=openai requires OPENAI_API_KEY to be set."
            )
        if provider == ProviderKind.AZURE_OPENAI and not self._env("AZURE_OPENAI_ENDPOINT"):
            raise RuntimeError(
                "AZURE_FUNCTIONS_AGENTS_PROVIDER=azure_openai requires "
                "AZURE_OPENAI_ENDPOINT to be set."
            )
        if provider == ProviderKind.FOUNDRY and not self._env("FOUNDRY_PROJECT_ENDPOINT"):
            raise RuntimeError(
                "AZURE_FUNCTIONS_AGENTS_PROVIDER=foundry requires "
                "FOUNDRY_PROJECT_ENDPOINT to be set."
            )
        if provider not in ProviderKind:
            raise RuntimeError(
                f"Unknown AZURE_FUNCTIONS_AGENTS_PROVIDER '{provider}'. "
                "Use one of: openai, azure_openai, foundry."
            )

    @classmethod
    def _resolve_model(cls, requested: str | None, provider: str) -> str:
        if requested:
            return requested
        runtime_model = runtime_env_value("AZURE_FUNCTIONS_AGENTS_MODEL")
        if provider == "azure_openai":
            return (
                os.environ.get("AZURE_OPENAI_DEPLOYMENT") or runtime_model or _DEFAULT_OPENAI_MODEL
            )
        if provider == "foundry":
            return os.environ.get("FOUNDRY_MODEL") or runtime_model or _DEFAULT_FOUNDRY_MODEL
        return runtime_model or _DEFAULT_OPENAI_MODEL

    def build_chat_client(self, model: str | None) -> Any:
        client, _ = self._build_maf_chat_client_with_target(model)
        return client

    def build_chat_client_with_target(
        self, model: str | None
    ) -> tuple[Any, InferenceTarget]:
        if self._has_custom_chat_client_builder():
            return self.build_chat_client(model), InferenceTarget()
        return self._build_maf_chat_client_with_target(model)

    def _has_custom_chat_client_builder(self) -> bool:
        """Return whether a subclass overrides the existing public builder hook."""
        return type(self).build_chat_client is not MAFClientManager.build_chat_client

    def _build_maf_chat_client_with_target(
        self, model: str | None
    ) -> tuple[Any, InferenceTarget]:
        target = _resolve_inference_target_for_manager(type(self), model)
        provider = cast(str, target.provider)
        resolved = cast(str, target.model)
        logger.info("MAF provider=%s model=%s", provider, resolved)
        if provider == "openai":
            client = self._build_openai(resolved)
        elif provider == "azure_openai":
            client = self._build_azure_openai(resolved)
        elif provider == "foundry":
            client = self._build_foundry(resolved)
        else:
            raise RuntimeError(
                f"Unknown AZURE_FUNCTIONS_AGENTS_PROVIDER '{provider}'. "
                "Use one of: openai, azure_openai, foundry."
            )
        return client, target

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
        return (os.environ.get(name) or "").strip()

    @classmethod
    def _provider(cls) -> str:
        explicit = cls._env("AZURE_FUNCTIONS_AGENTS_PROVIDER").lower()
        if explicit:
            return explicit
        if cls._env("AZURE_OPENAI_ENDPOINT"):
            return "azure_openai"
        if cls._env("FOUNDRY_PROJECT_ENDPOINT"):
            return "foundry"
        if cls._env("OPENAI_API_KEY"):
            return "openai"
        raise RuntimeError(
            "No MAF provider configured. Set one of: "
            "OPENAI_API_KEY (OpenAI), "
            "AZURE_OPENAI_ENDPOINT (+ AZURE_OPENAI_API_KEY or managed identity) for Azure OpenAI, "
            "or FOUNDRY_PROJECT_ENDPOINT for Microsoft Foundry. "
            "You can also set AZURE_FUNCTIONS_AGENTS_PROVIDER=openai|azure_openai|foundry "
            "to override."
        )

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
                "AZURE_FUNCTIONS_AGENTS_PROVIDER=azure_openai requires "
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
                "AZURE_FUNCTIONS_AGENTS_PROVIDER=foundry requires "
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
_BUILTIN_INSTANCE: MAFClientManager | None = None


def _resolve_inference_target_for_manager(
    manager_type: type[MAFClientManager], requested: str | None
) -> InferenceTarget:
    provider = manager_type._provider()
    return InferenceTarget(
        provider=provider,
        model=manager_type._resolve_model(requested, provider),
    )


def _resolve_builtin_inference_target(requested: str | None) -> InferenceTarget:
    """Resolve the built-in provider/model without constructing a MAF client."""
    return _resolve_inference_target_for_manager(MAFClientManager, requested)


def get_client_manager() -> ClientManager:
    """Return the process-wide :class:`ClientManager` instance.

    Today this always returns :class:`MAFClientManager`. Future versions may
    switch on an env var (e.g. ``AZURE_FUNCTIONS_AGENTS_PROVIDER``) to pick
    between alternative implementations.
    """
    global _BUILTIN_INSTANCE, _INSTANCE
    if _INSTANCE is None:
        built_in = MAFClientManager()
        _BUILTIN_INSTANCE = built_in
        _INSTANCE = built_in
        logger.info("ClientManager initialized: %s", _INSTANCE.name)
    return _INSTANCE


def _is_active_client_manager_builtin() -> bool:
    """Return whether the active manager is the runtime-created built-in instance."""
    manager = get_client_manager()
    return manager is _BUILTIN_INSTANCE and type(manager) is MAFClientManager


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
    manager, _INSTANCE = _INSTANCE, None
    if manager is not None:
        await manager.close()
