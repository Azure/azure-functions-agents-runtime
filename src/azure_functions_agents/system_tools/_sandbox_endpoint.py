"""Pure ACA sandbox endpoint resolution and eligibility helpers."""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..config.env import has_unresolved_placeholders, substitute_env_vars_in_value
from ..config.schema import DynamicSessionsCodeInterpreterConfig


class SandboxEndpointStatus(StrEnum):
    MISSING = "missing"
    UNRESOLVED = "unresolved"
    INVALID = "invalid"
    ELIGIBLE = "eligible"


_ALLOWED_SESSIONS_HOST_SUFFIX = ".dynamicsessions.io"


@dataclass(frozen=True)
class SandboxEndpointResolution:
    """Resolved sandbox-endpoint state for collision checks and tool creation."""

    status: SandboxEndpointStatus
    raw_endpoint: str | None = None
    endpoint: str | None = None

    @property
    def eligible(self) -> bool:
        return self.status is SandboxEndpointStatus.ELIGIBLE


def is_allowed_sessions_endpoint(endpoint: str) -> bool:
    """Return ``True`` only for https URLs on an ACA dynamic sessions host."""
    try:
        parsed = urllib.parse.urlparse(endpoint)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if "@" in parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return host.endswith(_ALLOWED_SESSIONS_HOST_SUFFIX)


def resolve_sandbox_endpoint(
    config: DynamicSessionsCodeInterpreterConfig | Mapping[str, Any] | None,
) -> SandboxEndpointResolution:
    """Resolve one sandbox endpoint without importing optional runtime dependencies."""
    if config is None:
        return SandboxEndpointResolution(SandboxEndpointStatus.MISSING)

    raw_endpoint = config.get("endpoint") if isinstance(config, Mapping) else config.endpoint
    if raw_endpoint is None or raw_endpoint == "":
        return SandboxEndpointResolution(SandboxEndpointStatus.MISSING)

    raw_value = str(raw_endpoint)
    endpoint = substitute_env_vars_in_value(raw_value)
    if not endpoint or has_unresolved_placeholders(endpoint):
        return SandboxEndpointResolution(
            SandboxEndpointStatus.UNRESOLVED, raw_endpoint=raw_value
        )
    if not is_allowed_sessions_endpoint(endpoint):
        return SandboxEndpointResolution(
            SandboxEndpointStatus.INVALID, raw_endpoint=raw_value, endpoint=endpoint
        )
    return SandboxEndpointResolution(
        SandboxEndpointStatus.ELIGIBLE, raw_endpoint=raw_value, endpoint=endpoint
    )


def sandbox_runtime_tool_enabled(
    config: DynamicSessionsCodeInterpreterConfig | Mapping[str, Any] | None,
    *,
    tools_disabled: bool,
) -> bool:
    """Return whether ``fhs_execute_python`` is actually enabled for this role."""
    return not tools_disabled and resolve_sandbox_endpoint(config).eligible
