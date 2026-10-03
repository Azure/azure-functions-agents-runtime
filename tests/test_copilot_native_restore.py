"""Real SDK/native filesystem boundaries without inference or downloads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 300


def runtime_is_cached() -> bool:
    """Report whether the pinned runtime bundle is already present; never download it."""
    from copilot._cli_version import get_runtime_platform

    bundle = REPO_ROOT / ".tmp-validation" / "runtime-1.0.85" / "prebuilds" / get_runtime_platform()
    wrapper = "copilot-runtime.exe" if os.name == "nt" else "copilot-runtime"
    return all((bundle / name).is_file() for name in (
        wrapper, "runtime.node", ".hostless-runtime-assets-v2"
    ))


def run_worker(
    phase: int, session_dir: Path, storage_root: Path, out: Path, cwd: Path,
    *, expected_exit: int = 0,
) -> dict:
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
    result = json.loads(out.read_text(encoding="utf-8"))
    assert completed.returncode == expected_exit, result
    return result


pytestmark = pytest.mark.skipif(
    os.environ.get("AZURE_FUNCTIONS_AGENTS_TEST_NATIVE_COPILOT") != "1" or not runtime_is_cached(),
    reason="Explicit offline native test only; pinned assets must already be cached.",
)


def test_replacement_process_uses_the_same_known_workspace_with_opaque_files(tmp_path):
    session_dir = tmp_path / "sessions"
    first_cwd = tmp_path / "first-cwd"
    second_cwd = tmp_path / "second-cwd"
    for directory in (session_dir, first_cwd, second_cwd):
        directory.mkdir(parents=True, exist_ok=True)

    created = run_worker(1, session_dir, tmp_path / "root-a", tmp_path / "a.json", first_cwd)
    assert created["outcome"] == "ok", created.get("error")
    assert "session-state/opaque.sdk" in created["files"]
    assert created["denied_paths"] == []

    resumed = run_worker(2, session_dir, tmp_path / "root-a", tmp_path / "b.json", second_cwd)
    assert resumed["outcome"] == "ok", resumed.get("error")
    assert resumed["pid"] != created["pid"]
    assert resumed["cwd"] != created["cwd"]
    assert resumed["workspace"] == created["workspace"]
    assert resumed["denied_paths"] == []
    assert "session-state/opaque.sdk" in resumed["files"]
    assert any(path.startswith("/session-state") for path in resumed["paths"])


def test_recorded_unknown_workspace_is_denied_without_suffix_adoption(tmp_path):
    session_dir = tmp_path / "sessions"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    created = run_worker(1, session_dir, tmp_path / "root-a", tmp_path / "a.json", cwd)
    assert created["outcome"] == "ok", created.get("error")
    resumed = run_worker(
        2, session_dir, tmp_path / "root-b", tmp_path / "b.json", cwd, expected_exit=1
    )
    assert resumed["outcome"] == "error"
    assert resumed["workspace"] != created["workspace"]
    assert created["workspace"] in resumed["denied_paths"]
