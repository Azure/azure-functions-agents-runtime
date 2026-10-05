"""Stable agent IDs from the Function App site name and canonical agent slug."""

from __future__ import annotations

from .config.env import EnvVar, runtime_env_value


def agent_id(agent_slug: str) -> str:
    """Return the lower-case site name (or local) qualified by the canonical slug."""
    if not agent_slug:
        raise ValueError("agent_slug must not be empty")
    site_name = runtime_env_value(EnvVar.WEBSITE_SITE_NAME).lower() or "local"
    return f"{site_name}/{agent_slug}"
