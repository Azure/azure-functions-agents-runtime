"""Shared validation for persisted conversation-history identities."""

from __future__ import annotations

import re

AGENT_SLUG_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_agent_slug(agent_slug: str) -> str:
    """Return a canonical agent slug or raise before it becomes a path segment."""
    if not isinstance(agent_slug, str) or not AGENT_SLUG_PATTERN.fullmatch(agent_slug):
        raise ValueError(
            f"Invalid agent_slug (must match {AGENT_SLUG_PATTERN.pattern})"
        )
    return agent_slug
