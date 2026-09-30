"""Stable agent identity helpers."""

from __future__ import annotations

import os
import uuid

RESOURCE_ID_ENV = "AZURE_FUNCTIONS_AGENTS_RESOURCE_ID"

_WEBSITE_OWNER_NAME_ENV = "WEBSITE_OWNER_NAME"
_WEBSITE_RESOURCE_GROUP_ENV = "WEBSITE_RESOURCE_GROUP"
_WEBSITE_SITE_NAME_ENV = "WEBSITE_SITE_NAME"


def _env_value(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def resolve_app_resource_id() -> str:
    """Resolve the current Function App resource identity."""
    override = _env_value(RESOURCE_ID_ENV)
    if override is not None:
        return override

    owner_name = _env_value(_WEBSITE_OWNER_NAME_ENV)
    resource_group = _env_value(_WEBSITE_RESOURCE_GROUP_ENV)
    site_name = _env_value(_WEBSITE_SITE_NAME_ENV)
    if owner_name is not None and resource_group is not None and site_name is not None:
        subscription_id = owner_name.split("+", 1)[0]
        if subscription_id:
            return (
                f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
                f"/providers/Microsoft.Web/sites/{site_name}"
            )

    if site_name is not None:
        return f"/providers/Microsoft.Web/sites/{site_name}"

    return "local"


def agent_id(agent_slug: str, *, resource_id: str | None = None) -> str:
    """Return a deterministic UUID for an agent in one Function App."""
    if not agent_slug:
        raise ValueError("agent_slug must not be empty")
    resolved_resource_id = resource_id if resource_id is not None else resolve_app_resource_id()
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"azure-functions-agents:{resolved_resource_id.lower()}/agents/{agent_slug}",
        )
    )
