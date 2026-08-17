"""Opt-in deployed ACA history proof.

The unit doubles prove controller behavior only. This test requires an
operator-prepared deployed app and retained/lost/corrupt session fixtures.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from tests.live.aca_smoke_support import (
    AcaHistorySmokeConfig,
    aca_history_smoke_config_from_environment,
    request_aca_history_smoke,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE") != "1",
    reason="Set AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE=1 after human authorization to run live ACA.",
)


@pytest.fixture
def aca_history_smoke_config() -> AcaHistorySmokeConfig:
    return aca_history_smoke_config_from_environment()


def _body(response_body: bytes) -> dict[str, object]:
    decoded = json.loads(response_body)
    assert isinstance(decoded, dict)
    return decoded


def _messages(body: dict[str, object]) -> list[dict[str, str]]:
    messages = body.get("messages")
    assert isinstance(messages, list)
    assert all(
        isinstance(message, dict)
        and isinstance(message.get("role"), str)
        and isinstance(message.get("text"), str)
        for message in messages
    )
    return messages  # type: ignore[return-value]


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_live_aca_history_preserves_two_completed_turns(
    aca_history_smoke_config: AcaHistorySmokeConfig,
) -> None:
    session_id = f"aca-history-{uuid.uuid4().hex}"
    first_prompt = f"history-first-{uuid.uuid4().hex}"
    second_prompt = f"history-second-{uuid.uuid4().hex}"
    chat_path = f"/agents/{aca_history_smoke_config.agent_slug}/chat"
    history_path = f"/agents/{aca_history_smoke_config.agent_slug}/history"

    first_chat = await request_aca_history_smoke(
        aca_history_smoke_config,
        method="POST",
        path=chat_path,
        session_id=session_id,
        body=json.dumps({"prompt": first_prompt}).encode("utf-8"),
    )
    first_history = await request_aca_history_smoke(
        aca_history_smoke_config,
        method="GET",
        path=history_path,
        session_id=session_id,
    )

    assert first_chat.status_code == 200
    assert first_history.status_code == 200
    first_messages = _messages(_body(first_history.body))
    assert [message["role"] for message in first_messages] == ["user", "assistant"]
    assert first_messages[0]["text"] == first_prompt
    assert first_messages[1]["text"]

    second_chat = await request_aca_history_smoke(
        aca_history_smoke_config,
        method="POST",
        path=chat_path,
        session_id=session_id,
        body=json.dumps({"prompt": second_prompt}).encode("utf-8"),
    )
    second_history = await request_aca_history_smoke(
        aca_history_smoke_config,
        method="GET",
        path=history_path,
        session_id=session_id,
    )

    assert second_chat.status_code == 200
    assert second_history.status_code == 200
    messages = _messages(_body(second_history.body))
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert [messages[0]["text"], messages[2]["text"]] == [first_prompt, second_prompt]
    assert messages[1]["text"]
    assert messages[3]["text"]


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_live_aca_history_reports_resumed_lost_and_unavailable_scenarios(
    aca_history_smoke_config: AcaHistorySmokeConfig,
) -> None:
    history_path = f"/agents/{aca_history_smoke_config.agent_slug}/history"
    resumed = await request_aca_history_smoke(
        aca_history_smoke_config,
        method="GET",
        path=history_path,
        session_id=aca_history_smoke_config.resumed_session_id,
    )
    gone = await request_aca_history_smoke(
        aca_history_smoke_config,
        method="GET",
        path=history_path,
        session_id=aca_history_smoke_config.gone_session_id,
    )
    unavailable = await request_aca_history_smoke(
        aca_history_smoke_config,
        method="GET",
        path=history_path,
        session_id=aca_history_smoke_config.unavailable_session_id,
    )

    assert resumed.status_code == 200
    assert resumed.headers.get("x-ms-aca-history-resumed") == "true"
    assert [message["role"] for message in _messages(_body(resumed.body))] == ["user", "assistant"]
    assert gone.status_code == 410
    assert _body(gone.body) == {"error": "history_gone"}
    assert unavailable.status_code == 503
    assert _body(unavailable.body) == {"error": "history_unavailable"}
