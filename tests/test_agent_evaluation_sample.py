from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from azure_functions_agents.config.loader import load_agent_specs
from azure_functions_agents.discovery.tools import (
    clear_tool_discovery_cache,
    discover_project_tools,
)

SAMPLE_SRC = Path(__file__).resolve().parents[1] / "samples" / "agent-evaluation" / "src"
EVAL_SPEC = SAMPLE_SRC.parent / "eval.yaml"


def test_agent_evaluation_sample_exposes_anonymous_receipt_chat() -> None:
    [agent] = load_agent_specs(SAMPLE_SRC, strict=True)

    assert Path(agent.source_file).name == "receipt.agent.md"
    assert agent.builtin_endpoints is not None
    assert agent.builtin_endpoints.chat_api is True
    assert agent.builtin_endpoints.http_auth.mode == "anonymous"


def test_agent_evaluation_sample_discovers_deterministic_receipt_tool() -> None:
    clear_tool_discovery_cache()
    discovered = discover_project_tools(SAMPLE_SRC)

    assert {tool.name for tool in discovered.user_tools} == {"read_receipt"}
    assert discovered.workflow_tools == []


def test_agent_evaluation_receipt_tool_matches_eval_contract() -> None:
    module = runpy.run_path(str(SAMPLE_SRC / "tools" / "receipt.py"))
    read_receipt = cast("Any", module["read_receipt"])

    assert read_receipt("USD") == {
        "merchant": "Contoso Cafe",
        "total": 42.18,
        "currency": "USD",
    }


def test_agent_evaluation_sample_uses_native_vally_spec() -> None:
    document = cast("dict[str, Any]", yaml.safe_load(EVAL_SPEC.read_text(encoding="utf-8")))

    assert document["defaults"] == {
        "executor": {
            "name": "azure-functions-agent",
            "config": {
                "endpointUrlEnv": "AGENT_EVAL_TARGET_URL",
                "auth": {"type": "anonymous"},
            },
        },
        "runs": 2,
        "timeout": "120s",
    }
    stimulus, memory = document["stimuli"]
    assert stimulus["prompt"] == "Read the receipt and return the total."
    assert stimulus["graders"] == [
        {
            "type": "output-contains",
            "config": {"substring": "The receipt total is 42.18 USD."},
        },
        {
            "type": "tool-calls",
            "config": {
                "required": [
                    {
                        "name": "^read_receipt$",
                        "args": {"currency": "^USD$"},
                        "result": '\"total\":42\\.18',
                    }
                ]
            },
        },
    ]
    assert memory == {
        "name": "receipt-memory",
        "turns": [
            "Remember confirmation code BLUE-ORBIT-731. Acknowledge with only OK.",
            "What confirmation code did I ask you to remember? Reply with only the code.",
        ],
        "graders": [
            {
                "type": "output-contains",
                "config": {"substring": "BLUE-ORBIT-731"},
            },
            {
                "type": "tool-calls",
                "turn": 1,
                "config": {"disallowed": ["^read_receipt$"]},
            },
        ],
    }
