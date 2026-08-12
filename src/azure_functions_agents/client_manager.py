"""Pluggable chat-client providers.

The runtime uses an abstract :class:`ClientManager` so that different
backends (today: Microsoft Agent Framework via Azure OpenAI / OpenAI / Foundry;
in the future: other agent frameworks) can be plugged in without touching the
agent registration or HTTP/streaming layers.

Only one implementation ships today: :class:`MAFClientManager`. It is selected
automatically by :func:`get_client_manager` and lives behind a process-wide
singleton. It caches provider clients by resolved target so one Python worker
reuses the underlying HTTP connection pools across requests.

ABC surface
-----------

* :meth:`ClientManager.resolve_model` — pick the actual model/deployment to
  use given an optional per-call request.
* :meth:`ClientManager.build_chat_client` — return a ``ChatClient`` bound to a
  specific model.
* :meth:`ClientManager.build_chat_client_with_target` — return a client with
  authoritative inference-target metadata when available.
* :meth:`ClientManager.close` — release any resources held by the manager
  when an embedding host owns an async shutdown lifecycle.
"""

from __future__ import annotations

import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

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


@dataclass(frozen=True)
class _ClientCacheKey:
    provider: str
    model: str


@dataclass(frozen=True)
class _ProviderConfig:
    endpoint: str = ""
    api_version: str = ""
    auth_mode: str = ""
    organization: str = ""


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

    def __init__(self) -> None:
        self._clients: dict[_ClientCacheKey, Any] = {}
        self._provider_configs: dict[str, _ProviderConfig] = {}
        self._async_credential: Any | None = None
        self._lock = threading.RLock()
        self._closed = False

    def resolve_model(self, requested: str | None) -> str:
        """Resolve model as requested > provider-specific env > runtime env > default."""
        return self._resolve_model(requested, self._provider())

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
        provider = self._provider()
        resolved = self._resolve_model(model, provider)
        cache_key = _ClientCacheKey(provider, resolved)
        with self._lock:
            if self._closed:
                raise RuntimeError("MAFClientManager is closed and cannot build new clients.")
            provider_config = self._provider_config(provider)
            previous_config = self._provider_configs.get(provider)
            if previous_config is not None and previous_config != provider_config:
                raise RuntimeError(
                    f"{provider} provider configuration changed during this worker lifetime; "
                    "restart the worker to apply updated settings"
                )
            client = self._clients.get(cache_key)
            if client is None:
                client = self._build_provider_client(provider, resolved)
                self._provider_configs[provider] = provider_config
                self._clients[cache_key] = client
                logger.info("Created MAF provider client: provider=%s model=%s", provider, resolved)
            else:
                logger.debug("Reusing MAF provider client: provider=%s model=%s", provider, resolved)
        return client, InferenceTarget(
            provider=provider,
            model=resolved,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_provider_client(self, provider: str, model: str) -> Any:
        if provider == "openai":
            return self._build_openai(model)
        if provider == "azure_openai":
            return self._build_azure_openai(model)
        if provider == "foundry":
            return self._build_foundry(model)
        raise RuntimeError(
            f"Unknown AZURE_FUNCTIONS_AGENTS_PROVIDER '{provider}'. "
            "Use one of: openai, azure_openai, foundry."
        )

    @classmethod
    def _provider_config(cls, provider: str) -> _ProviderConfig:
        if provider == "openai":
            return _ProviderConfig(
                endpoint=cls._env("OPENAI_BASE_URL"),
                auth_mode="api_key",
                organization=cls._env("OPENAI_ORG_ID"),
            )
        if provider == "azure_openai":
            return _ProviderConfig(
                endpoint=cls._env("AZURE_OPENAI_ENDPOINT"),
                api_version=cls._env("AZURE_OPENAI_API_VERSION"),
                auth_mode="api_key" if cls._env("AZURE_OPENAI_API_KEY") else "credential",
            )
        if provider == "foundry":
            return _ProviderConfig(
                endpoint=cls._env("FOUNDRY_PROJECT_ENDPOINT"),
                auth_mode="credential",
            )
        return _ProviderConfig()

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

    def _build_azure_openai(self, model: str) -> Any:
        from agent_framework.openai import OpenAIChatClient

        endpoint = self._env("AZURE_OPENAI_ENDPOINT")
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
        api_version = self._env("AZURE_OPENAI_API_VERSION")
        if api_version:
            kwargs["api_version"] = api_version
        api_key = self._env("AZURE_OPENAI_API_KEY")
        if api_key:
            kwargs["api_key"] = api_key
        else:
            kwargs["credential"] = self._get_async_credential()
        return OpenAIChatClient(**kwargs)

    def _build_foundry(self, model: str) -> Any:
        from agent_framework.foundry import FoundryChatClient

        endpoint = self._env("FOUNDRY_PROJECT_ENDPOINT")
        if not endpoint:
            raise RuntimeError(
                "AZURE_FUNCTIONS_AGENTS_PROVIDER=foundry requires "
                "FOUNDRY_PROJECT_ENDPOINT to be set."
            )
        return FoundryChatClient(
            project_endpoint=endpoint,
            model=model,
            credential=self._get_async_credential(),
        )

    def _get_async_credential(self) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("MAFClientManager is closed and cannot build a credential.")
            if self._async_credential is None:
                self._async_credential = build_async_credential()
            return self._async_credential

    async def close(self) -> None:
        """Close all provider transports and the shared credential exactly once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            cached_clients = list(self._clients.items())
            self._clients.clear()
            self._provider_configs.clear()
            credential = self._async_credential
            self._async_credential = None

        errors: list[Exception] = []
        closed_resource_ids: set[int] = set()
        for key, chat_client in cached_clients:
            await self._close_owned_resource(
                getattr(chat_client, "client", None),
                f"{key.provider} AsyncOpenAI transport",
                errors,
                closed_resource_ids,
            )
            if key.provider == "foundry":
                await self._close_owned_resource(
                    getattr(chat_client, "project_client", None),
                    "Foundry AIProjectClient transport",
                    errors,
                    closed_resource_ids,
                )
        await self._close_owned_resource(
            credential,
            "shared async credential",
            errors,
            closed_resource_ids,
            required=False,
        )
        if errors:
            raise ExceptionGroup("Failed to close one or more MAF client resources.", errors)

    @staticmethod
    async def _close_owned_resource(
        resource: Any,
        label: str,
        errors: list[Exception],
        closed_resource_ids: set[int],
        *,
        required: bool = True,
    ) -> None:
        if resource is None:
            if required:
                logger.warning(
                    "%s is unavailable; agent-framework 1.3 client internals may have changed.",
                    label,
                )
            return
        resource_id = id(resource)
        if resource_id in closed_resource_ids:
            return
        closed_resource_ids.add(resource_id)
        close = getattr(resource, "close", None)
        if not callable(close):
            if required:
                logger.warning("%s does not expose close().", label)
            return
        try:
            result = close()
            if isawaitable(result):
                await result
        except Exception as exc:
            logger.error("Failed to close %s: %s", label, exc)
            errors.append(exc)


# ---------------------------------------------------------------------------
# Process-wide singleton selection
# ---------------------------------------------------------------------------

_SHUTTING_DOWN = object()
_INSTANCE: ClientManager | object | None = None
_INSTANCE_LOCK = threading.Lock()


def get_client_manager() -> ClientManager:
    """Return the process-wide :class:`ClientManager` instance.

    Today this always returns :class:`MAFClientManager`. Future versions may
    switch on an env var (e.g. ``AZURE_FUNCTIONS_AGENTS_PROVIDER``) to pick
    between alternative implementations.
    """
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is _SHUTTING_DOWN:
            raise RuntimeError("Client manager shutdown is in progress")
        if _INSTANCE is None:
            _INSTANCE = MAFClientManager()
            logger.info("ClientManager initialized: %s", _INSTANCE.name)
        assert isinstance(_INSTANCE, ClientManager)
        return _INSTANCE


def set_client_manager(manager: ClientManager) -> None:
    """Override the process-wide :class:`ClientManager`.

    Intended for tests and for advanced apps that want to plug in a custom
    backend. Call and await :func:`shutdown_client_manager` before replacing
    any active manager, including a custom implementation.
    """
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is _SHUTTING_DOWN:
            raise RuntimeError("Client manager shutdown is in progress")
        current = _INSTANCE
        if current is manager:
            return
        if current is not None:
            raise RuntimeError(
                "Cannot replace the active ClientManager. "
                "Await shutdown_client_manager() before replacing it."
            )
        _INSTANCE = manager


async def shutdown_client_manager() -> None:
    """Close the active manager; sequential calls are safe, concurrent calls are rejected."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        manager = _INSTANCE
        if manager is _SHUTTING_DOWN:
            raise RuntimeError("Client manager shutdown is already in progress")
        if manager is None:
            return
        _INSTANCE = _SHUTTING_DOWN
    assert isinstance(manager, ClientManager)
    try:
        await manager.close()
    finally:
        with _INSTANCE_LOCK:
            if _INSTANCE is _SHUTTING_DOWN:
                _INSTANCE = None
