from __future__ import annotations

import pytest

from azure_functions_agents._agent_identity import agent_id, resolve_app_correlation_key


@pytest.fixture(autouse=True)
def isolated_identity(monkeypatch):
    for name in ("WEBSITE_OWNER_NAME", "WEBSITE_DEPLOYMENT_ID", "WEBSITE_SITE_NAME"):
        monkeypatch.delenv(name, raising=False)


def test_shared_identity_contract_preserves_the_canonical_slug_and_explicit_key():
    assert agent_id("Canonical_Slug", correlation_key="OWNER/Deployment") == (
        "owner/deployment/Canonical_Slug"
    )
    assert resolve_app_correlation_key() == "local"
    assert agent_id("agent") == "local/agent"


def test_shared_identity_ignores_legacy_overrides_and_resource_group(monkeypatch):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_RESOURCE_ID", "/subscriptions/ignored")
    monkeypatch.setenv("WEBSITE_RESOURCE_GROUP", "ignored")
    monkeypatch.setenv("WEBSITE_OWNER_NAME", " Owner+RG ")
    monkeypatch.setenv("WEBSITE_SITE_NAME", " Site ")
    assert resolve_app_correlation_key() == "owner+rg/site"
    assert agent_id("agent") == "owner+rg/site/agent"


def test_shared_identity_rejects_an_empty_slug():
    with pytest.raises(ValueError, match="agent_slug"):
        agent_id("")
