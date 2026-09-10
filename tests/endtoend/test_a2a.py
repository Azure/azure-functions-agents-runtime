from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from tests.endtoend._func_host import _free_port, running_host

APPS_DIR = Path(__file__).resolve().parent / "apps"
REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_CLIENT = REPO_ROOT / "samples" / "a2a-incident-triage" / "client.py"


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]


def _a2a_client_python() -> Path:
    configured = os.environ.get("A2A_CLIENT_PYTHON")
    if configured:
        return Path(configured)

    venv = REPO_ROOT / ".e2e-a2a-client"
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def test_a2a_maf_and_raw_clients_cross_real_functions_http_process() -> None:
    client_python = _a2a_client_python()
    if not client_python.is_file():
        pytest.skip(
            "isolated MAF A2A client environment is missing; "
            "run `python eng/scripts/install_e2e_dependencies.py`"
        )

    client_environment = subprocess.run(
        [
            str(client_python),
            "-c",
            (
                "from importlib.metadata import version; "
                "from importlib.util import find_spec; "
                "assert version('agent-framework-a2a') == '1.0.0b260821'; "
                "assert tuple(map(int, version('agent-framework-core').split('.')[:2])) >= (1, 15); "
                "assert find_spec('azure_functions_agents') is None"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert client_environment.returncode == 0, (
        f"invalid isolated A2A client environment:\n{client_environment.stderr}"
    )

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

        prompt = "checkout-api CPU doubled immediately after deploy-1842."
        context_id = "incident-1842-maf"
        client_result = subprocess.run(
            [
                str(client_python),
                str(SAMPLE_CLIENT),
                prompt,
                "--base-url",
                host.base_url,
                "--context-id",
                context_id,
                "--json-output",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert client_result.returncode == 0, (
            f"MAF A2A client failed:\nstdout:\n{client_result.stdout}\n"
            f"stderr:\n{client_result.stderr}"
        )
        maf_result = json.loads(client_result.stdout)
        assert maf_result == {
            "cardUrl": f"{host.base_url}/agents/main/.well-known/agent-card.json",
            "interface": {
                "url": a2a_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            },
            "contextId": context_id,
            "responseText": (
                f"Triage received: {prompt} "
                "Immediate action: pause the latest rollout."
            ),
        }

        request_id = f"e2e-{uuid.uuid4().hex}"
        raw_prompt = "payments-api CPU doubled immediately after deploy-1843."
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
                        "contextId": "incident-1843-raw",
                        "role": "ROLE_USER",
                        "parts": [{"text": raw_prompt}],
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
        assert envelope["result"]["message"]["contextId"] == "incident-1843-raw"
        assert envelope["result"]["message"]["role"] == "ROLE_AGENT"
        assert envelope["result"]["message"]["parts"] == [
            {
                "text": (
                    f"Triage received: {raw_prompt} "
                    "Immediate action: pause the latest rollout."
                )
            }
        ]

        assert httpx.get(f"{host.base_url}/agents/missing/.well-known/agent-card.json").status_code == 404
        assert httpx.post(f"{host.base_url}/agents/missing/a2a").status_code == 404
