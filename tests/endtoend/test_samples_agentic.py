"""End-to-end *agentic* tests: exercise a real LLM through a booted sample app.

Unlike the startup smoke tests in ``test_samples_start.py``, these drive the
full stack — Functions host, builtin chat endpoint, MAF runner, session history
provider, and a **live model provider** — and assert the agent actually produces
a coherent, instruction-following response.

Provider configuration:

* In CI the Foundry endpoint/model arrive as **pipeline variables**
  (``FOUNDRY_PROJECT_ENDPOINT`` / ``FOUNDRY_MODEL``). They are *not* baked into
  the committed ``local.settings.template.json``, so
  :func:`overlay_provider_settings` copies them from the environment into the
  app's (gitignored) ``local.settings.json`` before the host starts.
* Locally, whatever provider is already in the sample's ``local.settings.json``
  is used as-is.
* When no provider can be resolved the whole module skips.

The target app is ``samples/basic-chat`` which enables ``builtin_endpoints``,
exposing ``POST /agents/main/chat``. It uses ``AzureWebJobsStorage`` against
Azurite, so Azurite must be running for the session-memory test.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.endtoend._agent_probe import chat, wait_until_responsive
from tests.endtoend._func_host import (
    HostHandle,
    configured_provider,
    overlay_provider_settings,
    running_host,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = REPO_ROOT / "samples"
CHAT_APP = SAMPLES_DIR / "basic-chat" / "src"
CHAT_SLUG = "main"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        shutil.which("func") is None,
        reason="Azure Functions Core Tools (`func`) not found on PATH",
    ),
]


@pytest.fixture(scope="module")
def chat_host() -> Iterator[HostHandle]:
    """Boot ``basic-chat`` with the resolved provider and keep it running."""
    overlay_provider_settings(CHAT_APP)
    provider = configured_provider(CHAT_APP)
    if provider is None:
        pytest.skip(
            "no LLM provider configured — set FOUNDRY_PROJECT_ENDPOINT / FOUNDRY_MODEL "
            "(or another provider) to run the agentic E2E tests"
        )
    with running_host(CHAT_APP) as handle:
        wait_until_responsive(handle.base_url)
        yield handle


def test_agent_returns_a_response(chat_host: HostHandle) -> None:
    """The agent answers a simple prompt with non-empty content and a session id."""
    reply = chat(chat_host.base_url, CHAT_SLUG, "In one short sentence, say hello.")

    assert reply.status == 200, f"chat request failed: {reply.status} {reply.body}"
    assert reply.response_text.strip(), f"expected a non-empty response, got {reply.body}"
    assert reply.session_id, "expected the chat endpoint to return a session id"


def test_agent_follows_a_simple_instruction(chat_host: HostHandle) -> None:
    """The model honours an explicit output instruction (basic agentic behaviour)."""
    reply = chat(
        chat_host.base_url,
        CHAT_SLUG,
        "Reply with exactly the single word PONG and nothing else.",
    )

    assert reply.status == 200, f"chat request failed: {reply.status} {reply.body}"
    assert "pong" in reply.response_text.strip().lower(), (
        f"model did not follow the instruction: {reply.response_text!r}"
    )


def test_agent_remembers_context_within_a_session(chat_host: HostHandle) -> None:
    """Conversation history is threaded across turns sharing a session id."""
    session_id = f"e2e-{uuid.uuid4().hex}"

    first = chat(
        chat_host.base_url,
        CHAT_SLUG,
        "My name is Ada Lovelace. Reply with just: OK.",
        session_id=session_id,
    )
    assert first.status == 200, f"first turn failed: {first.status} {first.body}"

    second = chat(
        chat_host.base_url,
        CHAT_SLUG,
        "What is my name? Answer with the full name only.",
        session_id=session_id,
    )
    assert second.status == 200, f"second turn failed: {second.status} {second.body}"
    assert "ada" in second.response_text.lower(), (
        f"agent did not recall session context: {second.response_text!r}"
    )
