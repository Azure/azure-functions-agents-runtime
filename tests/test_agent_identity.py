"""Tests for stable agent identity helpers."""

from __future__ import annotations

import uuid
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


def test_owner_and_deployment_pair_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "  sub+rg-eastuswebspace  ")
    monkeypatch.setenv("WEBSITE_DEPLOYMENT_ID", "  deployment-123  ")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "site")

    assert resolve_app_correlation_key() == "sub+rg-eastuswebspace/deployment-123"


def test_owner_without_deployment_falls_back_to_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "sub+rg-eastuswebspace")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "agent-app")

    assert resolve_app_correlation_key() == "site/agent-app"


def test_deployment_without_owner_falls_back_to_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_DEPLOYMENT_ID", "deployment-123")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "agent-app")

    assert resolve_app_correlation_key() == "site/agent-app"


def test_blank_values_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "  ")
    monkeypatch.setenv("WEBSITE_DEPLOYMENT_ID", "\t")
    monkeypatch.setenv("WEBSITE_SITE_NAME", " agent-app ")

    assert resolve_app_correlation_key() == "site/agent-app"


def test_site_name_only_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_SITE_NAME", "agent-app")

    assert resolve_app_correlation_key() == "site/agent-app"


def test_local_fallback() -> None:
    assert resolve_app_correlation_key() == "local"


def test_resource_group_and_legacy_override_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_RESOURCE_ID", "/subscriptions/override")
    monkeypatch.setenv("WEBSITE_OWNER_NAME", "owner")
    monkeypatch.setenv("WEBSITE_DEPLOYMENT_ID", "deployment")
    monkeypatch.setenv("WEBSITE_RESOURCE_GROUP", "resource-group")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "agent-app")

    assert resolve_app_correlation_key() == "owner/deployment"


def test_agent_id_is_deterministic_valid_and_distinguishes_inputs() -> None:
    first = agent_id("billing", correlation_key="SUB+RG/Deployment")
    second = agent_id("billing", correlation_key="SUB+RG/Deployment")
    different_slug = agent_id("support", correlation_key="SUB+RG/Deployment")
    different_key = agent_id("billing", correlation_key="OTHER/Deployment")
    case_variant = agent_id("billing", correlation_key="SUB+RG/DEPLOYMENT")
    lower_variant = agent_id("billing", correlation_key="sub+rg/deployment")

    assert first == second
    assert first != different_slug
    assert first != different_key
    assert case_variant == lower_variant
    assert str(uuid.UUID(first)) == first


@pytest.mark.parametrize("slug", ["", pytest.param(None, id="none")])
def test_agent_id_rejects_empty_slug(slug: Any) -> None:
    with pytest.raises(ValueError, match="agent_slug"):
        agent_id(slug)
