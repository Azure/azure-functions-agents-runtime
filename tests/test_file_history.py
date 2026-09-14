from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from agent_framework import FileHistoryProvider, Message

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


def test_missing_scoped_file_ignores_unscoped_file_without_path_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session_id = "existing-unscoped-session"
    unscoped_path = tmp_path / f"{session_id}.jsonl"
    unscoped_content = json.dumps(_message("existing record").to_dict()) + "\n"
    unscoped_path.write_text(unscoped_content, encoding="utf-8")
    provider = ScopedFileHistoryProvider(
        storage_root=tmp_path,
        agent_slug="billing",
    )

    original_path_resolver = FileHistoryProvider._session_file_path
    resolved_paths: list[Path] = []

    def _track_session_path(
        self: FileHistoryProvider,
        current_session_id: str | None,
    ) -> Path:
        path = original_path_resolver(self, current_session_id)
        resolved_paths.append(path)
        return path

    monkeypatch.setattr(FileHistoryProvider, "_session_file_path", _track_session_path)

    assert asyncio.run(provider.get_messages(session_id)) == []
    scoped_path = tmp_path / "billing" / f"{session_id}.jsonl"
    assert resolved_paths
    assert set(resolved_paths) == {scoped_path}
    assert not scoped_path.exists()
    assert unscoped_path.read_text(encoding="utf-8") == unscoped_content


def test_save_writes_only_scoped_file(tmp_path: Path) -> None:
    session_id = "existing-unscoped-session"
    unscoped_path = tmp_path / f"{session_id}.jsonl"
    unscoped_content = json.dumps(_message("existing").to_dict()) + "\n"
    unscoped_path.write_text(unscoped_content, encoding="utf-8")
    provider = ScopedFileHistoryProvider(storage_root=tmp_path, agent_slug="billing")

    asyncio.run(provider.save_messages(session_id, [_message("new")]))

    assert unscoped_path.read_text(encoding="utf-8") == unscoped_content
    scoped_path = tmp_path / "billing" / f"{session_id}.jsonl"
    assert scoped_path.is_file()
    assert "new" in scoped_path.read_text(encoding="utf-8")
