"""Tests for stable agent identity helpers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from azure_functions_agents._agent_identity import (
    agent_id,
    resolve_app_correlation_key,
)

_IDENTITY_ENV_VARS = (
    "AZURE_FUNCTIONS_AGENTS_RESOURCE_ID",
    "WEBSITE_DEPLOYMENT_ID",
    "WEBSITE_OWNER_NAME",
    "WEBSITE_RESOURCE_GROUP",
    "WEBSITE_SITE_NAME",
)


@pytest.fixture(autouse=True)
def clear_identity_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in _IDENTITY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


def test_agent_id_uses_owner_and_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "  SUB+RG-EastUSWebspace  ")
    monkeypatch.setenv("WEBSITE_DEPLOYMENT_ID", "  Deployment-123  ")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "site")

    assert resolve_app_correlation_key() == "sub+rg-eastuswebspace/deployment-123"
    assert agent_id("billing") == "sub+rg-eastuswebspace/deployment-123/billing"


def test_owner_only_uses_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "Sub+RG-EastUSWebspace")

    assert resolve_app_correlation_key() == "sub+rg-eastuswebspace"
    assert agent_id("billing") == "sub+rg-eastuswebspace/billing"


def test_owner_without_deployment_uses_site_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "Sub+RG-EastUSWebspace")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "Contoso-Agents")

    assert resolve_app_correlation_key() == "sub+rg-eastuswebspace/contoso-agents"
    assert agent_id("billing") == "sub+rg-eastuswebspace/contoso-agents/billing"


def test_deployment_only_uses_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_DEPLOYMENT_ID", "Deployment-123")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "agent-app")

    assert resolve_app_correlation_key() == "deployment-123"
    assert agent_id("billing") == "deployment-123/billing"


def test_site_name_only_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_SITE_NAME", "Agent-App")

    assert resolve_app_correlation_key() == "agent-app"
    assert agent_id("billing") == "agent-app/billing"


def test_local_fallback() -> None:
    assert resolve_app_correlation_key() == "local"
    assert agent_id("billing") == "local/billing"


def test_blank_values_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "  ")
    monkeypatch.setenv("WEBSITE_DEPLOYMENT_ID", "\t")
    monkeypatch.setenv("WEBSITE_SITE_NAME", " Agent-App ")

    assert resolve_app_correlation_key() == "agent-app"
    assert agent_id("billing") == "agent-app/billing"


def test_resource_group_and_legacy_override_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_RESOURCE_ID", "/subscriptions/override")
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "OWNER")
    monkeypatch.setenv("WEBSITE_DEPLOYMENT_ID", "DEPLOYMENT")
    monkeypatch.setenv("WEBSITE_RESOURCE_GROUP", "resource-group")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "agent-app")

    assert resolve_app_correlation_key() == "owner/deployment"
    assert agent_id("billing") == "owner/deployment/billing"


def test_agent_id_is_deterministic_and_distinguishes_inputs() -> None:
    first = agent_id("billing", correlation_key="SUB+RG/Deployment")
    second = agent_id("billing", correlation_key="SUB+RG/Deployment")
    different_slug = agent_id("support", correlation_key="SUB+RG/Deployment")
    different_key = agent_id("billing", correlation_key="OTHER/Deployment")
    lower_variant = agent_id("billing", correlation_key="sub+rg/deployment")

    assert first == "sub+rg/deployment/billing"
    assert first == second
    assert first == lower_variant
    assert first != different_slug
    assert first != different_key


@pytest.mark.parametrize("slug", ["", pytest.param(None, id="none")])
def test_agent_id_rejects_empty_slug(slug: Any) -> None:
    with pytest.raises(ValueError, match="agent_slug"):
        agent_id(slug)
