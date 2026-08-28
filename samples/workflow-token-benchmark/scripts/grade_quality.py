"""Grade exported benchmark ATIF trajectories with the Vally prompt judge."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

SAMPLE_ROOT = Path(__file__).resolve().parents[1]
EVAL_SPEC = SAMPLE_ROOT / "evals" / "eval.yaml"
VALLY_PACKAGE = "@microsoft/vally-cli@0.14.0"


def grade_trajectories(
    *,
    results_dir: Path,
    judge_model: str | None,
    repeat: int | None,
) -> list[Path]:
    npx = shutil.which("npx")
    if npx is None:
        raise RuntimeError("required executable 'npx' was not found")
    atif_dir = results_dir / "atif"
    trajectories = sorted(atif_dir.glob("*.json"))
    if repeat is not None:
        trajectories = [
            path for path in trajectories if f"-repeat-{repeat}-" in path.name
        ]
    if not trajectories:
        raise RuntimeError(f"no matching ATIF trajectories found under {atif_dir}")

    output_dir = results_dir / "vally"
    output_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.setdefault("VALLY_TELEMETRY_OPTOUT", "1")
    outputs: list[Path] = []
    for trajectory in trajectories:
        command = [
            npx,
            "--yes",
            VALLY_PACKAGE,
            "grade",
            "--eval-spec",
            str(EVAL_SPEC),
            "--stimulus",
            "report-quality",
            "--output",
            "jsonl",
        ]
        if judge_model:
            command.extend(["--judge-model", judge_model])
        print(f"Grading {trajectory.name}...")
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                input=trajectory.read_text(encoding="utf-8"),
                env=environment,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Vally could not grade {trajectory.name}: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"Vally failed for {trajectory.name}: {detail[-2000:]}")
        output_path = output_dir / f"{trajectory.stem}.jsonl"
        output_path.write_text(completed.stdout, encoding="utf-8")
        outputs.append(output_path)
    return outputs


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--judge-model")
    parser.add_argument(
        "--repeat",
        type=int,
        help="Grade only one repeat per service count to limit judge cost",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        outputs = grade_trajectories(
            results_dir=args.results_dir.resolve(),
            judge_model=args.judge_model,
            repeat=args.repeat,
        )
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"Wrote {len(outputs)} Vally result(s) under {outputs[0].parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
