"""Tests for stable agent identity."""

from __future__ import annotations

from typing import Any

import pytest

from azure_functions_agents._agent_identity import agent_id


@pytest.fixture(autouse=True)
def clear_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)


@pytest.mark.parametrize("site_name", ["Agent-App", "  Agent-App \t "])
def test_agent_id_uses_trimmed_lowercase_site(monkeypatch: pytest.MonkeyPatch, site_name: str) -> None:
    monkeypatch.setenv("WEBSITE_SITE_NAME", site_name)

    assert agent_id("billing") == "agent-app/billing"


@pytest.mark.parametrize("site_name", [None, "", " \t "])
def test_missing_or_blank_site_uses_local(
    monkeypatch: pytest.MonkeyPatch, site_name: str | None
) -> None:
    if site_name is not None:
        monkeypatch.setenv("WEBSITE_SITE_NAME", site_name)

    assert agent_id("billing") == "local/billing"


@pytest.mark.parametrize("site_name", [None, "", " \t ", "Agent-App"])
def test_other_identity_variables_have_no_effect(
    monkeypatch: pytest.MonkeyPatch, site_name: str | None
) -> None:
    if site_name is not None:
        monkeypatch.setenv("WEBSITE_SITE_NAME", site_name)
    expected = agent_id("billing")
    for name, value in (
        ("WEBSITE_OWNER_NAME", "OWNER"),
        ("WEBSITE_DEPLOYMENT_ID", "DEPLOYMENT"),
        ("WEBSITE_RESOURCE_GROUP", "resource-group"),
        ("AZURE_FUNCTIONS_AGENTS_RESOURCE_ID", "/subscriptions/override"),
    ):
        monkeypatch.setenv(name, value)
        assert agent_id("billing") == expected

    assert expected == ("agent-app/billing" if site_name == "Agent-App" else "local/billing")


def test_agent_id_is_deterministic_and_distinguishes_sites_and_slugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBSITE_SITE_NAME", "Agent-App")
    first = agent_id("billing")
    assert agent_id("billing") == first
    assert agent_id("support") != first
    monkeypatch.setenv("WEBSITE_SITE_NAME", "agent-app")
    assert agent_id("billing") == first
    monkeypatch.setenv("WEBSITE_SITE_NAME", "Other-App")
    assert agent_id("billing") != first


@pytest.mark.parametrize("slug", ["", pytest.param(None, id="none")])
def test_agent_id_rejects_empty_slug(slug: Any) -> None:
    with pytest.raises(ValueError, match="agent_slug"):
        agent_id(slug)
