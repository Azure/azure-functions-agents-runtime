#!/usr/bin/env python3
"""Assemble and deploy the private durable-loop spike without handling secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_SAMPLE_ROOT = Path(__file__).resolve().parents[2] / "samples" / "durable-agent-loop-spike"
_DEFAULT_SOURCE_ROOT = _DEFAULT_SAMPLE_ROOT / "src"
_DEFAULT_ARTIFACT_ROOT = _DEFAULT_SAMPLE_ROOT / ".artifacts"
_DEFAULT_RESOURCE_GROUP = "larohra-durable-agent-loop"
_DEFAULT_FUNCTION_APP = "func-durable-loop-0904"
_RUNTIME_EXTRAS = ("aca_sandbox", "monitor")
_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }
)
_IGNORED_FILES = frozenset(
    {
        ".funcignore",
        "ASSEMBLY_SLOT.md",
        "local.settings.json",
        "local.settings.template.json",
        "requirements.txt",
        "requirements.extra.txt",
    }
)


class DurableLoopDeploymentError(Exception):
    """A redacted assembly or deployment contract failure."""


@dataclass(frozen=True, slots=True)
class AssemblyResult:
    """Paths and digests produced by one deterministic assembly."""

    archive_path: Path
    archive_sha256: str
    staging_root: Path
    wheel_name: str


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reset_generated_directory(path: Path, *, protected: Sequence[Path]) -> None:
    resolved = path.resolve()
    resolved_key = os.path.normcase(os.fspath(resolved))
    protected_paths_and_ancestors = {
        os.path.normcase(os.fspath(candidate))
        for item in protected
        for candidate in (item.resolve(), *item.resolve().parents)
    }
    if (
        resolved_key in protected_paths_and_ancestors
        or resolved == Path(resolved.anchor)
        or resolved == Path.home().resolve()
    ):
        raise DurableLoopDeploymentError(f"unsafe_generated_directory:{path.name}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def select_runtime_wheel(candidate_paths: Sequence[Path]) -> Path:
    """Select exactly one locally built runtime wheel."""
    wheels = sorted(
        (
            path
            for path in candidate_paths
            if path.is_file()
            and path.suffix == ".whl"
            and path.name.startswith("azurefunctions_agents_runtime-")
        ),
        key=lambda path: path.name,
    )
    if not wheels:
        raise DurableLoopDeploymentError("runtime_wheel_missing")
    if len(wheels) > 1:
        raise DurableLoopDeploymentError("runtime_wheel_ambiguous")
    return wheels[0]


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    operation: str,
    timeout_seconds: float,
) -> None:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        raise DurableLoopDeploymentError(f"{operation}_timeout") from None
    except OSError as error:
        raise DurableLoopDeploymentError(
            f"{operation}_unavailable:{type(error).__name__}"
        ) from None
    if completed.returncode != 0:
        raise DurableLoopDeploymentError(f"{operation}_failed:exit_{completed.returncode}")


def build_runtime_wheel(repo_root: Path, dist_root: Path) -> Path:
    """Build exactly one local runtime wheel without resolving dependencies."""
    if not (repo_root / "pyproject.toml").is_file():
        raise DurableLoopDeploymentError("repo_root_invalid")
    _reset_generated_directory(dist_root, protected=(repo_root,))
    _run_command(
        (
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(dist_root),
            str(repo_root),
        ),
        cwd=repo_root,
        operation="runtime_wheel_build",
        timeout_seconds=900,
    )
    return select_runtime_wheel(tuple(dist_root.iterdir()))


def _source_files(source_root: Path) -> tuple[Path, ...]:
    if not source_root.is_dir():
        raise DurableLoopDeploymentError("application_source_missing")
    if not (source_root / "function_app.py").is_file():
        raise DurableLoopDeploymentError("application_source_incomplete:function_app.py")
    selected: list[Path] = []
    for path in sorted(source_root.rglob("*")):
        relative = path.relative_to(source_root)
        if any(part in _IGNORED_DIRECTORIES for part in relative.parts):
            continue
        if path.is_file() and path.name not in _IGNORED_FILES:
            selected.append(path)
    return tuple(selected)


def _render_requirements(wheel_name: str, requirements_extra: Path | None) -> str:
    extras = ",".join(_RUNTIME_EXTRAS)
    sections = [f"./{wheel_name}[{extras}]"]
    if requirements_extra is not None:
        if not requirements_extra.is_file():
            raise DurableLoopDeploymentError("requirements_extra_missing")
        extra = requirements_extra.read_text(encoding="utf-8").strip()
        if extra:
            sections.append(extra)
    return "\n\n".join(sections) + "\n"


def stage_application(
    *,
    source_root: Path,
    wheel_path: Path,
    staging_root: Path,
    commit_sha: str,
    requirements_extra: Path | None = None,
) -> Path:
    """Stage final source plus one runtime wheel and a content manifest."""
    source_files = _source_files(source_root)
    if not wheel_path.is_file():
        raise DurableLoopDeploymentError("runtime_wheel_missing")
    if not commit_sha.strip():
        raise DurableLoopDeploymentError("commit_sha_empty")

    _reset_generated_directory(
        staging_root,
        protected=(source_root, wheel_path.parent),
    )
    for source_path in source_files:
        relative = source_path.relative_to(source_root)
        destination = staging_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)

    staged_wheel = staging_root / wheel_path.name
    shutil.copyfile(wheel_path, staged_wheel)
    (staging_root / "requirements.txt").write_text(
        _render_requirements(wheel_path.name, requirements_extra),
        encoding="utf-8",
        newline="\n",
    )

    files = {
        path.relative_to(staging_root).as_posix(): _sha256_file(path)
        for path in sorted(staging_root.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "schema": 1,
        "commit_sha": commit_sha.strip(),
        "files": files,
        "wheel": {
            "filename": wheel_path.name,
            "sha256": _sha256_file(staged_wheel),
        },
    }
    (staging_root / "DEPLOYMENT_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return staging_root


def write_deterministic_archive(staging_root: Path, archive_path: Path) -> str:
    """Write a byte-stable ZIP using sorted paths and fixed metadata."""
    if not staging_root.is_dir():
        raise DurableLoopDeploymentError("staging_root_missing")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.unlink(missing_ok=True)
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in sorted(staging_root.rglob("*")):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(
                path.relative_to(staging_root).as_posix(),
                date_time=_FIXED_ZIP_TIMESTAMP,
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    return _sha256_file(archive_path)


def _git_commit_sha(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DurableLoopDeploymentError("git_commit_unavailable") from None
    commit_sha = completed.stdout.strip()
    if completed.returncode != 0 or len(commit_sha) != 40:
        raise DurableLoopDeploymentError("git_commit_unavailable")
    return commit_sha


def assemble(args: argparse.Namespace) -> AssemblyResult:
    """Build the wheel, stage the final app, and create its deterministic ZIP."""
    repo_root = Path(args.repo_root).resolve()
    source_root = Path(args.source_root).resolve()
    artifact_root = Path(args.artifact_root).resolve()
    _source_files(source_root)
    dist_root = artifact_root / "dist"
    staging_root = artifact_root / "staging"
    archive_path = artifact_root / "durable-agent-loop-spike.zip"
    wheel = build_runtime_wheel(repo_root, dist_root)
    stage_application(
        source_root=source_root,
        wheel_path=wheel,
        staging_root=staging_root,
        commit_sha=_git_commit_sha(repo_root),
        requirements_extra=(
            Path(args.requirements_extra).resolve() if args.requirements_extra else None
        ),
    )
    archive_sha256 = write_deterministic_archive(staging_root, archive_path)
    return AssemblyResult(
        archive_path=archive_path,
        archive_sha256=archive_sha256,
        staging_root=staging_root,
        wheel_name=wheel.name,
    )


def _run_az(arguments: Sequence[str], *, timeout_seconds: float) -> None:
    _run_command(
        ("az", *arguments, "--only-show-errors", "--output", "none"),
        cwd=Path.cwd(),
        operation=f"az_{'_'.join(arguments[:2])}",
        timeout_seconds=timeout_seconds,
    )


def deploy(args: argparse.Namespace) -> None:
    """Deploy one already-assembled ZIP without reading or changing app settings."""
    archive_path = Path(args.archive_path).resolve()
    if not archive_path.is_file():
        raise DurableLoopDeploymentError("deployment_archive_missing")
    if args.acknowledge_existing_app != args.app_name:
        raise DurableLoopDeploymentError("existing_app_acknowledgment_mismatch")
    _run_az(
        (
            "functionapp",
            "show",
            "--name",
            args.app_name,
            "--resource-group",
            args.resource_group,
        ),
        timeout_seconds=30,
    )
    _run_az(
        (
            "functionapp",
            "deployment",
            "source",
            "config-zip",
            "--name",
            args.app_name,
            "--resource-group",
            args.resource_group,
            "--src",
            str(archive_path),
            "--build-remote",
            "true",
        ),
        timeout_seconds=1200,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    assemble_parser = subcommands.add_parser(
        "assemble",
        help="build the local runtime wheel and deterministic deployment ZIP",
    )
    assemble_parser.add_argument(
        "--repo-root",
        default=str(_DEFAULT_SAMPLE_ROOT.parents[1]),
    )
    assemble_parser.add_argument(
        "--source-root",
        default=str(_DEFAULT_SOURCE_ROOT),
    )
    assemble_parser.add_argument(
        "--artifact-root",
        default=str(_DEFAULT_ARTIFACT_ROOT),
    )
    assemble_parser.add_argument("--requirements-extra")

    deploy_parser = subcommands.add_parser(
        "deploy",
        help="deploy an assembled ZIP to the existing Flex app",
    )
    deploy_parser.add_argument(
        "--archive-path",
        default=str(_DEFAULT_ARTIFACT_ROOT / "durable-agent-loop-spike.zip"),
    )
    deploy_parser.add_argument(
        "--resource-group",
        default=_DEFAULT_RESOURCE_GROUP,
    )
    deploy_parser.add_argument("--app-name", default=_DEFAULT_FUNCTION_APP)
    deploy_parser.add_argument(
        "--acknowledge-existing-app",
        required=True,
        help="must exactly repeat --app-name",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """Dispatch deterministic assembly or explicit deployment."""
    args = _parser().parse_args(arguments)
    try:
        if args.command == "assemble":
            result = assemble(args)
            print(
                "Assembly completed: "
                f"archive={result.archive_path} "
                f"sha256={result.archive_sha256} "
                f"wheel={result.wheel_name}"
            )
        else:
            deploy(args)
            print("Function App ZIP deployment completed.")
    except DurableLoopDeploymentError as error:
        print(f"Durable loop deployment failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
