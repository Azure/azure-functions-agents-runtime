"""Private configuration and startup guards for hybrid tool execution."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..config.paths import get_app_root
from ..config.schema import GlobalConfig, ResolvedAgent

HYBRID_SANDBOX_GROUP_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_SANDBOX_GROUP_RESOURCE_ID"
)
HYBRID_SANDBOX_REGION_ENV = "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION"
HYBRID_ALLOWED_HOSTS_ENV = "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_ALLOWED_HOSTS"
HYBRID_SANDBOX_DISK_ENV = "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_SANDBOX_DISK"
HYBRID_CREATE_TIMEOUT_ENV = "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_CREATE_TIMEOUT_SECONDS"
HYBRID_READY_TIMEOUT_ENV = "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_READY_TIMEOUT_SECONDS"
HYBRID_DRAIN_TIMEOUT_ENV = "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_DRAIN_TIMEOUT_SECONDS"
HYBRID_ORPHAN_AGE_ENV = "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_ORPHAN_AGE_SECONDS"
HYBRID_TOOL_BUNDLE_ROOT_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_BUNDLE_ROOT"
)
HYBRID_APIM_BASE_URL_ENV = "AZURE_FUNCTIONS_AGENTS_APIM_MODEL_BASE_URL"
HYBRID_APIM_AUDIENCE_ENV = "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_APIM_AUDIENCE"
HYBRID_APIM_KEY_ENV = "AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY"
HYBRID_APIM_MODEL_ENV = "AZURE_FUNCTIONS_AGENTS_APIM_MODEL"
HYBRID_APIM_KEY_HEADER = "api-key"

_DEFAULT_CREATE_TIMEOUT_SECONDS = 90.0
_DEFAULT_READY_TIMEOUT_SECONDS = 45.0
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 10.0
_DEFAULT_ORPHAN_AGE_SECONDS = 1200


class HybridConfigurationError(RuntimeError):
    """The private hybrid spike setting is inconsistent or unsafe."""


@dataclass(frozen=True, slots=True)
class HybridSandboxSettings:
    """Validated process settings for one hybrid sandbox lease."""

    group_resource_id: str
    region: str
    allowed_hosts: tuple[str, ...]
    sandbox_disk: str
    create_timeout_seconds: float
    ready_timeout_seconds: float
    drain_timeout_seconds: float
    orphan_age_seconds: int
    tool_bundle_root: Path | None = None

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> HybridSandboxSettings | None:
        source = os.environ if environment is None else environment
        group_resource_id = _optional_text(source, HYBRID_SANDBOX_GROUP_ENV)
        if not group_resource_id:
            return None
        region = _optional_text(source, HYBRID_SANDBOX_REGION_ENV)
        if not region:
            raise HybridConfigurationError(f"{HYBRID_SANDBOX_REGION_ENV} is required.")
        create_timeout = _positive_float(
            source,
            HYBRID_CREATE_TIMEOUT_ENV,
            _DEFAULT_CREATE_TIMEOUT_SECONDS,
        )
        ready_timeout = _positive_float(
            source,
            HYBRID_READY_TIMEOUT_ENV,
            _DEFAULT_READY_TIMEOUT_SECONDS,
        )
        drain_timeout = _positive_float(
            source,
            HYBRID_DRAIN_TIMEOUT_ENV,
            _DEFAULT_DRAIN_TIMEOUT_SECONDS,
        )
        orphan_age = _bounded_integer(
            source,
            HYBRID_ORPHAN_AGE_ENV,
            _DEFAULT_ORPHAN_AGE_SECONDS,
            minimum=1,
        )
        return cls(
            group_resource_id=group_resource_id,
            region=region,
            allowed_hosts=_allowed_hosts(source),
            sandbox_disk=_optional_text(source, HYBRID_SANDBOX_DISK_ENV) or "python-3.13",
            create_timeout_seconds=create_timeout,
            ready_timeout_seconds=ready_timeout,
            drain_timeout_seconds=drain_timeout,
            orphan_age_seconds=orphan_age,
            tool_bundle_root=resolve_hybrid_tool_bundle_root(source),
        )

    def validate_reaper_bound(self, maximum_run_seconds: float) -> None:
        """Require age-based reaping to stay outside every live run window."""
        live_bound = maximum_run_seconds + self.drain_timeout_seconds
        if self.orphan_age_seconds <= live_bound:
            raise HybridConfigurationError(
                f"{HYBRID_ORPHAN_AGE_ENV} must exceed the maximum run timeout plus "
                "the hybrid drain timeout."
            )


def hybrid_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """Return whether the private hybrid spike gate is configured."""
    source = os.environ if environment is None else environment
    return bool(_optional_text(source, HYBRID_SANDBOX_GROUP_ENV))


def resolve_hybrid_tool_bundle_root(
    environment: Mapping[str, str],
    *,
    app_root: Path | None = None,
) -> Path | None:
    """Resolve the optional private bundle beneath the Function app root."""
    raw = _optional_text(environment, HYBRID_TOOL_BUNDLE_ROOT_ENV)
    if not raw:
        return None
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise HybridConfigurationError(
            f"{HYBRID_TOOL_BUNDLE_ROOT_ENV} must be a relative path without '..'."
        )
    try:
        resolved_app_root = (app_root or get_app_root()).resolve(strict=True)
        resolved_bundle = (resolved_app_root / relative).resolve(strict=True)
    except OSError:
        raise HybridConfigurationError(
            f"{HYBRID_TOOL_BUNDLE_ROOT_ENV} must identify an existing directory."
        ) from None
    if not resolved_bundle.is_relative_to(resolved_app_root) or not resolved_bundle.is_dir():
        raise HybridConfigurationError(
            f"{HYBRID_TOOL_BUNDLE_ROOT_ENV} must resolve to a directory beneath the app root."
        )
    tools_root = resolved_bundle / "tools"
    if not tools_root.is_dir() or not any(
        candidate.is_file() for candidate in tools_root.glob("*.py")
    ):
        raise HybridConfigurationError(
            f"{HYBRID_TOOL_BUNDLE_ROOT_ENV} must contain a non-empty tools directory."
        )
    return resolved_bundle


def validate_hybrid_application(
    global_config: GlobalConfig,
    resolved_agents: Sequence[ResolvedAgent],
) -> None:
    """Reject surfaces that could execute outside the hybrid spike boundary."""
    if not hybrid_enabled():
        return
    if global_config.session_runtime is not None:
        raise HybridConfigurationError(
            "Hybrid tool execution cannot be combined with session_runtime."
        )
    for resolved in resolved_agents:
        source = Path(resolved.source_file or "<unknown>").name
        if resolved.sandbox_config is not None:
            raise HybridConfigurationError(
                f"{source}: Dynamic Sessions are disabled by hybrid tool execution."
            )
        if resolved.web_request_config is not None:
            raise HybridConfigurationError(
                f"{source}: web_request must be disabled for hybrid tool execution."
            )
        if resolved.subagents:
            raise HybridConfigurationError(
                f"{source}: subagents are disabled by hybrid tool execution."
            )
        if resolved.workflows is not None and resolved.workflows.enabled:
            raise HybridConfigurationError(
                f"{source}: Dynamic Workflows are disabled by hybrid tool execution."
            )
        if resolved.enabled_skills_names:
            raise HybridConfigurationError(
                f"{source}: executable skills are disabled by hybrid tool execution."
            )


def validate_hybrid_runner_inputs(
    *,
    tools: list[object] | None,
    sandbox_tools: list[object] | None,
    skill_paths: list[Path] | None,
    web_request_tools: list[object] | None,
    workflow_enabled: bool,
    subagents: Sequence[object] | None,
) -> None:
    """Fail closed if a worker-local executable reaches top-level assembly."""
    if not hybrid_enabled():
        return
    if tools:
        raise HybridConfigurationError(
            "Hybrid tool execution rejects worker-local executable tools."
        )
    if sandbox_tools:
        raise HybridConfigurationError(
            "Hybrid tool execution cannot use Dynamic Sessions tools."
        )
    if skill_paths:
        raise HybridConfigurationError("Hybrid tool execution cannot use executable skills.")
    if web_request_tools:
        raise HybridConfigurationError("Hybrid tool execution cannot use web_request.")
    if workflow_enabled:
        raise HybridConfigurationError("Hybrid tool execution cannot use Dynamic Workflows.")
    if subagents:
        raise HybridConfigurationError("Hybrid tool execution cannot use subagents.")


def resolve_hybrid_apim_settings(
    environment: Mapping[str, str] | None = None,
) -> tuple[str, str | None, str | None]:
    """Return the APIM base URL plus exactly one configured caller credential."""
    source = os.environ if environment is None else environment
    base_url = _optional_text(source, HYBRID_APIM_BASE_URL_ENV)
    if not base_url:
        raise HybridConfigurationError(f"{HYBRID_APIM_BASE_URL_ENV} is required.")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HybridConfigurationError(f"{HYBRID_APIM_BASE_URL_ENV} must be an HTTPS URL.")
    audience = _optional_text(source, HYBRID_APIM_AUDIENCE_ENV)
    subscription_key = _optional_text(source, HYBRID_APIM_KEY_ENV)
    if bool(audience) == bool(subscription_key):
        raise HybridConfigurationError(
            "Hybrid APIM authentication requires exactly one managed-identity audience "
            "or subscription key."
        )
    return base_url.rstrip("/"), audience or None, subscription_key or None


def _optional_text(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not isinstance(value, str):
        raise HybridConfigurationError(f"{name} must be a string.")
    return value.strip()


def _positive_float(
    environment: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    raw = _optional_text(environment, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise HybridConfigurationError(f"{name} must be a number.") from exc
    if not math.isfinite(value) or value <= 0:
        raise HybridConfigurationError(f"{name} must be positive and finite.")
    return value


def _bounded_integer(
    environment: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
) -> int:
    raw = _optional_text(environment, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise HybridConfigurationError(f"{name} must be an integer.") from exc
    if value < minimum:
        raise HybridConfigurationError(f"{name} must be at least {minimum}.")
    return value


def _allowed_hosts(environment: Mapping[str, str]) -> tuple[str, ...]:
    raw = _optional_text(environment, HYBRID_ALLOWED_HOSTS_ENV)
    if not raw:
        return ()
    hosts: set[str] = set()
    for item in raw.split(","):
        host = item.strip().casefold()
        if not host or "/" in host or "://" in host or host.startswith("."):
            raise HybridConfigurationError(f"{HYBRID_ALLOWED_HOSTS_ENV} has an invalid host.")
        hosts.add(host)
    return tuple(sorted(hosts))
