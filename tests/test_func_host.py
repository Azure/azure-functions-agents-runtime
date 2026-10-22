from __future__ import annotations

import queue
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.endtoend import _func_host


def _agent_app(tmp_path: Path) -> Path:
    (tmp_path / "function_app.py").write_text(
        "from azure_functions_agents import create_function_app\n"
        "app = create_function_app()\n",
        encoding="utf-8",
    )
    (tmp_path / "main.agent.md").write_text("---\nname: Main\n---\n", encoding="utf-8")
    return tmp_path


def test_harness_selection_accepts_expected_marker(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(_func_host.HARNESS_SELECTOR, raising=False)
    app_dir = _agent_app(tmp_path)

    assert (
        _func_host._harness_selection_failure(
            app_dir,
            "Agent harness selected: harness=maf\nAgent harness selected: harness=maf\n",
            None,
        )
        is None
    )


def test_harness_selection_rejects_conflicting_marker(tmp_path: Path) -> None:
    app_dir = _agent_app(tmp_path)

    failure = _func_host._harness_selection_failure(
        app_dir,
        "Agent harness selected: harness=maf\n",
        {_func_host.HARNESS_SELECTOR: "true"},
    )

    assert failure == "expected harness 'copilot', but host output also selected 'maf'"


def test_harness_selection_allows_missing_marker(tmp_path: Path) -> None:
    app_dir = _agent_app(tmp_path)

    assert (
        _func_host._harness_selection_failure(
            app_dir,
            "Host started\n",
            {_func_host.HARNESS_SELECTOR: "true"},
        )
        is None
    )


def test_harness_selection_ignores_non_agent_host(tmp_path: Path) -> None:
    (tmp_path / "function_app.py").write_text(
        "from azure.functions import FunctionApp\napp = FunctionApp()\n",
        encoding="utf-8",
    )

    assert _func_host._harness_selection_failure(tmp_path, "Host started\n", None) is None


def _host_process() -> _func_host._HostProcess:
    return _func_host._HostProcess(
        proc=Mock(),
        line_queue=queue.Queue(),
        reader=Mock(),
        lines=[],
    )


def test_running_host_retries_transient_startup_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first_host = _host_process()
    second_host = _host_process()
    spawn = Mock(side_effect=[first_host, second_host])
    await_ready = Mock(
        side_effect=[
            (False, False, "timed out after 150s waiting for the host to start"),
            (True, False, "host started"),
        ]
    )
    terminate = Mock()

    monkeypatch.setattr(_func_host, "ensure_local_settings", Mock())
    monkeypatch.setattr(_func_host.shutil, "which", Mock(return_value="func"))
    monkeypatch.setattr(_func_host, "_spawn", spawn)
    monkeypatch.setattr(_func_host, "_await_ready", await_ready)
    monkeypatch.setattr(_func_host, "_terminate", terminate)
    monkeypatch.setattr(_func_host, "_drain", Mock())

    with _func_host.running_host(tmp_path, port=7071) as handle:
        assert handle.port == 7071
        assert handle._host is second_host

    assert spawn.call_count == 2
    terminate.assert_any_call(first_host.proc)
    terminate.assert_any_call(second_host.proc)


def test_running_host_does_not_retry_explicit_startup_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    host = _host_process()
    spawn = Mock(return_value=host)

    monkeypatch.setattr(_func_host, "ensure_local_settings", Mock())
    monkeypatch.setattr(_func_host.shutil, "which", Mock(return_value="func"))
    monkeypatch.setattr(_func_host, "_spawn", spawn)
    monkeypatch.setattr(
        _func_host,
        "_await_ready",
        Mock(return_value=(False, True, "detected failure marker: failed to index functions")),
    )
    monkeypatch.setattr(_func_host, "_terminate", Mock())
    monkeypatch.setattr(_func_host, "_drain", Mock())

    with (
        pytest.raises(RuntimeError, match="failed to index functions"),
        _func_host.running_host(tmp_path, port=7071),
    ):
        pass

    spawn.assert_called_once()