"""Chat-client construction helpers for built-in providers."""

from __future__ import annotations

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
    normalized = (provider or "").strip().lower().replace("-", "_")
    if not normalized:
        raise ClientFactoryError(
            "Provider must be supplied explicitly; endpoint-based provider autodetection "
            "is no longer supported."
        )
    return normalized


def build_chat_client(cfg: AgentConfiguration) -> ChatClient:
    """Construct and return a chat client from resolved agent configuration."""
    provider = _normalize_provider(cfg.provider)
    kwargs: dict[str, Any] = cfg.provider_config.model_dump(exclude_none=True)
    kwargs.setdefault("model", cfg.model)
    if cfg.timeout is not None:
        kwargs.setdefault("timeout", cfg.timeout)

    # AUDIT: never log kwarg values, never log raw config, never log a credential object.
    logger.info("MAF provider=%s kwargs=%s", provider, _safe_kwarg_summary(kwargs))
    spec = get_provider(provider)
    try:
        return spec.client_factory(**kwargs)
    except TypeError as exc:
        raise ClientFactoryError(
            f"Failed to construct MAF client for provider {provider!r}: {exc}. "
            f"Offending kwargs={list(kwargs)}. "
            f"Check your agent_configuration.{provider} sub-block."
        ) from exc


__all__ = [
    "ClientFactoryError",
]
