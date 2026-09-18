"""Opt-in end-to-end Vally evaluation through a real Core Tools host."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.endtoend._func_host import (
    configured_provider,
    overlay_provider_settings,
    running_host,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_ROOT = REPO_ROOT / "samples" / "agent-evaluation"
APP_ROOT = SAMPLE_ROOT / "src"
EXECUTOR_ROOT = REPO_ROOT / "integrations" / "vally-executor-azure-functions"
CLI = EXECUTOR_ROOT / "node_modules" / "@microsoft" / "vally-cli" / "dist" / "index.js"
PLUGIN = EXECUTOR_ROOT / "dist" / "index.js"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("RUN_VALLY_E2E") != "1",
        reason="set RUN_VALLY_E2E=1 to run the live Vally sample",
    ),
    pytest.mark.skipif(shutil.which("func") is None, reason="Core Tools not found"),
    pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not found"),
    pytest.mark.skipif(not CLI.exists(), reason="Vally CLI dependencies are not installed"),
    pytest.mark.skipif(not PLUGIN.exists(), reason="Vally executor is not built"),
]


def test_vally_sample_against_core_tools(tmp_path: Path) -> None:
    """The source-controlled sample eval passes through the hosted receipt agent."""
    overlay_provider_settings(APP_ROOT)
    if configured_provider(APP_ROOT) is None:
        pytest.skip("no LLM provider configured for the receipt agent")

    with running_host(APP_ROOT) as host:
        environment = {
            **os.environ,
            "AGENT_EVAL_TARGET_URL": f"{host.base_url}/agents/receipt/chat",
            "VALLY_TELEMETRY_OPTOUT": "1",
        }
        result = subprocess.run(
            [
                "node",
                str(CLI),
                "eval",
                "--eval-spec",
                str(SAMPLE_ROOT / "eval.yaml"),
                "--executor-plugin",
                str(PLUGIN),
                "--require-pass",
                "--junit",
                "--output-dir",
                str(tmp_path / "vally-results"),
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )

    assert result.returncode == 0, (
        f"Vally sample failed with exit {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert list((tmp_path / "vally-results").rglob("results.jsonl"))
    assert list((tmp_path / "vally-results").rglob("eval-results.junit.xml"))
