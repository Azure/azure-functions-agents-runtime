"""Shared validation for persisted conversation-history identities."""

from __future__ import annotations

import re
import threading

from ._logger import logger

AGENT_SLUG_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LEGACY_HISTORY_WARNINGS: set[tuple[str, str]] = set()
_LEGACY_HISTORY_WARNINGS_LOCK = threading.Lock()


def validate_agent_slug(agent_slug: str) -> str:
    """Return a canonical agent slug or raise before it becomes a path segment."""
    if not isinstance(agent_slug, str) or not AGENT_SLUG_PATTERN.fullmatch(agent_slug):
        raise ValueError(
            f"Invalid agent_slug (must match {AGENT_SLUG_PATTERN.pattern})"
        )
    return agent_slug


def warn_legacy_history_detected(*, agent_slug: str, backend: str) -> None:
    """Warn once per process when a backend finds a legacy unscoped history path."""
    key = (backend, agent_slug)
    with _LEGACY_HISTORY_WARNINGS_LOCK:
        if key in _LEGACY_HISTORY_WARNINGS:
            return
        _LEGACY_HISTORY_WARNINGS.add(key)

    logger.warning(
        "Legacy unscoped chat history path detected; agent-scoped history remains empty "
        "(agent_slug=%s backend=%s).",
        agent_slug,
        backend,
    )
