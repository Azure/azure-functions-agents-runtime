#!/usr/bin/env python3
"""Deployed ACA qualification tooling (FRD 0008 §14, issue #166).

Five commands, each doing one thing a deployed qualification run needs. They are
runnable by hand and carry no pipeline wiring of their own:

``install-tooling``
    Install the shared Python tooling a qualification run needs.

``stamp``
    Write ``BUILD_INFO.json`` into the fixture app before it is packaged, so the
    deployed bytes carry the identity of the build that produced them.

``assemble``
    Copy the live fixture app and exactly one built runtime wheel into an upload
    directory, then write the marker and final remote-build ``requirements.txt``.

``deploy``
    Verify deployment rights, configure the authored region, package the staged
    fixture, deploy it, and add best-effort portal metadata.

``check-build``
    Ask the deployed app what it is running and compare it with this build. The
    marker is only meaningful because it is a *file inside the package*: a file
    can be served only if the package containing it is on disk, so a stale app
    cannot claim a build it is not running. An app setting or resource tag could
    be changed without deploying anything, which is where a service reporting
    its own version stops being evidence.

Each command fails closed on its own contract. ``check-build`` specifically
prevents qualification evidence from being attributed to the wrong deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MARKER_FILENAME = "BUILD_INFO.json"
_MARKER_SCHEMA = 1

_DEPLOY_PREFLIGHT_TIMEOUT_SECONDS = 30.0
_DEPLOY_CONFIGURATION_TIMEOUT_SECONDS = 300.0
_DEPLOY_TIMEOUT_SECONDS = 1_200.0
_DEFAULT_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "live" / "apps" / "aca-qualification"


class QualificationPipelineError(Exception):
    """A redacted pipeline configuration or verification failure."""


def build_marker(
    *,
    commit_sha: str,
    build_id: str,
    branch: str,
    runtime_version: str,
) -> dict[str, Any]:
    """Build the marker object stamped into the deployed package."""
    for name, value in (
        ("commit_sha", commit_sha),
        ("build_id", build_id),
        ("branch", branch),
        ("runtime_version", runtime_version),
    ):
        if not value or not value.strip():
            raise QualificationPipelineError(f"marker_field_empty:{name}")
    return {
        "schema": _MARKER_SCHEMA,
        "commit_sha": commit_sha.strip(),
        "build_id": build_id.strip(),
        "branch": branch.strip(),
        "runtime_version": runtime_version.strip(),
    }


def stamp_marker(app_root: Path, marker: Mapping[str, Any]) -> Path:
    """Write the marker into the fixture app root."""
    if not app_root.is_dir():
        raise QualificationPipelineError(f"app_root_missing:{app_root.name}")
    target = app_root / _MARKER_FILENAME
    target.write_text(json.dumps(dict(marker), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def select_runtime_wheel(candidate_filenames: Iterable[str]) -> str:
    """Select the only runtime wheel eligible for deployment."""
    names = sorted(Path(name).name for name in candidate_filenames if Path(name).suffix == ".whl")
    if not names:
        raise QualificationPipelineError("runtime_wheel_missing")
    if len(names) > 1:
        raise QualificationPipelineError(f"runtime_wheel_ambiguous:{','.join(names)}")
    return names[0]


def _render_requirements_text(
    *,
    wheel_filename: str,
    template_text: str,
    constraints_text: str,
) -> str:
    """Render remote-build requirements from the wheel name and pinned deps."""
    wheel_requirement = f"./{wheel_filename}"
    template_lines = template_text.splitlines()
    rendered_template: list[str] = []
    replaced_placeholder = False
    for line in template_lines:
        if (
            "{{RUNTIME_WHEEL}}" in line
            or "{runtime_wheel}" in line
            or "BUILD_WHEEL_PLACEHOLDER" in line
        ):
            rendered_template.append(wheel_requirement)
            replaced_placeholder = True
            continue
        if replaced_placeholder and line.strip() and not line.strip().startswith("#"):
            break
        rendered_template.append(line)

    sections: list[str] = []
    if not replaced_placeholder:
        sections.append(wheel_requirement)
        template = template_text.strip()
        if template:
            sections.append(template)
    elif rendered_template:
        sections.append("\n".join(rendered_template).strip())
    pinned = constraints_text.strip()
    if pinned:
        sections.append(pinned)
    return "\n\n".join(sections) + "\n"


def render_requirements(
    *,
    wheel_filename: str,
    constraints_path: Path,
    template_path: Path | None = None,
) -> str:
    """Render the final fixture requirements from checked-in dependency inputs."""
    if not constraints_path.is_file():
        raise QualificationPipelineError(f"constraints_file_missing:{constraints_path}")
    template_text = ""
    if template_path is not None:
        if not template_path.is_file():
            raise QualificationPipelineError(f"requirements_template_missing:{template_path}")
        template_text = template_path.read_text(encoding="utf-8")
    return _render_requirements_text(
        wheel_filename=wheel_filename,
        template_text=template_text,
        constraints_text=constraints_path.read_text(encoding="utf-8"),
    )


def _copy_fixture_app(fixture_root: Path, staging_root: Path) -> None:
    if not fixture_root.is_dir():
        raise QualificationPipelineError(f"fixture_root_missing:{fixture_root}")
    if staging_root.exists():
        shutil.rmtree(staging_root)
    shutil.copytree(
        fixture_root,
        staging_root,
        ignore=shutil.ignore_patterns("__pycache__", ".venv", ".pytest_cache", ".ruff_cache", "*.pyc"),
    )


def assemble_upload_directory(args: argparse.Namespace) -> Path:
    """Create the deployable remote-build upload directory."""
    artifact_root = Path(args.artifact_root)
    dist_root = artifact_root / "dist"
    wheel_name = select_runtime_wheel(path.name for path in dist_root.glob("*.whl"))
    fixture_root = Path(args.fixture_root)
    staging_root = Path(args.staging_root)
    _copy_fixture_app(fixture_root, staging_root)
    shutil.copy2(dist_root / wheel_name, staging_root / wheel_name)

    marker = build_marker(
        commit_sha=args.commit_sha,
        build_id=args.build_id,
        branch=args.branch,
        runtime_version=args.runtime_version,
    )
    stamp_marker(staging_root, marker)
    template_path = Path(args.requirements_template) if args.requirements_template else fixture_root / "requirements.txt"
    requirements = render_requirements(
        wheel_filename=wheel_name,
        constraints_path=Path(args.constraints_file),
        template_path=template_path,
    )
    (staging_root / "requirements.txt").write_text(requirements, encoding="utf-8")
    return staging_root


@dataclass(frozen=True, slots=True)
class MarkerComparison:
    """Result of comparing a deployed app's report against this build."""

    mismatches: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.mismatches


def compare_marker(
    reported: Mapping[str, Any],
    *,
    expected_build_id: str,
    expected_commit_sha: str,
    expected_python: str,
) -> MarkerComparison:
    """Compare a ``/__buildinfo`` response with the build that should be live.

    Returns field names only. Values are deliberately omitted: this runs in a
    pipeline log, and the comparison is a yes/no question.
    """
    mismatches: list[str] = []

    build = reported.get("build")
    if not isinstance(build, Mapping):
        return MarkerComparison(("build_section_missing",))

    marker_state = build.get("marker")
    if marker_state != "present":
        return MarkerComparison((f"marker_{marker_state or 'unknown'}",))

    if build.get("schema") != _MARKER_SCHEMA:
        mismatches.append("schema")
    if str(build.get("build_id", "")) != expected_build_id:
        mismatches.append("build_id")
    if str(build.get("commit_sha", "")) != expected_commit_sha:
        mismatches.append("commit_sha")

    runtime = reported.get("runtime")
    if not isinstance(runtime, Mapping):
        mismatches.append("runtime_section_missing")
    elif str(runtime.get("python_version", "")) != expected_python:
        mismatches.append("python_version")

    return MarkerComparison(tuple(mismatches))


def content_report(reported: Mapping[str, Any]) -> str:
    """Render deployed-content size against the platform package limits."""
    content = reported.get("content")
    if not isinstance(content, Mapping):
        return "content=unavailable"
    entries = content.get("entry_count")
    total = content.get("total_bytes")
    if not isinstance(entries, int) or not isinstance(total, int):
        return "content=unavailable"
    mib = total / (1024 * 1024)
    return (
        f"content_entries={entries} (cap 65535, {entries / 65535:.1%}) "
        f"content_size={mib:.1f}MiB (cap 256MiB, {mib / 256:.1%}) "
        f"truncated={str(content.get('truncated', False)).lower()}"
    )


_MAX_REASON_CHARS = 400
_SECRETISH = re.compile(
    r"(?i)(bearer\s+\S+|[?&](sig|sv|se|st|skoid|sig)=[^&\s]+|eyJ[A-Za-z0-9_\-.]{20,})"
)


def _redacted_reason(error: BaseException) -> str:
    """Render an operational cause without leaking credential material."""
    text = str(error).strip() or type(error).__name__
    text = _SECRETISH.sub("<redacted>", text)
    text = " ".join(text.split())
    if len(text) > _MAX_REASON_CHARS:
        text = text[:_MAX_REASON_CHARS] + "…"
    return text


def deploy_preflight_failure_message(
    *,
    app_name: str,
    resource_group: str,
    check_name: str,
    slow: bool = False,
) -> str:
    """Name the Function App deployment role needed before ZIP deployment."""
    slow_sentence = (
        f" The check hit its {_DEPLOY_PREFLIGHT_TIMEOUT_SECONDS:.0f}s deadline; fail-fast "
        "authorization probes treat that as a deployment-readiness failure."
        if slow
        else ""
    )
    return (
        f"functionapp_deploy_preflight_failed:{check_name}: grant Website Contributor on "
        f"{app_name} in resource group {resource_group} to the deployment identity "
        "so it can read the site before ZIP deployment."
        f"{slow_sentence}"
    )


def _run_az(args: Sequence[str], *, timeout_seconds: float = _DEPLOY_PREFLIGHT_TIMEOUT_SECONDS) -> None:
    command = ["az", *args, "--only-show-errors", "--output", "json"]
    operation = ":".join(args[:2])
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        raise QualificationPipelineError(
            f"az_timeout:{operation}:{int(timeout_seconds)}s"
        ) from None
    except OSError as error:
        raise QualificationPipelineError(
            f"az_unavailable:{operation}:{type(error).__name__}"
        ) from None
    if completed.returncode != 0:
        raise QualificationPipelineError(
            f"az_failed:{operation}:exit_{completed.returncode}"
        ) from None


def run_install_tooling() -> int:
    """Install the common dependencies for manual qualification runs."""
    commands = (
        [sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
        [sys.executable, "-m", "pip", "install", "-U", "-e", ".[dev,aca_sandbox]"],
    )
    for command in commands:
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as error:
            raise QualificationPipelineError("pipeline_tooling_install_failed") from error
    return 0


def run_preflight_deploy(args: argparse.Namespace) -> int:
    """Verify the deployment identity can read the app and publishing config."""
    app_name = args.app_name
    resource_group = args.resource_group
    checks = (
        (
            "site_read",
            ["functionapp", "show", "--name", app_name, "--resource-group", resource_group],
        ),
        # Flex disables SCM basic auth, so publishing-profile checks are invalid.
    )
    for check_name, command in checks:
        try:
            _run_az(command)
        except QualificationPipelineError as error:
            raise QualificationPipelineError(
                deploy_preflight_failure_message(
                    app_name=app_name,
                    resource_group=resource_group,
                    check_name=check_name,
                    slow=str(error).startswith("az_timeout:"),
                )
            ) from error
    print("Function App deployment preflight succeeded.")
    return 0


def _write_deployment_archive(staging_root: Path, archive_path: Path) -> None:
    if not staging_root.is_dir():
        raise QualificationPipelineError(f"staging_root_missing:{staging_root.name}")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.unlink(missing_ok=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(staging_root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(staging_root).as_posix())


def run_deploy(args: argparse.Namespace) -> int:
    """Preflight and deploy one staged fixture through the customer-equivalent path."""
    run_preflight_deploy(args)
    region = args.region.strip()
    if not region:
        raise QualificationPipelineError("deployment_region_empty")

    _run_az(
        [
            "functionapp",
            "config",
            "appsettings",
            "set",
            "--name",
            args.app_name,
            "--resource-group",
            args.resource_group,
            "--settings",
            f"AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION={region}",
        ],
        timeout_seconds=_DEPLOY_CONFIGURATION_TIMEOUT_SECONDS,
    )

    archive_path = Path(args.archive_path)
    _write_deployment_archive(Path(args.staging_root), archive_path)
    _run_az(
        [
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
        ],
        timeout_seconds=_DEPLOY_TIMEOUT_SECONDS,
    )

    try:
        _run_az(
            [
                "tag",
                "update",
                "--operation",
                "merge",
                "--resource-group",
                args.resource_group,
                "--name",
                args.app_name,
                "--resource-type",
                "Microsoft.Web/sites",
                "--tags",
                f"build_id={args.build_id}",
                f"commit_sha={args.commit_sha}",
            ]
        )
    except QualificationPipelineError as error:
        print(
            "warning: Function App metadata tag failed: "
            f"{_redacted_reason(error)}"
        )
    print("Function App deployment completed.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser(
        "install-tooling",
        help="install Python dependencies shared by qualification runs",
    )

    stamp = subcommands.add_parser("stamp", help="write BUILD_INFO.json into the fixture app")
    stamp.add_argument("--app-root", required=True)
    stamp.add_argument("--commit-sha", required=True)
    stamp.add_argument("--build-id", required=True)
    stamp.add_argument("--branch", required=True)
    stamp.add_argument("--runtime-version", required=True)

    assemble = subcommands.add_parser("assemble", help="build the Flex remote-build upload directory")
    assemble.add_argument("--artifact-root", required=True)
    assemble.add_argument("--staging-root", required=True)
    assemble.add_argument("--fixture-root", default=str(_DEFAULT_FIXTURE_ROOT))
    assemble.add_argument("--constraints-file", required=True)
    assemble.add_argument("--requirements-template")
    assemble.add_argument("--commit-sha", required=True)
    assemble.add_argument("--build-id", required=True)
    assemble.add_argument("--branch", required=True)
    assemble.add_argument("--runtime-version", required=True)

    deploy = subcommands.add_parser(
        "deploy",
        help="preflight, configure, package, and deploy one staged fixture",
    )
    deploy.add_argument("--staging-root", required=True)
    deploy.add_argument("--archive-path", required=True)
    deploy.add_argument("--app-name", required=True)
    deploy.add_argument("--resource-group", required=True)
    deploy.add_argument("--region", required=True)
    deploy.add_argument("--build-id", required=True)
    deploy.add_argument("--commit-sha", required=True)

    check = subcommands.add_parser("check-build", help="verify the deployed app is this build")
    check.add_argument("--base-url", required=True)
    check.add_argument("--token-scope", required=True)
    check.add_argument("--build-id", required=True)
    check.add_argument("--commit-sha", required=True)
    check.add_argument("--python-version", required=True)

    return parser


_READINESS_DEADLINE_SECONDS = 300.0
_READINESS_POLL_SECONDS = 10.0
_NOT_READY_STATUSES = frozenset({408, 429, *range(500, 600)})


async def fetch_build_info(base_url: str, token_scope: str) -> dict[str, Any]:
    """Read and parse the embedded build marker without logging its values."""
    import aiohttp
    from azure.identity.aio import DefaultAzureCredential

    credential = DefaultAzureCredential()
    try:
        token = await credential.get_token(token_scope)
    finally:
        await credential.close()
    url = f"{base_url.rstrip('/')}/__buildinfo"
    timeout = aiohttp.ClientTimeout(total=60)
    deadline = time.monotonic() + _READINESS_DEADLINE_SECONDS
    last_status: int | None = None

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(
                    url, headers={"Authorization": f"Bearer {token.token}"}
                ) as response:
                    if response.status == 200:
                        payload = await response.json(content_type=None)
                        break
                    last_status = response.status
                    if response.status not in _NOT_READY_STATUSES:
                        raise QualificationPipelineError(f"buildinfo_http_{response.status}")
            except (TimeoutError, aiohttp.ClientError) as error:
                last_status = last_status or -1
                if time.monotonic() >= deadline:
                    raise QualificationPipelineError(
                        f"buildinfo_unreachable:{type(error).__name__}"
                    ) from None
            if time.monotonic() >= deadline:
                raise QualificationPipelineError(
                    f"buildinfo_not_ready_after_{int(_READINESS_DEADLINE_SECONDS)}s"
                    f":last_status_{last_status}"
                )
            print(f"App not ready yet (status {last_status}); waiting for restart to settle.")
            await asyncio.sleep(_READINESS_POLL_SECONDS)

    if not isinstance(payload, dict):
        raise QualificationPipelineError("buildinfo_malformed")
    return payload


def run_check_build(args: argparse.Namespace) -> int:
    """Fail fast when the deployed app is not the build under qualification."""
    try:
        reported = asyncio.run(fetch_build_info(args.base_url, args.token_scope))
    except QualificationPipelineError:
        raise
    except Exception as error:
        raise QualificationPipelineError(f"buildinfo_unreachable:{type(error).__name__}") from None

    comparison = compare_marker(
        reported,
        expected_build_id=args.build_id,
        expected_commit_sha=args.commit_sha,
        expected_python=args.python_version,
    )
    print(content_report(reported))
    if not comparison.matches:
        print(
            "Deployed app does not match this build: "
            f"{','.join(comparison.mismatches)}",
            file=sys.stderr,
        )
        return 1
    print(f"Deployed build verified (python {args.python_version}).")
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    """Dispatch one qualification command."""
    args = _parser().parse_args(arguments)
    try:
        if args.command == "install-tooling":
            return run_install_tooling()
        if args.command == "stamp":
            marker = build_marker(
                commit_sha=args.commit_sha,
                build_id=args.build_id,
                branch=args.branch,
                runtime_version=args.runtime_version,
            )
            target = stamp_marker(Path(args.app_root), marker)
            print(f"Stamped {target.name} for build {marker['build_id']}.")
            return 0
        if args.command == "assemble":
            staging_root = assemble_upload_directory(args)
            print(f"Assembled upload directory {staging_root}.")
            return 0
        if args.command == "deploy":
            return run_deploy(args)
        return run_check_build(args)
    except QualificationPipelineError as error:
        print(f"ACA qualification pipeline failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
