from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

from tests.endtoend import test_samples_start
from tests.endtoend._func_host import FuncStartResult


def test_startup_env_does_not_mutate_process_environment(monkeypatch, tmp_path: Path) -> None:
    key = test_samples_start.RUNTIME_LOG_LEVEL
    monkeypatch.setenv(key, "Warning")

    assert test_samples_start._startup_env(tmp_path) == {key: "Information"}
    assert os.environ[key] == "Warning"


def test_start_sample_app_overlays_provider_settings_before_start(monkeypatch, tmp_path: Path) -> None:
    overlay = Mock()
    startup_env = {
        "AzureWebJobs.timer.Disabled": "true",
        test_samples_start.RUNTIME_LOG_LEVEL: "Information",
    }
    result = FuncStartResult(started=True, reason="host started", output="")
    start = Mock(return_value=result)

    monkeypatch.setattr(test_samples_start, "overlay_provider_settings", overlay)
    monkeypatch.setattr(test_samples_start, "_startup_env", Mock(return_value=startup_env))
    monkeypatch.setattr(test_samples_start, "start_and_verify", start)

    assert test_samples_start._start_sample_app(tmp_path) is result
    overlay.assert_called_once_with(tmp_path)
    start.assert_called_once_with(tmp_path, env=startup_env)
