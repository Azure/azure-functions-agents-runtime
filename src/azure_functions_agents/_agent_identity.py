"""Stable agent identity helpers using a best-effort app correlation key.

The key is not a guaranteed unique Azure resource ID. Platform-provided values
vary by Linux SKU: Flex Consumption/Legion can omit the owner name, while
Premium/Dedicated resolved references or app settings can override values.
When deployment id is missing, site name is included with owner when available
because an owner-only key is shared across apps in the same webspace.
"""

from __future__ import annotations

from .config.env import runtime_env_value

_WEBSITE_DEPLOYMENT_ID_ENV = "WEBSITE_DEPLOYMENT_ID"
_WEBSITE_OWNER_NAME_ENV = "WEBSITE_OWNER_NAME"
_WEBSITE_SITE_NAME_ENV = "WEBSITE_SITE_NAME"


def resolve_app_correlation_key() -> str:
    """Resolve a lower-case, best-effort app key from platform env."""
    owner_name = runtime_env_value(_WEBSITE_OWNER_NAME_ENV)
    deployment_id = runtime_env_value(_WEBSITE_DEPLOYMENT_ID_ENV)
    site_name = runtime_env_value(_WEBSITE_SITE_NAME_ENV)
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
