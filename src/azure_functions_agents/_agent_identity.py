"""Stable agent identity helpers using a best-effort app correlation key.

The key is not a guaranteed unique Azure resource ID. Platform-provided values
vary by Linux SKU: Flex Consumption/Legion can omit the owner name, while
Premium/Dedicated resolved references or app settings can override values.
"""

from __future__ import annotations

import os
import uuid

_WEBSITE_DEPLOYMENT_ID_ENV = "WEBSITE_DEPLOYMENT_ID"
_WEBSITE_OWNER_NAME_ENV = "WEBSITE_OWNER_NAME"
_WEBSITE_SITE_NAME_ENV = "WEBSITE_SITE_NAME"


def _env_value(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def resolve_app_correlation_key() -> str:
    """Resolve a best-effort app key from platform/customer-controlled env."""
    owner_name = _env_value(_WEBSITE_OWNER_NAME_ENV)
    deployment_id = _env_value(_WEBSITE_DEPLOYMENT_ID_ENV)
    site_name = _env_value(_WEBSITE_SITE_NAME_ENV)
    if owner_name is not None and deployment_id is not None:
        return f"{owner_name}/{deployment_id}"

    if site_name is not None:
        return f"site/{site_name}"

    return "local"


def agent_id(agent_slug: str, *, correlation_key: str | None = None) -> str:
    """Return a deterministic UUID for an agent and app correlation key."""
    if not agent_slug:
        raise ValueError("agent_slug must not be empty")
    resolved_key = correlation_key if correlation_key is not None else resolve_app_correlation_key()
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"azure-functions-agents:{resolved_key.lower()}/agents/{agent_slug}",
        )
    )
