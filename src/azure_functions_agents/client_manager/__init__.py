"""Pluggable chat-client providers.

The runtime uses an abstract :class:`ClientManager` so that different
backends can be plugged in without touching agent registration or runtime
layers.

Only one implementation ships today: :class:`MAFClientManager`. It is selected
automatically by :func:`get_client_manager` and lives behind a process-wide
singleton.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from .._logger import logger
from .providers import get_provider

if TYPE_CHECKING:
    from ..config.schema import AgentConfiguration


def _safe_kwarg_summary(kwargs: dict[str, Any]) -> list[str]:
    """Return sorted kwarg key names for logging. Never returns values."""
    return sorted(kwargs.keys())


class ClientFactoryError(RuntimeError):
    """Raised when a provider client cannot be constructed from config."""


class ClientManager(ABC):
    """Provider-agnostic interface for building chat clients."""

    name: str = "abstract"

    @abstractmethod
    def get_chat_client(self, cfg: AgentConfiguration) -> Any:
        """Construct and return a chat client from resolved agent configuration."""

    async def close(self) -> None:
        """Release any resources held by the manager. Default: no-op."""
        return None


def _normalize_provider(provider: str | None) -> str:
    normalized = (provider or "").strip().lower().replace("-", "_")
    if not normalized:
        raise ClientFactoryError(
            "Provider must be supplied explicitly; endpoint-based provider autodetection "
            "is no longer supported."
        )
    return normalized


class MAFClientManager(ClientManager):
    """Build Microsoft Agent Framework chat clients."""

    name = "maf"

    def get_chat_client(self, cfg: AgentConfiguration) -> Any:
        provider = _normalize_provider(cfg.provider)
        kwargs: dict[str, Any] = cfg.provider_config.model_dump(exclude_none=True)
        if cfg.timeout is not None:
            kwargs.setdefault("timeout", cfg.timeout)

        # AUDIT: never log kwarg values, never log raw config, never log a credential object.
        logger.info("MAF provider=%s kwargs=%s", provider, _safe_kwarg_summary(kwargs))
        return self._construct_chat_client(provider, kwargs)

    @staticmethod
    def _construct_chat_client(provider: str, kwargs: dict[str, Any]) -> Any:
        spec = get_provider(provider)
        try:
            return spec.client_factory(**kwargs)
        except TypeError as exc:
            raise ClientFactoryError(
                f"Failed to construct MAF client for provider {provider!r}: {exc}. "
                f"Offending kwargs={list(kwargs)}. "
                f"Check your agent_configuration.{provider} sub-block."
            ) from exc


_INSTANCE: ClientManager | None = None


def get_client_manager() -> ClientManager:
    """Return the process-wide :class:`ClientManager` instance."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = MAFClientManager()
        logger.info("ClientManager initialized: %s", _INSTANCE.name)
    return _INSTANCE


def set_client_manager(manager: ClientManager) -> None:
    """Override the process-wide :class:`ClientManager`."""
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


__all__ = [
    "ClientFactoryError",
    "ClientManager",
    "MAFClientManager",
    "get_client_manager",
    "set_client_manager",
    "shutdown_client_manager",
]
