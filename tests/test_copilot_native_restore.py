"""Cross-process native cold-restore coverage against the pinned runtime (no model calls)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from azure_functions_agents._copilot_session_fs import WORKSPACE_ROOT

REPO_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 300


def runtime_is_cached() -> bool:
    """Report whether the pinned runtime bundle is already present; never download it."""
    from copilot._cli_download import ensure_runtime_wrapper

    previous = os.environ.get("COPILOT_SKIP_CLI_DOWNLOAD")
    os.environ["COPILOT_SKIP_CLI_DOWNLOAD"] = "1"
    try:
        return bool(ensure_runtime_wrapper())
    except Exception:
        return False
    finally:
        if previous is None:
            os.environ.pop("COPILOT_SKIP_CLI_DOWNLOAD", None)
        else:
            os.environ["COPILOT_SKIP_CLI_DOWNLOAD"] = previous


def run_worker(phase: int, session_dir: Path, storage_root: Path, out: Path, cwd: Path) -> dict:
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "src")]),
        "COPILOT_SKIP_CLI_DOWNLOAD": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "tests._native_restore_worker", str(phase), str(session_dir),
         str(storage_root), str(out)],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    assert out.is_file(), f"worker {phase} produced no report: {completed.stderr[-2000:]}"
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.skipif(not runtime_is_cached(), reason="pinned native runtime is not cached")
def test_second_process_resumes_the_hydrated_session_from_a_different_host_root(tmp_path):
    """A replacement worker with its own host root and cwd must resume the persisted tree."""
    session_dir = tmp_path / "sessions"
    first_cwd = tmp_path / "first-cwd"
    second_cwd = tmp_path / "second-cwd"
    for directory in (session_dir, first_cwd, second_cwd):
        directory.mkdir(parents=True, exist_ok=True)

    created = run_worker(1, session_dir, tmp_path / "root-a", tmp_path / "a.json", first_cwd)
    assert created["outcome"] == "ok", created.get("error")
    assert "/session-state/events.jsonl" in created["files"]
    assert WORKSPACE_ROOT not in created["paths"], "creation never resolves the host workspace"

    resumed = run_worker(2, session_dir, tmp_path / "root-b", tmp_path / "b.json", second_cwd)
    assert resumed["outcome"] == "ok", resumed.get("error")
    assert resumed["pid"] != created["pid"]
    assert resumed["cwd"] != created["cwd"]
    assert resumed["workspace"] != created["workspace"]
    # The runtime resolves the recorded host cwd through SessionFs before loading events.
    assert WORKSPACE_ROOT in resumed["paths"]
    assert "/session-state/events.jsonl" in resumed["paths"]
    assert "/session-state/events.jsonl" in resumed["files"]
