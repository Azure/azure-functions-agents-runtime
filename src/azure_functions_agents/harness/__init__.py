"""Sandbox-only harness components."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

SANDBOX_MARKER_ENV_VAR = "AZURE_FUNCTIONS_AGENTS_SANDBOX"
_BASE_CAPABILITIES: Mapping[str, str] = MappingProxyType(
    {
        "atomic_commit": "atomic_commit_v1",
        "watchdog": "watchdog_v1",
    }
)


def _ensure_sandbox() -> None:
    """Reject harness activation outside a sandbox process."""
    if SANDBOX_MARKER_ENV_VAR not in os.environ:
        raise RuntimeError("Sandbox harness activation requires a sandbox process")


@dataclass(slots=True)
class HarnessCapabilityRegistry:
    """A small one-way registry for harness feature capabilities."""

    _capabilities: dict[str, str] = field(default_factory=lambda: dict(_BASE_CAPABILITIES))
    _providers: set[str] = field(default_factory=lambda: {"base"})
    _frozen: Mapping[str, str] | None = None

    def register(self, name: str, mapping: Mapping[str, str]) -> None:
        """Append a provider's closed feature mapping before the manifest freezes."""
        if self._frozen is not None:
            raise RuntimeError("Harness capabilities are already frozen.")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Harness capability provider name must be non-empty.")
        if name in self._providers:
            raise ValueError(f"Harness capability provider {name!r} is already registered.")
        additions = dict(mapping)
        if not additions:
            raise ValueError("Harness capability provider mapping must not be empty.")
        for feature, capability in additions.items():
            if not isinstance(feature, str) or not feature.strip():
                raise ValueError("Harness feature names must be non-empty strings.")
            if not isinstance(capability, str) or not capability.strip():
                raise ValueError("Harness capability names must be non-empty strings.")
            if feature in self._capabilities:
                raise ValueError(f"Harness feature {feature!r} is already registered.")
            if capability in self._capabilities.values():
                raise ValueError(f"Harness capability {capability!r} is already registered.")
        self._capabilities.update(additions)
        self._providers.add(name)

    def freeze(self) -> Mapping[str, str]:
        """Return the immutable capability snapshot consumed by a live manifest."""
        if self._frozen is None:
            self._frozen = MappingProxyType(dict(self._capabilities))
        return self._frozen

    def capability_for_feature(self, feature: str) -> str:
        """Resolve a known feature or fail closed before harness activation."""
        snapshot = self.freeze()
        try:
            return snapshot[feature]
        except KeyError:
            raise ValueError(f"Unsupported harness feature {feature!r}.") from None


_capability_registry = HarnessCapabilityRegistry()


def register_harness_capability_provider(name: str, mapping: Mapping[str, str]) -> None:
    """Register an extension provider before a future harness entrypoint freezes it."""
    _capability_registry.register(name, mapping)


def freeze_harness_capabilities() -> Mapping[str, str]:
    """Freeze and return the process-wide immutable capability snapshot."""
    return _capability_registry.freeze()


def capability_for_harness_feature(feature: str) -> str:
    """Resolve one declared harness feature through the total frozen map."""
    return _capability_registry.capability_for_feature(feature)
