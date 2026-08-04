from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SAMPLE_ROOT = (
    Path(__file__).resolve().parents[1] / "samples" / "workflow-subagents-preview"
)
VERIFY_SCRIPT = SAMPLE_ROOT / "scripts" / "verify.py"


def _load_verify_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("workflow_subagent_verify", VERIFY_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verify_script_builds_repeatable_request() -> None:
    verify = _load_verify_module()

    request = verify.build_request()

    assert request["report_blob"] == "reports/functions-pr-status.html"
    assert [item["url"] for item in request["pull_requests"]] == [
        "https://github.com/Azure/azure-functions-host/pull/123",
        "https://github.com/Azure/azure-functions-python-worker/pull/456",
    ]


def test_verify_script_builds_isolated_emulator_commands() -> None:
    verify = _load_verify_module()

    commands = verify.build_emulator_commands("test-run")

    assert commands.azurite[:5] == [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
    ]
    assert "workflow-subagents-azurite-test-run" in commands.azurite
    assert "workflow-subagents-dts-test-run" in commands.dts
    assert "DTS_TASK_HUB_NAMES=prstatusreports" in commands.dts
    assert "127.0.0.1::8080" in commands.dts
    assert "127.0.0.1::8082" in commands.dts


def test_verify_script_validates_report_content() -> None:
    verify = _load_verify_module()
    request = verify.build_request()
    html = (
        "<!doctype html><html><body>"
        + "".join(item["url"] for item in request["pull_requests"])
        + "</body></html>"
    )

    verify.validate_report(html.encode(), request)

    with pytest.raises(RuntimeError, match="missing pull-request URL"):
        verify.validate_report(b"<html><body>incomplete</body></html>", request)
