"""Chat-client construction helpers for built-in providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from .._logger import logger
from .providers import ChatClient, get_provider

if TYPE_CHECKING:
    from ..config.schema import AgentConfiguration


def _safe_kwarg_summary(kwargs: dict[str, Any]) -> list[str]:
    """Return sorted kwarg key names for logging. Never returns values."""
    return sorted(kwargs.keys())


class ClientFactoryError(RuntimeError):
    """Raised when a provider client cannot be constructed from config."""


def _normalize_provider(provider: str | None) -> str:
    normalized = (provider or '').strip().lower().replace('-', '_')
    if not normalized:
        raise ClientFactoryError(
            'Provider must be supplied explicitly in agent_configuration.provider.'
        )
    return normalized


def build_chat_client(cfg: AgentConfiguration) -> ChatClient:
    """Construct and return a chat client from resolved agent configuration."""
    provider = _normalize_provider(cfg.provider)
    kwargs: dict[str, Any] = cfg.provider_config.model_dump(exclude_none=True)
    kwargs['model'] = cfg.model

    logger.info('MAF provider=%s kwargs=%s', provider, _safe_kwarg_summary(kwargs))
    spec = get_provider(provider)
    try:
        return spec.client_factory(**kwargs)
    except TypeError as exc:
        raise ClientFactoryError(
            f'Failed to construct MAF client for provider {provider!r}: {exc}. '
            f'Offending kwargs={list(kwargs)}. '
            f'Check your agent_configuration.{provider} sub-block.'
        ) from exc


class ClientManager(ABC):
    """Provider-agnostic interface for building chat clients."""

    name: str = 'abstract'

    @abstractmethod
    def build_chat_client(self, cfg: AgentConfiguration) -> ChatClient:
        """Construct and return a chat client for the given configuration."""

    async def close(self) -> None:
        return None


class MAFClientManager(ClientManager):
    """Default client manager backed by the built-in provider registry."""

    name = 'maf'

    def build_chat_client(self, cfg: AgentConfiguration) -> ChatClient:
        return build_chat_client(cfg)


_INSTANCE: ClientManager | None = None


def get_client_manager() -> ClientManager:
    """Return the process-wide :class:`ClientManager` instance."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = MAFClientManager()
        logger.info('ClientManager initialized: %s', _INSTANCE.name)
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
    'ClientFactoryError',
    'ClientManager',
    'MAFClientManager',
    'build_chat_client',
    'get_client_manager',
    'set_client_manager',
    'shutdown_client_manager',
]
