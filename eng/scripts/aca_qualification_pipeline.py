#!/usr/bin/env python3
"""Post-main ACA qualification pipeline helpers (FRD 0008 §14, issue #166).

Five commands, each doing one thing the official pipeline needs:

``stamp``
    Write ``BUILD_INFO.json`` into the fixture app before it is packaged, so the
    deployed bytes carry the identity of the build that produced them.

``assemble``
    Copy the live fixture app and exactly one built runtime wheel into an upload
    directory, then write the marker and final remote-build ``requirements.txt``.

``preflight-deploy``
    Verify the deployment identity can read the target Function App and its
    publishing configuration before One Deploy can fail later with an opaque 403.

``check-build``
    Ask the deployed app what it is running and compare it with this build. The
    marker is only meaningful because it is a *file inside the package*: a file
    can be served only if the package containing it is on disk, so a stale app
    cannot claim a build it is not running. An app setting or resource tag could
    be changed without deploying anything, which is where a service reporting
    its own version stops being evidence.

``sweep``
    Look for sandboxes left behind by *earlier* runs, report them, and delete
    them. Never fatal.

Nothing here fails a qualification on its own except ``check-build``: if the app
is not the build we deployed, everything downstream is measuring the wrong thing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_MARKER_FILENAME = "BUILD_INFO.json"
_MARKER_SCHEMA = 1

# A sweep hunts *other* runs' leftovers, so it cannot narrow by this run's
# Build.BuildId the way the reaper does. Age is the substitute safety property:
# comfortably longer than any qualification run and than the controller's
# ~1 hour reconciliation cadence, so a live sandbox can never be in scope.
_DEFAULT_MAX_AGE_HOURS = 6

_GROUP_RESOURCE_ID_ENV = "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID"
_PROBE_TIMEOUT_SECONDS = 30.0
_DEPLOY_PREFLIGHT_TIMEOUT_SECONDS = 30.0
_DEFAULT_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "live" / "apps" / "aca-qualification"


class QualificationPipelineError(Exception):
    """A redacted pipeline configuration or verification failure."""


# --------------------------------------------------------------------------
# Build marker
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Deploy package assembly
# --------------------------------------------------------------------------


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
    # 256 MiB and 65,535 entries are the platform caps the closure must stay under.
    return (
        f"content_entries={entries} (cap 65535, {entries / 65535:.1%}) "
        f"content_size={mib:.1f}MiB (cap 256MiB, {mib / 256:.1%}) "
        f"truncated={str(content.get('truncated', False)).lower()}"
    )


# --------------------------------------------------------------------------
# Pre-run sweep
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepSelection:
    """Sandboxes a sweep would delete, and those it deliberately would not."""

    stale_ids: tuple[str, ...]
    unknown_age_ids: tuple[str, ...]
    live_count: int


def parse_created_at(value: str | None) -> datetime | None:
    """Parse a provider timestamp, treating anything unparseable as unknown."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def select_stale_sandboxes(
    summaries: Iterable[Any],
    *,
    now: datetime,
    max_age_hours: int = _DEFAULT_MAX_AGE_HOURS,
) -> SweepSelection:
    """Select sandboxes old enough that no live run could still own them.

    Unknown creation time is **never** treated as stale. A sandbox we cannot age
    is one we cannot prove is abandoned, and deleting a live session is far worse
    than leaving one for the next sweep. It is reported separately rather than
    silently ignored, because a group full of un-ageable sandboxes is itself a
    finding.
    """
    cutoff = now - timedelta(hours=max_age_hours)
    stale: list[str] = []
    unknown: list[str] = []
    live = 0
    for summary in summaries:
        created = parse_created_at(getattr(summary, "created_at", None))
        sandbox_id = getattr(summary, "sandbox_id", "")
        if created is None:
            unknown.append(sandbox_id)
        elif created < cutoff:
            stale.append(sandbox_id)
        else:
            live += 1
    return SweepSelection(tuple(stale), tuple(unknown), live)


def render_sweep_report(selection: SweepSelection, *, deleted: int, max_age_hours: int) -> str:
    """Render the sweep outcome as an operator-readable one-liner."""
    return (
        f"ACA pre-run sweep: stale={len(selection.stale_ids)} deleted={deleted} "
        f"unknown_age={len(selection.unknown_age_ids)} recent={selection.live_count} "
        f"age_threshold={max_age_hours}h"
    )


_MAX_REASON_CHARS = 400
# Anything resembling a credential, token, signature, or bearer value is dropped
# before a reason reaches the log.
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


async def sweep_with_adapter(
    adapter: Any,
    *,
    now: datetime,
    max_age_hours: int,
) -> str:
    """Run the sweep against an already-open adapter.

    Split out so the adapter interaction is exercisable without Azure. The first
    live run failed here on a signature mismatch that pure-function tests could
    never have caught.
    """
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            # An empty selector means "no label filter". The group is
            # CI-dedicated, so age alone is the correct scope: label-scoping
            # would silently miss leaks minted with labels this script does not
            # know about, and a selector that matches nothing looks exactly like
            # a clean group.
            summaries = await adapter.list_sandboxes(labels={})
        selection = select_stale_sandboxes(summaries, now=now, max_age_hours=max_age_hours)
        deleted = 0
        for sandbox_id in selection.stale_ids:
            try:
                await adapter.delete_sandbox(sandbox_id)
                deleted += 1
            except Exception as error:  # noqa: BLE001 - sweep is advisory, never fatal
                print(f"sweep delete failed: {type(error).__name__}", file=sys.stderr)
        return render_sweep_report(selection, deleted=deleted, max_age_hours=max_age_hours)
    finally:
        await adapter.close()


async def _sweep(group_resource_id: str, *, max_age_hours: int) -> str:
    from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

    adapter = await AcaSandboxAdapter.open(group_resource_id)
    return await sweep_with_adapter(
        adapter, now=datetime.now(UTC), max_age_hours=max_age_hours
    )


def run_sweep(environment: Mapping[str, str], *, max_age_hours: int) -> int:
    """Report and clear stale sandboxes. Always returns success.

    A leak is a signal that ACA idle-delete or the controller's hourly
    reconciliation has stopped working -- worth shouting about, but never worth
    blocking a qualification that has not started yet.
    """
    group_resource_id = environment.get(_GROUP_RESOURCE_ID_ENV, "").strip()
    if not group_resource_id:
        print(f"##vso[task.logissue type=warning]sweep skipped: {_GROUP_RESOURCE_ID_ENV} unset")
        return 0
    try:
        report = asyncio.run(_sweep(group_resource_id, max_age_hours=max_age_hours))
    except Exception as error:  # noqa: BLE001 - advisory
        # A crash is not the same as a clean group, and must not read like one.
        # The sweep did not observe anything, so nothing can be concluded about
        # whether sandboxes leaked.
        #
        # The reason is included, redacted. Redaction exists to keep prompts,
        # tokens, and customer content out of logs -- not to make an
        # infrastructure failure undiagnosable. A bare exception class name
        # cannot distinguish "DNS blocked" from "denied" from "wrong endpoint",
        # which leaves an operator with nothing to act on.
        print(
            "##vso[task.logissue type=warning]ACA pre-run sweep DID NOT RUN "
            f"({type(error).__name__}: {_redacted_reason(error)}). No conclusion "
            "can be drawn about leaked sandboxes; this is not evidence of a "
            "clean group."
        )
        return 0
    print(report)
    if "stale=0" not in report:
        print(
            "##vso[task.logissue type=warning]Stale ACA sandboxes were found and deleted. "
            "Automatic cleanup (ACA idle-delete or the controller reconciliation timer) "
            "may have stopped working."
        )
    return 0


# --------------------------------------------------------------------------
# Deployment preflight
# --------------------------------------------------------------------------


def deploy_preflight_failure_message(
    *,
    app_name: str,
    resource_group: str,
    check_name: str,
    slow: bool = False,
) -> str:
    """Name the Function App deployment role needed before One Deploy runs."""
    slow_sentence = (
        f" The check hit its {_DEPLOY_PREFLIGHT_TIMEOUT_SECONDS:.0f}s deadline; fail-fast "
        "authorization probes treat that as a deployment-readiness failure."
        if slow
        else ""
    )
    return (
        f"functionapp_deploy_preflight_failed:{check_name}: grant Website Contributor on "
        f"{app_name} in resource group {resource_group} to the deployment connection's identity "
        "so it can read the site and publishing configuration before One Deploy."
        f"{slow_sentence}"
    )


def _run_az(args: Sequence[str], *, timeout_seconds: float = _DEPLOY_PREFLIGHT_TIMEOUT_SECONDS) -> None:
    command = ["az", *args, "--only-show-errors", "--output", "json"]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f":{detail[-1][:200]}" if detail else ""
        raise QualificationPipelineError(f"az_failed:{args[0]}:{suffix}") from None


def run_preflight_deploy(args: argparse.Namespace) -> int:
    """Verify the deployment identity can read the app and publishing config."""
    app_name = args.app_name
    resource_group = args.resource_group
    checks = (
        (
            "site_read",
            ["functionapp", "show", "--name", app_name, "--resource-group", resource_group],
        ),
        (
            "publishing_config_read",
            [
                "functionapp",
                "deployment",
                "list-publishing-profiles",
                "--name",
                app_name,
                "--resource-group",
                resource_group,
            ],
        ),
    )
    for check_name, command in checks:
        try:
            _run_az(command)
        except subprocess.TimeoutExpired:
            raise QualificationPipelineError(
                deploy_preflight_failure_message(
                    app_name=app_name,
                    resource_group=resource_group,
                    check_name=check_name,
                    slow=True,
                )
            ) from None
        except QualificationPipelineError as error:
            raise QualificationPipelineError(
                deploy_preflight_failure_message(
                    app_name=app_name,
                    resource_group=resource_group,
                    check_name=check_name,
                )
            ) from error
    print("Function App deployment preflight succeeded.")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

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

    preflight = subcommands.add_parser(
        "preflight-deploy",
        help="verify Function App deploy permissions before One Deploy",
    )
    preflight.add_argument("--app-name", required=True)
    preflight.add_argument("--resource-group", required=True)

    check = subcommands.add_parser("check-build", help="verify the deployed app is this build")
    check.add_argument("--base-url", required=True)
    check.add_argument("--token-scope", required=True)
    check.add_argument("--build-id", required=True)
    check.add_argument("--commit-sha", required=True)
    check.add_argument("--python-version", required=True)

    sweep = subcommands.add_parser("sweep", help="report and clear stale sandboxes")
    sweep.add_argument("--max-age-hours", type=int, default=_DEFAULT_MAX_AGE_HOURS)
    return parser


async def _fetch_build_info(base_url: str, token_scope: str) -> dict[str, Any]:
    import aiohttp
    from azure.identity.aio import DefaultAzureCredential

    credential = DefaultAzureCredential()
    try:
        token = await credential.get_token(token_scope)
    finally:
        await credential.close()
    url = f"{base_url.rstrip('/')}/__buildinfo"
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"Authorization": f"Bearer {token.token}"}) as response:
            if response.status != 200:
                raise QualificationPipelineError(f"buildinfo_http_{response.status}")
            payload = await response.json(content_type=None)
    if not isinstance(payload, dict):
        raise QualificationPipelineError("buildinfo_malformed")
    return payload


def run_check_build(args: argparse.Namespace) -> int:
    """Fail fast when the deployed app is not the build under qualification."""
    try:
        reported = asyncio.run(_fetch_build_info(args.base_url, args.token_scope))
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
    """Dispatch one qualification pipeline command."""
    args = _parser().parse_args(arguments)
    try:
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
        if args.command == "preflight-deploy":
            return run_preflight_deploy(args)
        if args.command == "check-build":
            return run_check_build(args)
        return run_sweep(os.environ, max_age_hours=args.max_age_hours)
    except QualificationPipelineError as error:
        print(f"ACA qualification pipeline failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
