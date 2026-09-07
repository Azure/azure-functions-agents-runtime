from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_durable_loop_sample_runs_without_azure() -> None:
    root = Path(__file__).parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "samples" / "durable-loop-foundation" / "run_sample.py"),
        ],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output == {
        "first_status": "Completed",
        "human_question": "Which region should I inspect?",
        "second_status": "Completed",
        "session_id": "sample-session",
        "turns": 2,
    }
