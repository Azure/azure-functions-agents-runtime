from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the runtime wheel and sync deploy requirements for samples."
    )
    parser.add_argument(
        "--sample",
        action="append",
        default=[],
        help=(
            "Sample directory to update. Accepts a sample name like 'basic-chat' or "
            "a path like '.' when run from a sample directory. Defaults to all samples."
        ),
    )
    return parser.parse_args()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_optional_dependencies(repo_root: Path) -> dict[str, list[str]]:
    pyproject = repo_root / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)

    optional_dependencies = data.get("project", {}).get("optional-dependencies", {})
    return {
        extra: [str(requirement) for requirement in requirements]
        for extra, requirements in optional_dependencies.items()
    }


def _build_runtime_wheel(repo_root: Path) -> Path:
    wheelhouse = repo_root / "artifacts" / "sample-wheels"
    wheelhouse.mkdir(parents=True, exist_ok=True)

    for existing_wheel in wheelhouse.glob("azurefunctions_agents_runtime-*.whl"):
        existing_wheel.unlink()

    uv = shutil.which("uv")
    if uv is not None:
        subprocess.run(
            [
                uv,
                "build",
                "--wheel",
                "--out-dir",
                str(wheelhouse),
            ],
            check=True,
            cwd=repo_root,
        )
    else:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(wheelhouse),
                str(repo_root),
            ],
            check=True,
            cwd=repo_root,
        )

    wheels = sorted(wheelhouse.glob("azurefunctions_agents_runtime-*.whl"))
    if not wheels:
        raise FileNotFoundError("No runtime wheel was produced.")

    return wheels[-1]


def _resolve_samples(repo_root: Path, requested_samples: list[str]) -> list[Path]:
    if not requested_samples:
        return sorted(
            sample_root
            for sample_root in (repo_root / "samples").iterdir()
            if (sample_root / "azure.yaml").is_file()
        )

    resolved: list[Path] = []
    seen: set[Path] = set()

    for requested in requested_samples:
        candidate = Path(requested)
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()

        if (candidate / "azure.yaml").is_file():
            sample_root = candidate
        else:
            sample_root = (repo_root / "samples" / requested).resolve()

        if not (sample_root / "azure.yaml").is_file():
            raise FileNotFoundError(f"Sample '{requested}' does not contain azure.yaml")

        if sample_root not in seen:
            resolved.append(sample_root)
            seen.add(sample_root)

    return resolved


def _expand_runtime_requirement(
    requirement: str,
    wheel_name: str,
    optional_dependencies: dict[str, list[str]],
) -> list[str]:
    extras_part = requirement.partition("../../..")[2].strip()
    extras = extras_part.removeprefix("[").removesuffix("]")

    expanded = [f"./wheels/{wheel_name}"]
    if extras:
        for extra in (name.strip() for name in extras.split(",") if name.strip()):
            expanded.extend(optional_dependencies.get(extra, []))

    return expanded


def _sync_sample(
    sample_root: Path,
    runtime_wheel: Path,
    optional_dependencies: dict[str, list[str]],
) -> None:
    sample_src = sample_root / "src"
    wheels_dir = sample_src / "wheels"
    wheels_dir.mkdir(parents=True, exist_ok=True)

    for existing_wheel in wheels_dir.glob("azurefunctions_agents_runtime-*.whl"):
        existing_wheel.unlink()

    destination_wheel = wheels_dir / runtime_wheel.name
    shutil.copy2(runtime_wheel, destination_wheel)

    requirements_dev_path = sample_src / "requirements.dev.txt"
    requirements_path = sample_src / "requirements.txt"

    requirements_dev_lines = requirements_dev_path.read_text(encoding="utf-8").splitlines()
    requirements_lines: list[str] = []

    for line in requirements_dev_lines:
        stripped = line.strip()
        if stripped.startswith("-e ../../.."):
            requirements_lines.extend(
                _expand_runtime_requirement(stripped, runtime_wheel.name, optional_dependencies)
            )
            continue

        requirements_lines.append(line)

    requirements_path.write_text(
        "\n".join(requirements_lines).rstrip() + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = _parse_args()
    repo_root = _repo_root()
    optional_dependencies = _load_optional_dependencies(repo_root)
    runtime_wheel = _build_runtime_wheel(repo_root)
    sample_roots = _resolve_samples(repo_root, args.sample)

    for sample_root in sample_roots:
        _sync_sample(sample_root, runtime_wheel, optional_dependencies)
        print(f"Synced {sample_root.relative_to(repo_root)}")

    print(f"Wheel: {runtime_wheel.name}")
    shutil.rmtree(runtime_wheel.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
