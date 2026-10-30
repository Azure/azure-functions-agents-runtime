"""Run release-review scenarios against real Durable backends."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest

from tests.endtoend._func_host import HostHandle, running_host
from tests.test_workflow_fanout_sample import SAMPLE_ROOT, verifier

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Core Tools is not installed"),
]


def _drain_activities(host: HostHandle) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        output = host.read_output()
        started = set(
            re.findall(
                r"Executing 'Functions\.agents_workflow_run_tool'.*?Id=([0-9a-f-]+)",
                output,
            )
        )
        finished = set(
            re.findall(
                r"Executed 'Functions\.agents_workflow_run_tool'.*?Id=([0-9a-f-]+)",
                output,
            )
        )
        if started <= finished:
            return
        time.sleep(0.5)
    raise AssertionError(f"Activities did not finish before host shutdown:\n{output}")


@pytest.fixture(scope="module", params=["storage", "dts"])
def fanout_sample_host(request: Any, tmp_path_factory: Any) -> Iterator[HostHandle]:
    backend = request.param
    endpoint = os.environ.get("DURABLE_TASK_SCHEDULER_CONNECTION_STRING")
    if backend == "dts" and not endpoint:
        pytest.skip("Set DURABLE_TASK_SCHEDULER_CONNECTION_STRING to test DTS")
    app_dir = tmp_path_factory.mktemp(f"fanout-{backend}") / "src"
    shutil.copytree(
        SAMPLE_ROOT / "src",
        app_dir,
        ignore=shutil.ignore_patterns("local.settings.json", "__pycache__", ".venv"),
    )
    settings = json.loads((app_dir / "local.settings.template.json").read_text())
    settings["Values"]["AzureWebJobsStorage"] = os.environ.get(
        "AZUREWEBJOBSSTORAGE",
        "UseDevelopmentStorage=true",
    )
    if backend == "dts":
        shutil.copyfile(app_dir / "host.dts.json", app_dir / "host.json")
        settings["Values"]["DURABLE_TASK_SCHEDULER_CONNECTION_STRING"] = endpoint
        settings["Values"]["TASKHUB_NAME"] = os.environ.get("TASKHUB_NAME", "workflowfanoutreview")
    else:
        host_config = json.loads((app_dir / "host.json").read_text())
        host_config["extensions"]["durableTask"] = {"hubName": f"FanoutReview{uuid4().hex[:8]}"}
        (app_dir / "host.json").write_text(json.dumps(host_config), encoding="utf-8")
    (app_dir / "local.settings.json").write_text(json.dumps(settings), encoding="utf-8")
    with running_host(
        app_dir,
        env={
            "AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT": "false",
            "PYTHONPATH": str(SAMPLE_ROOT.parents[1] / "src"),
            "VIRTUAL_ENV": sys.prefix,
            "languageWorkers__python__defaultExecutablePath": sys.executable,
        },
    ) as host:
        try:
            yield host
        finally:
            _drain_activities(host)


@pytest.mark.parametrize("scenario", verifier.SCENARIOS)
def test_release_review_scenario(fanout_sample_host: HostHandle, scenario: str) -> None:
    try:
        verifier.run_scenario(fanout_sample_host.base_url, scenario)
    except Exception:
        print(fanout_sample_host.read_output())
        raise
