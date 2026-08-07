"""Runtime-owned configuration for a stock sandbox Python disk."""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .._logger import logger
from ..egress.credentials import compile_model_key_headers
from ..egress.policy import compile_egress_policy
from ..harness import SANDBOX_MARKER_ENV_VAR
from ..journal_paths import JOURNAL_ROOT_PATH, SESSION_PATH
from ..transport.transport_models import (
    DiskIdSource,
    DiskSource,
    SandboxCreateRequest,
    SandboxCreateSource,
    SandboxEgressPolicy,
    SandboxEgressRule,
    SandboxProvisioningError,
    SandboxProvisioningLabels,
)

SANDBOX_ENV_PREFIX = "SandboxEnv__"
SANDBOX_DISK_ENV = "AZURE_FUNCTIONS_AGENTS_SANDBOX_DISK"
SANDBOX_DISK_ID_ENV = "AZURE_FUNCTIONS_AGENTS_SANDBOX_DISK_ID"
SANDBOX_APPLICATION_DIRECTORY = "/app"
SANDBOX_SITE_PACKAGES_DIRECTORY = f"{SANDBOX_APPLICATION_DIRECTORY}/.python_packages/lib/site-packages"
SANDBOX_PYTHONPATH = f"{SANDBOX_APPLICATION_DIRECTORY}:{SANDBOX_SITE_PACKAGES_DIRECTORY}"
SANDBOX_SESSION_DIRECTORY = SESSION_PATH
MODEL_API_KEY_PLACEHOLDER = "sandbox-proxy-managed"
_PROXY_MANAGED_KEY_ENV_NAMES = ("AZURE_OPENAI_API_KEY", "OPENAI_API_KEY")

BUILTIN_SANDBOX_ENV_NAMES = (
    "AZURE_FUNCTIONS_AGENTS_PROVIDER",
    "AZURE_FUNCTIONS_AGENTS_MODEL",
    "AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS",
    "AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT",
    "AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_API_VERSION",
    "FOUNDRY_PROJECT_ENDPOINT",
    "FOUNDRY_MODEL",
)


@dataclass(frozen=True, slots=True)
class SandboxCreateProfile:
    """App-scoped sandbox fields that stay stable across durable create retries."""

    source: SandboxCreateSource
    environment: Mapping[str, str]
    entrypoint: tuple[str, ...]
    cmd: tuple[str, ...]
    egress_policy: SandboxEgressPolicy

    def build_request(
        self,
        *,
        labels: SandboxProvisioningLabels,
        remaining_setup_budget_seconds: float,
        auto_suspend_seconds: int,
    ) -> SandboxCreateRequest:
        """Bind one durable operation's labels and budget to this immutable profile."""

        return SandboxCreateRequest.create(
            source=self.source,
            labels=labels,
            remaining_setup_budget_seconds=remaining_setup_budget_seconds,
            auto_suspend_seconds=auto_suspend_seconds,
            auto_suspend_mode="Disk",
            environment=self.environment,
            entrypoint=self.entrypoint,
            cmd=self.cmd,
            egress_policy=self.egress_policy,
            ports=(),
            skip_egress_proxy=False,
        )


def build_sandbox_environment(
    environment: Mapping[str, str] | None = None,
) -> Mapping[str, str]:
    """Return only the documented runtime profile and explicit customer settings."""

    source = os.environ if environment is None else environment
    forwarded: dict[str, str] = {}
    for name in BUILTIN_SANDBOX_ENV_NAMES:
        value = source.get(name)
        if value is not None and not isinstance(value, str):
            raise SandboxProvisioningError("Sandbox environment values must be strings.")
        if value:
            forwarded[name] = value

    for name, value in source.items():
        if not name.startswith(SANDBOX_ENV_PREFIX):
            continue
        target = name.removeprefix(SANDBOX_ENV_PREFIX)
        if not target:
            raise SandboxProvisioningError("SandboxEnv__ settings must name an environment variable.")
        if not isinstance(value, str):
            raise SandboxProvisioningError("Sandbox environment values must be strings.")
        if target in _PROXY_MANAGED_KEY_ENV_NAMES:
            logger.warning(
                "%s forwards a model API key into guest code and bypasses proxy-managed isolation.",
                name,
            )
        forwarded[target] = value
    forwarded[SANDBOX_MARKER_ENV_VAR] = "1"
    _add_model_key_placeholders(source, forwarded)
    _add_harness_import_paths(forwarded)
    return MappingProxyType(forwarded)


def resolve_sandbox_create_source(
    environment: Mapping[str, str] | None = None,
    *,
    python_minor: int | None = None,
) -> SandboxCreateSource:
    """Resolve an explicit public disk or a customer-pinned disk override."""

    source = os.environ if environment is None else environment
    disk = _configured_value(source, SANDBOX_DISK_ENV)
    disk_id = _configured_value(source, SANDBOX_DISK_ID_ENV)
    if disk and disk_id:
        raise SandboxProvisioningError(
            "Only one sandbox disk override may be configured at a time."
        )
    if disk_id:
        return DiskIdSource.create(disk_id)
    if disk:
        return DiskSource.create(disk)
    minor = sys.version_info.minor if python_minor is None else python_minor
    if not isinstance(minor, int) or minor < 1:
        raise SandboxProvisioningError("Sandbox Python minor version must be positive.")
    return DiskSource.create(f"python-3.{minor}")


def build_bootstrap_entrypoint(
    session_directory: str = SANDBOX_SESSION_DIRECTORY,
    journal_directory: str = JOURNAL_ROOT_PATH,
) -> tuple[str, str, str]:
    """Return the one-shot Disk-resume bootstrap entrypoint."""

    if not session_directory.startswith("/") or not journal_directory.startswith("/"):
        raise SandboxProvisioningError("Sandbox bootstrap directories must be absolute.")
    quoted_directory = shlex.quote(session_directory)
    quoted_journal_directory = shlex.quote(journal_directory)
    script = "\n".join(
        (
            f"SESS={quoted_directory}",
            'while [ ! -f "$SESS/.boot-ready" ]; do sleep 0.2; done',
            f'exec python3 "$SESS/bootstrap.py" --session-root "$SESS" --journal-root {quoted_journal_directory} >>"$SESS/bootstrap.log" 2>&1',
        )
    )
    return ("/bin/sh", "-c", script)


def build_sandbox_create_profile(
    *,
    web_request_allowed_hosts: tuple[str, ...] | None,
    mcp_urls: tuple[str, ...],
    model_endpoint: str | None,
    telemetry_endpoint: str | None,
    egress_rules: tuple[SandboxEgressRule, ...] = (),
    environment: Mapping[str, str] | None = None,
) -> SandboxCreateProfile:
    """Build the exact app-scoped source, environment, bootstrap, and egress profile."""

    resolved_environment = build_sandbox_environment(environment)
    resolved_model_endpoint = _resolve_model_endpoint(resolved_environment, model_endpoint)
    resolved_telemetry_endpoint = _resolve_telemetry_endpoint(
        environment,
        telemetry_endpoint,
    )
    if resolved_telemetry_endpoint is None:
        resolved_telemetry_endpoint = _resolve_telemetry_endpoint(
            resolved_environment,
            telemetry_endpoint,
        )
    return SandboxCreateProfile(
        source=resolve_sandbox_create_source(environment),
        environment=resolved_environment,
        entrypoint=build_bootstrap_entrypoint(),
        cmd=(),
        egress_policy=compile_egress_policy(
            web_request_allowed_hosts=web_request_allowed_hosts,
            mcp_urls=mcp_urls,
            model_endpoint=resolved_model_endpoint,
            telemetry_endpoint=resolved_telemetry_endpoint,
            rules=egress_rules,
            model_headers=compile_model_key_headers(environment),
        ),
    )


def build_sandbox_create_request(
    *,
    labels: SandboxProvisioningLabels,
    remaining_setup_budget_seconds: float,
    auto_suspend_seconds: int,
    egress_policy: SandboxEgressPolicy,
    environment: Mapping[str, str] | None = None,
    source: SandboxCreateSource | None = None,
) -> SandboxCreateRequest:
    """Build a compatibility request from an explicit profile-shaped input."""

    profile = SandboxCreateProfile(
        source=source if source is not None else resolve_sandbox_create_source(environment),
        environment=build_sandbox_environment(environment),
        entrypoint=build_bootstrap_entrypoint(),
        cmd=(),
        egress_policy=egress_policy,
    )
    return profile.build_request(
        labels=labels,
        remaining_setup_budget_seconds=remaining_setup_budget_seconds,
        auto_suspend_seconds=auto_suspend_seconds,
    )


def _configured_value(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SandboxProvisioningError("Sandbox disk overrides must be strings.")
    return value.strip()


def _resolve_model_endpoint(
    environment: Mapping[str, str] | None,
    configured_endpoint: str | None,
) -> str | None:
    if configured_endpoint:
        return configured_endpoint
    source = os.environ if environment is None else environment
    for name in ("AZURE_OPENAI_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINT"):
        value = source.get(name)
        if isinstance(value, str) and value.strip():
            return value
    if source.get("OPENAI_API_KEY"):
        return "https://api.openai.com"
    return None


def _resolve_telemetry_endpoint(
    environment: Mapping[str, str] | None,
    configured_endpoint: str | None,
) -> str | None:
    if configured_endpoint:
        return configured_endpoint
    source = os.environ if environment is None else environment
    connection = source.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not isinstance(connection, str):
        return None
    for component in connection.split(";"):
        name, separator, value = component.partition("=")
        if separator and name.casefold() == "ingestionendpoint" and value.strip():
            return value.strip()
    return None


def _add_model_key_placeholders(
    source: Mapping[str, str],
    forwarded: dict[str, str],
) -> None:
    for name in _PROXY_MANAGED_KEY_ENV_NAMES:
        value = source.get(name)
        if value is not None and not isinstance(value, str):
            raise SandboxProvisioningError("Sandbox environment values must be strings.")
        if value and name not in forwarded:
            forwarded[name] = MODEL_API_KEY_PLACEHOLDER


def _add_harness_import_paths(forwarded: dict[str, str]) -> None:
    customer_path = forwarded.get("PYTHONPATH")
    forwarded["PYTHONPATH"] = (
        SANDBOX_PYTHONPATH
        if not customer_path
        else f"{SANDBOX_PYTHONPATH}:{customer_path}"
    )
