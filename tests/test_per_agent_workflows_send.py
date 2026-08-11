from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SAMPLE_ROOT = Path(__file__).resolve().parents[1] / "samples" / "per-agent-workflows"
SEND_SCRIPT = SAMPLE_ROOT / "scripts" / "send.py"


def _load_send_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(SEND_SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("per_agent_workflows_send", SEND_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_send_pipeline_posts_prompt_and_shared_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send = _load_send_module(monkeypatch)
    workflow_id = "0123456789abcdef0123456789abcdef-12345678123412341234123456789abc"
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response({"response": f"Started {workflow_id}"})

    monkeypatch.setattr(send, "urlopen", fake_urlopen)

    actual = send.send_pipeline(
        "incident",
        base_url="http://localhost:7071/",
        session_id="manual-shared-session",
        timeout=90,
    )

    request = captured["request"]
    assert request.full_url == "http://localhost:7071/agents/incident_commander/chat"
    assert request.get_header("X-ms-session-id") == "manual-shared-session"
    assert json.loads(request.data) == {"prompt": send.INCIDENT_PROMPT}
    assert captured["timeout"] == 90
    assert actual == workflow_id


def test_send_pipeline_selects_release_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    send = _load_send_module(monkeypatch)

    request = send.build_chat_request(
        "release",
        base_url="http://127.0.0.1:7071",
        session_id="release-session",
    )

    assert request.full_url == "http://127.0.0.1:7071/agents/release_manager/chat"
    assert json.loads(request.data) == {"prompt": send.RELEASE_PROMPT}
