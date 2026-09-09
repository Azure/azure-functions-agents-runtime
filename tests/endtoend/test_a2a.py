from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import httpx
import pytest

from tests.endtoend._func_host import _free_port, running_host

APPS_DIR = Path(__file__).resolve().parent / "apps"


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]


def test_a2a_card_and_message_cross_real_functions_http_process() -> None:
    port = _free_port()
    a2a_url = f"http://localhost:{port}/agents/main/a2a"
    with running_host(
        APPS_DIR / "a2a-simple",
        port=port,
        env={"A2A_PUBLIC_URL": a2a_url},
    ) as host:
        card_response = httpx.get(
            f"{host.base_url}/agents/main/.well-known/agent-card.json",
            timeout=10,
        )
        card_response.raise_for_status()
        card = card_response.json()
        assert card["supportedInterfaces"] == [
            {
                "url": a2a_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
                "tenant": "",
            }
        ]
        assert card["capabilities"]["streaming"] is False
        assert card["capabilities"]["pushNotifications"] is False

        request_id = f"e2e-{uuid.uuid4().hex}"
        prompt = "checkout-api CPU doubled immediately after deploy-1842."
        response = httpx.post(
            card["supportedInterfaces"][0]["url"],
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": uuid.uuid4().hex,
                        "contextId": "incident-1842",
                        "role": "ROLE_USER",
                        "parts": [{"text": prompt}],
                    },
                    "configuration": {
                        "acceptedOutputModes": ["text/plain"],
                        "returnImmediately": True,
                    },
                },
            },
            timeout=30,
        )
        response.raise_for_status()
        envelope = response.json()
        assert envelope["id"] == request_id
        assert envelope["result"]["message"]["contextId"] == "incident-1842"
        assert envelope["result"]["message"]["role"] == "ROLE_AGENT"
        assert envelope["result"]["message"]["parts"] == [
            {
                "text": (
                    f"Triage received: {prompt} "
                    "Immediate action: pause the latest rollout."
                )
            }
        ]

        assert httpx.get(f"{host.base_url}/agents/missing/.well-known/agent-card.json").status_code == 404
        assert httpx.post(f"{host.base_url}/agents/missing/a2a").status_code == 404
