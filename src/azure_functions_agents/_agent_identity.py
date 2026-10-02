"""Shared readable agent identity and best-effort app correlation key."""

from __future__ import annotations

from .config.env import EnvVar, runtime_env_value


def resolve_app_correlation_key() -> str:
    """Resolve a lower-case, best-effort app key from platform env."""
    owner_name = runtime_env_value(EnvVar.WEBSITE_OWNER_NAME)
    deployment_id = runtime_env_value(EnvVar.WEBSITE_DEPLOYMENT_ID)
    site_name = runtime_env_value(EnvVar.WEBSITE_SITE_NAME)
    parts = [owner_name]
    if deployment_id:
        parts.append(deployment_id)
    elif site_name:
        parts.append(site_name)
    return "/".join(part for part in parts if part).lower() or "local"


def agent_id(agent_slug: str, *, correlation_key: str | None = None) -> str:
    """Return a human-readable stable id for an agent and best-effort app key."""
    if not agent_slug:
        raise ValueError("agent_slug must not be empty")
    resolved_key = correlation_key if correlation_key is not None else resolve_app_correlation_key()
    return f"{resolved_key.lower()}/{agent_slug}"
