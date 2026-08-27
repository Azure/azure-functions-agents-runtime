#!/usr/bin/env python3
"""Run the protected predeployed ACA smoke suites without exposing credentials."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence

_PLACEHOLDER = re.compile(r"\$\([^)]+\)")
_DEPLOYED_ENVIRONMENT = (
    "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL",
    "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_AGENT_SLUG",
    "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE",
    "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE",
    "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TIMEOUT_SECONDS",
    "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_SERVICE_URI",
    "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_NAME",
    "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SUBSCRIPTION_ID",
    "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SITE_NAME",
    "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID",
    "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION",
)
_PROVISION_CONCURRENCIES = frozenset({1, 2, 4})
_SMOKE_RUN_ID = "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_RUN_ID"


class QualificationError(Exception):
    """A redacted deployed-qualification configuration or preflight error."""


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value or _PLACEHOLDER.search(value):
        raise QualificationError(f"required_environment_invalid:{name}")
    return value


def _optional(environment: Mapping[str, str], name: str) -> str | None:
    value = environment.get(name, "").strip()
    if not value:
        return None
    if _PLACEHOLDER.search(value):
        raise QualificationError(f"optional_environment_invalid:{name}")
    return value


def _integer(value: str, *, name: str, minimum: int, maximum: int) -> int:
    if _PLACEHOLDER.search(value) or re.fullmatch(r"[0-9]+", value) is None:
        raise QualificationError(f"invalid_integer:{name}")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise QualificationError(f"integer_out_of_range:{name}")
    return parsed


def validate_deployed_environment(
    environment: Mapping[str, str],
    *,
    runtime_target: str,
    load_concurrency: str | None = None,
    provision_concurrency: str | None = None,
) -> tuple[int | None, int | None]:
    """Validate redacted deployed-suite inputs and shared-group limits."""
    for name in _DEPLOYED_ENVIRONMENT:
        _required(environment, name)
    if runtime_target not in {"both", "python313", "python314"}:
        raise QualificationError("invalid_runtime_target")

    load = (
        _integer(load_concurrency, name="acaLoadConcurrency", minimum=1, maximum=100)
        if load_concurrency is not None
        else None
    )
    provision = (
        _integer(provision_concurrency, name="acaProvisionConcurrency", minimum=1, maximum=4)
        if provision_concurrency is not None
        else None
    )
    if provision is not None and provision not in _PROVISION_CONCURRENCIES:
        raise QualificationError("invalid_provision_concurrency")
    if runtime_target == "both" and load is not None and load > 5:
        raise QualificationError("dual_runtime_load_concurrency_requires_single_runtime")
    if runtime_target == "both" and provision is not None and provision > 1:
        raise QualificationError("provision_concurrency_requires_single_runtime")
    if load == 100 and environment.get("BUILD_REASON", "").strip() != "Manual":
        raise QualificationError("formal_n100_requires_manual_build")
    return load, provision


def validate_cold_start_samples(environment: Mapping[str, str]) -> int | None:
    """Validate the optional three-sample pipeline cap."""
    value = _optional(environment, "ACA_DEPLOYED_COLD_START_SAMPLES")
    if value is None:
        return None
    return _integer(value, name="ACA_DEPLOYED_COLD_START_SAMPLES", minimum=1, maximum=3)


async def _get_easy_auth_token(scope: str) -> None:
    from azure.identity.aio import DefaultAzureCredential

    credential = DefaultAzureCredential()
    try:
        token = await credential.get_token(scope)
    finally:
        await credential.close()
    if not token.token:
        raise QualificationError("auth_preflight_failed")


def preflight_auth(environment: Mapping[str, str]) -> None:
    """Acquire an Easy Auth token while keeping token material out of output."""
    try:
        asyncio.run(
            _get_easy_auth_token(
                _required(environment, "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE")
            )
        )
    except QualificationError:
        raise
    except Exception:
        raise QualificationError("auth_preflight_failed") from None
    print("Azure service connection authenticated")


def _run_pytest(paths: Sequence[str], environment: Mapping[str, str]) -> int:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "live_aca",
            *paths,
            "-v",
            "-o",
            "log_cli=true",
            "-o",
            "log_cli_level=INFO",
        ],
        check=False,
        env=dict(environment),
    )
    return result.returncode


def run_deployed_suite(
    environment: Mapping[str, str],
    *,
    runtime_target: str,
    load_concurrency: str,
    provision_concurrency: str,
) -> int:
    """Run the protected deployed turn, lifecycle, loss, and N=5 smoke suite."""
    load, provision = validate_deployed_environment(
        environment,
        runtime_target=runtime_target,
        load_concurrency=load_concurrency,
        provision_concurrency=provision_concurrency,
    )
    assert load is not None and provision is not None
    inherited = dict(environment)
    inherited["AZURE_FUNCTIONS_AGENTS_ACA_LOAD_CONCURRENCY"] = str(load)
    inherited["AZURE_FUNCTIONS_AGENTS_ACA_PROVISION_CONCURRENCY"] = str(provision)
    preflight_auth(inherited)
    return _run_pytest(
        (
            "tests/live/test_aca_deployed_agent_turn.py",
            "tests/live/test_aca_deployed_lifecycle.py",
            "tests/live/test_aca_deployed_loss.py",
            "tests/live/test_aca_deployed_load.py",
        ),
        inherited,
    )


def run_cold_start(
    environment: Mapping[str, str],
    *,
    runtime_target: str,
) -> int:
    """Run the protected deployed cold-start smoke suite."""
    validate_deployed_environment(environment, runtime_target=runtime_target)
    inherited = dict(environment)
    samples = validate_cold_start_samples(inherited)
    if samples is not None:
        inherited["AZURE_FUNCTIONS_AGENTS_ACA_COLD_START_SAMPLES"] = str(samples)
    preflight_auth(inherited)
    return _run_pytest(("tests/live/test_aca_deployed_cold_start.py",), inherited)


async def _probe_harness_group(group_resource_id: str, region: str) -> None:
    from tests.live.aca_smoke_support import ci_smoke_reaper_labels

    from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

    adapter = await AcaSandboxAdapter.open(group_resource_id, region=region)
    try:
        async with asyncio.timeout(30):
            await adapter.list_sandboxes(labels=ci_smoke_reaper_labels())
    finally:
        await adapter.close()


def preflight_harness(environment: Mapping[str, str]) -> None:
    """Validate the harness smoke inputs and its data-plane access."""
    group_resource_id = _required(
        environment, "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID"
    )
    region = _required(environment, "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION")
    _required(environment, "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK")
    if not re.fullmatch(
        r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.App/sandboxGroups/[^/]+",
        group_resource_id,
    ):
        raise QualificationError("invalid_sandbox_group_resource_id")
    try:
        asyncio.run(_probe_harness_group(group_resource_id, region))
    except QualificationError:
        raise
    except Exception:
        raise QualificationError("harness_data_plane_preflight_failed") from None
    print("Azure service connection authenticated")


def run_harness_suite(environment: Mapping[str, str]) -> int:
    """Run the lower-level ACA harness smoke suite."""
    preflight_harness(environment)
    return _run_pytest(
        (
            "tests/live/test_aca_harness_entrypoint_smoke.py",
            "tests/live/test_aca_run_journal_acceptance.py",
        ),
        environment,
    )


async def _reap_harness_smoke(environment: Mapping[str, str]) -> None:
    from tests.live.aca_smoke_support import ci_smoke_reaper_labels, session_belongs_to_run

    from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

    adapter = await AcaSandboxAdapter.open(
        _required(environment, "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID"),
        region=_required(environment, "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION"),
    )
    try:
        run_id = _required(environment, _SMOKE_RUN_ID)
        for sandbox in await adapter.list_sandboxes(labels=ci_smoke_reaper_labels()):
            if session_belongs_to_run(sandbox.labels, run_id):
                await adapter.delete_sandbox(sandbox.sandbox_id)
    finally:
        await adapter.close()


def reap_harness_smoke(environment: Mapping[str, str]) -> None:
    """Delete only current-run, label-scoped harness smoke sandboxes."""
    try:
        asyncio.run(_reap_harness_smoke(environment))
    except QualificationError:
        raise
    except Exception:
        raise QualificationError("harness_smoke_cleanup_failed") from None
    print("ACA smoke cleanup completed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    deployed = ("validate-environment", "preflight-auth", "deployed-suite", "cold-start")
    for command in deployed:
        command_parser = subcommands.add_parser(command)
        command_parser.add_argument("--runtime-target", required=True)
        if command in {"validate-environment", "preflight-auth", "deployed-suite"}:
            command_parser.add_argument("--load-concurrency", required=True)
            command_parser.add_argument("--provision-concurrency", required=True)
    subcommands.add_parser("harness-preflight")
    subcommands.add_parser("harness-suite")
    subcommands.add_parser("reap-harness-smoke")
    return parser


def main(arguments: Sequence[str] | None = None, environment: Mapping[str, str] | None = None) -> int:
    """Dispatch a redacted ACA qualification command."""
    args = _parser().parse_args(arguments)
    effective_environment = os.environ if environment is None else environment
    try:
        if args.command == "validate-environment":
            validate_deployed_environment(
                effective_environment,
                runtime_target=args.runtime_target,
                load_concurrency=args.load_concurrency,
                provision_concurrency=args.provision_concurrency,
            )
            return 0
        if args.command == "preflight-auth":
            validate_deployed_environment(
                effective_environment,
                runtime_target=args.runtime_target,
                load_concurrency=args.load_concurrency,
                provision_concurrency=args.provision_concurrency,
            )
            preflight_auth(effective_environment)
            return 0
        if args.command == "deployed-suite":
            return run_deployed_suite(
                effective_environment,
                runtime_target=args.runtime_target,
                load_concurrency=args.load_concurrency,
                provision_concurrency=args.provision_concurrency,
            )
        if args.command == "cold-start":
            return run_cold_start(effective_environment, runtime_target=args.runtime_target)
        if args.command == "harness-preflight":
            preflight_harness(effective_environment)
            return 0
        if args.command == "harness-suite":
            return run_harness_suite(effective_environment)
        reap_harness_smoke(effective_environment)
        return 0
    except QualificationError as error:
        print(f"ACA qualification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
