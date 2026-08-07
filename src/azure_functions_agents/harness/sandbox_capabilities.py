"""Sandbox harness capability provider and its exact manifest contract."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..conformance.capability_map import CapabilityDescriptor
from . import register_harness_capability_provider

_PROVIDER_NAME = "sandbox_runtime"
_SANDBOX_CAPABILITIES: Mapping[str, str] = MappingProxyType(
    {
        "bootstrap": "bootstrap-v1",
        "delegation": "delegation-v1",
    }
)
REQUIRED_HARNESS_CAPABILITIES: Mapping[str, str] = MappingProxyType(
    {
        "atomic_commit": "atomic-commit-v1",
        "watchdog": "watchdog-v1",
        **_SANDBOX_CAPABILITIES,
    }
)
HARNESS_CAPABILITIES = (
    CapabilityDescriptor(name="atomic-commit-v1", features=("atomic_commit",)),
    CapabilityDescriptor(name="watchdog-v1", features=("watchdog",)),
    CapabilityDescriptor(name="bootstrap-v1", features=("bootstrap",)),
    CapabilityDescriptor(name="delegation-v1", features=("delegation",)),
)
_registered = False


def register_sandbox_capabilities() -> None:
    """Register the sandbox-only features before the harness capability map freezes."""

    global _registered
    if _registered:
        return
    register_harness_capability_provider(_PROVIDER_NAME, _SANDBOX_CAPABILITIES)
    _registered = True
