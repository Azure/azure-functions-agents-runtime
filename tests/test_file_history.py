from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from agent_framework import Message

from azure_functions_agents._file_history import ScopedFileHistoryProvider


def _message(text: str) -> Message:
    return Message(role="user", contents=[text])


@pytest.mark.parametrize("agent_slug", ["", ".", "..", "billing/support", "billing support"])
def test_rejects_invalid_agent_slug(tmp_path: Path, agent_slug: str) -> None:
    with pytest.raises(ValueError, match="agent_slug"):
        ScopedFileHistoryProvider(storage_root=tmp_path, agent_slug=agent_slug)


def test_same_session_id_remains_independent_across_agent_slugs(tmp_path: Path) -> None:
    billing = ScopedFileHistoryProvider(storage_root=tmp_path, agent_slug="billing")
    support = ScopedFileHistoryProvider(storage_root=tmp_path, agent_slug="support")

    asyncio.run(billing.save_messages("shared-session", [_message("billing reply")]))
    asyncio.run(support.save_messages("shared-session", [_message("support reply")]))

    assert [message.text for message in asyncio.run(billing.get_messages("shared-session"))] == [
        "billing reply"
    ]
    assert [message.text for message in asyncio.run(support.get_messages("shared-session"))] == [
        "support reply"
    ]
    assert (tmp_path / "billing" / "shared-session.jsonl").is_file()
    assert (tmp_path / "support" / "shared-session.jsonl").is_file()


def test_legacy_unscoped_file_is_not_loaded_and_warning_omits_path_details(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    session_id = "legacy-session"
    second_session_id = "another-legacy-session"
    legacy_path = tmp_path / f"{session_id}.jsonl"
    second_legacy_path = tmp_path / f"{second_session_id}.jsonl"
    legacy_content = json.dumps(_message("must not be loaded").to_dict()) + "\n"
    legacy_path.write_text(legacy_content, encoding="utf-8")
    second_legacy_path.write_text(legacy_content, encoding="utf-8")
    provider = ScopedFileHistoryProvider(
        storage_root=tmp_path,
        agent_slug="file_warning_agent",
    )

    with caplog.at_level("WARNING", logger="azure.functions.AgentRuntime"):
        assert asyncio.run(provider.get_messages(session_id)) == []
        assert asyncio.run(provider.get_messages(second_session_id)) == []

    assert not (tmp_path / "file_warning_agent" / f"{session_id}.jsonl").exists()
    assert legacy_path.read_text(encoding="utf-8") == legacy_content
    warning_text = caplog.text
    assert warning_text.count("Legacy unscoped chat history path detected") == 1
    assert "agent_slug=file_warning_agent backend=file" in warning_text
    for omitted_detail in (
        session_id,
        second_session_id,
        "must not be loaded",
        str(tmp_path),
    ):
        assert omitted_detail not in warning_text


def test_missing_scoped_and_legacy_files_emit_no_warning(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    provider = ScopedFileHistoryProvider(
        storage_root=tmp_path,
        agent_slug="missing_file_warning_agent",
    )

    with caplog.at_level("WARNING", logger="azure.functions.AgentRuntime"):
        assert asyncio.run(provider.get_messages("missing-session")) == []
    assert "Legacy unscoped chat history path detected" not in caplog.text


def test_save_never_mutates_legacy_file(tmp_path: Path) -> None:
    session_id = "legacy-session"
    legacy_path = tmp_path / f"{session_id}.jsonl"
    legacy_content = json.dumps(_message("legacy").to_dict()) + "\n"
    legacy_path.write_text(legacy_content, encoding="utf-8")
    provider = ScopedFileHistoryProvider(storage_root=tmp_path, agent_slug="billing")

    asyncio.run(provider.save_messages(session_id, [_message("new")]))

    assert legacy_path.read_text(encoding="utf-8") == legacy_content
    scoped_path = tmp_path / "billing" / f"{session_id}.jsonl"
    assert scoped_path.is_file()
    assert "new" in scoped_path.read_text(encoding="utf-8")
