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
from urllib.parse import quote

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
)
_PROVISION_CONCURRENCIES = frozenset({1, 2, 4})
_SMOKE_RUN_ID = "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_RUN_ID"
_PREFLIGHT_WORKERS = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_PREFLIGHT_WORKERS"
_PREFLIGHT_QUIET_SECONDS = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_PREFLIGHT_QUIET_SECONDS"
_PREFLIGHT_INTERVAL_SECONDS = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_PREFLIGHT_INTERVAL_SECONDS"


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


async def _run_deployed_identity_preflight(environment: Mapping[str, str]) -> None:
    """Exercise ARM and label-scoped data access using the deployed identity."""
    import aiohttp
    from azure.identity.aio import DefaultAzureCredential

    base_url = _required(environment, "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL").rstrip("/")
    slug = quote(_required(environment, "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_AGENT_SLUG"), safe="")
    workers = _integer(
        environment.get(_PREFLIGHT_WORKERS, "1"),
        name=_PREFLIGHT_WORKERS,
        minimum=1,
        maximum=32,
    )
    quiet_seconds = float(environment.get(_PREFLIGHT_QUIET_SECONDS, "30"))
    interval_seconds = float(environment.get(_PREFLIGHT_INTERVAL_SECONDS, "5"))
    if quiet_seconds < 1 or interval_seconds <= 0:
        raise QualificationError("invalid_preflight_window")

    credential = DefaultAzureCredential()
    try:
        token = await credential.get_token(
            _required(environment, "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE")
        )
        if not token.token:
            raise QualificationError("identity_preflight_auth_failed")
        headers = {"Authorization": "Bearer" + " " + token.token}
        url = f"{base_url}/api/agents/{slug}/sandbox-preflight"
        timeout = aiohttp.ClientTimeout(total=30)

        async def probe(session: aiohttp.ClientSession) -> str:
            try:
                async with session.get(url, headers=headers) as response:
                    payload = await response.json(content_type=None)
                    return _validate_preflight_response(response.status, payload)
            except QualificationError:
                raise
            except Exception:
                raise QualificationError("deployed_identity_preflight_failed") from None

        async with aiohttp.ClientSession(timeout=timeout) as session:
            population: set[str] = set()
            for _ in range(max(workers * 2, workers)):
                population.add(await probe(session))
            if len(population) < workers:
                raise QualificationError(
                    f"scale_out_population_insufficient:{len(population)}:{workers}"
                )
            deadline = asyncio.get_running_loop().time() + quiet_seconds
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.gather(*(probe(session) for _ in range(workers)))
                await asyncio.sleep(min(interval_seconds, max(0, deadline - asyncio.get_running_loop().time())))
    finally:
        await credential.close()
    print(f"Deployed identity preflight succeeded across {workers} worker(s); quiet window clear.")


def _validate_preflight_response(status: int, payload: object) -> str:
    """Validate one deployed preflight response and return its worker identity."""
    if status != 200:
        raise QualificationError("deployed_identity_preflight_failed")
    if not isinstance(payload, dict) or not payload.get("arm_get") or not payload.get("data_plane_list"):
        raise QualificationError("deployed_identity_preflight_incomplete")
    instance_id = payload.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise QualificationError("deployed_identity_preflight_missing_instance")
    return instance_id


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


def preflight_deployed_identity(environment: Mapping[str, str]) -> None:
    """Require the deployed Function identity to pass both provider probes."""
    try:
        asyncio.run(_run_deployed_identity_preflight(environment))
    except QualificationError:
        raise
    except Exception:
        raise QualificationError("deployed_identity_preflight_failed") from None


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
    preflight_deployed_identity(inherited)
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
    preflight_deployed_identity(inherited)
    return _run_pytest(("tests/live/test_aca_deployed_cold_start.py",), inherited)


async def _probe_harness_group(group_resource_id: str) -> None:
    from tests.live.aca_smoke_support import ci_smoke_reaper_labels

    from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

    adapter = await AcaSandboxAdapter.open(group_resource_id)
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
    _required(environment, "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK")
    if not re.fullmatch(
        r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.App/sandboxGroups/[^/]+",
        group_resource_id,
    ):
        raise QualificationError("invalid_sandbox_group_resource_id")
    try:
        asyncio.run(_probe_harness_group(group_resource_id))
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
        _required(environment, "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID")
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
    deployed = (
        "validate-environment",
        "preflight-auth",
        "preflight-identity",
        "deployed-suite",
        "cold-start",
    )
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
        if args.command == "preflight-identity":
            validate_deployed_environment(
                effective_environment,
                runtime_target=args.runtime_target,
            )
            preflight_deployed_identity(effective_environment)
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
