"""Tests for stable agent identity helpers."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from azure_functions_agents._agent_identity import (
    RESOURCE_ID_ENV,
    agent_id,
    resolve_app_resource_id,
)

_IDENTITY_ENV_VARS = (
    RESOURCE_ID_ENV,
    "WEBSITE_OWNER_NAME",
    "WEBSITE_RESOURCE_GROUP",
    "WEBSITE_SITE_NAME",
)


@pytest.fixture(autouse=True)
def clear_identity_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in _IDENTITY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


def test_resource_id_override_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RESOURCE_ID_ENV, "  /subscriptions/override/resourceGroups/rg  ")
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "sub+rg-eastuswebspace")
    monkeypatch.setenv("WEBSITE_RESOURCE_GROUP", "site-rg")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "site")

    assert resolve_app_resource_id() == "/subscriptions/override/resourceGroups/rg"


def test_full_resource_id_built_from_website_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "sub-id+rg-eastuswebspace")
    monkeypatch.setenv("WEBSITE_RESOURCE_GROUP", "app-rg")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "agent-app")

    assert resolve_app_resource_id() == (
        "/subscriptions/sub-id/resourceGroups/app-rg/providers/Microsoft.Web/sites/agent-app"
    )


def test_site_name_only_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_SITE_NAME", "agent-app")

    assert resolve_app_resource_id() == "/providers/Microsoft.Web/sites/agent-app"


def test_local_fallback() -> None:
    assert resolve_app_resource_id() == "local"


def test_blank_values_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RESOURCE_ID_ENV, "  ")
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "sub-id+rg-eastuswebspace")
    monkeypatch.setenv("WEBSITE_RESOURCE_GROUP", "  ")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "agent-app")

    assert resolve_app_resource_id() == "/providers/Microsoft.Web/sites/agent-app"


def test_agent_id_is_deterministic_valid_and_distinguishes_inputs() -> None:
    first = agent_id("billing", resource_id="/subscriptions/SUB/resourceGroups/RG/sites/App")
    second = agent_id("billing", resource_id="/subscriptions/SUB/resourceGroups/RG/sites/App")
    different_slug = agent_id("support", resource_id="/subscriptions/SUB/resourceGroups/RG/sites/App")
    different_resource = agent_id("billing", resource_id="/subscriptions/OTHER/resourceGroups/RG/sites/App")
    case_variant = agent_id("billing", resource_id="/SUBSCRIPTIONS/sub/RESOURCEGROUPS/rg/SITES/app")
    lower_variant = agent_id("billing", resource_id="/subscriptions/sub/resourcegroups/rg/sites/app")

    assert first == second
    assert first != different_slug
    assert first != different_resource
    assert case_variant == lower_variant
    assert str(uuid.UUID(first)) == first


@pytest.mark.parametrize("slug", ["", pytest.param(None, id="none")])
def test_agent_id_rejects_empty_slug(slug: Any) -> None:
    with pytest.raises(ValueError, match="agent_slug"):
        agent_id(slug)
