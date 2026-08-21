from __future__ import annotations

import json
from pathlib import Path

import pytest

from azure_functions_agents.foundry_responses.fha_private_history import (
    FhaHistoryFactory,
    FhaPrivateHistoryError,
    FhaResponsesRequestEnvelope,
)


def _envelope(**updates: str | None) -> FhaResponsesRequestEnvelope:
    values: dict[str, str | None] = {
        "agent_slug": "model_only",
        "history_scope": "o1-" + ("a" * 52),
        "runtime_session_id": "opaque-session-1",
        "runtime_run_id": "a" * 32,
        "prompt": "Hello",
        "input": None,
    }
    values.update(updates)
    return FhaResponsesRequestEnvelope(**values)


def test_request_envelope_accepts_one_input_shape_and_rejects_identity_fields() -> None:
    parsed = FhaResponsesRequestEnvelope.parse_json_input(
        json.dumps(
            {
                "agent_slug": "model_only",
                "history_scope": "o1-" + ("a" * 52),
                "runtime_session_id": "opaque-session-1",
                "runtime_run_id": "a" * 32,
                "input": "Hello",
            }
        )
    )

    assert parsed.effective_prompt == "Hello"
    with pytest.raises(FhaPrivateHistoryError):
        FhaResponsesRequestEnvelope.parse_json_input(
            '{"agent_slug":"model_only","agent_slug":"other",'
            '"history_scope":"o1-'
            + ("a" * 52)
            + '",'
            '"runtime_session_id":"opaque-session-1","runtime_run_id":"'
            + ("a" * 32)
            + '","prompt":"Hello"}'
        )
    with pytest.raises(FhaPrivateHistoryError):
        FhaResponsesRequestEnvelope.parse_json_input(
            json.dumps(
                {
                    "agent_slug": "model_only",
                    "history_scope": "o1-" + ("a" * 52),
                    "runtime_session_id": "opaque-session-1",
                    "runtime_run_id": "a" * 32,
                    "prompt": "Hello",
                    "owner": "raw-claim",
                }
            )
        )


def test_history_paths_are_private_and_owner_scoped(tmp_path: Path) -> None:
    envelope = _envelope()
    paths = FhaHistoryFactory(home_directory=tmp_path).paths_for(envelope)

    assert paths.session_directory.parent.name == envelope.history_scope
    assert paths.session_directory == (
        tmp_path
        / ".azure-functions-agents-runtime"
        / "history"
        / paths.session_directory.parent.name
        / envelope.runtime_session_id
    )
    assert paths.run_marker_path.name == f"{envelope.runtime_run_id}.json"


def test_history_paths_separate_owners_with_the_same_runtime_session(tmp_path: Path) -> None:
    factory = FhaHistoryFactory(home_directory=tmp_path)

    first = factory.paths_for(_envelope())
    second = factory.paths_for(_envelope(history_scope="o1-" + ("b" * 52)))

    assert first.session_directory != second.session_directory


def test_history_commit_is_idempotent_by_run_id_before_a_checkpoint(tmp_path: Path) -> None:
    factory = FhaHistoryFactory(home_directory=tmp_path)
    envelope = _envelope()

    first = factory.commit_model_stage(envelope, "first answer")
    duplicate = factory.commit_model_stage(envelope, "second answer")

    assert first.output == "first answer"
    assert duplicate.output == "first answer"
    assert factory.read_committed_stage(envelope) == first


def test_history_rejects_a_marker_for_a_different_input(tmp_path: Path) -> None:
    factory = FhaHistoryFactory(home_directory=tmp_path)
    factory.commit_model_stage(_envelope(), "answer")

    with pytest.raises(FhaPrivateHistoryError, match="input does not match"):
        factory.read_committed_stage(_envelope(prompt="Changed"))
