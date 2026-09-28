from __future__ import annotations

import json
from pathlib import Path

import pytest

from azure_functions_agents._copilot_state import NativeState
from azure_functions_agents._harness import CopilotPreviewError


def _native_file(state: NativeState, native_id: str) -> Path:
    path = state.native_root / "session-state" / native_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"assistant.message"}\n', encoding="utf-8")
    return path


def test_completed_native_state_resumes_after_recreating_owner(tmp_path):
    state = NativeState(tmp_path)
    native_id = state.native_id("main", "session-one")
    state.begin(native_id, new_session=True)
    _native_file(state, native_id)
    state.complete(native_id)
    state.close()
    restored = NativeState(tmp_path)
    try:
        restored.begin(native_id, new_session=False)
    finally:
        restored.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "pending", "metadata", "version"])
def test_missing_corrupt_and_unfinished_state_never_resets(tmp_path, damage):
    state = NativeState(tmp_path)
    native_id = state.native_id("main", "existing")
    try:
        state.begin(native_id, new_session=True)
        native = _native_file(state, native_id)
        if damage != "pending":
            state.complete(native_id)
        if damage == "missing":
            native.unlink()
        elif damage == "corrupt":
            native.write_text("corrupt", encoding="utf-8")
        elif damage == "metadata":
            state._marker_path(native_id).write_text("not json", encoding="utf-8")
        elif damage == "version":
            marker = json.loads(state._marker_path(native_id).read_text(encoding="utf-8"))
            marker["identity"]["runtime"] = "different"
            state._marker_path(native_id).write_text(json.dumps(marker), encoding="utf-8")
        with pytest.raises(CopilotPreviewError):
            state.begin(native_id, new_session=False)
    finally:
        state.close()


def test_unknown_supplied_session_never_creates_native_state(tmp_path):
    state = NativeState(tmp_path)
    try:
        with pytest.raises(CopilotPreviewError, match="not found"):
            state.begin(state.native_id("main", "unknown"), new_session=False)
        assert not state.native_root.exists()
    finally:
        state.close()


def test_native_ids_isolate_agent_session_case_and_path_segments():
    identities = {(agent, session) for agent in ("main", "Main", "other") for session in (".", "..", "CON", "id", "ID")}
    native_ids = {NativeState.native_id(*identity) for identity in identities}
    assert len(native_ids) == len(identities)
    assert all("/" not in value and "\\" not in value for value in native_ids)


def test_second_local_owner_fails_then_can_claim_after_close(tmp_path):
    first, second = NativeState(tmp_path), NativeState(tmp_path)
    try:
        first.claim()
        with pytest.raises(CopilotPreviewError, match="owned by another"):
            second.claim()
        first.close()
        second.claim()
    finally:
        first.close()
        second.close()


def test_new_session_cannot_overwrite_existing_state(tmp_path):
    state = NativeState(tmp_path)
    native_id = state.native_id("main", "collision")
    try:
        state.begin(native_id, new_session=True)
        with pytest.raises(CopilotPreviewError, match="refusing to reset"):
            state.begin(native_id, new_session=True)
    finally:
        state.close()
