from __future__ import annotations

import pytest
from agent_framework import Message

from azure_functions_agents.foundry_responses.fha_private_history import (
    FhaHistoryFactory,
    FhaResponsesRequestEnvelope,
)


@pytest.mark.asyncio
async def test_maf_history_provider_commits_one_delta_per_opaque_run(tmp_path) -> None:
    envelope = FhaResponsesRequestEnvelope(
        agent_slug="model_only",
        history_scope="o1-" + ("a" * 52),
        runtime_session_id="opaque-session-1",
        runtime_run_id="a" * 32,
        prompt="Hello",
    )
    factory = FhaHistoryFactory(home_directory=tmp_path)
    provider = factory.create_maf_history_provider(envelope)

    await provider.save_messages(
        envelope.runtime_session_id,
        [Message("user", ["Hello"]), Message("assistant", ["First answer"])],
    )
    await provider.save_messages(
        envelope.runtime_session_id,
        [Message("assistant", ["Duplicate answer"])],
    )

    messages = await provider.get_messages(envelope.runtime_session_id)

    assert [message.text for message in messages] == ["Hello", "First answer"]
    assert factory.read_committed_stage(envelope) is not None
    assert factory.read_committed_stage(envelope).output == "First answer"
    assert factory.commit_model_stage(envelope, "First answer").output == "First answer"


@pytest.mark.asyncio
async def test_pending_history_commit_blocks_a_duplicate_delta_after_model_rerun(tmp_path) -> None:
    envelope = FhaResponsesRequestEnvelope(
        agent_slug="model_only",
        history_scope="o1-" + ("a" * 52),
        runtime_session_id="opaque-session-1",
        runtime_run_id="a" * 32,
        prompt="Hello",
    )
    factory = FhaHistoryFactory(home_directory=tmp_path)
    provider = factory.create_maf_history_provider(envelope)

    await provider.save_messages(
        envelope.runtime_session_id,
        [Message("user", ["Hello"]), Message("assistant", ["First attempt"])],
    )
    await provider.save_messages(
        envelope.runtime_session_id,
        [Message("user", ["Hello"]), Message("assistant", ["Rerun attempt"])],
    )

    messages = await provider.get_messages(envelope.runtime_session_id)

    assert [message.text for message in messages] == ["Hello", "First attempt"]
    assert factory.commit_model_stage(envelope, "Rerun attempt").output == "First attempt"
