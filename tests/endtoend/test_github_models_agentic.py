"""Live GitHub Models smoke test through a booted sample Function App.

This module runs only when ``GITHUB_MODELS_TOKEN`` is intentionally supplied.
It never falls back to ``GITHUB_TOKEN`` so routine GitHub CI credentials cannot
trigger external model calls unexpectedly.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.endtoend._agent_probe import chat, wait_until_responsive
from tests.endtoend._func_host import (
    HostHandle,
    overlay_provider_settings,
    running_host,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_APP = REPO_ROOT / "samples" / "github-models-chat" / "src"
CHAT_SLUG = "main"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        shutil.which("func") is None,
        reason="Azure Functions Core Tools (`func`) not found on PATH",
    ),
    pytest.mark.skipif(
        not (os.environ.get("GITHUB_MODELS_TOKEN") or "").strip(),
        reason="GITHUB_MODELS_TOKEN is required for the live GitHub Models E2E test",
    ),
]


@pytest.fixture(scope="module")
def github_chat_host() -> Iterator[HostHandle]:
    """Boot the GitHub Models sample with its dedicated test token."""
    overlay_provider_settings(CHAT_APP)
    with running_host(CHAT_APP) as handle:
        wait_until_responsive(handle.base_url)
        yield handle


def test_github_models_returns_an_instruction_following_response(
    github_chat_host: HostHandle,
) -> None:
    """Exercise real token auth, endpoint routing, model resolution, and inference."""
    reply = chat(
        github_chat_host.base_url,
        CHAT_SLUG,
        "Reply with exactly the single word PONG and nothing else.",
    )

    assert reply.status == 200, f"chat request failed: {reply.status} {reply.body}"
    assert "pong" in reply.response_text.strip().lower(), (
        f"GitHub Models did not follow the instruction: {reply.response_text!r}"
    )