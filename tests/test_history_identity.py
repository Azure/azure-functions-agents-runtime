from __future__ import annotations

import pytest

from azure_functions_agents.harness._history_identity import (
    AGENT_SLUG_PATTERN,
    validate_agent_slug,
)


@pytest.mark.parametrize("slug", ["main", "_", "_agent2", "Agent_123", "a" * 256])
def test_accepts_canonical_ascii_slug_without_rewriting(slug: str) -> None:
    assert validate_agent_slug(slug) == slug
    assert AGENT_SLUG_PATTERN.fullmatch(slug) is not None


@pytest.mark.parametrize(
    "slug",
    [None, 17, b"main", "1agent", "Agent-name", " agent", "agent\t", "á", "agent\x00"],
)
def test_rejects_noncanonical_identity_before_persistence(slug) -> None:
    with pytest.raises(ValueError, match="agent_slug"):
        validate_agent_slug(slug)
